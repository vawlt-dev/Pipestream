# =============================================================================
# chain_control.py — GUI-side entry point into core.ChainRegistry
# =============================================================================
# Importing paths (below) sets WORK_DIR correctly for this process before
# core.py ever gets imported — see paths.py's own comment for why that
# ordering matters on Windows. Every debug_gui module that needs to import
# a real agentt module should import paths first for the same reason.
# =============================================================================

import sys

from paths import AGENTT_ROOT

if AGENTT_ROOT not in sys.path:
    sys.path.insert(0, AGENTT_ROOT)

import core  # noqa: E402 — import after paths sets WORK_DIR, see note above


def get_state() -> dict:
    """Raw live_state.json contents — running/queued/max_slots/chains/etc."""
    return core._llm_slot._read_state()


def set_chain_control(call_id: str, control: str) -> None:
    """control: 'run' | 'pause' | 'stop'."""
    core._llm_slot.set_chain_control(call_id, control)


def set_chain_priority(call_id: str, priority: int) -> None:
    """Positive uplifts, negative suppresses, 0 is the default every chain starts at."""
    core._llm_slot.set_chain_priority(call_id, priority)


def stage_override(call_id: str, payload: dict) -> None:
    """Make this one decision yourself instead of the model — payload must
    match the shape the calling code expects back from llm_structured()."""
    core._llm_slot.stage_override(call_id, payload)


def stage_edited_prompt(call_id: str, prompt: str) -> None:
    """Replace the prompt text for this call_id's next LLM call (only takes
    effect while paused, before resuming — see core.llm_structured())."""
    core._llm_slot.stage_edited_prompt(call_id, prompt)
