# =============================================================================
# research.py — Seed-Question Research Framework
# =============================================================================
# Replaces the old fixed light/medium/deep gather_info() pipeline. Every
# research target is either:
#   "persona"  — a role/category, not a named entity. Reusable across many
#                unrelated prospects (e.g. "property manager").
#   "prospect" — a specific named real-world entity. Not reusable
#                (e.g. "BRB Property Management").
#
# This distinction is threaded through query construction and extraction
# below, NOT just the cache schema — that's the primary defense against
# persona-level cache entries getting contaminated with a specific prospect's
# name or company (a search for a generic role almost always surfaces real
# named people/companies, since that's what real content about a role looks
# like). The extraction prompt's "generalize, don't leak specifics" instruction
# is a backup, not the main mechanism — small-model prompt adherence isn't
# fully reliable, so don't rely on it alone.
#
# One function owns the cache: answer_question(). Every caller (the default
# gather_info() loop, the branching deep-dive, any future persona-priming or
# idle-refresh job) goes through it rather than each re-implementing the
# get-or-compute-and-cache pattern.
# =============================================================================

import re

from core import llm_structured, check_cancelled
from schemas import s_object, s_string, s_bool, s_enum, s_array
from tools_web import web_search
from memory import memory_get_question, memory_set_question, VOLATILITY_DAYS

# =============================================================================
# THE 15 SEED QUESTIONS
# =============================================================================
# Deliberately abstract enough to apply to almost any topic. {X} is the
# substitution placeholder, replaced via .replace("{X}", topic) — not
# str.format(), so question text is never at risk of choking on stray braces.
#
# Known limitation: Q4, Q9, Q11 are more prone to drifting into
# tangential-but-connected territory (e.g. "history of X" can wander into
# general industry history). Accepted tradeoff for genericity — mitigated by
# the relevance check in branch_research(), not eliminated.

SEED_QUESTIONS = {
    "Q1":  "What is {X}, fundamentally — what category of thing does it belong to?",
    "Q2":  "What is the primary purpose or function of {X}?",
    "Q3":  "Who are the key people, entities, or stakeholders associated with {X}?",
    "Q4":  "What is the history or origin of {X}?",
    "Q5":  "What is {X} currently doing, or what state is it currently in?",
    "Q6":  "What problems or challenges does {X} commonly face?",
    "Q7":  "What does {X} need, want, or value?",
    "Q8":  "Who or what does {X} interact with, depend on, or relate to?",
    "Q9":  "What makes {X} similar to or different from comparable things?",
    "Q10": "What recent changes, events, or developments involve {X}?",
    "Q11": "What rules, norms, standards, or constraints govern {X}?",
    "Q12": "What resources (financial, physical, human, informational) does {X} have or lack?",
    "Q13": "What language, terminology, or framing does {X} use to describe itself?",
    "Q14": "What is {X} known for, publicly or within its field?",
    "Q15": "What would change {X}'s current situation, for better or worse?",
}

# Per-question search query templates — fixed per question ID rather than
# derived from question text at runtime, since there are only 15 of them.
# prospect templates keep {X} as a literal proper-noun search target (today's
# existing pattern). persona templates never do — genericized/role-framed
# phrasing only. This split is the primary contamination defense.

_PROSPECT_QUERY = {
    "Q1":  "{X} company overview",
    "Q2":  "{X} what they do products services",
    "Q3":  "{X} leadership team key people",
    "Q4":  "{X} history founded",
    "Q5":  "{X} news recent",
    "Q6":  "{X} challenges problems",
    "Q7":  "{X} needs looking for",
    "Q8":  "{X} partners clients customers",
    "Q9":  "{X} competitors alternatives",
    "Q10": "{X} news recent announcement",
    "Q11": "{X} regulations compliance",
    "Q12": "{X} funding revenue size",
    "Q13": "{X} about us mission",
    "Q14": "{X} known for reputation",
    "Q15": "{X} plans future outlook",
}

_PERSONA_QUERY = {
    "Q1":  "what does a {X} do",
    "Q2":  "{X} role responsibilities overview",
    "Q3":  "who does a {X} typically work with stakeholders",
    "Q4":  "history of the {X} role",
    "Q5":  "current trends in {X} industry",
    "Q6":  "common challenges {X}s face",
    "Q7":  "what {X}s look for in vendors",
    "Q8":  "who {X}s rely on or report to",
    "Q9":  "how {X} roles differ by industry",
    "Q10": "recent trends affecting {X}s",
    "Q11": "standards and regulations for {X}s",
    "Q12": "typical budget and resources for {X}s",
    "Q13": "terminology {X}s use",
    "Q14": "what {X}s are known for",
    "Q15": "what would improve a {X}'s situation",
}


def _build_query(topic: str, topic_level: str, question_id: str) -> str:
    template_set = _PROSPECT_QUERY if topic_level == "prospect" else _PERSONA_QUERY
    template = template_set.get(question_id, "{X}")
    return template.replace("{X}", topic)


# =============================================================================
# QUESTION SELECTION
# =============================================================================

_QUESTION_SELECTION_SCHEMA = s_object({
    "selected": s_array(s_enum(list(SEED_QUESTIONS.keys()))),
})


def select_seed_questions(topic: str, topic_level: str, context: str = "") -> list[str]:
    """
    Ask the LLM to pick the 3 most relevant of the 15 seed questions for this
    topic and task context. enum-constrained to the 15 known Q-ids, so the
    model can't return a malformed or invented question ID.
    """
    question_list = "\n".join(
        f"{qid}: {text.replace('{X}', topic)}" for qid, text in SEED_QUESTIONS.items()
    )
    level_note = (
        "a general role/category, not a specific named entity"
        if topic_level == "persona"
        else "a specific named real-world entity"
    )

    prompt = f"""You are selecting which questions to research about "{topic}" ({topic_level}-level — {level_note}).

Task context: {context or "none"}

Questions available:
{question_list}

Pick the 3 most useful questions for this context."""

    result = llm_structured(prompt, _QUESTION_SELECTION_SCHEMA, schema_name="seed_question_selection")
    ids = result.get("selected") or []

    seen: list[str] = []
    for qid in ids:
        if qid in SEED_QUESTIONS and qid not in seen:
            seen.append(qid)

    if not seen:
        seen = ["Q1", "Q5", "Q14"]  # sane fallback if the model returns an empty selection

    return seen[:3]


# =============================================================================
# EXTRACTION
# =============================================================================

_ANSWER_SCHEMA = s_object({
    "answer":     s_string(),
    "confidence": s_enum(["high", "medium", "low"]),
    "volatility": s_enum(list(VOLATILITY_DAYS.keys())),
})


def _extract_answer(topic: str, topic_level: str, question_text: str, corpus: str, context: str) -> dict:
    """One LLM call: answer + confidence + volatility classification, in one shot."""
    if topic_level == "persona":
        persona_rules = (
            "This is GENERIC research about a role/category, not a specific entity. "
            "Extract only patterns that would hold across many real-world instances of "
            "this role. Do NOT include any specific person names, company names, or "
            "identifying details that appear in the source material — strip them out and "
            "generalize, or say there isn't enough to generalize from. "
        )
    else:
        persona_rules = ""

    prompt = f"""{persona_rules}Based on this research, answer the question about "{topic}":

Question: {question_text}

Research:
{corpus[:8000]}

answer: 1-2 sentences, or "not found" if the research doesn't address it
confidence: high, medium, or low
volatility: how fast THIS SPECIFIC answer would go stale. Judge the actual content, not the question category — "they just rebranded last week" is VOLATILE even for a usually-stable question."""

    result = llm_structured(prompt, _ANSWER_SCHEMA, schema_name="seed_question_answer")
    return {
        "answer":     result.get("answer") or "not found",
        "confidence": (result.get("confidence") or "medium").lower(),
        "volatility": (result.get("volatility") or "NORMAL").upper(),
    }


# =============================================================================
# SINGLE-PASS ANSWER — owns the cache (the one function every caller goes through)
# =============================================================================

def answer_question(
    topic: str,
    topic_level: str,
    question_id: str,
    question_text: str,
    context: str,
    task_id: str,
    client,
    log,
    force_refresh: bool = False,
) -> dict:
    """
    Get-or-compute-and-cache for one (topic, topic_level, question_id). Used by
    the default gather_info() loop AND by branch_research() at every node —
    the single place the memory cache is read from and written to.
    """
    if not force_refresh:
        cached = memory_get_question(topic, topic_level, question_id)
        if cached:
            log(f"📚 cached: {question_text}", "info")
            return cached

    query = _build_query(topic, topic_level, question_id)
    log(f"🔍 {query}", "tool_call")
    result_text = web_search(query, max_results=5)
    log(f"Got {len(result_text)} chars", "tool_result")

    if check_cancelled(task_id, client):
        return {}

    extracted = _extract_answer(topic, topic_level, question_text, result_text, context)
    log(f"{question_id}: {extracted['answer'][:150]}", "agent")

    was_answered = extracted["answer"].strip().lower() not in ("not found", "")

    memory_set_question(
        topic, topic_level, question_id, question_text,
        answer=extracted["answer"],
        confidence=extracted["confidence"],
        volatility=extracted["volatility"],
        source_urls=[],
        was_answered=was_answered,
    )

    return {
        "topic": topic, "topic_level": topic_level, "question_id": question_id,
        "question_text": question_text, "answer": extracted["answer"],
        "confidence": extracted["confidence"], "volatility": extracted["volatility"],
        "was_answered": was_answered, "important": False, "source_urls": [],
    }


# =============================================================================
# BOUNDED BRANCHING DEEP DIVE
# =============================================================================

_FOLLOWUPS_SCHEMA = s_object({"followups": s_array(s_string())})
_RELEVANCE_SCHEMA = s_object({"relevant": s_bool()})


def generate_followups(topic: str, parent_question_text: str, answer_text: str, context: str) -> list[str]:
    """Up to 4 follow-up questions generated from one answered question."""
    prompt = f"""Based on this question and answer about "{topic}", generate up to 4 specific follow-up questions that would deepen understanding for this goal: {context or "general research"}

Question: {parent_question_text}
Answer: {answer_text}

Return an empty list if none are useful."""

    result = llm_structured(prompt, _FOLLOWUPS_SCHEMA, schema_name="followups")
    followups = result.get("followups") or []
    return [f.strip() for f in followups if f.strip()][:4]


def check_relevance(followup_question: str, seed_question_text: str, context: str) -> bool:
    """
    Mandatory gate before spending a search on any generated follow-up —
    every child, every depth, no exceptions. A branching question generator
    produces questions that are topically connected to their parent but not
    necessarily still in service of the original goal.
    """
    prompt = (
        f'Original research goal: {seed_question_text}\n'
        f'Task context: {context or "none"}\n\n'
        f'Candidate follow-up question: "{followup_question}"\n\n'
        f'Does answering this follow-up question help achieve the original research goal?'
    )
    result = llm_structured(prompt, _RELEVANCE_SCHEMA, schema_name="relevance_check")
    return bool(result.get("relevant"))


def branch_research(
    topic: str,
    topic_level: str,
    seed_question_id: str,
    seed_question_text: str,
    context: str,
    task_id: str,
    client,
    log,
    max_depth: int = 3,
    branching_factor: int = 4,
) -> list[dict]:
    """
    Bounded branching deep-dive. Worst case at branching_factor=4, max_depth=3
    is 4**3 = 64 leaf-level searches for this one seed question — acceptable
    for a deliberate one-off deep dive, not for every task. Only ever called
    from the synchronous escape hatch (gather_info's should_deep_dive gate) or
    future offline persona-priming — never from the default per-task path.
    """
    all_rows: list[dict] = []

    def _recurse(question_id: str, question_text: str, depth: int):
        if check_cancelled(task_id, client):
            return
        row = answer_question(topic, topic_level, question_id, question_text, context, task_id, client, log)
        if not row:
            return
        all_rows.append(row)

        if depth >= max_depth:
            return

        followups = generate_followups(topic, question_text, row["answer"], context)
        survivors = []
        for fq in followups[:branching_factor]:
            if check_relevance(fq, seed_question_text, context):
                survivors.append(fq)
            else:
                log(f"✗ discarded (not relevant): {fq}", "info")

        for i, fq in enumerate(survivors, 1):
            _recurse(f"{question_id}.{i}", fq, depth + 1)

    log(f"🌳 Deep dive starting: {seed_question_text}", "deep_dive")
    _recurse(seed_question_id, seed_question_text, depth=1)
    log(f"🌳 Deep dive complete — {len(all_rows)} question(s) answered", "deep_dive")
    return all_rows


_DEEP_DIVE_SCHEMA = s_object({"decision": s_bool(), "justification": s_string()})


def should_deep_dive(topic: str, context: str, task_id: str, client, log) -> tuple[bool, str]:
    """
    The synchronous escape-hatch gate. Narrow, explicit prompt — not a vague
    "does this seem important" judgment call. Logs the justification under the
    dedicated 'deep_dive' log_type regardless of outcome, so it's auditable
    how often this fires and whether it's firing for weak reasons.

    Deliberate latency tradeoff if triggered: there is no async task pipeline
    yet, so a YES here blocks the current task for however long branch_research
    takes (potentially many minutes at the depth-3/branching-factor-4 ceiling).
    """
    prompt = (
        f'Would significantly more research on "{topic}" change the outcome of this '
        f'specific task, enough to justify several minutes of additional research time '
        f'before continuing?\n\n'
        f'Task context: {context or "none"}\n\n'
        f'Give a one-line justification either way.'
    )
    result = llm_structured(prompt, _DEEP_DIVE_SCHEMA, schema_name="deep_dive_decision")
    fires = bool(result.get("decision"))
    justification = result.get("justification") or ""

    log(
        f"{'🌳 Deep dive triggered' if fires else 'Deep dive declined'}: {justification}",
        "deep_dive",
    )
    return fires, justification


# =============================================================================
# CONTACT LOOKUP — outside the 15-question framework
# =============================================================================
# None of the 15 generic questions ask for a literal sendable address, but
# outreach workflows need one. Kept narrow and mechanical rather than forced
# in as a 16th question. Prospect-only — doesn't make sense for a persona/role
# topic.

_CONTACT_EMAIL_SCHEMA = s_object({"email": s_string()})


def find_contact_email(topic: str, task_id: str, client, log) -> str | None:
    log(f"🔍 {topic} contact email", "tool_call")
    result_text = web_search(f"{topic} contact email", max_results=5)
    log(f"Got {len(result_text)} chars", "tool_result")

    match = re.search(r'[\w.+-]+@[\w-]+\.[a-z]{2,}', result_text)
    if match:
        return match.group()

    result = llm_structured(
        f'Find a contact email address for "{topic}" in this text, if present.\n\n'
        f'{result_text[:4000]}\n\n'
        f'Return an empty string for "email" if none is present in the text.',
        _CONTACT_EMAIL_SCHEMA,
        schema_name="contact_email",
    )
    extracted = (result.get("email") or "").strip()
    match = re.search(r'[\w.+-]+@[\w-]+\.[a-z]{2,}', extracted)
    return match.group() if match else None
