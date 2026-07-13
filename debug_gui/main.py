# =============================================================================
# main.py — Pipestream Debug GUI entry point
# =============================================================================
# Run with: python debug_gui/main.py
# Needs PySide6 + watchdog, plus everything in ../requirements.txt (the Test
# Runner panel imports real agentt modules directly) — see
# debug_gui/requirements.txt for the full note.
# =============================================================================

import os
import sys

# Must happen before PySide6 is ever imported (anywhere in this process —
# including by accident, e.g. via a transitive import). PySide6's shiboken
# signature-introspection hook inspects every module imported AFTER it
# loads, and crashes on dateparser's lazy six.moves submodules
# ('_SixMetaPathImporter' object has no attribute '_path') — confirmed via a
# standalone repro. dateparser is a real dependency of three workflows
# (calendar_booking, delete_calendar_events, email_triage) that the Test
# Runner needs to be able to load; importing it first means PySide6's hook
# never sees it as "new" and never trips.
import dateparser  # noqa: F401,E402

_HERE = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.join(_HERE, "data")
_PANELS_DIR = os.path.join(_HERE, "panels")
for p in (_DATA_DIR, _PANELS_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

from PySide6.QtWidgets import QApplication, QMainWindow, QTabWidget

import paths  # noqa: E402 — sets WORK_DIR as a side effect, see paths.py
from file_watcher import WorkspaceWatcher  # noqa: E402

from memory_inspector import MemoryInspectorPanel  # noqa: E402
from chain_explorer import ChainExplorerPanel  # noqa: E402
from prompt_queue import PromptQueuePanel  # noqa: E402
from log_feed import LogFeedPanel  # noqa: E402
from disambiguation_log import DisambiguationLogPanel  # noqa: E402
from guardrail_log import GuardrailLogPanel  # noqa: E402
from persona_growth import PersonaGrowthPanel  # noqa: E402
from test_runner import TestRunnerPanel  # noqa: E402


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Pipestream Debug GUI")
        self.resize(1280, 860)

        self.watcher = WorkspaceWatcher()
        self.watcher.start()

        tabs = QTabWidget()
        tabs.addTab(MemoryInspectorPanel(self.watcher), "Memory Inspector")
        tabs.addTab(ChainExplorerPanel(self.watcher), "Task && Chain Explorer")
        tabs.addTab(PromptQueuePanel(self.watcher), "Live Prompt Queue")
        tabs.addTab(LogFeedPanel(self.watcher), "Log Feed")
        tabs.addTab(DisambiguationLogPanel(self.watcher), "Disambiguation Log")
        tabs.addTab(GuardrailLogPanel(self.watcher), "Guardrail Log")
        tabs.addTab(PersonaGrowthPanel(self.watcher), "Persona Growth")
        tabs.addTab(TestRunnerPanel(), "Test Runner")
        self.setCentralWidget(tabs)

    def closeEvent(self, event):
        self.watcher.stop()
        super().closeEvent(event)


def main() -> None:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
