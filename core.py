# =============================================================================
# core.py — Shared helpers available to all workflows
# =============================================================================
# Import from here in any workflow file:
#   from core import llm_call, gather_info, wait_for_input, ...
# =============================================================================

import os
import re
import time
import json
from datetime import datetime

from langchain_openai import ChatOpenAI
from tools_web import web_search, scrape_url

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

_llm = ChatOpenAI(
    model=os.getenv("OPENAI_MODEL", "local-model"),
    base_url=os.getenv("OPENAI_API_BASE", "http://host.docker.internal:1234/v1"),
    api_key=os.getenv("OPENAI_API_KEY", "not-needed"),
    temperature=0.7,
)

def llm_call(prompt: str) -> str:
    dbg_block(f"LLM PROMPT  ({len(prompt)} chars)", prompt)
    t0       = time.time()
    response = _llm.invoke(prompt).content
    elapsed  = time.time() - t0
    dbg_block(f"LLM RESPONSE  ({len(response)} chars, {elapsed:.1f}s)", response)
    return response


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

# Shown verbatim to the LLM when it selects a depth — keep descriptions honest.
RESEARCH_DEPTHS = {
    "light": (
        "1 web search, snippet text only, no page scraping. "
        "Fast (~10s). Good for well-known subjects where a quick overview is enough."
    ),
    "medium": (
        "3 web searches (general, recent news, contact/people), snippet text only. "
        "~30s. Good for most subjects — solid coverage without spending time scraping."
    ),
    "deep": (
        "3 discovery searches + LLM picks best URLs to scrape (up to 4 full pages) + "
        "gap-analysis round that issues up to 2 targeted follow-up searches + more scraping. "
        "~2-3 min. Best for obscure subjects, small companies, or when high accuracy is critical."
    ),
}


def select_research_depth(topic: str, context: str = "") -> str:
    """
    Ask the LLM to pick light / medium / deep given the topic and context.
    The LLM sees the full description of each level before choosing.
    """
    depth_menu = "\n".join(
        f'  "{key}": {desc}' for key, desc in RESEARCH_DEPTHS.items()
    )
    prompt = f"""You need to gather information about "{topic}" for an automated task.

Available research depths (read carefully before choosing):
{depth_menu}

Task context: {context or "none"}

Which depth is most appropriate? Consider whether the subject is well-known or obscure, and how much accuracy matters.

Reply with ONLY one word — light, medium, or deep:"""

    result = llm_call(prompt).strip().lower()
    for key in RESEARCH_DEPTHS:
        if key in result:
            return key
    return "medium"


def gather_info(topic: str, depth: str, task_id: str, client, log) -> dict:
    """
    Generic information gathering at a specified depth.

    topic  — anything: a company, person, technology, concept, etc.
    depth  — "light", "medium", or "deep" (see RESEARCH_DEPTHS for what each does)

    Returns:
        summary       — 1-2 sentence overview
        key_facts     — notable facts / recent news / milestones
        key_people    — names and roles of notable people found
        contact_info  — any contact email or URL found
        confidence    — high / medium / low
        raw           — raw LLM extraction text
        corpus        — full gathered text (snippets + scraped pages)

    Returns empty dict if the task is cancelled mid-run.
    """
    all_snippets: list[str] = []
    all_scraped:  list[str] = []
    scrape_count  = 0
    MAX_SCRAPES   = 4

    log(f"🔬 Research depth: {depth}", "info")

    # -------------------------------------------------------------------------
    # Build search queries based on depth
    # -------------------------------------------------------------------------
    if depth == "light":
        queries = [f"{topic}"]
    elif depth == "medium":
        queries = [
            f"{topic}",
            f"{topic} news recent",
            f"{topic} contact people",
        ]
    else:  # deep
        queries = [
            f"{topic}",
            f"{topic} about leadership team",
            f"{topic} contact email",
        ]

    candidate_urls: list[str] = []

    for query in queries:
        log(f"🔍 {query}", "tool_call")
        result = web_search(query, max_results=5)
        all_snippets.append(f"Search: {query}\n{result}")
        urls = re.findall(r'^(https?://\S+)', result, re.MULTILINE)
        candidate_urls.extend(urls)
        log(f"Got {len(result)} chars, {len(urls)} URLs", "tool_result")
        if check_cancelled(task_id, client):
            return {}

    # -------------------------------------------------------------------------
    # Deep only: LLM picks best URLs → scrape → gap analysis → follow-ups
    # -------------------------------------------------------------------------
    if depth == "deep":
        unique_urls = list(dict.fromkeys(candidate_urls))[:15]
        url_list    = "\n".join(f"- {u}" for u in unique_urls)

        select_prompt = f"""From these URLs found while researching "{topic}", select up to 3 most likely to contain rich information (official site, About/Team page, news article, contact page). Prefer official domains.

URLs:
{url_list}

Reply with ONLY a JSON array of up to 3 URLs:
["https://...", "https://..."]"""

        try:
            selected_raw  = llm_call(select_prompt)
            json_match    = re.search(r'\[.*?\]', selected_raw, re.DOTALL)
            selected_urls: list[str] = json.loads(json_match.group())[:3] if json_match else unique_urls[:3]
        except Exception:
            selected_urls = unique_urls[:3]

        for url in selected_urls:
            if scrape_count >= MAX_SCRAPES:
                break
            log(f"🌐 Scraping: {url}", "tool_call")
            content = scrape_url(url, max_chars=4000)
            all_scraped.append(f"FROM {url}:\n{content}")
            scrape_count += 1
            log(f"Got {len(content)} chars", "tool_result")
            if check_cancelled(task_id, client):
                return {}

        log("🔬 Evaluating research gaps", "info")
        combined = "\n\n---\n\n".join(all_snippets + all_scraped)

        evaluate_prompt = f"""You are gathering information about "{topic}". Evaluate what you know and what's still missing.

Research so far:
{combined[:10000]}

CONFIDENCE: <high/medium/low>
MISSING: <what is still unclear, or "nothing">
FOLLOW_UP_SEARCHES: <up to 2 specific queries to fill gaps, comma-separated, or "none">"""

        evaluation    = llm_call(evaluate_prompt)
        log(f"Gap analysis:\n{evaluation}", "agent")
        if check_cancelled(task_id, client):
            return {}

        confidence    = extract_field(evaluation, "CONFIDENCE")
        follow_up_raw = extract_field(evaluation, "FOLLOW_UP_SEARCHES")

        if confidence.lower() != "high" and follow_up_raw.lower() not in ("none", "not found", ""):
            follow_up_queries = [
                q.strip().strip('"').strip("'")
                for q in re.split(r',|\n', follow_up_raw)
                if q.strip() and q.strip().lower() not in ("none", "not found")
            ][:2]

            for query in follow_up_queries:
                log(f"🔍 Follow-up: {query}", "tool_call")
                result = web_search(query, max_results=4)
                all_snippets.append(f"Follow-up: {query}\n{result}")
                log(f"Got {len(result)} chars", "tool_result")
                if check_cancelled(task_id, client):
                    return {}

                if scrape_count < MAX_SCRAPES:
                    follow_urls = re.findall(r'^(https?://\S+)', result, re.MULTILINE)
                    if follow_urls:
                        log(f"🌐 Scraping follow-up: {follow_urls[0]}", "tool_call")
                        content = scrape_url(follow_urls[0], max_chars=3000)
                        all_scraped.append(f"FROM {follow_urls[0]}:\n{content}")
                        scrape_count += 1
                        log(f"Got {len(content)} chars", "tool_result")
                        if check_cancelled(task_id, client):
                            return {}

    # -------------------------------------------------------------------------
    # Final extraction — generic fields any workflow can use
    # -------------------------------------------------------------------------
    log("📋 Extracting structured info", "info")

    corpus = "\n\n---\n\n".join(all_snippets + all_scraped)

    extract_prompt = f"""Based on this research about "{topic}", extract:

SUMMARY: What is this about? (1-2 sentences)
KEY_FACTS: Notable facts, recent news, or milestones (1-2 sentences, or "none found")
KEY_PEOPLE: Notable people and their roles (or "none found")
CONTACT_INFO: Any contact email or URL (or "none found")
CONFIDENCE: Overall confidence in accuracy (high/medium/low)

Research:
{corpus[:12000]}

Respond in the exact format above:"""

    extracted = llm_call(extract_prompt)
    log(f"Info gathered:\n{extracted}", "agent")

    return {
        "summary":      extract_field(extracted, "SUMMARY"),
        "key_facts":    extract_field(extracted, "KEY_FACTS"),
        "key_people":   extract_field(extracted, "KEY_PEOPLE"),
        "contact_info": extract_field(extracted, "CONTACT_INFO"),
        "confidence":   extract_field(extracted, "CONFIDENCE"),
        "raw":          extracted,
        "corpus":       corpus,
    }
