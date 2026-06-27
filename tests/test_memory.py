# =============================================================================
# test_memory.py — fast unit tests for memory.py's pure SQL functions
# =============================================================================
# No LLM, no network — every test isolated from the real production DB via
# the temp_memory_db fixture (conftest.py).
# =============================================================================

import sqlite3
from datetime import datetime, timedelta, timezone

from memory import (
    memory_set_question, memory_get_question, memory_list_personas,
    memory_get_persona_coverage, memory_forget_topic, _normalize,
)


def test_normalize_strips_corporate_suffix_and_case():
    assert _normalize("Acme Ltd") == _normalize("Acme")
    assert _normalize("Acme Inc.") == _normalize("acme")
    assert _normalize("  Acme   Corp  ") == _normalize("acme")


def test_set_then_get_roundtrip(temp_memory_db):
    memory_set_question(
        "Acme Corp", "prospect", "Q1", "What is Acme Corp?",
        answer="A widget maker.", confidence="high", volatility="NORMAL",
    )
    row = memory_get_question("Acme Corp", "prospect", "Q1")
    assert row is not None
    assert row["answer"] == "A widget maker."
    assert row["was_answered"] is True
    assert row["confidence"] == "high"


def test_get_miss_returns_none(temp_memory_db):
    assert memory_get_question("Nonexistent Co", "prospect", "Q1") is None


def test_topic_level_is_part_of_the_cache_key(temp_memory_db):
    """Same topic string, different topic_level — must not collide."""
    memory_set_question("Acme", "prospect", "Q1", "q", answer="prospect answer")
    memory_set_question("Acme", "persona", "Q1", "q", answer="persona answer")
    assert memory_get_question("Acme", "prospect", "Q1")["answer"] == "prospect answer"
    assert memory_get_question("Acme", "persona", "Q1")["answer"] == "persona answer"


def test_expired_row_returns_none(temp_memory_db):
    memory_set_question(
        "Old News Inc", "prospect", "Q10", "Recent news?",
        answer="Something happened.", volatility="VOLATILE",
    )
    # Force the row into the past rather than waiting on VOLATILE's real TTL.
    import memory as memory_module
    conn = sqlite3.connect(memory_module.DB_PATH)
    past = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    conn.execute("UPDATE memory SET expires_at = ? WHERE question_id = 'Q10'", (past,))
    conn.commit()
    conn.close()

    assert memory_get_question("Old News Inc", "prospect", "Q10") is None


def test_list_personas_only_returns_persona_level(temp_memory_db):
    memory_set_question("Acme Corp", "prospect", "Q1", "q", answer="a")
    memory_set_question("accounting firm", "persona", "Q1", "q", answer="a")
    assert memory_list_personas() == ["accounting firm"]


def test_list_personas_empty_on_cold_start(temp_memory_db):
    assert memory_list_personas() == []


def test_persona_coverage_reports_fresh_ids(temp_memory_db):
    memory_set_question("accounting firm", "persona", "Q1", "q", answer="a")
    memory_set_question("accounting firm", "persona", "PITCH1", "q", answer="a")
    coverage = memory_get_persona_coverage("accounting firm")
    assert set(coverage["fresh"]) == {"Q1", "PITCH1"}


def test_persona_coverage_excludes_expired(temp_memory_db):
    memory_set_question("accounting firm", "persona", "Q1", "q", answer="a", volatility="VOLATILE")
    import memory as memory_module
    conn = sqlite3.connect(memory_module.DB_PATH)
    past = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    conn.execute("UPDATE memory SET expires_at = ?", (past,))
    conn.commit()
    conn.close()

    coverage = memory_get_persona_coverage("accounting firm")
    assert coverage["fresh"] == []


def test_forget_topic_removes_only_that_level(temp_memory_db):
    memory_set_question("Acme", "prospect", "Q1", "q", answer="a")
    memory_set_question("Acme", "persona", "Q1", "q", answer="a")
    deleted = memory_forget_topic("Acme", "prospect")
    assert deleted == 1
    assert memory_get_question("Acme", "prospect", "Q1") is None
    assert memory_get_question("Acme", "persona", "Q1") is not None


def test_set_question_upserts_not_duplicates(temp_memory_db):
    memory_set_question("Acme", "prospect", "Q1", "q", answer="first")
    memory_set_question("Acme", "prospect", "Q1", "q", answer="second")
    import memory as memory_module
    conn = sqlite3.connect(memory_module.DB_PATH)
    count = conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0]
    conn.close()
    assert count == 1
    assert memory_get_question("Acme", "prospect", "Q1")["answer"] == "second"
