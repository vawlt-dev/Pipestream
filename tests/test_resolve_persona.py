# =============================================================================
# test_resolve_persona.py — constrained integration test
# =============================================================================
# The actual mechanism behind the "RJ and Decker" scenario from this
# session's design discussion: an opaquely-named entity should resolve to an
# EXISTING persona once grounded research content is available — without
# needing the full ~14-minute gather_info() cascade to prove it. Seeds the
# persona directly (no LLM call for the seed itself), so this test's only
# real LLM call is the one actually under test.
#
# Marked integration — makes one real call against LM Studio. Uses
# dynamic_invoke.call() rather than `from research import resolve_persona`,
# so this test keeps working even if research.py's internal module layout
# changes later.
# =============================================================================

import pytest

from dynamic_invoke import call
from memory import memory_set_question, memory_list_personas


@pytest.mark.integration
def test_opaque_named_entity_matches_existing_persona(temp_memory_db, fake_client, log_fn):
    memory_set_question(
        "accounting firm", "persona", "Q1",
        "What is accounting firm, fundamentally?",
        answer=(
            "A professional services firm that handles bookkeeping, tax "
            "compliance, and financial reporting for clients."
        ),
        confidence="high", volatility="GLACIAL",
    )
    assert memory_list_personas() == ["accounting firm"]

    result = call(
        "research.resolve_persona",
        "RJ and Decker",
        "RJ and Decker is a chartered accounting and business advisory "
        "practice offering tax compliance, bookkeeping, and financial "
        "advisory services.",
        "test-task-constrained", fake_client, log_fn,
    )

    assert result == "accounting firm", (
        f"Expected the opaque-named entity to match the existing persona "
        f"exactly (same cache key), got {result!r} instead"
    )


@pytest.mark.integration
def test_cold_start_mints_a_category(temp_memory_db, fake_client, log_fn):
    """No existing vocabulary at all — resolve_persona must still produce
    something usable rather than returning empty."""
    assert memory_list_personas() == []

    result = call(
        "research.resolve_persona",
        "Beany",
        "Beany is a free app that helps users discover and explore "
        "specialty coffee shops worldwide.",
        "test-task-cold-start", fake_client, log_fn,
    )

    assert result and result != "uncategorized"
