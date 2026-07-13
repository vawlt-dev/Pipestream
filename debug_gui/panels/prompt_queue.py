# =============================================================================
# prompt_queue.py — live running/queued/slots + per-chain registry
# =============================================================================
# Watches live_state.json for changes (file_watcher, not a timer) — see the
# project plan's "live means live" section for why a poll-based version
# would have fallen short here.
# =============================================================================

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTableWidget, QTableWidgetItem,
)
from PySide6.QtCore import QTimer

import chain_control

# live_state.json changes on every acquire/release/register/unregister of
# every single llm_structured() call — under a busy multi-chain task that's
# a high write rate. Rebuilding this table on every one of those signals
# (confirmed contributing to "not responding" under real load) is wasted
# work; debounce to at most one refresh per REFRESH_INTERVAL_MS regardless
# of how many change signals arrive in between.
REFRESH_INTERVAL_MS = 300


class PromptQueuePanel(QWidget):
    def __init__(self, watcher, parent=None):
        super().__init__(parent)
        self._watcher = watcher
        self._dirty = False

        layout = QVBoxLayout(self)

        summary = QHBoxLayout()
        self.running_label = QLabel("running: -")
        self.queued_label = QLabel("queued: -")
        self.slots_label = QLabel("max_slots: -")
        self.completed_label = QLabel("completed_this_session: -")
        for lbl in (self.running_label, self.queued_label, self.slots_label, self.completed_label):
            summary.addWidget(lbl)
        layout.addLayout(summary)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["call_id", "task_id", "schema_name", "status", "control", "priority"])
        layout.addWidget(self.table, stretch=1)

        self._watcher.live_state_changed.connect(self._mark_dirty)
        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_INTERVAL_MS)
        self._timer.timeout.connect(self._maybe_refresh)
        self._timer.start()
        self._refresh()

    def _mark_dirty(self) -> None:
        self._dirty = True

    def _maybe_refresh(self) -> None:
        if self._dirty:
            self._dirty = False
            self._refresh()

    def _refresh(self) -> None:
        try:
            state = chain_control.get_state()
        except Exception:
            return

        self.running_label.setText(f"running: {state.get('running', 0)}")
        self.queued_label.setText(f"queued: {state.get('queued', 0)}")
        self.slots_label.setText(f"max_slots: {state.get('max_slots', '-')}")
        self.completed_label.setText(f"completed_this_session: {state.get('completed_this_session', 0)}")

        chains = state.get("chains", {})
        self.table.setRowCount(len(chains))
        for row, (call_id, chain) in enumerate(sorted(chains.items(), key=lambda kv: kv[1].get("started_at", ""))):
            values = [
                call_id[:8], chain.get("task_id") or "", chain.get("schema_name", ""),
                chain.get("status", ""), chain.get("control", "run"), str(chain.get("priority", 0)),
            ]
            for col, val in enumerate(values):
                self.table.setItem(row, col, QTableWidgetItem(val))
