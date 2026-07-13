# =============================================================================
# log_feed.py — filterable/searchable local_logs.db view
# =============================================================================
# Same data the website's dashboard shows, rendered locally and live (file
# watcher on local_logs.db, not a poll). Test Runner output lands in this
# exact same table — no separate "test mode" log surface.
# =============================================================================

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QPushButton, QTableWidget,
    QTableWidgetItem, QLabel, QMessageBox,
)
from PySide6.QtCore import QTimer

import memory_reader as mr

# local_logs.db gets a write on every single client.log() call across every
# workflow — frequent during a busy task. Debounced the same way as the
# Chain Explorer / Live Prompt Queue, for the same reason: rebuilding this
# table on every individual write is wasted work under load.
REFRESH_INTERVAL_MS = 300


class LogFeedPanel(QWidget):
    def __init__(self, watcher, parent=None):
        super().__init__(parent)
        self._watcher = watcher
        self._dirty = False

        layout = QVBoxLayout(self)
        filters = QHBoxLayout()
        self.task_filter = QLineEdit()
        self.task_filter.setPlaceholderText("task_id filter")
        self.type_filter = QLineEdit()
        self.type_filter.setPlaceholderText("log_type filter (e.g. error)")
        self.keyword_filter = QLineEdit()
        self.keyword_filter.setPlaceholderText("keyword search")
        for w in (self.task_filter, self.type_filter, self.keyword_filter):
            w.textChanged.connect(self._refresh)
            filters.addWidget(w)
        purge_btn = QPushButton("Purge this task's logs")
        purge_btn.clicked.connect(self._purge)
        filters.addWidget(purge_btn)
        layout.addLayout(filters)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["timestamp", "task_id", "log_type", "message"])
        layout.addWidget(self.table, stretch=1)

        self._watcher.local_logs_changed.connect(self._mark_dirty)
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
        rows = mr.list_local_logs(
            task_id=self.task_filter.text().strip() or None,
            log_type=self.type_filter.text().strip() or None,
            keyword=self.keyword_filter.text().strip() or None,
        )
        self.table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            values = [row["timestamp"], row["task_id"], row["log_type"], row["message"]]
            for col, val in enumerate(values):
                self.table.setItem(i, col, QTableWidgetItem(str(val)))

    def _purge(self) -> None:
        task_id = self.task_filter.text().strip()
        if not task_id:
            QMessageBox.warning(self, "No task_id", "Type a task_id in the filter box first.")
            return
        reply = QMessageBox.question(self, "Confirm purge", f"Delete all local log lines for task_id '{task_id}'?")
        if reply != QMessageBox.Yes:
            return
        n = mr.delete_local_logs(task_id)
        QMessageBox.information(self, "Purged", f"Deleted {n} row(s).")
        self._refresh()
