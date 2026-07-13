# =============================================================================
# file_watcher.py — watchdog Observer wrapping the watched paths
# =============================================================================
# OS-level filesystem notifications, not a QTimer polling loop — the actual
# fix for "live means live" (see the project plan): a panel hears about a
# change within milliseconds of it being written, rather than up to a fixed
# poll interval later. Given this system's actual event cadence (LLM calls
# take seconds to minutes), this comfortably clears the bar for "real-time".
# =============================================================================

import os

from PySide6.QtCore import QObject, Signal
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from paths import WORKSPACE_DIR, TRACE_DIR, LIVE_STATE_PATH, LOCAL_LOGS_DB_PATH, MEMORY_DB_PATH


class _Handler(FileSystemEventHandler):
    def __init__(self, on_change):
        self._on_change = on_change

    def on_modified(self, event):
        self._on_change(event.src_path)

    def on_created(self, event):
        self._on_change(event.src_path)

    def on_moved(self, event):
        # core.ChainRegistry._write_state() writes atomically — a temp file
        # then os.replace() onto the real path — specifically so the GUI
        # never reads a half-written live_state.json. That os.replace()
        # surfaces to watchdog as a MOVE (temp path -> real path), not a
        # "modified" event, which on_modified/on_created above never catch.
        # dest_path is the real path the rename landed on.
        self._on_change(event.dest_path)


class WorkspaceWatcher(QObject):
    """
    One Observer over the whole workspace directory (traces/, memory.db,
    local_logs.db, live_state.json all live under it), emitting a single Qt
    signal with the changed path — panels filter by suffix/name themselves
    rather than each running their own Observer.
    """

    trace_changed = Signal(str)        # path to the changed *.jsonl file
    live_state_changed = Signal()
    local_logs_changed = Signal()
    memory_db_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._observer = Observer()
        os.makedirs(WORKSPACE_DIR, exist_ok=True)
        os.makedirs(TRACE_DIR, exist_ok=True)
        self._observer.schedule(_Handler(self._dispatch), WORKSPACE_DIR, recursive=True)

    def start(self) -> None:
        self._observer.start()

    def stop(self) -> None:
        self._observer.stop()
        self._observer.join(timeout=2)

    def _dispatch(self, path: str) -> None:
        path = os.path.abspath(path)
        if path.endswith(".jsonl"):
            self.trace_changed.emit(path)
        elif path == os.path.abspath(LIVE_STATE_PATH):
            self.live_state_changed.emit()
        elif path == os.path.abspath(LOCAL_LOGS_DB_PATH):
            self.local_logs_changed.emit()
        elif path == os.path.abspath(MEMORY_DB_PATH):
            self.memory_db_changed.emit()
