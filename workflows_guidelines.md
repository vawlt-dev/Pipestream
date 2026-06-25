# Workflow Development Guidelines

## Research Levels: persona vs prospect

`gather_info()` requires a `topic_level` argument — no default, every call
site must specify it explicitly:

- **`"prospect"`** — a specific, named, real-world entity (a company, a
  person). Not reusable; specific to one target. This is what you want almost
  all the time — researching "Acme Corp" or "Bob Schneider".
- **`"persona"`** — a role/category, not a named entity (e.g. "commercial
  landscaping company director", "property manager"). Reusable across many
  unrelated prospects that share the role. Rare in practice today — both
  existing research-using workflows (`business_intro`, `lead_gen_outreach`)
  are `"prospect"`-level throughout.

This distinction exists because the two need different search queries and
different extraction discipline. A `persona` query must never target the role
as a literal proper-noun search (`"property manager"` as a search returns
pages about specific real property managers, not an abstraction) — genericized
phrasing is required (`"common challenges property managers face"`). If you're
not sure which one applies, it's `"prospect"` — `"persona"` is only for
genuinely reusable role-level research, not a shortcut for "I don't have a
specific name yet."

## Persistent Memory (`memory.py` + `research.py`)

Mistral has a small context window — don't make it re-research the same topic
every run. `gather_info()` handles this automatically: it picks 3 of 15 fixed
seed questions relevant to your task, answers each (checking the cache first),
and returns the answers — no separate memory calls needed in workflow code.

```python
info = gather_info(
    "Acme Corp", "prospect", task_id, client, log,
    context="Writing a cold outreach email. Goal: introduce our consulting service",
)
if not info:
    return  # cancelled mid-research

research_context = format_answers_as_context(info)   # for embedding in drafting prompts

# Best-effort — only present if Q3 (stakeholders) happened to be selected
key_person = next(
    (a["answer"] for a in info["answers"] if a["question_id"] == "Q3" and a["was_answered"]),
    "not found",
)
```

- Backed by SQLite at `/workspace/memory.db`, one row per
  `(topic, topic_level, question_id)` — survives restarts and rebuilds
- Cache reads/writes happen *inside* `research.answer_question()` — workflows
  never call the cache directly, only `gather_info()`
- Cache freshness is volatility-based, not a fixed TTL — the model classifies
  each answer's expected staleness (`VOLATILE` through `GLACIAL`) when it's
  extracted; expiry is computed from that, not a runtime parameter
- None of the 15 seed questions ask for a literal contact email/address — use
  `find_contact_email(topic, task_id, client, log)` from `research.py`
  directly for that (prospect-only, doesn't apply to persona topics)
- Return shape has **no** `summary`/`key_facts`/`key_people`/`contact_info`/
  `raw`/`corpus` fields — that legacy shape is gone. Use
  `format_answers_as_context(info)` for prompt-building, or iterate
  `info["answers"]` directly for per-question access (`question_id`,
  `question_text`, `answer`, `confidence`, `volatility`, `was_answered`)

### The synchronous deep-dive escape hatch

Every `gather_info()` call includes a gate (`research.should_deep_dive`) that
can trigger a full bounded branching research pass (up to depth 3, branching
factor 4 — worst case 64 extra searches) on the most relevant selected
question. This is a deliberate latency tradeoff: there's no async task
pipeline yet, so triggering it blocks the current task for potentially many
minutes. It's logged under a dedicated `log_type="deep_dive"` (styled
distinctly in the website's log view) specifically so it's easy to spot how
often it fires and whether it's earning its cost. Don't add code that
encourages it to fire more often without a real reason — if it starts firing
frequently, that's a signal to build the deferred/async version, not to relax
the gate prompt.

## LLM Function Selection

The two LLM functions have different purposes — use the right one for the job.

### `llm_call(prompt)` — for writing
Use when Mistral should produce free-form text intended for a human to read.
- Drafting emails
- Writing reply bodies
- Summarising research
- Any creative or conversational output

### `llm_classify_prefill(prompt)` — for structured output
Use when Mistral needs to return a constrained, machine-readable response.
- Classifying emails (reply_needed / fyi / etc.)
- KEEP / REMOVE decisions
- YES / SKIP / EDIT decisions
- Any numbered list output

A system message forces "classifier mode" — no explanations, no reasoning, just
the format. The prefill forces output to start with `1:` so the model can't open
with prose. This saves significant tokens on every classification call.

### `llm_classify(prompt)` — structured output without a numbered list
Same constraint preamble as `llm_classify_prefill` but without the `1:`
prefill. Use for single-value classification (e.g. one label, one word).

> **Note:** Mistral 7B in LM Studio only supports `user` and `assistant` roles.
> Never use `SystemMessage` — it will crash with a 400 error. Constraints must
> be prepended to the user message instead.

---

## Prompt Design Rules

**Structured output prompts** — keep them minimal:
- List the input as `N. sender | subject` — no snippets, no extra fields
- One-line format instruction at the end: `Reply with one line per email in the format   N: action`
- Do NOT put example output lines at the bottom — the model treats them as
  its own output and continues in the wrong style
- Do NOT ask for reasoning or explanations — that's wasted tokens

**Text generation prompts** — be explicit about what NOT to include:
- "Do NOT include a greeting or sign-off"
- "Write ONLY the reply body"
- Explicitly state tone/length expectations

---

## Workflow Structure

Every workflow file must expose:
```python
WORKFLOW_META = {
    "name": "workflow_name",       # snake_case, matches filename
    "description": "...",          # shown to the router LLM — be specific about
                                   # when to use this workflow vs others
}

def run(task_id: str, input_text: str, client) -> None:
    ...
```

### Subroutine entry points
If a workflow can be called programmatically by another workflow (no user
interaction, structured data in), expose a separate function:
```python
def do_thing_directly(task_id, arg1, arg2, ..., client) -> bool:
    ...
```
See `calendar_booking.book_directly()` as the reference pattern.

---

## Logging

```python
def log(msg: str, log_type: str = 'info'):
    print(f'  [{log_type.upper()}] {msg}')
    client.log(task_id, msg, log_type)
```

| log_type     | When to use                                      |
|--------------|--------------------------------------------------|
| `info`       | General status updates shown on the website      |
| `tool_call`  | About to call an external tool/API               |
| `tool_result`| Result of a tool call                            |
| `agent`      | LLM reasoning output (classifications, drafts)   |
| `error`      | Something went wrong                             |
| `success`    | Task completed successfully                      |
| `deep_dive`  | Branching research escape hatch fired (or was declined) — see Research Levels above |

Use `dbg()` / `dbg_block()` from `core.py` for Docker console-only debug output
that should never appear on the website.

---

## User Interaction

```python
answer = wait_for_input(task_id, question, client, timeout=300)
if not answer:
    return  # timed out or cancelled
```

- In **trusted mode**, `wait_for_input` returns `"yes"` immediately — never use
  it to gather actual data (dates, names, etc.) in a trusted context
- If you need missing data and the task is trusted, skip cleanly rather than
  trying to re-extract from `"yes"`
- Always check `if not answer: return` immediately after

---

## Cancellation

Call `check_cancelled(task_id, client)` between every major step:
```python
if check_cancelled(task_id, client):
    return
```
Long-running workflows (email fetch, multi-batch classification, research) should
check after every batch or API call.
