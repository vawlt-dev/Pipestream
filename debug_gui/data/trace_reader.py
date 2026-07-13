# =============================================================================
# trace_reader.py — parse workspace/traces/*.jsonl into typed records
# =============================================================================
# Tracks a byte offset per file so a panel can re-read incrementally after a
# file_watcher signal fires, instead of re-parsing the whole (potentially
# large, untruncated-prompt-containing) file on every change.
# =============================================================================

import os
import json
import glob

from paths import TRACE_DIR


def list_task_ids() -> list[str]:
    if not os.path.isdir(TRACE_DIR):
        return []
    return sorted(
        os.path.splitext(os.path.basename(p))[0]
        for p in glob.glob(os.path.join(TRACE_DIR, "*.jsonl"))
    )


def task_trace_path(task_id: str) -> str:
    return os.path.join(TRACE_DIR, f"{task_id}.jsonl")


def read_all_events(task_id: str) -> list[dict]:
    path = task_trace_path(task_id)
    events = []
    if not os.path.exists(path):
        return events
    # errors="replace" — trace lines can carry scraped page text that isn't
    # always clean UTF-8; one bad byte sequence in an old trace file must
    # not crash an otherwise-working read of the rest of it.
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


class IncrementalTailer:
    """
    Tracks a byte offset into one task's trace file, so a repeated call
    after a file_watcher signal only parses newly-appended lines instead of
    the whole file. A trace file is append-only by design (see
    tools_google.py/core.py — nothing in the GUI ever writes to one), so a
    byte offset is always safe to resume from.
    """

    def __init__(self, task_id: str):
        self.task_id = task_id
        self._offset = 0

    def read_new_events(self) -> list[dict]:
        path = task_trace_path(self.task_id)
        if not os.path.exists(path):
            return []
        events = []
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(self._offset)
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            self._offset = f.tell()
        return events

    def reset(self) -> None:
        self._offset = 0


def build_ancestry_tree(events: list[dict]) -> dict:
    """
    Groups events by call_id, attaches each call_id's events under it, and
    nests by parent_call_id. Returns {"roots": [...], "by_call_id": {...}}
    — root_call_id is whatever has parent_call_id == None (normally exactly
    one: the task_start/route_workflow push_call() root).
    """
    by_call_id: dict[str, dict] = {}
    for ev in events:
        cid = ev.get("call_id")
        if cid is None:
            continue
        node = by_call_id.setdefault(cid, {"call_id": cid, "parent_call_id": ev.get("parent_call_id"), "events": [], "children": []})
        node["events"].append(ev)
        if node["parent_call_id"] is None and ev.get("parent_call_id") is not None:
            node["parent_call_id"] = ev.get("parent_call_id")

    roots = []
    for cid, node in by_call_id.items():
        parent_id = node["parent_call_id"]
        if parent_id and parent_id in by_call_id:
            by_call_id[parent_id]["children"].append(node)
        else:
            roots.append(node)

    return {"roots": roots, "by_call_id": by_call_id}
