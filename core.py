# =============================================================================
# core.py — Shared helpers available to all workflows
# =============================================================================
# Import from here in any workflow file:
#   from core import llm_call, gather_info, wait_for_input, ...
# =============================================================================

import os
import re
import time
from datetime import datetime

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, AIMessage

# Sender identity — set in .env
SENDER_NAME    = os.getenv("SENDER_NAME", "")
SENDER_TITLE   = os.getenv("SENDER_TITLE", "")
SENDER_WEBSITE = os.getenv("SENDER_WEBSITE", "")
SENDER_BIO     = os.getenv("SENDER_BIO", "")

# =============================================================================
# CONSOLE DEBUG LOGGING  (stdout only — never sent to website)
# =============================================================================

def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]

def dbg(msg: str) -> None:
    """Print a debug line to Docker console only. Never goes to the website."""
    print(f"  \033[90m[DEBUG {_ts()}] {msg}\033[0m", flush=True)

def dbg_block(title: str, content: str, max_chars: int = 3000) -> None:
    """Print a titled debug block to Docker console."""
    bar = "─" * 60
    truncated = content[:max_chars] + (f"\n... ({len(content)-max_chars} chars truncated)" if len(content) > max_chars else "")
    print(f"\n\033[90m┌{bar}", flush=True)
    print(f"│ [DEBUG {_ts()}] {title}")
    print(f"├{bar}")
    for line in truncated.splitlines():
        print(f"│ {line}")
    print(f"└{bar}\033[0m\n", flush=True)

# =============================================================================
# LLM
# =============================================================================

_BASE_KWARGS = dict(
    model   = os.getenv("OPENAI_MODEL", "local-model"),
    base_url= os.getenv("OPENAI_API_BASE", "http://host.docker.internal:1234/v1"),
    api_key = os.getenv("OPENAI_API_KEY", "not-needed"),
)

# General-purpose LLM — creative, longer responses
_llm = ChatOpenAI(**_BASE_KWARGS, temperature=0.7)

# Classification LLM — low temperature, hard token cap so it can't ramble
_llm_classify = ChatOpenAI(**_BASE_KWARGS, temperature=0.1, max_tokens=512)


def llm_call(prompt: str) -> str:
    """General LLM call — for drafting, reasoning, extraction."""
    dbg_block(f"LLM PROMPT  ({len(prompt)} chars)", prompt)
    t0       = time.time()
    response = _llm.invoke(prompt).content
    elapsed  = time.time() - t0
    dbg_block(f"LLM RESPONSE  ({len(response)} chars, {elapsed:.1f}s)", response)
    return response


_CLASSIFY_PREAMBLE = (
    "Output ONLY the exact format requested. "
    "No explanations, no reasoning, no extra text.\n\n"
)


def llm_classify(prompt: str) -> str:
    """Classification LLM call — constraint preamble + token cap keeps output tight."""
    full_prompt = _CLASSIFY_PREAMBLE + prompt
    dbg_block(f"LLM CLASSIFY PROMPT  ({len(full_prompt)} chars)", full_prompt)
    t0       = time.time()
    response = _llm_classify.invoke(full_prompt).content
    elapsed  = time.time() - t0
    dbg_block(f"LLM CLASSIFY RESPONSE  ({len(response)} chars, {elapsed:.1f}s)", response)
    return response


def llm_classify_prefill(prompt: str) -> str:
    """
    Classification call with constraint preamble + assistant prefill '1:'.
    Preamble suppresses prose, prefill forces structured numbered output
    from the first token. Only user/assistant roles used — compatible with
    Mistral and other models that don't support system role.
    Returns the full output including the prefilled '1:'.
    """
    full_prompt = _CLASSIFY_PREAMBLE + prompt
    dbg_block(f"LLM CLASSIFY_PREFILL PROMPT  ({len(full_prompt)} chars)", full_prompt)
    t0       = time.time()
    messages = [HumanMessage(content=full_prompt), AIMessage(content="1:")]
    response = _llm_classify.invoke(messages).content
    elapsed  = time.time() - t0
    full     = "1:" + response
    dbg_block(f"LLM CLASSIFY_PREFILL RESPONSE  ({len(full)} chars, {elapsed:.1f}s)", full)
    return full


# =============================================================================
# TASK HELPERS
# =============================================================================

def check_cancelled(task_id: str, client) -> bool:
    """Returns True if the task has been cancelled or failed externally."""
    task   = client.get_task(task_id)
    status = task.get("status") if task else "unknown"
    if status in ("cancelled", "failed"):
        dbg(f"check_cancelled → TRUE  (status={status})")
        return True
    return False


def extract_field(text: str, field: str) -> str:
    """Pull a labelled field from LLM-structured output."""
    match = re.search(rf"{field}:\s*(.+)", text, re.IGNORECASE)
    return match.group(1).strip() if match else "not found"


def parse_duration(duration_str: str) -> int:
    """Convert a duration string to minutes. Defaults to 60."""
    if not duration_str or duration_str.lower() in ("not specified", "not found"):
        return 60
    s = duration_str.lower()
    hours = re.search(r'(\d+(?:\.\d+)?)\s*h', s)
    mins  = re.search(r'(\d+)\s*m', s)
    total = 0
    if hours:
        total += int(float(hours.group(1)) * 60)
    if mins:
        total += int(mins.group(1))
    return total if total > 0 else 60


def wait_for_input(task_id: str, question: str, client, timeout: int = 300) -> str | None:
    """
    Post a question to the user and block until they reply.
    Returns the answer string, or None on timeout / cancellation.
    Skips immediately (returns "yes") if the task is marked trusted.
    """
    task = client.get_task(task_id)
    if task and task.get("trusted"):
        dbg(f"wait_for_input: task is trusted — skipping question")
        client.log(task_id, f"💬 (trusted) Skipping: {question}", "info")
        client.log(task_id, "✅ Proceeding autonomously", "success")
        return "yes"

    dbg(f"wait_for_input: posting question ({len(question)} chars), timeout={timeout}s")
    client.update_status(task_id, "awaiting_input", pending_question=question, user_input="")
    client.log(task_id, f"💬 {question}", "info")

    waited = 0
    while waited < timeout:
        time.sleep(5)
        waited += 5
        dbg(f"wait_for_input: polling... ({waited}s elapsed)")

        task = client.get_task(task_id)
        if not task:
            dbg("wait_for_input: task not found — aborting")
            return None

        if task.get("trusted") and task.get("status") == "running":
            dbg("wait_for_input: trust granted mid-wait — returning yes")
            client.log(task_id, "✅ Trust granted — proceeding autonomously", "success")
            return "yes"

        status = task.get("status")
        dbg(f"wait_for_input: status={status}")
        if status == "running":
            answer = task.get("user_input", "")
            if answer:
                dbg(f"wait_for_input: got answer → {answer!r}")
                client.log(task_id, f"💬 You replied: {answer}", "info")
                return answer

        if status in ("cancelled", "failed"):
            dbg(f"wait_for_input: task {status} — aborting")
            return None

    dbg("wait_for_input: timed out")
    client.log(task_id, "⏰ Timed out waiting for reply", "error")
    client.update_status(task_id, "failed", error_message="Timed out waiting for user reply")
    return None


# =============================================================================
# INFORMATION GATHERING
# =============================================================================
# Backed by the seed-question framework in research.py — see that module's
# docstring for the persona/prospect distinction and the bounded branching
# deep-dive. gather_info() is the orchestration entry point; it owns no cache
# access directly (research.answer_question() does) and does a deferred
# import of research.py to avoid a circular import (research.py imports core
# for llm_call/extract_field/etc).

def format_answers_as_context(result: dict) -> str:
    """
    Join a gather_info() result's per-question answers into a readable block
    for embedding in downstream drafting prompts.
    """
    if not result or not result.get("answers"):
        return "No research available."
    return "\n\n".join(
        f"{a['question_text']}\n{a['answer']}" for a in result["answers"]
    )


def gather_info(topic: str, topic_level: str, task_id: str, client, log, context: str = "") -> dict:
    """
    Generic information gathering via the 15-question seed framework.

    topic       — anything: a company, person, technology, concept, etc.
    topic_level — "persona" (a role/category, reusable across unrelated
                  prospects, e.g. "property manager") or "prospect" (a
                  specific named real-world entity, e.g. "Acme Corp").
                  Required — no default, so a new workflow can't silently
                  get the wrong query/extraction behavior by omitting it.
    context     — why we're researching this. Drives question selection and
                  the deep-dive gate; not cached, since it varies per call.

    Returns:
        {"topic": ..., "topic_level": ...,
         "answers": [{"question_id", "question_text", "answer",
                       "confidence", "volatility"}, ...]}
    Returns empty dict if the task is cancelled mid-run.
    """
    if topic_level not in ("persona", "prospect"):
        raise ValueError(f'topic_level must be "persona" or "prospect", got {topic_level!r}')

    from research import SEED_QUESTIONS, select_seed_questions, answer_question, should_deep_dive, branch_research

    log(f"🧭 Selecting research questions for '{topic}' ({topic_level})", "info")
    question_ids = select_seed_questions(topic, topic_level, context)
    log(f"Selected: {', '.join(question_ids)}", "agent")

    answers = []
    for qid in question_ids:
        if check_cancelled(task_id, client):
            return {}
        question_text = SEED_QUESTIONS.get(qid, qid).replace("{X}", topic)
        row = answer_question(topic, topic_level, qid, question_text, context, task_id, client, log)
        if row:
            answers.append(row)

    if check_cancelled(task_id, client):
        return {}

    fires, _justification = should_deep_dive(topic, context, task_id, client, log)
    if fires and answers:
        top = answers[0]
        deep_rows = branch_research(
            topic, topic_level, top["question_id"], top["question_text"], context,
            task_id, client, log,
        )
        merged = {a["question_id"]: a for a in answers}
        for row in deep_rows:
            merged[row["question_id"]] = row
        answers = list(merged.values())

    return {"topic": topic, "topic_level": topic_level, "answers": answers}


# =============================================================================
# EMAIL DRAFT SANITIZATION
# =============================================================================
# Every draft prompt across workflows tells the model "do NOT include a
# greeting/signature/subject line" — and the model ignores that instruction
# essentially every time, returning a fully-formatted email anyway (its own
# "Subject: ...", its own "Hi Team,", its own "Best regards, ..." signature).
# The workflow code then concatenates its own greeting + signature on top with
# no stripping, producing a double greeting, a stray "Subject:" line baked
# into the body, and a duplicate sign-off. This is a defensive cleanup layer,
# not a prompt fix — we've seen throughout this codebase that "don't include
# X" instructions aren't reliable with this model, so don't rely on the
# prompt alone to prevent this.

_SUBJECT_PREFIX_RE = re.compile(r'^subject\s*:\s*', re.IGNORECASE)
_GREETING_LINE_RE  = re.compile(r'^(hi|hello|dear|greetings|hey)\b.*,?\s*$', re.IGNORECASE)
_SIGNOFF_RE        = re.compile(
    r'\n\s*(best regards|best wishes|best,|kind regards|regards,|sincerely|'
    r'warm regards|warmly|cheers,|thanks,|thank you,)\b',
    re.IGNORECASE,
)


def clean_email_draft(body: str) -> str:
    """
    Strip a leaked 'Subject:' line, a leaked greeting line, and a leaked
    sign-off block from a model-generated email body before concatenating it
    with the workflow's own greeting + signature.
    """
    lines = body.strip().splitlines()

    def _drop_leading_blank():
        while lines and not lines[0].strip():
            lines.pop(0)

    _drop_leading_blank()
    if lines and _SUBJECT_PREFIX_RE.match(lines[0].strip()):
        lines.pop(0)
        _drop_leading_blank()
    if lines and _GREETING_LINE_RE.match(lines[0].strip()):
        lines.pop(0)
        _drop_leading_blank()

    text = '\n'.join(lines)
    signoff_match = _SIGNOFF_RE.search(text)
    if signoff_match:
        text = text[:signoff_match.start()]

    return text.strip()


def clean_subject_line(subject: str) -> str:
    """
    Models sometimes return several alternative subject lines (separated by
    blank lines and the word "or") instead of the single line requested. A
    multi-line subject crashes MIME header encoding outright — take only the
    first real candidate.
    """
    subject = _SUBJECT_PREFIX_RE.sub('', subject.strip())
    first_line = next((l.strip() for l in subject.splitlines() if l.strip()), subject)
    first_line = re.sub(r'^or\s+', '', first_line, flags=re.IGNORECASE)
    return first_line.strip().strip('"').strip("'")


def extract_greeting_name(key_person_text: str) -> str | None:
    """
    Pull a first name out of a free-text "key people" answer, for an email
    greeting. Naively taking the first word fails constantly because these
    answers almost always open with the company name, not a person's name
    (e.g. "Acme Corp is co-founded by Jane Smith..." -> first word "Acme").
    Only returns a name when a clear name-introducing pattern matches;
    returns None otherwise so callers fall back to a generic greeting instead
    of guessing wrong.
    """
    if not key_person_text or key_person_text.strip().lower() in ("not found", "none found", ""):
        return None

    match = (
        re.search(r'(?:founded|co-founded|led|run|managed|headed|owned)\s+by\s+([A-Z][a-zA-Z]+)', key_person_text)
        or re.search(r'\b(?:CEO|Founder|Co-Founder|Owner|Director|Manager)\s+([A-Z][a-zA-Z]+)\b', key_person_text)
    )
    if not match:
        return None

    name = match.group(1)
    if name.lower() in ('ceo', 'founder', 'manager', 'director', 'owner', 'co', 'not', 'none'):
        return None
    return name
