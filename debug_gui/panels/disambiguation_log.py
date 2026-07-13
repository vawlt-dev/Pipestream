# =============================================================================
# disambiguation_log.py — disambiguation_decision trace events, all tasks
# =============================================================================
# Originally re-read EVERY task's trace file from scratch (including the
# 84MB Fonterra trace) on EVERY single watchdog signal — confirmed a major
# contributor to high CPU/UI unresponsiveness under a real busy task, worse
# than the other panels since it touched every byte of every file, every
# time. Fixed the same way as the Chain Explorer: one IncrementalTailer per
# task (only ever reads NEW bytes), and the actual table rebuild is
# debounced to at most once per REBUILD_INTERVAL_MS.
# =============================================================================

from PySide6.QtWidgets import QWidget, QVBoxLayout, QPushButton, QTableWidget, QTableWidgetItem
from PySide6.QtCore import QTimer

import trace_reader

REBUILD_INTERVAL_MS = 300


class DisambiguationLogPanel(QWidget):
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
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["task", "topic", "candidates", "trusted", "auto_picked", "answer"])
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
        """Explicit, user-requested full re-read — the one place this panel
        ever pays the cost of reading whole files again, not on every
        signal."""
        self._matched_events = []
        self._tailers = {}
        for task_id in trace_reader.list_task_ids():
            tailer = self._ensure_tailer(task_id)
            for ev in trace_reader.read_all_events(task_id):
                if ev.get("event") == "disambiguation_decision":
                    self._matched_events.append(ev)
            tailer.read_new_events()  # advance offset past what we just bulk-read
        self._rebuild_table()

    def _on_trace_changed(self, path: str) -> None:
        for task_id, tailer in self._tailers.items():
            if path.endswith(f"{task_id}.jsonl"):
                for ev in tailer.read_new_events():
                    if ev.get("event") == "disambiguation_decision":
                        self._matched_events.append(ev)
                        self._dirty = True
                return
        # A task we haven't seen before — pick it up on the next full
        # rescan trigger point (the periodic task-discovery check, same
        # pattern as the Chain Explorer) rather than re-scanning here.
        if path.endswith(".jsonl"):
            self._dirty = True

    def _maybe_rebuild(self) -> None:
        known_ids = set(self._tailers.keys())
        current_ids = set(trace_reader.list_task_ids())
        if current_ids - known_ids:
            for task_id in current_ids - known_ids:
                tailer = self._ensure_tailer(task_id)
                for ev in trace_reader.read_all_events(task_id):
                    if ev.get("event") == "disambiguation_decision":
                        self._matched_events.append(ev)
                tailer.read_new_events()
            self._dirty = True
        if self._dirty:
            self._dirty = False
            self._rebuild_table()

    def _rebuild_table(self) -> None:
        self.table.setRowCount(len(self._matched_events))
        for i, ev in enumerate(self._matched_events):
            values = [
                ev.get("task", ""), ev.get("topic", ""), str(len(ev.get("candidates") or [])),
                str(ev.get("trusted")), ev.get("auto_picked") or "", ev.get("answer") or "",
            ]
            for col, val in enumerate(values):
                self.table.setItem(i, col, QTableWidgetItem(val))
