# =============================================================================
# test_outreach_history.py — fast unit tests, no LLM
# =============================================================================

from memory import (
    memory_record_outreach, memory_update_outreach_status,
    memory_get_contacted_prospects, memory_find_outreach_by_email,
    _normalize,
)


def test_record_then_lookup_roundtrip(temp_memory_db):
    memory_record_outreach(
        "BrightPath Bookkeeping", "ABC Accounts", "drafted",
        target_email="jane@abcaccounts.example", subject_line="Quick question",
        angle="tax season relief", draft_body="Hi Jane, ...",
    )
    assert memory_get_contacted_prospects("BrightPath Bookkeeping") == {_normalize("ABC Accounts")}


def test_any_status_counts_as_contacted(temp_memory_db):
    # Names deliberately avoid trailing corporate-suffix words ("Co", "Ltd",
    # etc.) — _normalize() strips those, so asserting against the raw
    # lowercased string would be wrong for names that end in one (caught by
    # this test originally using "No Contact Co" etc. and failing).
    memory_record_outreach("Acme", "Nocontacto Industries", "skipped_no_contact")
    memory_record_outreach("Acme", "Faildrafto Industries", "skipped_draft_failed")
    memory_record_outreach("Acme", "Flago Industries", "skipped_inappropriate")
    contacted = memory_get_contacted_prospects("Acme")
    assert contacted == {
        _normalize("Nocontacto Industries"),
        _normalize("Faildrafto Industries"),
        _normalize("Flago Industries"),
    }


def test_find_by_email_returns_full_record(temp_memory_db):
    memory_record_outreach(
        "BrightPath Bookkeeping", "ABC Accounts", "drafted",
        target_email="jane@abcaccounts.example", subject_line="Quick question",
        angle="tax season relief", draft_body="Hi Jane, ...",
    )
    found = memory_find_outreach_by_email("jane@abcaccounts.example")
    assert found is not None
    assert found["sender"] == "BrightPath Bookkeeping"
    assert found["prospect"] == "ABC Accounts"
    assert found["angle"] == "tax season relief"
    assert found["status"] == "drafted"


def test_find_by_email_returns_none_for_unknown_address(temp_memory_db):
    assert memory_find_outreach_by_email("nobody@nowhere.example") is None


def test_find_by_email_with_empty_string_returns_none(temp_memory_db):
    assert memory_find_outreach_by_email("") is None


def test_update_status_advances_existing_row(temp_memory_db):
    memory_record_outreach(
        "BrightPath Bookkeeping", "ABC Accounts", "drafted",
        target_email="jane@abcaccounts.example",
    )
    memory_update_outreach_status("BrightPath Bookkeeping", "ABC Accounts", "replied")
    found = memory_find_outreach_by_email("jane@abcaccounts.example")
    assert found["status"] == "replied"


def test_update_status_noops_on_missing_row(temp_memory_db):
    # Must not raise — a reply from someone never in outreach_history simply
    # isn't a tracked campaign contact.
    memory_update_outreach_status("Nobody", "Nothing", "replied")


def test_different_senders_have_independent_history(temp_memory_db):
    memory_record_outreach("Sender A", "Shared Prospect Inc", "drafted")
    assert memory_get_contacted_prospects("Sender A") == {_normalize("Shared Prospect Inc")}
    assert memory_get_contacted_prospects("Sender B") == set()


def test_record_upserts_not_duplicates(temp_memory_db):
    memory_record_outreach("Acme", "Prospect Co", "skipped_no_contact")
    memory_record_outreach("Acme", "Prospect Co", "drafted", target_email="x@y.example")
    import memory as memory_module
    import sqlite3
    conn = sqlite3.connect(memory_module.DB_PATH)
    count = conn.execute("SELECT COUNT(*) FROM outreach_history").fetchone()[0]
    conn.close()
    assert count == 1
    found = memory_find_outreach_by_email("x@y.example")
    assert found["status"] == "drafted"
