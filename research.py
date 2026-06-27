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
from urllib.parse import urlparse

from core import llm_structured, check_cancelled, dbg, run_concurrent
from schemas import s_object, s_string, s_bool, s_enum, s_array
from tools_web import web_search, web_search_structured, scrape_url
from memory import (
    memory_get_question, memory_set_question, memory_list_personas, VOLATILITY_DAYS,
)
from tracing import trace

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
# RECURSIVE PAGE EXPLORATION
# =============================================================================
# Replaces snippet-only research with an actual bounded page-visit loop:
# search -> visit the top real page -> optionally follow one link or refine
# the search -> stop. Bounded to a FIXED LLM-call count (not variable, not
# open-ended) since LLM call count is the dominant wall-clock cost on the
# local model backing this system (confirmed via live log analysis this
# session: mean 11.5s/call). max_hops=1 (bootstrap-only, zero added LLM
# calls — just real page content instead of a snippet) is used for every
# "prospect"-level question, which runs every single time; max_hops=2 (one
# extra reflect-and-decide call) is reserved for "persona"-level research,
# the rare path that gets cached and reused across every future same-category
# lookup, so the extra cost is amortized rather than paid per prospect.

_EXPLORE_DECISION_SCHEMA = s_object({
    "enough_info":  s_bool(),
    "follow_link":  s_string(),
    "refine_query": s_string(),
})


def _reflect(question_text: str, context: str, corpus_so_far: str, candidates: list[dict]) -> dict:
    """
    One llm_structured call: given what's been gathered so far and the
    current page's outbound links, decide whether to stop, follow one link,
    or try a different search. follow_link is a single URL, not an array —
    a flat schema field is materially more reliable in strict JSON-schema
    mode on this model than one where it also has to decide an array length;
    a second link still matters, the loop just reaches it via a second hop.
    """
    candidate_lines = "\n".join(
        f'- {c["text"]} ({urlparse(c["url"]).path or c["url"]})' for c in candidates
    ) or "(no links available on this page)"

    prompt = (
        f'Research question: {question_text}\n'
        f'Why we\'re researching this: {context or "none"}\n\n'
        f'Gathered so far:\n{corpus_so_far[:4000]}\n\n'
        f'Candidate links on the current page:\n{candidate_lines}\n\n'
        f'Is this enough to answer the question? If yes, set enough_info true. '
        f'If not, either pick ONE candidate link above (paste its exact URL into '
        f'follow_link) that looks most likely to help, OR propose a refined '
        f'search query instead (refine_query) if none of the links look useful. '
        f'Leave the unused field(s) empty.'
    )
    return llm_structured(prompt, _EXPLORE_DECISION_SCHEMA, schema_name="explore_decision")


def explore_topic(
    query: str,
    question_text: str,
    context: str,
    task_id: str,
    client,
    log,
    max_hops: int = 1,
) -> tuple[str, list[str]]:
    """
    Bounded page-visit loop. Returns (corpus_text, source_urls).

    max_hops=1: bootstrap-only — visit the rank-1 search result, no reflect
    call. Zero added LLM cost vs. the old snippet-only baseline, but real
    page content instead of a search-engine snippet.

    max_hops>=2: adds one reflect-and-decide call per extra hop. Each hop
    scrapes exactly one page (the chosen follow_link, or a refined search's
    rank-1 result) — the final hop always skips its own reflect call, since
    no further action would be possible anyway. This is what keeps the LLM
    call count FIXED per max_hops value rather than variable.
    """
    corpus: list[str] = []
    source_urls: list[str] = []

    results = web_search_structured(query, max_results=5)
    log(f"Got {len(results)} search results", "tool_result")
    trace("explore_search", task_id=task_id, query=query, results=results)
    if not results:
        return "", []
    current_url = results[0]["url"]

    for hop in range(max_hops):
        if check_cancelled(task_id, client):
            break

        log(f"🌐 visiting {current_url}", "tool_call")
        page = scrape_url(current_url)
        corpus.append(page["text"])
        source_urls.append(current_url)
        log(f"Got {len(page['text'])} chars, {len(page['links'])} links", "tool_result")
        trace(
            "explore_scrape", task_id=task_id, hop=hop, url=current_url,
            full_text=page.get("full_text", page["text"]), links=page["links"],
        )

        if hop == max_hops - 1:
            break  # final hop already scraped — no further action possible, skip reflect

        decision = _reflect(question_text, context, "\n\n".join(corpus), page["links"][:15])
        if not decision:
            dbg("explore_topic: _reflect returned {} — stopping (parse failure or empty)")
            trace("explore_decision", task_id=task_id, hop=hop, decision=None, action="stop_parse_failure")
            break

        if decision.get("enough_info"):
            log("✓ enough info gathered", "info")
            trace("explore_decision", task_id=task_id, hop=hop, decision=decision, action="stop_enough_info")
            break

        follow_link  = (decision.get("follow_link") or "").strip()
        refine_query = (decision.get("refine_query") or "").strip()
        valid_links  = {l["url"] for l in page["links"]}

        if follow_link in valid_links:
            log(f"→ following link: {follow_link}", "info")
            trace("explore_decision", task_id=task_id, hop=hop, decision=decision, action="follow_link", chosen=follow_link)
            current_url = follow_link
        elif refine_query:
            log(f"↻ refining search: {refine_query}", "info")
            trace("explore_decision", task_id=task_id, hop=hop, decision=decision, action="refine_query", chosen=refine_query)
            new_results = web_search_structured(refine_query, max_results=5)
            trace("explore_search", task_id=task_id, query=refine_query, results=new_results)
            if not new_results:
                break
            current_url = new_results[0]["url"]
        else:
            trace("explore_decision", task_id=task_id, hop=hop, decision=decision, action="stop_no_action")
            break  # nothing actionable returned — stop, don't spin

    return "\n\n".join(corpus), source_urls


# =============================================================================
# APPROPRIATENESS GATE — structural backstop before any persona-level write
# =============================================================================
# _extract_answer()'s persona_rules prompt instruction ("generalize, don't
# leak specifics") is already documented above as a backup, not a primary
# defense. This codebase's established pattern (see clean_email_draft,
# check_relevance) is "don't rely on a prompt instruction alone, add a
# structural gate" — this is that gate for persona-level memory writes.

_GENERALIZABLE_SCHEMA = s_object({"generalizable": s_bool()})


def _is_generalizable(persona: str, question_text: str, answer_text: str) -> bool:
    """
    One llm_structured call: does this answer hold for ANY instance of this
    persona, with no name/number/detail unique to one specific real entity
    leaking through? A failure here makes answer_question() discard the
    answer and cache "not found" instead — self-correcting via the existing
    TTL/volatility mechanism rather than permanently blocking the question.
    """
    prompt = (
        f'Persona/category: "{persona}"\n'
        f'Question: {question_text}\n'
        f'Answer: {answer_text}\n\n'
        f'Would this answer hold true for MOST/ANY organization or instance of '
        f'this category, with no name, number, or detail unique to one specific '
        f'real entity leaking through? Answer false if it leaks a specific name '
        f'or is too narrow to generalize.'
    )
    result = llm_structured(prompt, _GENERALIZABLE_SCHEMA, schema_name="generalizable_check")
    return bool(result.get("generalizable"))


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
    # persona-level research is rare and gets cached/reused across every
    # future same-category lookup, so it earns the expensive multi-hop
    # exploration; prospect-level research runs every single time and stays
    # bootstrap-only (see explore_topic()'s docstring for the cost reasoning)
    max_hops = 2 if topic_level == "persona" else 1
    result_text, source_urls = explore_topic(
        query, question_text, context, task_id, client, log, max_hops=max_hops,
    )

    if check_cancelled(task_id, client):
        return {}

    extracted = _extract_answer(topic, topic_level, question_text, result_text, context)
    log(f"{question_id}: {extracted['answer'][:150]}", "agent")

    was_answered = extracted["answer"].strip().lower() not in ("not found", "")

    if topic_level == "persona" and was_answered:
        passed = _is_generalizable(topic, question_text, extracted["answer"])
        trace(
            "generalizability_gate", task_id=task_id, topic=topic, question_id=question_id,
            question_text=question_text, answer=extracted["answer"], passed=passed,
        )
        if not passed:
            log(f"✗ discarded (too specific to generalize): {question_text}", "info")
            extracted["answer"] = "not found"
            was_answered = False

    memory_set_question(
        topic, topic_level, question_id, question_text,
        answer=extracted["answer"],
        confidence=extracted["confidence"],
        volatility=extracted["volatility"],
        source_urls=source_urls,
        was_answered=was_answered,
    )

    return {
        "topic": topic, "topic_level": topic_level, "question_id": question_id,
        "question_text": question_text, "answer": extracted["answer"],
        "confidence": extracted["confidence"], "volatility": extracted["volatility"],
        "was_answered": was_answered, "important": False, "source_urls": source_urls,
    }


# =============================================================================
# PITCH LOGIC — cached persona-level messaging guidance
# =============================================================================
# Distinct from the 15 descriptive seed questions above: these are about how
# RECEPTIVE this category of entity is to a pitch and what language resonates
# — cached scaffolding handed directly to the drafting prompts instead of
# asking the model to invent pitch strategy live on every single run.
# Persona-level only — pitch receptiveness is a property of the category,
# never of one specific entity's quirks. Don't call this for personas that
# are pure descriptive concepts rather than pitch targets (self_study.py's
# concept-discovery pass skips it for exactly this reason — "what objections
# would the concept of GST compliance have to a cold pitch" is nonsensical).

PITCH_QUESTIONS = {
    "PITCH1": "What pain points, needs, or pressures would make {X} receptive to outside help or a new vendor?",
    "PITCH2": "What language, keywords, or value propositions tend to resonate when pitching a product or service to {X}?",
    "PITCH3": "What objections or hesitations would {X} likely have toward a cold pitch, and how could they be pre-empted?",
}


def gather_pitch_logic(persona: str, task_id: str, client, log) -> dict:
    """
    Always answers all 3 pitch questions — no select_seed_questions-style
    selection needed, there's only 3 and all are relevant. Reuses
    answer_question()'s full cache/exploration/generalizability-gate
    machinery; PITCH1-3 rows sit in the same memory table as Q1-15, just a
    different question_id namespace (question_id is free text in memory.py,
    no schema change needed).
    """
    if check_cancelled(task_id, client):
        return {}

    tasks = [
        (lambda qid=qid, text=text: answer_question(
            persona, "persona", qid, text.replace("{X}", persona),
            "Pitch logic for cold outreach to this category of organization",
            task_id, client, log,
        ))
        for qid, text in PITCH_QUESTIONS.items()
    ]
    answers = [row for row in run_concurrent(tasks) if row]
    return {"topic": persona, "topic_level": "persona", "answers": answers}


# =============================================================================
# PERSONA RESOLUTION — self-growing, domain-agnostic vocabulary
# =============================================================================
# No hardcoded category list — a fixed enum would tie this generic research
# framework to one domain. The vocabulary IS whatever's already cached at
# topic_level='persona' (memory_list_personas()). Classification always runs
# against GROUNDED research content (an entity's actual Q1 answer), never a
# bare name — a name like "RJ and Decker" gives no lexical hint about what it
# is, but Q1's researched answer ("a chartered accounting and business
# advisory practice...") does. The match attempt against known personas is
# enum-constrained (same reliability trick already used for
# select_seed_questions' 15 question IDs) so two semantically-identical
# entities land on the IDENTICAL cache key string — minting a new label only
# happens via free text, rarely, once per genuine gap in the vocabulary.

_CLASSIFY_FRESH_SCHEMA = s_object({"category": s_string()})


def _classify_fresh(entity_name: str, grounded_context: str) -> str:
    """Cold-start path — no existing vocabulary to match against yet."""
    prompt = (
        f'Entity: "{entity_name}"\n'
        f'What we know about it: {grounded_context}\n\n'
        f'Propose a short, generic category label for this kind of entity '
        f'(e.g. "accounting firm", "property management company") — generic '
        f'enough to apply to many similar organizations, not just this one.'
    )
    result = llm_structured(prompt, _CLASSIFY_FRESH_SCHEMA, schema_name="classify_fresh")
    return (result.get("category") or "").strip()


def resolve_persona(entity_name: str, grounded_context: str, task_id: str, client, log) -> str:
    """
    Resolve an entity to a persona/category key. Matches against the existing
    vocabulary first (reliable, enum-constrained); only mints a new label
    when nothing already known fits. Returns "uncategorized" if even the
    cold-start/no-match path can't produce anything usable — callers should
    treat that as "skip the persona cascade for this one" rather than caching
    under a junk key.
    """
    if check_cancelled(task_id, client):
        return "uncategorized"

    known = memory_list_personas()

    if not known:
        proposed = _classify_fresh(entity_name, grounded_context)
        if proposed:
            log(f"🏷️ minted new persona: {proposed}", "info")
        result = (proposed or "uncategorized").strip().lower()
        trace(
            "resolve_persona", task_id=task_id, entity_name=entity_name,
            grounded_context=grounded_context, known=known, path="cold_start",
            result=result,
        )
        return result

    schema = s_object({
        "matched_category": s_enum(known + ["none"]),
        "new_category":     s_string(),
    })
    prompt = (
        f'Entity: "{entity_name}"\n'
        f'What we know about it: {grounded_context}\n\n'
        f'Does this fit one of these already-known categories: {", ".join(known)}?\n'
        f'If yes, set matched_category to the exact matching text. If genuinely '
        f'none fit, set matched_category to "none" and propose a short, generic '
        f'new_category label instead.'
    )
    decision = llm_structured(prompt, schema, schema_name="resolve_persona")
    matched = (decision.get("matched_category") or "").strip()

    if matched in known:
        log(f"🏷️ matched existing persona: {matched}", "info")
        trace(
            "resolve_persona", task_id=task_id, entity_name=entity_name,
            grounded_context=grounded_context, known=known, path="matched",
            decision=decision, result=matched,
        )
        return matched

    proposed = (decision.get("new_category") or "").strip()
    if proposed:
        log(f"🏷️ minted new persona: {proposed}", "info")
    result = (proposed or "uncategorized").strip().lower()
    trace(
        "resolve_persona", task_id=task_id, entity_name=entity_name,
        grounded_context=grounded_context, known=known, path="minted_new",
        decision=decision, result=result,
    )
    return result


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

    Deliberately NOT using explore_topic()'s multi-hop page exploration here
    (every answer_question() call inside this recursion still goes through it,
    but at whatever max_hops topic_level implies, same as any other call) —
    this is already the documented expensive escape hatch; compounding a
    third expensive mechanism into 64 worst-case leaf calls is a deliberate
    later decision, not something to bundle in silently.
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
# DISAMBIGUATION — catching "which real-world entity is this?" before
# research sinks effort into the wrong one
# =============================================================================
# Two bugs from this project's own history trace back to the same root
# cause: a name plausibly refers to more than one distinct real thing, and
# nothing noticed before research (and now, persona-cache writes) proceeded
# under one of them. Cheap by design — a single search, no scraping, no
# multi-hop exploration — since this runs before Q1 even starts, on every
# prospect-level gather_info() call, not just the rare ones that turn out
# ambiguous.

_AMBIGUITY_SCHEMA = s_object({
    "ambiguous": s_bool(),
    "candidates": s_array(s_object({
        "description":          s_string(),  # e.g. "McDonald's restaurant in Hastings, New Zealand"
        "distinguishing_detail": s_string(),  # e.g. "New Zealand" -- short token to refine the topic/query with
    })),
})


def check_ambiguity(topic: str, context: str, known_context_hint: str) -> dict:
    """
    One cheap search + one llm_structured call: do these results suggest
    MULTIPLE distinct real-world entities sharing this name (different
    countries/cities, or entirely unrelated organizations)? Returns
    {"ambiguous": bool, "candidates": [...]} — candidates is empty when not
    ambiguous.
    """
    results = web_search(topic, max_results=5)
    hint_line = f'Known background that might help disambiguate: {known_context_hint}\n' if known_context_hint else ""

    prompt = (
        f'Search results for "{topic}":\n{results[:3000]}\n\n'
        f'Context for this research: {context or "none"}\n'
        f'{hint_line}\n'
        f'Do these results suggest MULTIPLE distinct real-world entities that '
        f'happen to share this name (e.g. the same business name in different '
        f'countries/cities, or entirely unrelated organizations)? If yes, list '
        f'the distinct candidates with a short distinguishing detail each. If '
        f'the results plausibly describe just one real entity, set ambiguous '
        f'to false and leave candidates empty.'
    )
    return llm_structured(prompt, _AMBIGUITY_SCHEMA, schema_name="ambiguity_check")


_DISAMBIGUATION_QUESTION_SCHEMA = s_object({"question": s_string()})


def build_disambiguation_question(topic: str, candidates: list[dict], known_context_hint: str) -> str:
    """
    One llm_structured call. If known_context_hint strongly suggests one
    candidate, lead with that as a best guess and ask for confirmation
    rather than presenting a blank list of options — this is the concrete
    "informed by memory" mechanism: callers that already have relevant prior
    research (e.g. the sender's own already-cached company info) pass it in
    as known_context_hint, and the question gets built around it.
    """
    candidate_lines = "\n".join(f"- {c.get('description', '')}" for c in candidates)
    hint_line = f'Relevant background: {known_context_hint}\n' if known_context_hint else ""

    prompt = (
        f'Multiple real-world entities seem to share the name "{topic}":\n{candidate_lines}\n\n'
        f'{hint_line}\n'
        f'Write a single, short, clear question asking the user which one they '
        f'mean. If the background strongly suggests one option, lead with that '
        f'as your best guess and ask for confirmation rather than listing '
        f'options blankly.'
    )
    result = llm_structured(prompt, _DISAMBIGUATION_QUESTION_SCHEMA, schema_name="disambiguation_question")
    return result.get("question") or f"Which '{topic}' do you mean?\n{candidate_lines}"


def pick_best_guess_candidate(topic: str, candidates: list[dict], known_context_hint: str) -> str:
    """
    For trusted/autonomous tasks, where wait_for_input() would just return a
    meaningless "yes" rather than an actual disambiguating answer — picks the
    candidate best supported by known_context_hint instead of asking.
    Enum-constrained to the real candidate list (same reliability pattern as
    resolve_persona) so the result is always one of the actual options, never
    a hallucinated detail. Falls back to the first candidate if the hint
    doesn't clearly favor one.
    """
    details = [c.get("distinguishing_detail", "") for c in candidates if c.get("distinguishing_detail")]
    if not details:
        return ""
    if len(details) == 1:
        return details[0]

    schema = s_object({"best_guess": s_enum(details)})
    candidate_lines = "\n".join(f"- {c.get('description', '')}" for c in candidates)
    prompt = (
        f'Multiple real-world entities share the name "{topic}":\n{candidate_lines}\n\n'
        f'Known background: {known_context_hint or "none"}\n\n'
        f'Which one does the background most likely point to? Pick your best '
        f'guess even if not fully certain.'
    )
    result = llm_structured(prompt, schema, schema_name="pick_best_guess_candidate")
    return result.get("best_guess") or details[0]


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

    match = re.search(r'[\w.+-]+@[\w.-]+\.[a-z]{2,}', result_text, re.IGNORECASE)
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
    match = re.search(r'[\w.+-]+@[\w.-]+\.[a-z]{2,}', extracted, re.IGNORECASE)
    return match.group() if match else None
