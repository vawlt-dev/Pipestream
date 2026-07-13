# =============================================================================
# local_logs.py — local mirror of the dashboard log feed
# =============================================================================
# agent_worker.py's VPSClient.log() only ever POSTs to the VPS — there was no
# local record of task logs at all before this, meaning the debug GUI's Log
# Feed panel had nothing to read without adding a network/API-key dependency.
# This gives every log() call a local copy at the same choke point, so the
# GUI can show "the same data as the website" purely from local files.
#
# Also used directly by the debug GUI's Test Runner stand-in client, so a
# function/workflow invoked through the GUI logs into the exact same place
# and table shape a real production task would — no separate "test mode"
# log feed.
# =============================================================================

import os
import sqlite3
from datetime import datetime, timezone

WORK_DIR = os.getenv("WORK_DIR", "/workspace")
LOCAL_LOGS_DB_PATH = os.path.join(WORK_DIR, "local_logs.db")


def _get_connection() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(LOCAL_LOGS_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(LOCAL_LOGS_DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS local_logs (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id   TEXT NOT NULL,
            message   TEXT NOT NULL,
            log_type  TEXT NOT NULL,
            timestamp TEXT NOT NULL
        )
        """
    )
    return conn


def log_local(task_id: str, message: str, log_type: str = "info") -> None:
    """
    Append one log line to the local mirror. Never raises — a failure here
    must never break the real VPS log call it's mirroring alongside.
    """
    try:
        conn = _get_connection()
        with conn:
            conn.execute(
                "INSERT INTO local_logs (task_id, message, log_type, timestamp) VALUES (?, ?, ?, ?)",
                (task_id, message, log_type, datetime.now(timezone.utc).isoformat()),
            )
        conn.close()
    except Exception as e:
        print(f"  [LOCAL LOG ERROR] failed to write local log: {e}", flush=True)
