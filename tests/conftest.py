# =============================================================================
# conftest.py — shared pytest fixtures
# =============================================================================

import os
import sys

import pytest

# Make the app root importable exactly like router.py does for workflow
# files, so tests can `import core`, `from research import ...`, etc. the
# same way the running container does.
_APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _APP_ROOT not in sys.path:
    sys.path.insert(0, _APP_ROOT)


@pytest.fixture
def temp_memory_db(monkeypatch, tmp_path):
    """
    Isolate a test from the real /workspace/memory.db by pointing
    memory.DB_PATH at a fresh temp file. Use this in any test that calls
    memory_set_question/memory_get_question/etc., or that exercises
    research.py functions that read/write memory (resolve_persona,
    answer_question, gather_pitch_logic) — never run tests against the real
    production DB.
    """
    import memory
    monkeypatch.setattr(memory, "DB_PATH", str(tmp_path / "test_memory.db"))
    return memory


class FakeClient:
    """
    Minimal stand-in for the real VPS task client (agent_worker.py's
    VPSClient) — enough surface for any workflow/research function that
    takes `client` and calls .get_task()/.log()/.update_status(). Never
    talks to a network, so tests can exercise real code paths that expect a
    client object without needing a live task system.
    """

    def __init__(self, trusted: bool = True):
        self.trusted = trusted
        self.logs: list[tuple] = []
        self.status_updates: list[tuple] = []

    def get_task(self, task_id):
        return {"status": "running", "trusted": self.trusted}

    def log(self, task_id, msg, log_type):
        self.logs.append((task_id, msg, log_type))

    def update_status(self, task_id, status, **kwargs):
        self.status_updates.append((task_id, status, kwargs))


@pytest.fixture
def fake_client():
    return FakeClient()


@pytest.fixture
def log_fn():
    """
    Same `log(msg, log_type="info")` shape every workflow/run() function
    defines locally as a closure — standalone here so tests can pass a real
    callable without needing a live task system.
    """
    def log(msg, log_type="info"):
        print(f"  [{log_type.upper()}] {msg}")
    return log
