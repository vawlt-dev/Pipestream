# =============================================================================
# persona_growth.py — persona vocabulary growth over time
# =============================================================================
# first_seen approximated by MIN(updated_at) — a row's updated_at doesn't
# change again until its TTL expires, so this is a reasonable proxy for
# "when this category was first learned". No schema change needed.
# =============================================================================

from PySide6.QtWidgets import QWidget, QVBoxLayout, QPushButton, QTableWidget, QTableWidgetItem

import memory_reader as mr


class PersonaGrowthPanel(QWidget):
    def __init__(self, watcher, parent=None):
        super().__init__(parent)
        self._watcher = watcher
        layout = QVBoxLayout(self)
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self._refresh)
        layout.addWidget(refresh_btn)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["topic", "first_seen", "fact_count"])
        layout.addWidget(self.table, stretch=1)
        self._watcher.memory_db_changed.connect(self._refresh)
        self._refresh()

    def _refresh(self) -> None:
        rows = sorted(mr.persona_growth(), key=lambda r: r["first_seen"] or "")
        self.table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            values = [row["topic"], row["first_seen"] or "", str(row["fact_count"])]
            for col, val in enumerate(values):
                self.table.setItem(i, col, QTableWidgetItem(val))
