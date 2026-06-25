# Schema-First LLM Calls: Conversion Notes

This document summarizes a refactor performed on the `claude/code-review-hgl1no`
branch that converts every LLM call in the codebase from ad hoc text parsing to
a unified, schema-first pattern. It exists for context — the actual code lives
on `claude/code-review-hgl1no` (commits `c66b608`, `7cee4a0`), not on `master`.

## The mandate

The previous convention had three different LLM entry points in `core.py`:

- `llm_call(prompt)` — free text, for drafting and reasoning
- `llm_classify(prompt)` — low-temperature, constrained-by-preamble text
- `llm_classify_prefill(prompt)` — same, with an assistant-message `"1:"`
  prefill to force numbered-list output

Every call site that needed structured data (a parsed name/date/duration, a
KEEP/DELETE verdict, a list of candidate companies) got that structure by
asking the model to format its reply a certain way in the prompt, then
regex-matching the response on the way back out. This worked, but every
regex was a separate point of failure: models drop fields, reorder lines,
add prose around the requested format, or wrap answers in markdown. Several
workflows had grown defensive fallback logic (e.g. scanning raw input text
for a number when the `COUNT:` field came back empty) purely to compensate.

The directive: every LLM call should first build a JSON Schema describing the
exact shape of the response it needs, then build the prompt, then call the
LLM with that schema enforced, then read the parsed result directly — no
regex extraction from free text, anywhere.

## New building blocks

### `schemas.py` (new file)

A small composable builder layer:

- `s_string()`, `s_int()`, `s_bool()`
- `s_enum(choices)` — for closed-set classifications (verdicts, actions,
  confidence levels) instead of a free string that has to be validated after
  the fact
- `s_array(item_schema)`
- `s_object(properties, required=None)` — defaults to requiring every
  property and setting `additionalProperties: False`, so the model can't
  silently omit a field or bolt on extras

### `core.llm_structured(prompt, schema, schema_name)`

The new canonical entry point. Builds the request with `response_format`
constrained to the given JSON Schema, calls the model, and returns the
already-parsed dict — no string wrangling on the call site.

### `core.llm_generate_schema(description, schema_name)` — escape hatch

An opt-in mechanism for letting an LLM design a schema for *another* LLM
call, for cases that don't fit a hand-written shape. The generated schema is
validated against the JSON Schema meta-schema
(`jsonschema.Draft202012Validator.check_schema()`) before use; on any
validation failure it falls back to a permissive `{"type": "object"}` rather
than crashing. This is explicitly documented as not a default — hand-written
schemas remain the normal path.

`llm_call()` itself was kept, but narrowed to a single legitimate remaining
caller: `llm_generate_schema()`, which needs unconstrained text because its
job is producing a schema, not consuming one.

## Per-file changes

- **`router.py`** — `classify_intent()` now uses
  `s_object({"workflow": s_enum(valid_names)})` instead of free-text
  classification plus substring matching. This also fixed a latent bug where
  the router's output occasionally needed backslash-stripping before it
  could be matched against workflow names.
- **`research.py`** — seed-question answering and the deep-dive gate
  (`should_deep_dive`) converted to structured schemas.
- **`workflows/lead_gen_outreach.py`** — parsing of sender identity/goal/count,
  search query generation, candidate extraction, angle generation, draft
  body, and subject line all converted. Candidate extraction was upgraded
  from a flat array of company-name strings to an array of
  `{name, reason}` objects — see below for why.
- **`workflows/calendar_booking.py`** — booking-detail parsing and the
  YES/NO confirmation step converted; confirmation now reads as a real
  `bool` instead of a parsed string.
- **`workflows/business_intro.py`** — company/email parsing, angle
  generation, draft body, and subject line converted.
- **`workflows/delete_calendar_events.py`** — time-range/theme extraction
  converted; per-event DELETE/KEEP/UNSURE matching converted from
  line-by-line regex matching against numbered output to a single structured
  array call, consumed via `zip(events, matches)`.
- **`workflows/email_triage.py`** (largest conversion) — batch email
  classification, calendar-booking detail extraction (both the initial pass
  and the post-clarification re-extraction), reply-draft generation, the
  YES/SKIP/EDIT reply-intent decision, and edit-rewrite all converted. The
  old `_parse_classifications()` regex-based recovery helper was deleted
  entirely — no longer needed once the model can't return malformed output.

## Design pattern: `{name, reason}` over flat string arrays

For extraction tasks like "find candidate prospect companies," the schema
was deliberately built as an array of `{name, reason}` objects rather than
an array of bare strings. JSON-parsing reliability was already solved by
schema enforcement — that's not what this pattern buys. The actual problem
it addresses is *semantic* correctness: forcing the model to state a reason
for each inclusion is what catches things like a competitor being
mistakenly extracted as a prospect, because "this company sells competing
services to ours" surfaces as a visibly wrong reason at review/log time
instead of silently riding along in a string list.

## Design pattern: array alignment for one-output-per-input calls

Calls like "classify each of these N emails" or "give a verdict for each of
these N events" return a JSON array meant to line up positionally with the
input list. Schema enforcement guarantees well-formed JSON, but not that the
array is exactly length N (the model can still under- or over-generate
items). The convention adopted: read the array defensively
(`result.get("things") or []`), then pad or truncate to the expected length
before zipping with the input list, rather than trusting the model's count.

## Cleanup in `core.py`

Once every call site was converted, `llm_classify()`, `llm_classify_prefill()`,
`_CLASSIFY_PREAMBLE`, the dedicated `_llm_classify` low-temperature client,
and the regex-based `extract_field()` helper were all confirmed unused
repo-wide and deleted, along with the now-unused
`langchain_core.messages` import they depended on. Two stale comments
referencing the old `llm_call`/`extract_field` convention (in the module
docstring and the `gather_info()` section) were updated to reference
`llm_structured`.

## Documentation

`workflows_guidelines.md` was updated to replace the old "three LLM
functions" convention writeup with a new "every call is schema-first"
section: the `_THING_SCHEMA` + `llm_structured()` pattern (including for
free-form prose, wrapped in a single-field schema), the `schemas.py`
builder reference, the `{name, reason}` rationale, the array-alignment
padding convention, and `llm_generate_schema()` documented explicitly as an
escape hatch rather than a default.

## Status

All conversion work is complete and pushed to `claude/code-review-hgl1no`
(commit `7cee4a0`). It has not been merged into `master` — this document
was added directly to `master` as a standalone summary, at the user's
request, without merging the underlying code changes.
