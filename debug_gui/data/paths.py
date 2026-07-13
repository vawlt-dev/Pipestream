# =============================================================================
# paths.py — where the debug GUI finds Pipestream's on-disk state
# =============================================================================
# The GUI runs natively on Windows; the real worker runs inside Docker. Both
# look at the same files because docker-compose.yml bind-mounts ./workspace
# into the container at /workspace — so the GUI just needs the host-side
# path to that same directory, no Docker API calls involved.
# =============================================================================

import os
import sys

# router.py's load_workflows() (and other agentt modules) print() Unicode
# characters (✓, ⚠️) straight to stdout — fine inside Docker (UTF-8 locale
# by default), but this GUI runs natively on Windows, where stdout often
# defaults to the console codepage (cp1252) and raises UnicodeEncodeError on
# the first such print. Reconfigure before any agentt module (imported
# below or by any other debug_gui module) gets a chance to print anything.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# debug_gui/data/paths.py -> debug_gui/data -> debug_gui -> agentt root
AGENTT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WORKSPACE_DIR = os.path.join(AGENTT_ROOT, "workspace")

MEMORY_DB_PATH = os.path.join(WORKSPACE_DIR, "memory.db")
LOCAL_LOGS_DB_PATH = os.path.join(WORKSPACE_DIR, "local_logs.db")
LIVE_STATE_PATH = os.path.join(WORKSPACE_DIR, "live_state.json")
TRACE_DIR = os.path.join(WORKSPACE_DIR, "traces")

# CRITICAL: core.py/tracing.py/memory.py all resolve their own on-disk paths
# via os.getenv("WORK_DIR", "/workspace") — correct inside the Docker
# container, where /workspace is a real absolute path. Running natively on
# Windows, the literal string "/workspace" resolves to C:\workspace (current
# drive root), NOT this WORKSPACE_DIR — silently coordinating against the
# wrong file. Set here, in the module every other debug_gui module imports
# first, so it's in effect before ANY of core/tracing/memory ever gets
# imported anywhere in this app, by anything.
os.environ["WORK_DIR"] = WORKSPACE_DIR
