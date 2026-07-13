# =============================================================================
# tracing.py — Structured, persistent debug tracing
# =============================================================================
# Off by default (TRACE_ENABLED unset/"0" — a no-op, zero file I/O, safe to
# call unconditionally from any hot path). When TRACE_ENABLED=1, every LLM
# call, web search, scrape, and persona/memory decision gets appended as one
# UNTRUNCATED JSON line to /workspace/traces/<task_id>.jsonl — full prompts,
# full responses, full scraped page text, the actual schema dict sent. This
# is deliberately separate from core.dbg_block()'s stdout convention, which
# truncates for console readability and doesn't survive a container restart.
# Turn this on for a deliberate test run, inspect the .jsonl file afterward
# (grep/jq), turn it back off — not meant to run always-on in production.
#
# task_id is read from a contextvar by default rather than threaded through
# every call site's signature — most callers in research.py/memory.py already
# have task_id in scope and should pass it explicitly (more precise), but
# core.llm_structured() does not take a task_id parameter at all, and adding
# one would mean changing dozens of call sites just for tracing. router.py
# calls set_current_task() once per task; llm_structured() then traces
# correctly with zero signature changes.
#
# Caveat for future thread-pool parallelism (see session notes on LM Studio's
# 4 parallel slots): contextvars do NOT automatically propagate into threads
# spawned via ThreadPoolExecutor/threading.Thread. If/when concurrent LLM
# calls are added, either pass task_id explicitly into trace() from those
# call sites, or copy the context into each worker thread — don't assume the
# contextvar fallback alone will keep working under concurrency.
# =============================================================================

import os
import json
import random
import uuid
import contextvars
from contextlib import contextmanager
from datetime import datetime, timezone

WORK_DIR      = os.getenv("WORK_DIR", "/workspace")
TRACE_DIR     = os.path.join(WORK_DIR, "traces")
TRACE_ENABLED = os.getenv("TRACE_ENABLED", "0") == "1"

_current_task_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_task_id", default=None
)

# call_id/parent_call_id let the GUI's Chain Explorer reconstruct a real
# ancestry tree instead of guessing from timestamps. _current_call_id always
# reflects "whatever call is innermost right now" — push_call() reads it as
# the new node's parent, then restores the previous value on exit, so
# siblings (e.g. two items in the same run_concurrent() batch, run one after
# another in the same thread) don't leak into each other's parent chain.
_current_call_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_call_id", default=None
)
# Tracks the parent of whatever call_id is current, so trace() calls made
# without explicit call_id/parent_call_id (the common case — most call sites
# just call trace("event", ...) and rely on context) still get the correct
# parent attached, not just the correct call_id.
_current_parent_call_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_parent_call_id", default=None
)
# Full root-to-here lineage, not just the immediate parent — needed so the
# debug GUI's pause/stop/priority controls on one call_id apply to its whole
# descendant subtree (e.g. pausing the chain for one research question also
# pauses every LLM call several push_call() levels deeper inside it), not
# just calls made directly at that exact nesting level. core._check_chain_control()
# walks this list checking each ancestor's control state.
_call_stack: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar(
    "call_stack", default=()
)


@contextmanager
def push_call():
    """
    Start a new call node, nested under whatever call_id is currently active
    (or None if this is a root call, e.g. task_start). Yields the new
    call_id for the caller to attach to its own trace() calls. Restores the
    previous call_id/parent_call_id/call_stack on exit regardless of how the
    block ends, so nesting composes correctly through run_concurrent()'s
    per-task copied contexts (contextvars.copy_context() per submission, not
    one shared context — see core.run_concurrent()).
    """
    call_id = uuid.uuid4().hex[:8]
    parent_call_id = _current_call_id.get()
    call_token = _current_call_id.set(call_id)
    parent_token = _current_parent_call_id.set(parent_call_id)
    stack_token = _call_stack.set(_call_stack.get() + (call_id,))
    try:
        yield call_id, parent_call_id
    finally:
        _current_call_id.reset(call_token)
        _current_parent_call_id.reset(parent_token)
        _call_stack.reset(stack_token)


def current_call_id() -> str | None:
    """Whatever call_id is innermost right now, or None if no push_call() is active."""
    return _current_call_id.get()


def current_call_stack() -> tuple[str, ...]:
    """Root-first lineage of call_ids currently active in this context."""
    return _call_stack.get()

# Docker-style random name for trace events with no real task_id in scope
# (ad-hoc scripts, pytest runs that never call set_current_task()) — beats a
# literal "unknown.jsonl" that every untracked run dumps into and overwrites
# into the same indistinguishable pile. Generated lazily, once per process,
# so every event from one untracked run lands in the SAME file consistently
# rather than a fresh random name per call.
_ADJECTIVES = [
    "sparkling", "quiet", "brave", "fuzzy", "clever", "drowsy", "golden",
    "jolly", "lucky", "mighty", "nimble", "plucky", "silent", "spry",
    "vivid", "witty", "zesty", "amber", "breezy", "cosmic",
]
_NOUNS = [
    "keyring", "otter", "lantern", "compass", "thicket", "harbor", "ember",
    "meadow", "falcon", "anchor", "willow", "comet", "pebble", "marlin",
    "canyon", "sparrow", "ridge", "lagoon", "beacon", "orchard",
]
_fallback_name: str | None = None


def _get_fallback_name() -> str:
    global _fallback_name
    if _fallback_name is None:
        _fallback_name = f"{random.choice(_ADJECTIVES)}-{random.choice(_NOUNS)}"
    return _fallback_name


def set_current_task(task_id: str) -> None:
    """Called once per task (router.route_workflow) so llm_structured()'s
    trace() calls know which task they belong to without a signature change."""
    _current_task_id.set(task_id)


def get_current_task() -> str | None:
    """Whatever set_current_task() last set in this context, if anything."""
    return _current_task_id.get()


def trace(
    event_type: str,
    task_id: str | None = None,
    call_id: str | None = None,
    parent_call_id: str | None = None,
    **fields,
) -> None:
    """
    Append one structured event. No-op unless TRACE_ENABLED=1. Pass task_id
    explicitly when it's already in scope (the precise path); omit it to
    fall back to whatever set_current_task() last set (core.llm_structured()'s
    path, which has no task_id parameter of its own) — or, if that was never
    called either, a random memorable name shared by every untracked event in
    this process (see _get_fallback_name() above).

    call_id/parent_call_id default to whatever push_call() last set via the
    contextvar — pass them explicitly only when tracing a "started" and
    "completed" pair of events for the *same* call_id (the started event is
    emitted from inside the push_call() block; the completed event may want
    to reuse that same call_id after the block exits).
    """
    if not TRACE_ENABLED:
        return

    resolved_task_id = task_id or _current_task_id.get() or _get_fallback_name()
    resolved_call_id = call_id if call_id is not None else _current_call_id.get()
    resolved_parent_call_id = (
        parent_call_id if parent_call_id is not None else _current_parent_call_id.get()
    )
    record = {
        "ts":    datetime.now(timezone.utc).isoformat(),
        "task":  resolved_task_id,
        "event": event_type,
        "call_id": resolved_call_id,
        "parent_call_id": resolved_parent_call_id,
        **fields,
    }

    try:
        os.makedirs(TRACE_DIR, exist_ok=True)
        path = os.path.join(TRACE_DIR, f"{resolved_task_id}.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str, ensure_ascii=False) + "\n")
    except Exception as e:
        # Tracing must never break the actual workflow it's observing.
        print(f"  [TRACE ERROR] failed to write trace event: {e}", flush=True)
