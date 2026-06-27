# Pipestream

A self-hosted AI agent worker that polls a VPS for tasks, runs LLM-driven
business workflows (cold outreach, intro emails, inbox triage, calendar
booking), and builds up a persistent, reusable knowledge base as it goes —
all powered by a local model running in LM Studio, never a cloud LLM API.

Runs inside Docker on your own machine. The VPS only ever sees task requests
and results; the actual research, reasoning, and drafting happen locally
against your LM Studio instance.

## How it fits together

```
                 ┌──────────────┐         poll for tasks          ┌──────────────────┐
   you, via      │   VPS / web  │ ◄─────────────────────────────► │  agent_worker.py  │
   the website   │   frontend   │      results, logs, status      │  (inside Docker)  │
                 └──────────────┘                                 └─────────┬─────────┘
                                                                              │
                                                                  dispatches to
                                                                              ▼
                                                                     ┌────────────────┐
                                                                     │   router.py    │  scans workflows/,
                                                                     │                │  classifies intent,
                                                                     └───────┬────────┘  runs the match
                                                                             │
                          ┌──────────────────────────────────────────────────┼──────────────────────────────┐
                          ▼                                                  ▼                              ▼
                 workflows/lead_gen_outreach.py          workflows/email_triage.py        workflows/business_intro.py, ...
                          │                                                  │
                          └─────────────────────┬────────────────────────────┘
                                                 ▼
                          core.py (llm_structured, gather_info, guardrails)
                                                 │
                ┌────────────────────────────────┼─────────────────────────────────┐
                ▼                                ▼                                 ▼
        research.py (seed questions,      memory.py (SQLite cache:        tools_web.py / tools_google.py
        persona resolution, pitch          per-question facts +           (DuckDuckGo search/scrape,
        logic, disambiguation)             outreach history)              Gmail, Calendar)
                                                 │
                                                 ▼
                                    LM Studio (local model, OpenAI-
                                    compatible API, never leaves your machine)
```

## Why a local model, and why that shapes everything here

Every LLM call in this codebase goes through `core.llm_structured()`, which
uses LM Studio's strict `json_schema` response format — the backend
constrains generation token-by-token to a real JSON Schema, rather than
hoping a "reply with ONLY JSON" instruction gets followed. A lot of the
design in this repo exists specifically because the model behind it is
small and genuinely unreliable in ways a frontier API model usually isn't:

- **Structural reliability (schema enforcement) is not the same as semantic
  correctness.** Schema enforcement guarantees *shape*, not truth — the
  model can still confidently classify a coffee-discovery app as "a
  hypothetical character," or split one real organization into two
  hallucinated "candidates." Several guardrails in this repo exist
  specifically to catch that class of failure (see Guardrails below).
- **"Don't do X" prompt instructions get ignored constantly.** Email drafts
  reliably come back with a greeting/signature/subject line baked in
  despite being told not to — `core.py`'s sanitization functions exist
  because the fix has to be structural, not another sentence in the prompt.
- **LLM call latency dominates wall-clock time**, and the backend has a
  real, configurable concurrency ceiling (LM Studio's "Num Parallel
  Slots"). The concurrency model here (below) is built around that
  constraint specifically.

## Quick start

1. Copy `.env.example` to `.env` and fill in `VPS_API_KEY` (and review the
   other defaults — see [Configuration](#configuration)).
2. Put `credentials.json` and `token.pickle` (Google OAuth) in `./workspace/`.
   Run `python auth_setup.py` once if you don't have a `token.pickle` yet.
3. Have [LM Studio](https://lmstudio.ai/) running locally with a model
   loaded, with its "Num Parallel Slots" server setting matching
   `MAX_PARALLEL_LLM_CALLS` in your `.env`.
4. `docker-compose up --build`

The worker will start polling `VPS_BASE_URL` for tasks. Source files are
volume-mounted (see `docker-compose.yml`), so editing `core.py`, `research.py`,
any workflow, etc. only needs `docker-compose restart agent` to take effect —
a full rebuild is only needed for new top-level files, `requirements.txt`, or
`Dockerfile` changes.

## Workflows

Workflows are auto-discovered from `workflows/*.py` — drop in a new file
exposing `WORKFLOW_META = {"name": ..., "description": ...}` and
`run(task_id, input_text, client) -> None`, and `router.py` picks it up on
the next task with zero registration elsewhere. The router shows every
workflow's description to the LLM and asks it to classify which one a new
request matches (enum-constrained to the real discovered names, so it can't
hallucinate a workflow that doesn't exist).

| Workflow | What it does |
|---|---|
| `lead_gen_outreach` | Discovers multiple prospect companies for a stated goal, researches each, drafts a personalized cold email per prospect, saves each as a Gmail **draft** (never auto-sent). Excludes prospects already contacted in a prior run for the same sender. |
| `business_intro` | Researches one named company, drafts a personalized intro email, and **sends** it after you confirm via the website. |
| `email_triage` | Fetches recent inbox emails, classifies each (reply needed / book appointment / both / fyi / handled), drafts replies for your approval, and books calendar events directly for clear scheduling requests. Recognizes replies connected to a prior outreach campaign and drafts with that context instead of treating every thread as cold. |
| `calendar_booking` | Parses a scheduling request and books a Google Calendar event after confirmation. Also exposes `book_directly()` as a programmatic entry point other workflows call into. |
| `delete_calendar_events` | Finds and deletes matching calendar events after confirmation. |
| `self_study` | No specific company or task — proactively fills gaps in the persona knowledge base built up by other workflows, and researches concepts adjacent to what's already known. Bounded per run; re-trigger to keep going. |

## The research & memory system

### Seed questions, and the persona/prospect split

`research.py` answers questions about any topic — a company, a person, a
technology, a concept — using a fixed set of 15 generic seed questions
(`SEED_QUESTIONS`, "what is X fundamentally," "who are the key
stakeholders," etc.). Every topic is researched at one of two levels:

- **`prospect`** — a specific named real-world entity (e.g. "Acme Corp").
  Researched fresh, narrowly, every time it comes up.
- **`persona`** — a role or category (e.g. "accounting firm"), reusable
  across every unrelated prospect that falls into it.

`core.gather_info()` is the entry point every workflow calls. For a
`prospect` call, it always answers the foundational "what is this,
fundamentally" question first, uses that grounded answer (not the bare
name) to resolve a persona/category via `research.resolve_persona()`, then
cascades into persona-level research and pitch-logic for that category —
*reused from cache* for every future prospect that resolves to the same
category, so the system doesn't re-derive "what does an accounting firm
need" from scratch every single time.

### Growing vocabulary, not a hardcoded category list

`resolve_persona()` doesn't pick from a fixed enum of categories — the
vocabulary *is* whatever's already cached (`memory.memory_list_personas()`).
A new entity is checked against the existing list (enum-constrained, so the
match is reliable) before minting a new category, and — critically —
classification always runs against *grounded research content*, never the
entity's bare name. An opaque name like "RJ and Decker" gives no lexical
hint about what it is; "Bob's Accounts" does. Grounding first is what makes
both resolve to the same `"accounting firm"` cache key.

### Pitch logic

Beyond descriptive facts, `research.PITCH_QUESTIONS` answers three
persona-level questions specifically about *receptiveness to a pitch* —
what pain points make this category receptive to a vendor, what language
resonates, what objections to expect. `gather_pitch_logic()` caches these
the same way as everything else. `format_answers_as_context()` renders
pitch logic first, then persona facts, then prospect-specific facts, so a
drafting prompt sees cached strategic scaffolding before raw research —
handing a small model finished guidance to fill in around, rather than
asking it to invent a pitch strategy live on every single run.

### Disambiguation

A name can refer to more than one real thing — "ABC Accounts" cross-matched
an unrelated language school once during testing; a generic business name
can collide with an entirely different real company. Before any research
happens, `core.disambiguate_if_needed()` runs a cheap ambiguity check
(`research.check_ambiguity()`). If multiple genuinely distinct candidates
turn up, it either asks you directly (`wait_for_input()`) or, for a
trusted/autonomous task, auto-picks the best-supported candidate and logs
the decision — never silently guesses and never blindly trusts a "yes" from
a trusted-task fast path as if it were an actual disambiguating answer.

### Guardrails

Schema enforcement fixes structure, not truth or tone — these catch what it
doesn't:

- **`research._is_generalizable()`** — before caching a persona-level fact,
  checks it's actually generic enough for *any* instance of that category,
  not leaking one specific entity's details into reusable knowledge.
- **`core.check_draft_appropriateness()`** — before a draft is created,
  checks for presumptuous, sensitive, or unverifiable claims about the
  recipient (the kind of thing a small model will confidently generate from
  a vague "what pain points would this persona have" prompt without any
  sense that it's inappropriate to assert about real people). Drafts already
  require human approval before sending — this is a second layer, not a
  replacement.
- **`core.clean_email_draft()` / `clean_subject_line()`** — strips a leaked
  greeting/signature/subject line the model includes anyway, despite being
  told not to.

## Concurrency model

LM Studio's "Num Parallel Slots" setting is the real ceiling on how many
LLM calls can run at once — and it typically *divides* the model's total
context window across however many slots are configured, so raising slots
without raising context (or vice versa) has real tradeoffs, not just a
speed/memory one. Two decoupled mechanisms here, not one:

- **`core._llm_slot`** — a global semaphore, sized by `MAX_PARALLEL_LLM_CALLS`
  (set this to match LM Studio's actual slot count). Every `llm_structured()`
  call passes through it automatically, from anywhere in the codebase, with
  zero changes needed at any call site.
- **`core.run_concurrent()`** — a separate, more generous thread pool for
  running independent research chains concurrently. Deliberately **not**
  capped at the same number as the semaphore: a chain spends most of its
  time on non-LLM I/O (search, page scraping), so capping the chain pool at
  the LLM slot count would leave slots idle whenever a chain is mid-scrape.
  Safe to nest — a chain run here can itself call `run_concurrent()` again
  for its own sub-work, since the semaphore (not pool size) is what actually
  bounds real backend concurrency.

`gather_info()`'s own cascade is deliberately *staged*, not all fired at
once: the persona-level call and the pitch-logic call each spawn their own
internal concurrent batch, so running every group simultaneously would
multiply how many chains compete for the same slots at once rather than
just speeding things up — confirmed costly in practice on memory-constrained
local inference hardware, not just a semaphore-bound concern.

One escape hatch is **not** parallelized: `research.branch_research()`, the
bounded-branching "deep dive" triggered by `should_deep_dive()` when more
research would meaningfully change a task's outcome. Worst case 4³ = 64
leaf questions, fully sequential. It's the most expensive thing in this
codebase by a wide margin when it fires — budget for it accordingly, or
treat it as a known target for a future parallelization pass.

## Debugging and tracing

Set `TRACE_ENABLED=1` (restart required — read once at process start) to
capture **every** LLM call (full prompt, full schema, full response,
`elapsed_s` and `wait_s` — time spent queued behind the semaphore, separate
from actual inference time), every page scrape (untruncated), and every
persona/memory decision to `/workspace/traces/<task_id>.jsonl`. Off by
default — no file I/O overhead in normal operation. This is meant for a
deliberate test run, not always-on production logging; `core.dbg_block()`'s
stdout output (visible in `docker logs agent-worker`) is the always-on,
truncated-for-readability equivalent.

`wait_s` specifically is the number to watch if you change LM Studio's slot
count or `MAX_PARALLEL_LLM_CALLS` — consistently high means turn concurrency
up to match whatever LM Studio is actually configured for; consistently
near-zero means you're already at the ceiling.

## Testing

```
docker exec agent-worker python -m pytest tests/ -v               # fast tests only would need -m "not integration"
docker exec agent-worker python -m pytest tests/ -v -m integration # real LLM/network calls, needs LM Studio running
```

`tests/conftest.py` provides `temp_memory_db` (isolates every test from the
real `/workspace/memory.db`) and a `FakeClient` standing in for the real VPS
task client. `tests/dynamic_invoke.py` provides a generic invoke-anything
layer — `call("module.function", ...)` by dotted path, and
`call_workflow(name, ...)` that reuses `router.load_workflows()`'s own
discovery — so tests can target workflows or functions that didn't exist
when the test was written, with zero static imports to update.
`tests/test_workflow_registry.py` is parametrized at collection time over
whatever `load_workflows()` currently returns, so a new workflow file is
automatically covered by a structural smoke test without touching this repo.

Several existing workflows have real Gmail/Calendar side effects — the
registry smoke test deliberately only checks structure (valid
`WORKFLOW_META`, callable `run()`), never actually invokes a workflow's
`run()`.

## Configuration

See `.env.example` for the full list. Notable ones:

| Variable | Purpose |
|---|---|
| `VPS_BASE_URL`, `VPS_API_KEY` | Where to poll for tasks and how to authenticate. |
| `OPENAI_API_BASE`, `OPENAI_MODEL` | LM Studio's OpenAI-compatible endpoint and loaded model name. |
| `MAX_PARALLEL_LLM_CALLS` | Must match LM Studio's "Num Parallel Slots". See [Concurrency model](#concurrency-model). |
| `TRACE_ENABLED` | Full debug tracing to `/workspace/traces/`. See [Debugging and tracing](#debugging-and-tracing). |
| `SENDER_NAME`, `SENDER_TITLE`, `SENDER_WEBSITE`, `SENDER_BIO` | Fixed sender identity used by `business_intro`/`email_triage` (these workflows have no per-request sender the way `lead_gen_outreach` does — see Known limitations). |

## Known limitations

- **`business_intro`/`email_triage` share one fixed sender identity**
  (`SENDER_NAME` from `.env`), while `lead_gen_outreach` parses a per-request
  sender company from the task input. Cross-workflow outreach history (so a
  company already intro'd via `business_intro` is excluded from a later
  `lead_gen_outreach` run) only works if those identity strings happen to
  match — there's no unified signed-in-user identity yet to key this on
  properly.
- **`branch_research()` is not parallelized** (see Concurrency model) and is
  the single most expensive thing that can happen in this codebase when it
  fires — confirmed in practice to dominate a task's total runtime.
- **Disambiguation candidates can themselves be hallucinated.** The model
  can occasionally split one real entity into two "candidates" with a
  fabricated distinguishing detail (caught once in testing: a New
  Zealand-named organization got tagged "Australian" with zero supporting
  evidence in the actual search results). The appropriateness/generalizability
  gates don't currently cover this specific failure mode — worth a
  dedicated fix (requiring candidates to cite evidence from the actual
  search results, the same `{name, reason}`-forces-justification pattern
  already used for candidate company extraction).
- **No test framework existed before this round of work** — coverage is
  meaningfully incomplete; new functionality should add tests as it lands
  rather than assuming an existing safety net.
