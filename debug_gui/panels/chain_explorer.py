# =============================================================================
# chain_explorer.py — Task & Chain Explorer + manual chain control
# =============================================================================
# Builds a real ancestry tree from call_id/parent_call_id (not a guess from
# timestamps), grows live while a task is still running, and is where the
# pause/stop/resume/priority/override controls live — you select a call_id
# here, then act on it. Honest limitation surfaced in the UI itself: control
# takes effect at that chain's next LLM call checkpoint, not instantly (a
# call already mid-network-request can't be preempted).
# =============================================================================

import json

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QComboBox, QTreeWidget, QTreeWidgetItem,
    QTextEdit, QSplitter, QPushButton, QLabel, QSpinBox, QMessageBox, QLineEdit,
)
from PySide6.QtCore import Qt, QTimer

import trace_reader
import chain_control

# A busy task can append trace lines fast enough that rebuilding the whole
# QTreeWidget on every single watchdog signal pegs the CPU and makes the GUI
# unresponsive (confirmed under real load) — each rebuild is a from-scratch
# walk of the full ancestry tree, and a single ChainRegistry-backed
# llm_structured() call alone produces several underlying file writes, each
# capable of firing its own signal. A periodic dirty-flag timer decouples
# "how often the underlying files change" from "how often the UI actually
# redraws" — at most one rebuild per REBUILD_INTERVAL_MS, however many
# change signals arrived in between.
REBUILD_INTERVAL_MS = 300


class ChainExplorerPanel(QWidget):
    def __init__(self, watcher, parent=None):
        super().__init__(parent)
        self._watcher = watcher
        self._tailer: trace_reader.IncrementalTailer | None = None
        self._events: list[dict] = []
        self._selected_call_id: str | None = None
        self._dirty = False

        self._rebuild_timer = QTimer(self)
        self._rebuild_timer.setInterval(REBUILD_INTERVAL_MS)
        self._rebuild_timer.timeout.connect(self._maybe_rebuild)
        self._rebuild_timer.start()

        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        top.addWidget(QLabel("Task:"))
        self.task_combo = QComboBox()
        self.task_combo.currentTextChanged.connect(self._on_task_changed)
        top.addWidget(self.task_combo, stretch=1)
        refresh_btn = QPushButton("Refresh task list")
        refresh_btn.clicked.connect(self._reload_task_list)
        top.addWidget(refresh_btn)
        layout.addLayout(top)

        splitter = QSplitter(Qt.Horizontal)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["call", "event", "schema", "elapsed_s", "wait_s", "status"])
        self.tree.itemSelectionChanged.connect(self._on_selection_changed)
        splitter.addWidget(self.tree)

        right = QWidget()
        right_layout = QVBoxLayout(right)

        self.detail = QTextEdit()
        self.detail.setReadOnly(True)
        right_layout.addWidget(self.detail, stretch=1)

        control_label = QLabel(
            "Chain control — applies to the selected call_id and everything nested under it. "
            "Takes effect at its next LLM call, not instantly."
        )
        control_label.setWordWrap(True)
        right_layout.addWidget(control_label)

        controls = QHBoxLayout()
        self.pause_btn = QPushButton("Pause")
        self.pause_btn.clicked.connect(lambda: self._set_control("pause"))
        self.resume_btn = QPushButton("Resume")
        self.resume_btn.clicked.connect(lambda: self._set_control("run"))
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(lambda: self._set_control("stop"))
        controls.addWidget(self.pause_btn)
        controls.addWidget(self.resume_btn)
        controls.addWidget(self.stop_btn)
        right_layout.addLayout(controls)

        priority_row = QHBoxLayout()
        priority_row.addWidget(QLabel("Priority (0 = default; +uplift / -suppress):"))
        self.priority_spin = QSpinBox()
        self.priority_spin.setRange(-100, 100)
        apply_priority_btn = QPushButton("Apply priority")
        apply_priority_btn.clicked.connect(self._apply_priority)
        priority_row.addWidget(self.priority_spin)
        priority_row.addWidget(apply_priority_btn)
        right_layout.addLayout(priority_row)

        right_layout.addWidget(QLabel("Edited prompt (only takes effect while paused, before resuming):"))
        self.edit_prompt_box = QTextEdit()
        self.edit_prompt_box.setMaximumHeight(80)
        apply_edit_btn = QPushButton("Stage edited prompt")
        apply_edit_btn.clicked.connect(self._apply_edited_prompt)
        right_layout.addWidget(self.edit_prompt_box)
        right_layout.addWidget(apply_edit_btn)

        right_layout.addWidget(QLabel('Manual decision override (JSON dict — returned instead of calling the model, once):'))
        self.override_box = QLineEdit()
        self.override_box.setPlaceholderText('e.g. {"appropriate": true, "concern": ""}')
        apply_override_btn = QPushButton("Stage override")
        apply_override_btn.clicked.connect(self._apply_override)
        right_layout.addWidget(self.override_box)
        right_layout.addWidget(apply_override_btn)

        splitter.addWidget(right)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, stretch=1)

        self._watcher.trace_changed.connect(self._on_trace_changed)
        self._reload_task_list()

    # -- task list / tree building --------------------------------------

    def _reload_task_list(self) -> None:
        current = self.task_combo.currentText()
        self.task_combo.blockSignals(True)
        self.task_combo.clear()
        ids = trace_reader.list_task_ids()
        self.task_combo.addItems(ids)
        if current in ids:
            self.task_combo.setCurrentText(current)
        self.task_combo.blockSignals(False)
        if ids and not current:
            self._on_task_changed(self.task_combo.currentText())

    def _on_task_changed(self, task_id: str) -> None:
        if not task_id:
            return
        self._tailer = trace_reader.IncrementalTailer(task_id)
        self._events = trace_reader.read_all_events(task_id)
        self._tailer.read_new_events()  # advance offset past what we just bulk-read
        self._rebuild_tree()

    def _on_trace_changed(self, path: str) -> None:
        # Cheap on every signal: read whatever's new (an incremental tail,
        # not a full re-parse) and just mark dirty. The expensive part —
        # rebuilding the QTreeWidget — happens at most once per
        # REBUILD_INTERVAL_MS, in _maybe_rebuild(), not here.
        if self._tailer and path.endswith(f"{self._tailer.task_id}.jsonl"):
            new_events = self._tailer.read_new_events()
            if new_events:
                self._events.extend(new_events)
                self._dirty = True
        elif path.endswith(".jsonl"):
            # A file changed that isn't the currently-selected task — most
            # likely a brand new task that just started. Cheap existence
            # check only; the actual combo-box repopulation also happens in
            # _maybe_rebuild() so it's debounced the same way.
            self._dirty = True

    def _maybe_rebuild(self) -> None:
        self._refresh_task_list_if_changed()
        if self._dirty:
            self._dirty = False
            self._rebuild_tree()

    def _refresh_task_list_if_changed(self) -> None:
        current_ids = set(trace_reader.list_task_ids())
        known_ids = {self.task_combo.itemText(i) for i in range(self.task_combo.count())}
        if current_ids != known_ids:
            self._reload_task_list()

    def _rebuild_tree(self) -> None:
        self.tree.clear()
        ancestry = trace_reader.build_ancestry_tree(self._events)

        def add_node(node: dict, parent_item):
            events = node["events"]
            last = events[-1]
            label = node["call_id"][:8]
            item = QTreeWidgetItem([
                label, last.get("event", ""), last.get("schema_name", ""),
                str(last.get("elapsed_s", "")), str(last.get("wait_s", "")), "",
            ])
            item.setData(0, Qt.UserRole, node["call_id"])
            if parent_item is None:
                self.tree.addTopLevelItem(item)
            else:
                parent_item.addChild(item)
            for child in node["children"]:
                add_node(child, item)
            return item

        for root in ancestry["roots"]:
            add_node(root, None)
        self.tree.expandAll()

    # -- selection / detail -----------------------------------------------

    def _on_selection_changed(self) -> None:
        items = self.tree.selectedItems()
        if not items:
            self._selected_call_id = None
            self.detail.clear()
            return
        call_id = items[0].data(0, Qt.UserRole)
        self._selected_call_id = call_id
        matching = [e for e in self._events if e.get("call_id") == call_id]
        self.detail.setPlainText(json.dumps(matching, indent=2, default=str))

    # -- chain control actions ---------------------------------------------

    def _require_selection(self) -> str | None:
        if not self._selected_call_id:
            QMessageBox.warning(self, "No chain selected", "Select a node in the tree first.")
            return None
        return self._selected_call_id

    def _set_control(self, control: str) -> None:
        call_id = self._require_selection()
        if not call_id:
            return
        chain_control.set_chain_control(call_id, control)

    def _apply_priority(self) -> None:
        call_id = self._require_selection()
        if not call_id:
            return
        chain_control.set_chain_priority(call_id, self.priority_spin.value())

    def _apply_edited_prompt(self) -> None:
        call_id = self._require_selection()
        if not call_id:
            return
        text = self.edit_prompt_box.toPlainText().strip()
        if not text:
            return
        chain_control.stage_edited_prompt(call_id, text)

    def _apply_override(self) -> None:
        call_id = self._require_selection()
        if not call_id:
            return
        text = self.override_box.text().strip()
        if not text:
            return
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as e:
            QMessageBox.critical(self, "Invalid JSON", f"Couldn't parse override payload: {e}")
            return
        chain_control.stage_override(call_id, payload)
