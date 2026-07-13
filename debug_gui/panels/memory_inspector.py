# =============================================================================
# memory_inspector.py — personas, prospects/outreach, fact viewer, raw SQL
# =============================================================================
# Display lists use read-only connections. Edits/deletes route through
# memory.py's own functions where possible (memory_set_question,
# memory_forget_topic, memory_update_outreach_status) so they stay
# consistent with the app's normalization/TTL logic. The raw SQL box gets a
# real read-write connection — confirm-gated for anything that isn't SELECT.
# =============================================================================

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTabWidget, QTableWidget, QTableWidgetItem,
    QPushButton, QLabel, QLineEdit, QTextEdit, QMessageBox, QComboBox, QSplitter,
)
from PySide6.QtCore import Qt

import memory_reader as mr


class MemoryInspectorPanel(QWidget):
    def __init__(self, watcher, parent=None):
        super().__init__(parent)
        self._watcher = watcher
        self._selected_persona: str | None = None
        self._selected_fact: dict | None = None

        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        tabs.addTab(self._build_personas_tab(), "Personas")
        tabs.addTab(self._build_outreach_tab(), "Outreach history")
        tabs.addTab(self._build_sql_tab(), "Raw SQL")
        layout.addWidget(tabs)

        self._watcher.memory_db_changed.connect(self._refresh_personas)
        self._refresh_personas()
        self._refresh_outreach()

    # -- Personas tab --------------------------------------------------

    def _build_personas_tab(self) -> QWidget:
        w = QWidget()
        layout = QHBoxLayout(w)

        left = QVBoxLayout()
        self.persona_table = QTableWidget(0, 3)
        self.persona_table.setHorizontalHeaderLabels(["topic", "fact_count", "last_updated"])
        self.persona_table.itemSelectionChanged.connect(self._on_persona_selected)
        left.addWidget(self.persona_table)
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self._refresh_personas)
        left.addWidget(refresh_btn)
        delete_persona_btn = QPushButton("Delete selected persona (all facts)")
        delete_persona_btn.clicked.connect(self._delete_persona)
        left.addWidget(delete_persona_btn)
        layout.addLayout(left, stretch=1)

        right = QVBoxLayout()
        self.fact_table = QTableWidget(0, 6)
        self.fact_table.setHorizontalHeaderLabels(["question_id", "answer", "confidence", "volatility", "source_urls", "updated_at"])
        self.fact_table.itemSelectionChanged.connect(self._on_fact_selected)
        right.addWidget(self.fact_table)

        right.addWidget(QLabel("Edit selected fact's answer (so hallucinations like a fabricated detail can be fixed in place):"))
        self.edit_answer_box = QTextEdit()
        self.edit_answer_box.setMaximumHeight(70)
        right.addWidget(self.edit_answer_box)
        save_fact_btn = QPushButton("Save edit")
        save_fact_btn.clicked.connect(self._save_fact_edit)
        right.addWidget(save_fact_btn)
        layout.addLayout(right, stretch=1)

        return w

    def _refresh_personas(self) -> None:
        rows = mr.list_personas()
        self.persona_table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            self.persona_table.setItem(i, 0, QTableWidgetItem(row["topic"]))
            self.persona_table.setItem(i, 1, QTableWidgetItem(str(row["fact_count"])))
            self.persona_table.setItem(i, 2, QTableWidgetItem(row["last_updated"] or ""))

    def _on_persona_selected(self) -> None:
        items = self.persona_table.selectedItems()
        if not items:
            return
        row = items[0].row()
        self._selected_persona = self.persona_table.item(row, 0).text()
        facts = mr.get_topic_questions(self._selected_persona, "persona")
        self.fact_table.setRowCount(len(facts))
        for i, f in enumerate(facts):
            self.fact_table.setItem(i, 0, QTableWidgetItem(f["question_id"]))
            self.fact_table.setItem(i, 1, QTableWidgetItem(f["answer"] or ""))
            self.fact_table.setItem(i, 2, QTableWidgetItem(f["confidence"] or ""))
            self.fact_table.setItem(i, 3, QTableWidgetItem(f["volatility"] or ""))
            self.fact_table.setItem(i, 4, QTableWidgetItem(str(f["source_urls"])))
            self.fact_table.setItem(i, 5, QTableWidgetItem(f["updated_at"] or ""))
            self.fact_table.item(i, 0).setData(Qt.UserRole, f)

    def _on_fact_selected(self) -> None:
        items = self.fact_table.selectedItems()
        if not items:
            return
        row = items[0].row()
        self._selected_fact = self.fact_table.item(row, 0).data(Qt.UserRole)
        self.edit_answer_box.setPlainText(self._selected_fact.get("answer") or "")

    def _save_fact_edit(self) -> None:
        if not self._selected_fact or not self._selected_persona:
            QMessageBox.warning(self, "Nothing selected", "Select a fact row first.")
            return
        f = self._selected_fact
        reply = QMessageBox.question(
            self, "Confirm edit",
            f"Overwrite the cached answer for {self._selected_persona} / {f['question_id']}?",
        )
        if reply != QMessageBox.Yes:
            return
        mr.edit_fact(
            self._selected_persona, "persona", f["question_id"], f["question_text"],
            self.edit_answer_box.toPlainText(), f.get("confidence") or "medium", f.get("volatility") or "NORMAL",
        )
        self._on_persona_selected()

    def _delete_persona(self) -> None:
        if not self._selected_persona:
            QMessageBox.warning(self, "Nothing selected", "Select a persona row first.")
            return
        reply = QMessageBox.question(
            self, "Confirm delete",
            f"Delete ALL cached facts for persona '{self._selected_persona}'? This can't be undone.",
        )
        if reply != QMessageBox.Yes:
            return
        n = mr.delete_topic(self._selected_persona, "persona")
        QMessageBox.information(self, "Deleted", f"Deleted {n} row(s).")
        self._refresh_personas()
        self.fact_table.setRowCount(0)

    # -- Outreach tab ----------------------------------------------------

    def _build_outreach_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        top = QHBoxLayout()
        top.addWidget(QLabel("Sender:"))
        self.sender_combo = QComboBox()
        self.sender_combo.currentTextChanged.connect(self._refresh_outreach)
        top.addWidget(self.sender_combo, stretch=1)
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self._refresh_outreach)
        top.addWidget(refresh_btn)
        layout.addLayout(top)

        self.outreach_table = QTableWidget(0, 6)
        self.outreach_table.setHorizontalHeaderLabels(["sender", "prospect", "target_email", "status", "subject_line", "updated_at"])
        self.outreach_table.itemSelectionChanged.connect(self._on_outreach_selected)
        layout.addWidget(self.outreach_table)

        actions = QHBoxLayout()
        actions.addWidget(QLabel("Set status:"))
        self.status_edit = QLineEdit()
        self.status_edit.setPlaceholderText("e.g. replied, booked")
        actions.addWidget(self.status_edit)
        apply_status_btn = QPushButton("Apply")
        apply_status_btn.clicked.connect(self._apply_outreach_status)
        actions.addWidget(apply_status_btn)
        delete_btn = QPushButton("Delete row")
        delete_btn.clicked.connect(self._delete_outreach_row)
        actions.addWidget(delete_btn)
        layout.addLayout(actions)

        return w

    def _refresh_outreach(self) -> None:
        senders = mr.list_senders()
        current = self.sender_combo.currentText()
        self.sender_combo.blockSignals(True)
        self.sender_combo.clear()
        self.sender_combo.addItem("")
        self.sender_combo.addItems(senders)
        if current in senders:
            self.sender_combo.setCurrentText(current)
        self.sender_combo.blockSignals(False)

        rows = mr.list_outreach(self.sender_combo.currentText() or None)
        self.outreach_table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            values = [row["sender"], row["prospect"], row["target_email"], row["status"], row["subject_line"], row["updated_at"]]
            for col, val in enumerate(values):
                self.outreach_table.setItem(i, col, QTableWidgetItem(val or ""))
            self.outreach_table.item(i, 0).setData(Qt.UserRole, row)

    def _on_outreach_selected(self) -> None:
        items = self.outreach_table.selectedItems()
        if items:
            self.status_edit.setText(items[0].data(Qt.UserRole)["status"] if items[0].column() == 0 else self.status_edit.text())

    def _selected_outreach_row(self) -> dict | None:
        items = self.outreach_table.selectedItems()
        if not items:
            return None
        return self.outreach_table.item(items[0].row(), 0).data(Qt.UserRole)

    def _apply_outreach_status(self) -> None:
        row = self._selected_outreach_row()
        if not row:
            QMessageBox.warning(self, "Nothing selected", "Select an outreach row first.")
            return
        status = self.status_edit.text().strip()
        if not status:
            return
        reply = QMessageBox.question(self, "Confirm", f"Set status of {row['sender']} / {row['prospect']} to '{status}'?")
        if reply != QMessageBox.Yes:
            return
        mr.set_outreach_status(row["sender"], row["prospect"], status)
        self._refresh_outreach()

    def _delete_outreach_row(self) -> None:
        row = self._selected_outreach_row()
        if not row:
            QMessageBox.warning(self, "Nothing selected", "Select an outreach row first.")
            return
        reply = QMessageBox.question(self, "Confirm delete", f"Delete outreach row for {row['sender']} / {row['prospect']}?")
        if reply != QMessageBox.Yes:
            return
        mr.delete_outreach_row(row["sender_key"], row["prospect_key"])
        self._refresh_outreach()

    # -- Raw SQL tab -------------------------------------------------------

    def _build_sql_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.addWidget(QLabel("Runs against memory.db with a real read-write connection. Non-SELECT statements ask for confirmation first."))
        self.sql_box = QTextEdit()
        self.sql_box.setPlaceholderText("SELECT * FROM memory WHERE topic_level = 'persona' LIMIT 50;")
        layout.addWidget(self.sql_box)
        run_btn = QPushButton("Run")
        run_btn.clicked.connect(self._run_sql)
        layout.addWidget(run_btn)
        self.sql_result = QTableWidget(0, 0)
        layout.addWidget(self.sql_result, stretch=1)
        return w

    def _run_sql(self) -> None:
        sql = self.sql_box.toPlainText().strip()
        if not sql:
            return
        is_select = sql.lower().lstrip().startswith("select")
        if not is_select:
            reply = QMessageBox.question(
                self, "Confirm write",
                f"This statement is not a SELECT — it will modify memory.db:\n\n{sql}\n\nProceed?",
            )
            if reply != QMessageBox.Yes:
                return
        try:
            columns, rows = mr.run_raw_sql(sql)
        except Exception as e:
            QMessageBox.critical(self, "SQL error", str(e))
            return
        self.sql_result.setColumnCount(len(columns))
        self.sql_result.setHorizontalHeaderLabels(columns)
        self.sql_result.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, val in enumerate(row):
                self.sql_result.setItem(r, c, QTableWidgetItem(str(val)))
        if not is_select:
            self._refresh_personas()
            self._refresh_outreach()
