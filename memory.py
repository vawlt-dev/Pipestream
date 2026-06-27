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

from tracing import trace

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
        # A relationship between a sender and a prospect, not a fact about a
        # topic — deliberately a separate table rather than shoehorned into
        # the (topic_key, topic_level, question_id) schema above. Carries
        # enough (target_email, angle, draft_body) for email_triage.py to
        # recognize an incoming reply as connected to a specific outreach
        # attempt and draft an informed response, not just a dedup flag.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS outreach_history (
                sender_key   TEXT NOT NULL,
                sender       TEXT NOT NULL,
                prospect_key TEXT NOT NULL,
                prospect     TEXT NOT NULL,
                target_email TEXT NOT NULL DEFAULT '',
                subject_line TEXT NOT NULL DEFAULT '',
                angle        TEXT NOT NULL DEFAULT '',
                draft_body   TEXT NOT NULL DEFAULT '',
                status       TEXT NOT NULL,
                created_at   TEXT NOT NULL,
                updated_at   TEXT NOT NULL,
                PRIMARY KEY (sender_key, prospect_key)
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

    trace(
        "memory_write", topic=topic, topic_key=key, topic_level=topic_level,
        question_id=question_id, question_text=question_text, answer=answer,
        was_answered=was_answered, confidence=confidence, volatility=volatility,
        important=important, source_urls=source_urls or [], expires_at=expires_at,
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


def memory_list_personas() -> list[str]:
    """
    Every distinct topic currently cached at topic_level='persona' — the
    system's self-growing vocabulary of "kinds of things I already
    understand". No separate table: the vocabulary *is* whatever's already
    cached, so a freshly-learned persona is automatically part of the
    vocabulary for every future classification call with zero extra wiring.
    Original casing preserved (one arbitrary row's topic per distinct
    topic_key) since this is shown to the LLM as enum choices, not used as a
    lookup key itself.
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT topic FROM memory WHERE topic_level = 'persona' ORDER BY topic"
        ).fetchall()
    return [r[0] for r in rows]


def memory_get_persona_coverage(topic: str) -> dict:
    """
    Which question_ids have a fresh (non-expired) row for this persona vs.
    are missing or stale. Powers self_study.py's gap-filling pass — doesn't
    care whether question_ids come from SEED_QUESTIONS or PITCH_QUESTIONS,
    it's just whatever's actually in the table for this topic_key.
    Returns {"fresh": [question_id, ...], "stale_or_missing": [...]} — the
    caller supplies the full universe of ids it expects to compare against.
    """
    key = _normalize(topic)
    now = datetime.now(timezone.utc)
    with _connect() as conn:
        rows = conn.execute(
            "SELECT question_id, expires_at FROM memory WHERE topic_key = ? AND topic_level = 'persona'",
            (key,),
        ).fetchall()
    fresh = [qid for qid, expires_at in rows if datetime.fromisoformat(expires_at) >= now]
    return {"fresh": fresh}


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


# =============================================================================
# OUTREACH HISTORY — sender<->prospect relationship, not a topic fact
# =============================================================================

def memory_record_outreach(
    sender: str,
    prospect: str,
    status: str,
    target_email: str = "",
    subject_line: str = "",
    angle: str = "",
    draft_body: str = "",
) -> None:
    """
    Initial upsert — full record. Called from lead_gen_outreach.py/
    business_intro.py right after a draft attempt, success or failure, so a
    later run for the same sender knows not to retarget this prospect, and
    email_triage.py can later recognize a reply from target_email.
    """
    sender_key   = _normalize(sender)
    prospect_key = _normalize(prospect)
    now          = datetime.now(timezone.utc).isoformat()

    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO outreach_history (
                sender_key, sender, prospect_key, prospect, target_email,
                subject_line, angle, draft_body, status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(sender_key, prospect_key) DO UPDATE SET
                sender       = excluded.sender,
                prospect     = excluded.prospect,
                target_email = excluded.target_email,
                subject_line = excluded.subject_line,
                angle        = excluded.angle,
                draft_body   = excluded.draft_body,
                status       = excluded.status,
                updated_at   = excluded.updated_at
            """,
            (
                sender_key, sender, prospect_key, prospect, target_email,
                subject_line, angle, draft_body, status, now, now,
            ),
        )


def memory_update_outreach_status(sender: str, prospect: str, status: str) -> None:
    """
    Lifecycle update on an EXISTING row — just status + updated_at. Used by
    email_triage.py when a reply comes in ('replied') or a meeting gets
    booked from that reply ('booked'). No-ops (no error) if no row exists —
    a reply from someone never in outreach_history simply isn't a tracked
    campaign contact.
    """
    sender_key   = _normalize(sender)
    prospect_key = _normalize(prospect)
    now          = datetime.now(timezone.utc).isoformat()

    with _connect() as conn:
        conn.execute(
            """
            UPDATE outreach_history SET status = ?, updated_at = ?
            WHERE sender_key = ? AND prospect_key = ?
            """,
            (status, now, sender_key, prospect_key),
        )


def memory_get_contacted_prospects(sender: str) -> set[str]:
    """
    Normalized prospect keys already recorded for this sender, regardless of
    status — once a prospect has been considered for a sender (drafted, or
    tried and failed for any reason), it's excluded from future
    lead_gen_outreach runs for that same sender.
    """
    sender_key = _normalize(sender)
    with _connect() as conn:
        rows = conn.execute(
            "SELECT prospect_key FROM outreach_history WHERE sender_key = ?",
            (sender_key,),
        ).fetchall()
    return {r[0] for r in rows}


def memory_find_outreach_by_email(target_email: str) -> dict | None:
    """
    The bridge lookup email_triage.py uses: given an INCOMING email's sender
    address, find the matching outreach record, if any. Most recent match if
    more than one (target_email isn't part of the primary key, so this
    shouldn't normally happen, but a person could theoretically be reached
    twice under different prospect-name spellings).
    """
    if not target_email:
        return None
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT sender, prospect, target_email, subject_line, angle,
                   draft_body, status, created_at, updated_at
            FROM outreach_history
            WHERE target_email = ?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (target_email,),
        ).fetchone()

    if not row:
        return None

    (sender, prospect, email, subject_line, angle, draft_body,
     status, created_at, updated_at) = row
    return {
        "sender": sender, "prospect": prospect, "target_email": email,
        "subject_line": subject_line, "angle": angle, "draft_body": draft_body,
        "status": status, "created_at": created_at, "updated_at": updated_at,
    }
