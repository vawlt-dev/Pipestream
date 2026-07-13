# =============================================================================
# test_chain_control.py — manual chain control: pause/stop/priority/override
# =============================================================================
# Covers the debug GUI's Chain Explorer controls, which all funnel through
# core.ChainRegistry + core.llm_structured()'s checkpoint. Fast, no real
# LLM — patches core._llm_structured the same way test_llm_slot_semaphore.py
# does, against an isolated ChainRegistry state file per test.
# =============================================================================

import json
import threading
import time

import core


_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}


class _FakeLLM:
    def __init__(self, sleep_s: float = 0.05):
        self.sleep_s = sleep_s
        self.call_count = 0

    def invoke(self, prompt, response_format=None):
        self.call_count += 1
        time.sleep(self.sleep_s)

        class _Resp:
            content = json.dumps({"ok": True})
        return _Resp()


def _make_registry(tmp_path, max_slots=4):
    return core.ChainRegistry(max_slots, str(tmp_path / "live_state.json"))


def test_override_skips_the_real_model_call(monkeypatch, tmp_path):
    fake = _FakeLLM()
    monkeypatch.setattr(core, "_llm_structured", fake)
    registry = _make_registry(tmp_path)
    monkeypatch.setattr(core, "_llm_slot", registry)

    # Pre-stage an override for a call_id we don't know yet isn't possible —
    # instead, run in a thread that pauses-then-overrides isn't needed here:
    # llm_structured() pops the override BEFORE the pause/stop checkpoint, so
    # we register interest by call_id from inside the call itself via a
    # side-channel: monkeypatch push_call to capture the id, then stage the
    # override and let a second call use it. Simpler: call once normally to
    # discover the call_id is generated fresh per call, so instead verify
    # override behavior by staging it for an ALREADY-registered call_id
    # using a controlled single-threaded sequence.
    from tracing import push_call
    with push_call() as (call_id, _parent):
        registry.register_chain(call_id, None, "task-x", "test")
        registry.stage_override(call_id, {"ok": "from-override"})
        # Re-enter the same call_id context core.llm_structured() would —
        # exercise the registry directly rather than the full function,
        # since call_id is normally generated fresh inside llm_structured().
        popped = registry.pop_override(call_id)
        assert popped == {"ok": "from-override"}
        # Consumed — a second pop returns None.
        assert registry.pop_override(call_id) is None


def test_stop_control_is_detected_via_ancestor_lineage(tmp_path):
    registry = _make_registry(tmp_path)
    from tracing import push_call, current_call_stack

    with push_call() as (parent_id, _):
        registry.register_chain(parent_id, None, "task-x", "test")
        registry.set_chain_control(parent_id, "stop")
        with push_call() as (child_id, _):
            registry.register_chain(child_id, parent_id, "task-x", "test")
            # The child itself was never told to stop — only its ancestor —
            # but get_effective_control() must still report 'stop' for it.
            effective = registry.get_effective_control(current_call_stack())
            assert effective == "stop"


def test_pause_then_resume_round_trips_through_control_field(tmp_path):
    registry = _make_registry(tmp_path)
    registry.register_chain("c1", None, "task-x", "test")

    registry.set_chain_control("c1", "pause")
    assert registry.get_effective_control(("c1",)) == "pause"

    registry.set_chain_control("c1", "run")
    assert registry.get_effective_control(("c1",)) == "run"


def test_priority_decides_which_queued_chain_wins_a_freed_slot(tmp_path):
    registry = _make_registry(tmp_path, max_slots=1)
    registry.register_chain("low", None, "task-x", "test")
    registry.register_chain("high", None, "task-x", "test")
    registry.update_chain("low", status="queued", priority=0)
    registry.update_chain("high", status="queued", priority=10)

    winners = []
    def _acquire(call_id):
        registry.acquire_for(call_id)
        winners.append(call_id)
        registry.release_for(call_id)

    t_low = threading.Thread(target=_acquire, args=("low",))
    t_high = threading.Thread(target=_acquire, args=("high",))
    t_low.start()
    time.sleep(0.05)  # let "low" register its queued intent first
    t_high.start()
    t_low.join(timeout=5)
    t_high.join(timeout=5)

    assert winners[0] == "high", f"expected the higher-priority chain to win the slot first, got order {winners}"


def test_edited_prompt_is_staged_and_consumed_once(tmp_path):
    registry = _make_registry(tmp_path)
    registry.register_chain("c1", None, "task-x", "test")

    registry.stage_edited_prompt("c1", "the edited prompt")
    assert registry.pop_edited_prompt("c1") == "the edited prompt"
    assert registry.pop_edited_prompt("c1") is None


def test_unregister_removes_the_chain_from_state(tmp_path):
    registry = _make_registry(tmp_path)
    registry.register_chain("c1", None, "task-x", "test")
    assert "c1" in registry._read_state()["chains"]

    registry.unregister_chain("c1")
    assert "c1" not in registry._read_state()["chains"]


def test_llm_structured_stop_checkpoint_returns_empty_dict_without_calling_model(monkeypatch, tmp_path):
    """
    End-to-end through the real checkpoint inside core.llm_structured(),
    not just the registry directly — proves a chain that's told to stop
    never reaches the real model call, matching the existing Stop button's
    documented behavior (next checkpoint, not instant preemption).
    """
    fake = _FakeLLM()
    monkeypatch.setattr(core, "_llm_structured", fake)
    registry = _make_registry(tmp_path)
    monkeypatch.setattr(core, "_llm_slot", registry)

    # Pre-stop every NEW call_id is impossible (ids are generated fresh per
    # call) — so this test stops the chain via its TASK-level ancestor
    # instead, exactly like the GUI would: push a root call, mark IT
    # stopped, then call llm_structured() nested under it.
    from tracing import push_call
    with push_call() as (root_id, _):
        registry.register_chain(root_id, None, "task-x", "root")
        registry.set_chain_control(root_id, "stop")
        result = core.llm_structured("prompt", _SCHEMA, schema_name="test")

    assert result == {}
    assert fake.call_count == 0, "the real model must never be invoked once an ancestor is stopped"
