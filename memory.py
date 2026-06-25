# =============================================================================
# memory.py — Persistent Per-Question Knowledge Cache
# =============================================================================
# SQLite-backed cache, one row per (topic, topic_level, question_id). Backs the
# seed-question research framework in research.py — every read/write goes
# through research.answer_question(), never called directly by gather_info().
#
# topic_level ('persona' | 'prospect') is part of the primary key, not just a
# stored column — a topic string can never collide across levels even if the
# same string is somehow used both ways, which is the structural backstop for
# the persona/prospect contamination problem (see research.py module docstring).
#
# Stored in /workspace so it survives container restarts and rebuilds — same
# volume as credentials.
# =============================================================================

import os
import re
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager

WORK_DIR = os.getenv("WORK_DIR", "/workspace")
DB_PATH  = os.path.join(WORK_DIR, "memory.db")

# Model classifies into one of these buckets instead of producing a raw date —
# small models are unreliable at calendar arithmetic. Mapped to durations here
# so they can be tuned without touching prompts. No permanent/non-expiring
# state, even GLACIAL — a wrong classification self-corrects eventually; a
# permanent flag would not.
VOLATILITY_DAYS = {
    "VOLATILE": 1,
    "FAST":     3,
    "NORMAL":   14,   # fallback default
    "SLOW":     90,
    "GLACIAL":  365,
}

_CORP_SUFFIXES = re.compile(
    r'\b(ltd|limited|inc|incorporated|llc|llp|corp|corporation|co|company|plc)\.?\s*$',
    re.IGNORECASE,
)


def _normalize(topic: str) -> str:
    """
    Lowercase, strip punctuation, collapse whitespace, drop a trailing common
    corporate suffix. Minimum-viable canonicalization — known limitation:
    "Acme Property Mgmt" and "Acme Property Management" still won't collide.
    An LLM-assisted canonicalization step would catch more of these but isn't
    built here.
    """
    text = topic.strip().lower()
    text = re.sub(r'[^\w\s]', ' ', text)
    text = " ".join(text.split())
    text = _CORP_SUFFIXES.sub('', text).strip()
    return text


def _compute_expiry(volatility: str) -> str:
    days = VOLATILITY_DAYS.get(volatility, VOLATILITY_DAYS["NORMAL"])
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


@contextmanager
def _connect():
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS memory (
                topic_key     TEXT NOT NULL,
                topic         TEXT NOT NULL,
                topic_level   TEXT NOT NULL,
                question_id   TEXT NOT NULL,
                question_text TEXT NOT NULL,
                answer        TEXT,
                was_answered  INTEGER NOT NULL DEFAULT 0,
                confidence    TEXT,
                volatility    TEXT NOT NULL DEFAULT 'NORMAL',
                important     INTEGER NOT NULL DEFAULT 0,
                source_urls   TEXT,
                updated_at    TEXT NOT NULL,
                expires_at    TEXT NOT NULL,
                PRIMARY KEY (topic_key, topic_level, question_id)
            )
        """)
        yield conn
        conn.commit()
    finally:
        conn.close()


def memory_get_question(topic: str, topic_level: str, question_id: str) -> dict | None:
    """
    Fresh-row lookup for one (topic, topic_level, question_id). Returns None on
    miss or expiry — callers should treat None as "go answer it". Only called
    from inside research.answer_question(); gather_info() never calls this
    directly.
    """
    key = _normalize(topic)
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT topic, question_text, answer, was_answered, confidence,
                   volatility, important, source_urls, updated_at, expires_at
            FROM memory
            WHERE topic_key = ? AND topic_level = ? AND question_id = ?
            """,
            (key, topic_level, question_id),
        ).fetchone()

    if not row:
        return None

    (topic_orig, question_text, answer, was_answered, confidence,
     volatility, important, source_urls_json, updated_at, expires_at) = row

    if datetime.fromisoformat(expires_at) < datetime.now(timezone.utc):
        return None

    return {
        "topic":         topic_orig,
        "topic_level":   topic_level,
        "question_id":   question_id,
        "question_text": question_text,
        "answer":        answer,
        "was_answered":  bool(was_answered),
        "confidence":    confidence,
        "volatility":    volatility,
        "important":     bool(important),
        "source_urls":   json.loads(source_urls_json) if source_urls_json else [],
        "updated_at":    updated_at,
    }


def memory_set_question(
    topic: str,
    topic_level: str,
    question_id: str,
    question_text: str,
    answer: str,
    confidence: str = "medium",
    volatility: str = "NORMAL",
    important: bool = False,
    source_urls: list[str] | None = None,
    was_answered: bool = True,
) -> None:
    """Upsert one (topic, topic_level, question_id) row. Computes expires_at from volatility."""
    key         = _normalize(topic)
    now         = datetime.now(timezone.utc).isoformat()
    expires_at  = _compute_expiry(volatility)
    urls_json   = json.dumps(source_urls or [])

    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO memory (
                topic_key, topic, topic_level, question_id, question_text,
                answer, was_answered, confidence, volatility, important,
                source_urls, updated_at, expires_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(topic_key, topic_level, question_id) DO UPDATE SET
                topic         = excluded.topic,
                question_text = excluded.question_text,
                answer        = excluded.answer,
                was_answered  = excluded.was_answered,
                confidence    = excluded.confidence,
                volatility    = excluded.volatility,
                important     = excluded.important,
                source_urls   = excluded.source_urls,
                updated_at    = excluded.updated_at,
                expires_at    = excluded.expires_at
            """,
            (
                key, topic, topic_level, question_id, question_text,
                answer, int(was_answered), confidence, volatility, int(important),
                urls_json, now, expires_at,
            ),
        )


def memory_get_topic_questions(topic: str) -> list[dict]:
    """
    All fresh rows for a topic, across both levels. Convenience/introspection
    only (e.g. a future "show me everything cached about X" feature) — not
    part of the main read path used by gather_info()/answer_question().
    """
    key = _normalize(topic)
    now = datetime.now(timezone.utc)
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT topic, topic_level, question_id, question_text, answer, was_answered,
                   confidence, volatility, important, source_urls, updated_at, expires_at
            FROM memory
            WHERE topic_key = ?
            """,
            (key,),
        ).fetchall()

    results = []
    for (topic_orig, topic_level, question_id, question_text, answer, was_answered,
         confidence, volatility, important, urls_json, updated_at, expires_at) in rows:
        if datetime.fromisoformat(expires_at) < now:
            continue
        results.append({
            "topic": topic_orig, "topic_level": topic_level, "question_id": question_id,
            "question_text": question_text, "answer": answer, "was_answered": bool(was_answered),
            "confidence": confidence, "volatility": volatility, "important": bool(important),
            "source_urls": json.loads(urls_json) if urls_json else [], "updated_at": updated_at,
        })
    return results


def memory_forget_topic(topic: str, topic_level: str) -> int:
    """Delete all rows for a topic at a given level. Returns rows deleted."""
    key = _normalize(topic)
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM memory WHERE topic_key = ? AND topic_level = ?",
            (key, topic_level),
        )
        return cur.rowcount


def memory_refresh_candidates(limit: int = 20) -> list[dict]:
    """
    Rows that are expired, ordered important-first. This is the query a future
    idle-refresh pass would use to decide what to re-answer — no scheduler or
    background loop is wired up to call this yet (agent_worker.py is still
    single-task-sequential).
    """
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT topic, topic_level, question_id, question_text, important, expires_at
            FROM memory
            WHERE expires_at < ?
            ORDER BY important DESC, expires_at ASC
            LIMIT ?
            """,
            (now, limit),
        ).fetchall()

    return [
        {
            "topic": t, "topic_level": tl, "question_id": qid,
            "question_text": qt, "important": bool(imp), "expires_at": exp,
        }
        for (t, tl, qid, qt, imp, exp) in rows
    ]
