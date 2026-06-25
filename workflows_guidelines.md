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

## LLM Calls: every call is schema-first

There is one LLM-calling primitive for every call site, including free-form
writing: `llm_structured(prompt, schema, schema_name)` from `core.py`. The
old `llm_call` / `llm_classify` / `llm_classify_prefill` / `extract_field`
functions are gone — don't reintroduce regex/labeled-field parsing of raw
LLM text output. Build the schema first, then write the prompt around it,
then call the LLM, then read the parsed dict:

```python
from core import llm_structured
from schemas import s_object, s_string, s_enum, s_array, s_bool, s_int

_DRAFT_SCHEMA = s_object({"body": s_string()})

result = llm_structured(
    f"Draft a reply to this thread...\n\nbody: the reply body only",
    _DRAFT_SCHEMA,
    schema_name="reply_draft",
)
draft_body = str(result.get("body") or "")
```

This applies to free-form prose too (email bodies, drafted replies) — wrap it
in a single `{"body": ...}` field rather than reaching for unstructured text.
The backend hard-constrains generation token-by-token to the schema, which is
why this fixes an entire class of bugs that prompt instructions alone never
reliably did (a "single subject line" request coming back as five
newline-joined alternatives, a workflow name coming back markdown-escaped).

### `schemas.py` — composable schema builders
Use these instead of hand-writing raw schema dicts at call sites:
- `s_string()`, `s_int()`, `s_bool()` — primitive fields
- `s_enum(["a", "b"])` — a string constrained to a fixed, known set of
  values. **Always prefer this over a free-form string** wherever the valid
  values are known ahead of time (classifications, YES/SKIP/EDIT decisions,
  workflow names) — it makes invalid output structurally impossible rather
  than something you clean up after the fact
- `s_array(item_schema)` — a list of some other schema (string, enum, or
  nested object)
- `s_object(properties, required=None)` — top-level or nested object;
  defaults to requiring every declared property and `additionalProperties:
  false`, matching what `llm_structured()`'s `strict` mode needs

Declare one module-level `_THING_SCHEMA = s_object({...})` per distinct shape
near the top of the workflow file, and reuse it across call sites that share
a shape (e.g. one booking-extraction schema reused by both the first-pass
extraction and a clarification re-extraction).

When you need an array of objects rather than an array of bare strings — e.g.
extracting candidate companies — prefer `s_array(s_object({"name": ...,
"reason": ...}))` over a flat list of names. Forcing a `reason`/justification
field per item is what catches a model including something that doesn't
actually belong (a competitor, an unrelated entity) — the value isn't JSON
parsing reliability (schema-constrained output already guarantees that), it's
that requiring a justification surfaces semantically wrong inclusions that a
flat string list would hide.

### `llm_generate_schema(description)` — escape hatch, not a default
For the rare call whose output shape genuinely isn't knowable ahead of time,
`schemas.py` exposes `llm_generate_schema()` — it asks the LLM to design its
own schema for another LLM call, validates the result against the real JSON
Schema meta-schema, and falls back to a fully permissive `{"type": "object"}`
on any failure (bad JSON, invalid schema) rather than crashing. Prefer a
hand-built schema from the builders above wherever the shape is knowable —
this exists for genuinely dynamic/one-off shapes, not as a shortcut to avoid
designing a schema.

> **Note:** Mistral 7B in LM Studio only supports `user` and `assistant` roles.
> Never use `SystemMessage` — it will crash with a 400 error. Any constraint
> must be expressed in the prompt text itself; `llm_structured()`'s
> `response_format` schema is what actually enforces shape, not a system
> message.

---

## Prompt Design Rules

Since the schema enforces shape, prompts only need to describe content:
- List each field as a short label, e.g. `event_name: what is being booked,
  or "not found"` — don't restate JSON syntax, the schema already is the
  format instruction
- Do NOT ask for reasoning or explanations on fields that don't need one —
  that's wasted tokens
- Be explicit about content constraints text generation still needs:
  "Do NOT include a greeting or sign-off", explicit tone/length expectations
- For an array field meant to align 1:1 with a numbered input list (e.g. one
  classification per email), say so explicitly ("in the same order") and pad
  defensively on the read side — `result.get("things") or []` then
  `list(... ) + [default] * shortfall` — rather than assuming the model
  returns exactly the expected length

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
