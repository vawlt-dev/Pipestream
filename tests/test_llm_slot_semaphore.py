# =============================================================================
# test_llm_slot_semaphore.py — the real concurrency cap
# =============================================================================
# Fast, no real LLM — patches core._llm_structured with a fake that records
# how many invoke() calls are in flight simultaneously, so this asserts the
# actual guarantee (never more than MAX_PARALLEL_LLM_CALLS concurrent calls)
# rather than just trusting the implementation.
# =============================================================================

import json
import threading
import time

import core


class _ConcurrencyTrackingFakeLLM:
    """Stands in for core._llm_structured — sleeps briefly per call (to
    create real overlap opportunity) and records peak concurrency."""

    def __init__(self, sleep_s: float = 0.1):
        self.sleep_s = sleep_s
        self._lock = threading.Lock()
        self.in_flight = 0
        self.max_in_flight = 0

    def invoke(self, prompt, response_format=None):
        with self._lock:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        time.sleep(self.sleep_s)
        with self._lock:
            self.in_flight -= 1

        class _Resp:
            content = json.dumps({"ok": True})
        return _Resp()


_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}


def test_semaphore_caps_real_concurrency(monkeypatch):
    fake = _ConcurrencyTrackingFakeLLM(sleep_s=0.1)
    monkeypatch.setattr(core, "_llm_structured", fake)
    monkeypatch.setattr(core, "_llm_slot", threading.Semaphore(4))

    tasks = [
        (lambda: core.llm_structured("test prompt", _SCHEMA, schema_name="test"))
        for _ in range(12)
    ]
    results = core.run_concurrent(tasks, max_workers=12)

    assert len(results) == 12
    assert all(r == {"ok": True} for r in results)
    assert fake.max_in_flight <= 4, f"expected at most 4 concurrent LLM calls, saw {fake.max_in_flight}"
    assert fake.max_in_flight > 1, "test is too weak to prove anything — no real concurrency was observed"


def test_semaphore_of_one_fully_serializes(monkeypatch):
    # monkeypatch (not manual try/finally) so BOTH patches are guaranteed to
    # revert after this test — a prior version of this test patched
    # core._llm_structured directly without restoring it, which leaked the
    # fake into every later test in the same pytest session.
    fake = _ConcurrencyTrackingFakeLLM(sleep_s=0.05)
    monkeypatch.setattr(core, "_llm_structured", fake)
    monkeypatch.setattr(core, "_llm_slot", threading.Semaphore(1))

    tasks = [
        (lambda: core.llm_structured("test prompt", _SCHEMA, schema_name="test"))
        for _ in range(5)
    ]
    core.run_concurrent(tasks, max_workers=5)
    assert fake.max_in_flight == 1
