# =============================================================================
# guardrail_log.py — generalizability_gate + draft_appropriateness events
# =============================================================================
# Same fix as disambiguation_log.py: incremental per-task tailing instead of
# re-reading every trace file from scratch on every signal, plus a debounced
# table rebuild — see that file's header comment for the full story (this
# was a confirmed major contributor to high CPU/UI unresponsiveness under a
# real busy task, since it touched every byte of every trace file, every
# single time any one of them changed).
# =============================================================================

from PySide6.QtWidgets import QWidget, QVBoxLayout, QPushButton, QTableWidget, QTableWidgetItem
from PySide6.QtCore import QTimer

import trace_reader

_GUARDRAIL_EVENTS = {"generalizability_gate", "draft_appropriateness"}
REBUILD_INTERVAL_MS = 300


class GuardrailLogPanel(QWidget):
    def __init__(self, watcher, parent=None):
        super().__init__(parent)
        self._watcher = watcher
        self._tailers: dict[str, trace_reader.IncrementalTailer] = {}
        self._matched_events: list[dict] = []
        self._dirty = False

        layout = QVBoxLayout(self)
        refresh_btn = QPushButton("Rescan all task trace files from scratch")
        refresh_btn.clicked.connect(self._full_rescan)
        layout.addWidget(refresh_btn)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["task", "event", "verdict", "concern", "company_name"])
        layout.addWidget(self.table, stretch=1)

        self._watcher.trace_changed.connect(self._on_trace_changed)
        self._timer = QTimer(self)
        self._timer.setInterval(REBUILD_INTERVAL_MS)
        self._timer.timeout.connect(self._maybe_rebuild)
        self._timer.start()
        self._full_rescan()

    def _ensure_tailer(self, task_id: str) -> trace_reader.IncrementalTailer:
        tailer = self._tailers.get(task_id)
        if tailer is None:
            tailer = trace_reader.IncrementalTailer(task_id)
            self._tailers[task_id] = tailer
        return tailer

    def _full_rescan(self) -> None:
        self._matched_events = []
        self._tailers = {}
        for task_id in trace_reader.list_task_ids():
            tailer = self._ensure_tailer(task_id)
            for ev in trace_reader.read_all_events(task_id):
                if ev.get("event") in _GUARDRAIL_EVENTS:
                    self._matched_events.append(ev)
            tailer.read_new_events()
        self._rebuild_table()

    def _on_trace_changed(self, path: str) -> None:
        for task_id, tailer in self._tailers.items():
            if path.endswith(f"{task_id}.jsonl"):
                for ev in tailer.read_new_events():
                    if ev.get("event") in _GUARDRAIL_EVENTS:
                        self._matched_events.append(ev)
                        self._dirty = True
                return
        if path.endswith(".jsonl"):
            self._dirty = True

    def _maybe_rebuild(self) -> None:
        known_ids = set(self._tailers.keys())
        current_ids = set(trace_reader.list_task_ids())
        if current_ids - known_ids:
            for task_id in current_ids - known_ids:
                tailer = self._ensure_tailer(task_id)
                for ev in trace_reader.read_all_events(task_id):
                    if ev.get("event") in _GUARDRAIL_EVENTS:
                        self._matched_events.append(ev)
                tailer.read_new_events()
            self._dirty = True
        if self._dirty:
            self._dirty = False
            self._rebuild_table()

    def _rebuild_table(self) -> None:
        self.table.setRowCount(len(self._matched_events))
        for i, ev in enumerate(self._matched_events):
            if ev["event"] == "draft_appropriateness":
                verdict = str(ev.get("appropriate"))
                concern = ev.get("concern", "")
            else:
                verdict = str(ev.get("passed"))
                concern = ""
            values = [ev.get("task", ""), ev["event"], verdict, concern, ev.get("company_name", "")]
            for col, val in enumerate(values):
                self.table.setItem(i, col, QTableWidgetItem(str(val)))
