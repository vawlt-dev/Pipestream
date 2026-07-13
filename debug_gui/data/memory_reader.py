# =============================================================================
# memory_reader.py — read/write access to memory.db and local_logs.db
# =============================================================================
# Display queries use a read-only connection by construction
# (sqlite3.connect("file:...?mode=ro", uri=True)) — a list view has no
# reason to ever risk a write. Edits/deletes go through memory.py's own
# functions where possible (memory_set_question, memory_forget_topic,
# memory_update_outreach_status) so they stay consistent with the app's own
# normalization/TTL logic, not raw SQL reinventing it. The raw SQL box is
# the one place with a genuine read-write connection — anything goes there,
# confirm-gated by the panel before any non-SELECT statement runs.
# =============================================================================

import sqlite3

from paths import MEMORY_DB_PATH, LOCAL_LOGS_DB_PATH


def _ro_connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _rw_connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


# -- Memory Inspector: personas --------------------------------------------

def list_personas() -> list[dict]:
    """Persona topics with fact count and most recent update."""
    try:
        with _ro_connect(MEMORY_DB_PATH) as conn:
            rows = conn.execute(
                """
                SELECT topic, COUNT(*) AS fact_count, MAX(updated_at) AS last_updated,
                       MIN(updated_at) AS first_seen
                FROM memory
                WHERE topic_level = 'persona'
                GROUP BY topic_key
                ORDER BY topic
                """
            ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []  # memory.db doesn't exist yet — no task has run


def get_topic_questions(topic: str, topic_level: str) -> list[dict]:
    """Every cached row (fresh or expired) for one topic at one level."""
    try:
        with _ro_connect(MEMORY_DB_PATH) as conn:
            rows = conn.execute(
                """
                SELECT topic, topic_level, question_id, question_text, answer,
                       was_answered, confidence, volatility, important,
                       source_urls, updated_at, expires_at
                FROM memory
                WHERE topic_key = (
                    SELECT topic_key FROM memory WHERE topic = ? AND topic_level = ? LIMIT 1
                ) AND topic_level = ?
                ORDER BY question_id
                """,
                (topic, topic_level, topic_level),
            ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []


# -- Memory Inspector: prospects / outreach --------------------------------

def list_outreach(sender: str | None = None) -> list[dict]:
    try:
        with _ro_connect(MEMORY_DB_PATH) as conn:
            if sender:
                rows = conn.execute(
                    "SELECT * FROM outreach_history WHERE sender = ? ORDER BY updated_at DESC",
                    (sender,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM outreach_history ORDER BY updated_at DESC"
                ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []


def list_senders() -> list[str]:
    try:
        with _ro_connect(MEMORY_DB_PATH) as conn:
            rows = conn.execute("SELECT DISTINCT sender FROM outreach_history ORDER BY sender").fetchall()
        return [r[0] for r in rows]
    except sqlite3.OperationalError:
        return []


# -- Persona vocabulary growth ----------------------------------------------

def persona_growth() -> list[dict]:
    """One row per persona, first_seen approximated by MIN(updated_at) (a
    row's updated_at doesn't change again until its TTL expires, so this is
    a reasonable proxy for "when this category was first learned")."""
    return list_personas()


# -- Edits / deletes, routed through memory.py's own functions -------------

def edit_fact(topic: str, topic_level: str, question_id: str, question_text: str,
              answer: str, confidence: str, volatility: str) -> None:
    import sys
    from paths import AGENTT_ROOT
    if AGENTT_ROOT not in sys.path:
        sys.path.insert(0, AGENTT_ROOT)
    from memory import memory_set_question
    memory_set_question(
        topic, topic_level, question_id, question_text,
        answer=answer, confidence=confidence, volatility=volatility,
    )


def delete_topic(topic: str, topic_level: str) -> int:
    import sys
    from paths import AGENTT_ROOT
    if AGENTT_ROOT not in sys.path:
        sys.path.insert(0, AGENTT_ROOT)
    from memory import memory_forget_topic
    return memory_forget_topic(topic, topic_level)


def set_outreach_status(sender: str, prospect: str, status: str) -> None:
    import sys
    from paths import AGENTT_ROOT
    if AGENTT_ROOT not in sys.path:
        sys.path.insert(0, AGENTT_ROOT)
    from memory import memory_update_outreach_status
    memory_update_outreach_status(sender, prospect, status)


def delete_outreach_row(sender_key: str, prospect_key: str) -> None:
    """No equivalent in memory.py (outreach rows are never deleted by the
    app itself) — direct SQL, confirm-gated by the calling panel same as the
    raw SQL box."""
    with _rw_connect(MEMORY_DB_PATH) as conn:
        conn.execute(
            "DELETE FROM outreach_history WHERE sender_key = ? AND prospect_key = ?",
            (sender_key, prospect_key),
        )
        conn.commit()


# -- Raw SQL box -------------------------------------------------------------

def run_raw_sql(sql: str) -> tuple[list[str], list[tuple]]:
    """
    Executes any SQL against memory.db with a real read-write connection.
    Returns (column_names, rows) — column_names empty for statements with no
    result set (INSERT/UPDATE/DELETE), in which case rows is empty too.
    Confirm-gating for non-SELECT statements is the caller's (the panel's)
    responsibility — this function just executes what it's given.
    """
    with _rw_connect(MEMORY_DB_PATH) as conn:
        cur = conn.execute(sql)
        if cur.description:
            columns = [d[0] for d in cur.description]
            rows = cur.fetchall()
        else:
            columns, rows = [], []
        conn.commit()
        return columns, [tuple(r) for r in rows]


# -- Log Feed ----------------------------------------------------------------

def list_local_logs(task_id: str | None = None, log_type: str | None = None,
                     keyword: str | None = None, limit: int = 500) -> list[dict]:
    try:
        with _ro_connect(LOCAL_LOGS_DB_PATH) as conn:
            clauses, params = [], []
            if task_id:
                clauses.append("task_id = ?")
                params.append(task_id)
            if log_type:
                clauses.append("log_type = ?")
                params.append(log_type)
            if keyword:
                clauses.append("message LIKE ?")
                params.append(f"%{keyword}%")
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            rows = conn.execute(
                f"SELECT * FROM local_logs {where} ORDER BY id DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []


def delete_local_logs(task_id: str) -> int:
    with _rw_connect(LOCAL_LOGS_DB_PATH) as conn:
        cur = conn.execute("DELETE FROM local_logs WHERE task_id = ?", (task_id,))
        conn.commit()
        return cur.rowcount
