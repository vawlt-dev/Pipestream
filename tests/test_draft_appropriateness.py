# =============================================================================
# test_draft_appropriateness.py — integration tests, real LLM call
# =============================================================================

import pytest

from dynamic_invoke import call

_FLAGGED_DRAFT = (
    "Hi there,\n\n"
    "I noticed your team has likely been struggling with mental health issues "
    "due to overwork during tax season, and we'd love to help relieve that "
    "burden with our bookkeeping services.\n\n"
    "Let me know if you'd like to chat."
)

_CLEAN_DRAFT = (
    "Hi there,\n\n"
    "I came across ABC Accounts while researching firms expanding their "
    "virtual bookkeeping offerings, and thought there might be a good fit "
    "with what we do at BrightPath. Happy to share more if useful.\n\n"
    "Best regards"
)


@pytest.mark.integration
def test_flags_presumptuous_sensitive_claim():
    """The actual cited example this session: a draft asserting something
    like 'your workers are suffering from mental health issues' as a pitch
    angle — a wildly inappropriate, unverifiable claim about real people."""
    result = call("core.check_draft_appropriateness", _FLAGGED_DRAFT, "ABC Accounts")
    assert result["appropriate"] is False
    assert result["concern"]


@pytest.mark.integration
def test_does_not_flag_an_ordinary_professional_draft():
    """No false positives on a normal, inoffensive cold-outreach draft."""
    result = call("core.check_draft_appropriateness", _CLEAN_DRAFT, "ABC Accounts")
    assert result["appropriate"] is True
