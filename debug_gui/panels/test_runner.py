# =============================================================================
# test_runner.py — invoke any function/workflow, dynamic form, Stop button
# =============================================================================
# Reuses invoke.py (the promoted dynamic_invoke.py) to resolve a dotted
# function path or workflow name, inspect.signature() to build the input
# form automatically, and the existing check_cancelled() cooperative
# checkpoint (via GuiClient) for Stop — no new cancellation plumbing needed
# in agentt itself. Pointing this at something not-yet-implemented renders
# a clean "not found" message rather than crashing.
# =============================================================================

import json
import inspect

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QPushButton, QLabel,
    QFormLayout, QTextEdit, QRadioButton, QButtonGroup, QMessageBox,
)

import test_executor as te


class TestRunnerPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._worker: te.TestRunWorker | None = None
        self._client: te.GuiClient | None = None
        self._param_inputs: dict[str, QLineEdit] = {}

        layout = QVBoxLayout(self)

        mode_row = QHBoxLayout()
        self.function_radio = QRadioButton("Function (dotted path)")
        self.workflow_radio = QRadioButton("Workflow (by name)")
        self.function_radio.setChecked(True)
        group = QButtonGroup(self)
        group.addButton(self.function_radio)
        group.addButton(self.workflow_radio)
        mode_row.addWidget(self.function_radio)
        mode_row.addWidget(self.workflow_radio)
        layout.addLayout(mode_row)

        target_row = QHBoxLayout()
        target_row.addWidget(QLabel("Target:"))
        self.target_edit = QLineEdit()
        self.target_edit.setPlaceholderText("research.resolve_persona   or   lead_gen_outreach")
        target_row.addWidget(self.target_edit, stretch=1)
        resolve_btn = QPushButton("Resolve")
        resolve_btn.clicked.connect(self._resolve)
        target_row.addWidget(resolve_btn)
        layout.addLayout(target_row)

        self.warning_label = QLabel("")
        self.warning_label.setStyleSheet("color: #b36b00;")
        layout.addWidget(self.warning_label)

        self.form_container = QWidget()
        self.form_layout = QFormLayout(self.form_container)
        layout.addWidget(self.form_container)

        run_row = QHBoxLayout()
        self.run_btn = QPushButton("Run")
        self.run_btn.clicked.connect(self._run)
        self.run_btn.setEnabled(False)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(self._stop)
        self.stop_btn.setEnabled(False)
        run_row.addWidget(self.run_btn)
        run_row.addWidget(self.stop_btn)
        layout.addLayout(run_row)

        layout.addWidget(QLabel("Result:"))
        self.result_box = QTextEdit()
        self.result_box.setReadOnly(True)
        layout.addWidget(self.result_box, stretch=1)

        self._signature: inspect.Signature | None = None

    # -- resolve target, build dynamic form --------------------------------

    def _resolve(self) -> None:
        target = self.target_edit.text().strip()
        self._clear_form()
        self.warning_label.setText("")
        self.run_btn.setEnabled(False)
        if not target:
            return

        is_workflow = self.workflow_radio.isChecked()
        try:
            self._signature = te.resolve_signature(target, is_workflow)
        except te.TargetNotFound as e:
            self.result_box.setPlainText(f"Not found:\n{e}")
            return

        if is_workflow and target in te.SIDE_EFFECTING_WORKFLOWS:
            self.warning_label.setText(
                f"⚠ '{target}' has real Gmail/Calendar side effects — running it will actually send/create things."
            )

        if is_workflow:
            # call_workflow()'s own fixed signature (task_id, input_text,
            # client) — only input_text is meaningfully user-editable here,
            # task_id/client are always auto-filled (see _run()).
            self.form_layout.addRow("input_text:", self._make_input_field("input_text"))
        else:
            for name, param in self._signature.parameters.items():
                if name in ("client", "log", "task_id"):
                    label = QLabel("(auto-filled)")
                    self.form_layout.addRow(f"{name}:", label)
                    continue
                self.form_layout.addRow(f"{name}:", self._make_input_field(name, param))

        self.run_btn.setEnabled(True)
        self.result_box.clear()

    def _make_input_field(self, name: str, param: inspect.Parameter | None = None) -> QLineEdit:
        field = QLineEdit()
        if param is not None and param.default is not inspect.Parameter.empty:
            field.setPlaceholderText(f"default: {param.default!r}")
        self._param_inputs[name] = field
        return field

    def _clear_form(self) -> None:
        while self.form_layout.rowCount():
            self.form_layout.removeRow(0)
        self._param_inputs.clear()

    def _parse_field(self, name: str) -> object:
        text = self._param_inputs[name].text()
        if text == "":
            param = self._signature.parameters.get(name) if self._signature else None
            if param is not None and param.default is not inspect.Parameter.empty:
                return param.default
            return ""
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text  # treat as a raw string if it doesn't look like JSON

    # -- run / stop ----------------------------------------------------------

    def _run(self) -> None:
        target = self.target_edit.text().strip()
        is_workflow = self.workflow_radio.isChecked()
        task_id = te.new_test_task_id()
        self._client = te.GuiClient(task_id, trusted=True)

        if is_workflow:
            kwargs = {"task_id": task_id, "input_text": self._param_inputs["input_text"].text()}
        else:
            kwargs = {name: self._parse_field(name) for name in self._param_inputs}

        self.result_box.setPlainText(f"Running (task_id={task_id})...")
        self.run_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)

        self._worker = te.TestRunWorker(target, is_workflow, kwargs, self._client)
        self._worker.finished_ok.connect(self._on_finished_ok)
        self._worker.finished_error.connect(self._on_finished_error)
        self._worker.not_found.connect(self._on_not_found)
        self._worker.start()

    def _stop(self) -> None:
        if self._client:
            self._client.request_stop()
        self.stop_btn.setEnabled(False)

    def _on_finished_ok(self, result) -> None:
        self.result_box.setPlainText(json.dumps(result, indent=2, default=str))
        self.run_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)

    def _on_finished_error(self, message: str) -> None:
        self.result_box.setPlainText(f"Error:\n{message}")
        self.run_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)

    def _on_not_found(self, message: str) -> None:
        self.result_box.setPlainText(f"Not found:\n{message}")
        self.run_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
