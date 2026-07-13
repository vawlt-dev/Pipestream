# =============================================================================
# test_executor.py — Test Runner's execution engine
# =============================================================================
# Resolves a dotted function path or workflow name via the promoted
# invoke.py, introspects its signature to build the panel's input form,
# and runs it on a background QThread (these calls take seconds to minutes —
# never block the UI thread). GuiClient stands in for agent_worker.py's
# VPSClient — reuses the EXISTING check_cancelled(task_id, client) checkpoint
# pervasive throughout this codebase for the Stop button, and mirrors logs
# into the same local_logs.db table real tasks use, so Test Runner output
# renders identically in the Log Feed — no separate "test mode".
# =============================================================================

import sys
import inspect
import uuid

from paths import AGENTT_ROOT
from PySide6.QtCore import QThread, Signal

if AGENTT_ROOT not in sys.path:
    sys.path.insert(0, AGENTT_ROOT)

from local_logs import log_local  # noqa: E402

# Workflows known to have real Gmail/Calendar side effects — same list
# tests/test_workflow_registry.py's module docstring already documents.
# Surfaced as a one-line warning before running, not a hard block: the
# whole point of this panel is being able to fire off anything deliberately.
SIDE_EFFECTING_WORKFLOWS = {"lead_gen_outreach", "business_intro", "calendar_booking", "delete_calendar_events"}


class GuiClient:
    """
    Minimal stand-in for VPSClient, good enough for any function/workflow
    that takes `client` and calls .get_task()/.log()/.update_status() —
    exactly what FakeClient in tests/conftest.py already proves is enough
    surface for real workflow code. request_stop() is the Stop button's
    entire implementation: check_cancelled() (called between steps
    throughout this codebase, and now also inside core.llm_structured()'s
    own pause/stop checkpoint) reads get_task()'s status, so flipping this
    one flag is everything needed — no new cancellation plumbing in agentt
    itself.
    """

    def __init__(self, task_id: str, trusted: bool = True):
        self.task_id = task_id
        self.trusted = trusted
        self._stopped = False
        self._status = "running"
        self.pending_question: str | None = None
        self.user_input: str = ""

    def request_stop(self) -> None:
        self._stopped = True

    def get_task(self, task_id: str) -> dict:
        return {
            "status": "cancelled" if self._stopped else self._status,
            "trusted": self.trusted,
            "pending_question": self.pending_question,
            "user_input": self.user_input,
        }

    def log(self, task_id: str, message: str, log_type: str = "info") -> None:
        log_local(task_id, message, log_type)

    def update_status(self, task_id: str, status: str, **kwargs) -> None:
        self._status = status
        if "pending_question" in kwargs:
            self.pending_question = kwargs["pending_question"]
        if "user_input" in kwargs:
            self.user_input = kwargs["user_input"]


def new_test_task_id() -> str:
    return f"gui-test-{uuid.uuid4().hex[:8]}"


class TargetNotFound(Exception):
    pass


def resolve_signature(target: str, is_workflow: bool) -> inspect.Signature:
    """
    Raises TargetNotFound (not a crash) when target doesn't exist yet — a
    workflow file not written, a function not defined. This is a normal,
    expected state for this panel: pointing it at something not-yet-
    implemented should render cleanly, not blow up.
    """
    if is_workflow:
        from invoke import workflow_registry
        registry = workflow_registry()
        if target not in registry:
            raise TargetNotFound(f"No workflow named {target!r}. Available: {sorted(registry)}")
        return inspect.signature(registry[target]["run"])
    else:
        import importlib
        if "." not in target:
            raise TargetNotFound(f"{target!r} is not a dotted path (module.function)")
        module_path, func_name = target.rsplit(".", 1)
        try:
            module = importlib.import_module(module_path)
        except ModuleNotFoundError as e:
            raise TargetNotFound(f"No module {module_path!r}: {e}")
        func = getattr(module, func_name, None)
        if func is None or not callable(func):
            raise TargetNotFound(f"{module_path!r} has no callable {func_name!r}")
        return inspect.signature(func)


class TestRunWorker(QThread):
    """
    Runs one invocation on a background thread. finished/error/not_found are
    mutually exclusive terminal signals — exactly one fires per run.
    """

    finished_ok = Signal(object)
    finished_error = Signal(str)
    not_found = Signal(str)

    def __init__(self, target: str, is_workflow: bool, kwargs: dict, client: GuiClient, parent=None):
        super().__init__(parent)
        self.target = target
        self.is_workflow = is_workflow
        self.kwargs = kwargs
        self.client = client

    def run(self) -> None:
        try:
            if self.is_workflow:
                from invoke import call_workflow
                result = call_workflow(
                    self.target,
                    self.kwargs.get("task_id", self.client.task_id),
                    self.kwargs.get("input_text", ""),
                    self.client,
                )
            else:
                from invoke import call
                result = call(self.target, **self.kwargs)
            self.finished_ok.emit(result)
        except TargetNotFound as e:
            self.not_found.emit(str(e))
        except ValueError as e:
            # call_workflow() raises plain ValueError for "not found", not
            # TargetNotFound — same graceful-failure shape, different
            # exception type since invoke.py predates this panel.
            if "No workflow named" in str(e):
                self.not_found.emit(str(e))
            else:
                self.finished_error.emit(f"{type(e).__name__}: {e}")
        except Exception as e:
            self.finished_error.emit(f"{type(e).__name__}: {e}")
