# =============================================================================
# test_disambiguation.py
# =============================================================================

import pytest

import core
from dynamic_invoke import call


@pytest.mark.integration
def test_check_ambiguity_detects_a_genuinely_ambiguous_name():
    """
    "ABC Accounts" is the actual cross-contamination case confirmed earlier
    this session — find_contact_email() once pulled a contact address from
    an unrelated "ABC College of English" language school. Used here rather
    than a hypothetical example since it's grounded in a real prior bug, not
    a guess about what DuckDuckGo will surface (web_search() is hardcoded to
    region="nz-en", which drowns out genuinely cross-country ambiguous names
    like "McDonald's Hastings" — confirmed by this test originally using
    that example and failing because only the NZ location ever surfaced).

    Asserts on candidate COUNT, not the "ambiguous" boolean field — also
    confirmed via live testing that this model sometimes populates 2+
    genuinely distinct candidates while still setting ambiguous=false, so
    disambiguate_if_needed()'s actual gating logic (core.py) trusts
    candidate count over the boolean, and this test matches that.
    """
    decision = call(
        "research.check_ambiguity",
        "ABC Accounts",
        "Cold outreach prospect for an accounting/bookkeeping services firm",
        "",
    )
    assert len(decision.get("candidates") or []) >= 2


@pytest.mark.integration
@pytest.mark.parametrize("topic,context", [
    (
        "Premier Cleaning",
        "Cold outreach prospect for a cleaning supplies business",
    ),
    (
        "Apex Solutions",
        "Cold outreach prospect, general B2B services target",
    ),
])
def test_check_ambiguity_on_generic_business_names(topic, context):
    """
    Exploratory-confirmed cases (live run this session): "Premier Cleaning"
    surfaces a Bangladeshi finance company AND an unrelated NZ cleaning
    business under the same name — genuinely different entities, different
    industries, different countries. "Apex Solutions" surfaces at least two
    real, unrelated companies actually named "Apex Solutions" (one a tech
    firm, one flagged by a securities regulator) in different countries.
    Both are good positive cases: generic small-business-style names are
    exactly the pattern a real lead-gen prospect search would hit, unlike a
    globally dominant unique brand name (see the "Xero" case below).
    """
    decision = call("research.check_ambiguity", topic, context, "")
    assert len(decision.get("candidates") or []) >= 2


@pytest.mark.integration
def test_check_ambiguity_known_limitation_on_a_dominant_unique_brand():
    """
    NOT a correctness assertion — documents a known limitation found via
    live testing this session, so it doesn't get silently "fixed" by
    accident and forgotten. "Xero" search results return the dominant, truly
    unambiguous NZ accounting company PLUS an obscure, barely-related "Xero
    Competition" British racing team stub that search picked up — the model
    reliably finds 2 "candidates" here even though no reasonable person
    would actually confuse them. disambiguate_if_needed() gates on candidate
    count, so this WILL trigger a (mostly harmless, low-frequency) extra
    disambiguation question on an effectively unambiguous name. Accepted
    trade-off for now: asking one unnecessary question costs far less than
    the silent wrong-entity bugs this feature exists to catch (see ABC
    Accounts, Beany earlier this session). Revisit by asking the model to
    judge "would a person actually confuse these" rather than "do 2+
    technically distinct entities exist in the raw results" if the
    over-asking becomes a real annoyance in practice.
    """
    decision = call("research.check_ambiguity", "Xero", "Researching accounting software vendors", "")
    candidates = decision.get("candidates") or []
    assert len(candidates) >= 1  # documenting current behavior, not asserting it's ideal


def test_disambiguate_if_needed_trusted_auto_picks_without_blocking(temp_memory_db, monkeypatch, fake_client, log_fn):
    """Trusted tasks must never reach wait_for_input() for disambiguation —
    it would just return a meaningless 'yes', not an actual answer."""
    def _fake_check_ambiguity(topic, context, hint):
        return {
            "ambiguous": True,
            "candidates": [
                {"description": "McDonald's in Hastings, New Zealand", "distinguishing_detail": "New Zealand"},
                {"description": "McDonald's in Hastings, United Kingdom", "distinguishing_detail": "United Kingdom"},
            ],
        }

    def _fake_pick_best_guess(topic, candidates, hint):
        return "New Zealand"

    def _explode_if_called(*args, **kwargs):
        raise AssertionError("wait_for_input() must not be called on the trusted path")

    monkeypatch.setattr("research.check_ambiguity", _fake_check_ambiguity)
    monkeypatch.setattr("research.pick_best_guess_candidate", _fake_pick_best_guess)
    monkeypatch.setattr(core, "wait_for_input", _explode_if_called)

    fake_client.trusted = True
    result = core.disambiguate_if_needed(
        "McDonald's Hastings", "context", "your other prospects are NZ-based",
        "test-task", fake_client, log_fn,
    )
    assert result == "McDonald's Hastings (New Zealand)"


def test_disambiguate_if_needed_not_trusted_asks_and_folds_real_answer(temp_memory_db, monkeypatch, fake_client, log_fn):
    def _fake_check_ambiguity(topic, context, hint):
        return {
            "ambiguous": True,
            "candidates": [
                {"description": "McDonald's in Hastings, New Zealand", "distinguishing_detail": "New Zealand"},
                {"description": "McDonald's in Hastings, United Kingdom", "distinguishing_detail": "United Kingdom"},
            ],
        }

    def _fake_build_question(topic, candidates, hint):
        return "Which McDonald's Hastings do you mean?"

    calls = {}

    def _fake_wait_for_input(task_id, question, client, timeout=300):
        calls["question"] = question
        return "United Kingdom"

    monkeypatch.setattr("research.check_ambiguity", _fake_check_ambiguity)
    monkeypatch.setattr("research.build_disambiguation_question", _fake_build_question)
    monkeypatch.setattr(core, "wait_for_input", _fake_wait_for_input)

    fake_client.trusted = False
    result = core.disambiguate_if_needed(
        "McDonald's Hastings", "context", "", "test-task", fake_client, log_fn,
    )
    assert calls["question"] == "Which McDonald's Hastings do you mean?"
    assert result == "McDonald's Hastings (United Kingdom)"


def test_disambiguate_if_needed_returns_original_topic_when_not_ambiguous(temp_memory_db, monkeypatch, fake_client, log_fn):
    monkeypatch.setattr("research.check_ambiguity", lambda topic, context, hint: {"ambiguous": False, "candidates": []})
    result = core.disambiguate_if_needed("Acme Corp", "context", "", "test-task", fake_client, log_fn)
    assert result == "Acme Corp"


def test_disambiguate_if_needed_caches_resolution_and_skips_recheck_on_next_call(
    temp_memory_db, monkeypatch, fake_client, log_fn,
):
    """
    The concrete "use memory for disambiguation" fix: once a name is
    resolved, a second call for the SAME bare topic — even from a totally
    different task_id, simulating a different campaign/sender later — must
    be a pure cache hit with zero new search/LLM work, not a re-ask.
    """
    call_count = {"check_ambiguity": 0}

    def _fake_check_ambiguity(topic, context, hint):
        call_count["check_ambiguity"] += 1
        return {
            "ambiguous": True,
            "candidates": [
                {"description": "ABC Accounts, an Auckland bookkeeping firm", "distinguishing_detail": "Auckland bookkeeping firm"},
                {"description": "ABC College of English, a language school", "distinguishing_detail": "language school"},
            ],
        }

    monkeypatch.setattr("research.check_ambiguity", _fake_check_ambiguity)
    monkeypatch.setattr("research.pick_best_guess_candidate", lambda topic, candidates, hint: "Auckland bookkeeping firm")

    fake_client.trusted = True

    first = core.disambiguate_if_needed("ABC Accounts", "context", "", "task-one", fake_client, log_fn)
    second = core.disambiguate_if_needed("ABC Accounts", "context", "", "task-two-different-campaign", fake_client, log_fn)

    assert first == "ABC Accounts (Auckland bookkeeping firm)"
    assert second == first
    assert call_count["check_ambiguity"] == 1, "second call should have been a cache hit, not a re-check"
