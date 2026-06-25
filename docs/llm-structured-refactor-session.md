# Session Log: Schema-First LLM Refactor

This document covers the conversation that produced the refactor described
in `docs/llm-structured-refactor.md` — what was asked, what was decided, and
how the work was delivered. It's the "why and how the session went," not the
technical writeup; see the sibling doc for the architecture itself.

## The directive

The session opened with an explicit architectural mandate: every LLM call in
the codebase should follow a fixed sequence — build a JSON Schema for the
response first, then build the prompt, then call the LLM, then read the
already-structured result. No more formatting instructions baked into prompt
text with regex extraction on the way back out.

A specific concern was raised alongside the mandate: whether an LLM should
ever be allowed to design a schema for *another* LLM call. The instruction
was not to rule this out, but to treat it as something that "could go
wrong" — worth having available as an escape hatch, not as the default
path. This became `llm_generate_schema()`, gated behind meta-schema
validation with a safe fallback.

## Scope and execution

The conversion was scoped to "every LLM call," which in practice meant
walking the full call graph: `core.py` (where the LLM entry points live),
the new `schemas.py` builder module, `research.py` (seed-question
answering, deep-dive gating), `router.py` (intent classification), and every
workflow file that called into any of the old `llm_call` /
`llm_classify` / `llm_classify_prefill` / `extract_field` functions:
`lead_gen_outreach.py`, `calendar_booking.py`, `business_intro.py`,
`delete_calendar_events.py`, and `email_triage.py` (the largest of the five).
`workflows_guidelines.md` was updated last so the documented convention
matches the code.

Each file was converted, then compiled (`py_compile`) and grepped for stale
references before moving to the next, rather than converting everything and
debugging at the end. The router conversion incidentally fixed a latent bug
where its raw-text output sometimes needed backslash-stripping before it
could be matched against workflow names — schema enforcement made that
class of bug impossible, not just rarer.

Once every call site was converted, a repo-wide grep confirmed
`llm_classify`, `llm_classify_prefill`, `extract_field`,
`_CLASSIFY_PREAMBLE`, and the dedicated `_llm_classify` client in `core.py`
had no remaining callers anywhere. These were deleted as dead code, along
with an import they depended on, and two stale comments referencing the old
convention were updated — this cleanup wasn't separately requested, but
followed from the codebase's own stated convention that confirmed-unused
code should be deleted outright rather than left in place.

All of this was committed in one commit (`7cee4a0`, building on an earlier
`c66b608`) and pushed to `claude/code-review-hgl1no`.

## "push"

After the refactor commit was already pushed, the user sent a bare "push."
`git status` and `git log` showed the branch was already clean and synced
with `origin/claude/code-review-hgl1no` — nothing pending. Reported that
back rather than creating a no-op push.

## "write an MD ... push to main"

The next request was to write a markdown summary of the conversation and
improvements, and push it to `main`. `git branch -a` showed no `main`
branch exists in this repository — only `master`. Rather than guessing
which the user meant or silently substituting one for the other, this was
raised directly via a clarifying question with two options: push directly
to `master`, or open a PR against `master` instead. The user chose to push
directly.

This required deviating from the session's standing rule of never pushing
to a branch other than `claude/code-review-hgl1no` without explicit
permission — the user's branch choice constituted that explicit permission,
scoped specifically to this one action.

Switching to `master` and pulling fast-forwarded the local branch from
`523718e` to `9ac64bd`, pulling in separate prior work (`memory.py`,
persistent per-question research caching, the lead-gen workflow, and other
changes unrelated to this session). Importantly, `master` at that point did
**not** contain the schema-first refactor commits — those exist only on
`claude/code-review-hgl1no`. The working tree reverting to master's
pre-refactor file versions on checkout was expected git behavior, not
something to fix.

The first doc (`docs/llm-structured-refactor.md`) was written to summarize
the refactor itself, committed, and pushed straight to `master` as
`f9c19de` — without merging the actual refactor code, since the request was
for a summary document, not a merge.

## Follow-up: "does that MD contain info from this conversation?"

After the push, the user asked whether the doc covered the conversation
itself or just the refactor. It was the latter — a technical writeup,
not a session narrative. That gap is what this document fills.

## Net result

- `claude/code-review-hgl1no` (`7cee4a0`): the actual schema-first
  conversion, untouched by the master-branch work.
- `master` (`f9c19de`, then this commit): two summary documents — one
  describing the architecture and changes, one describing how the session
  arrived at them — with no code merge.
