# =============================================================================
# test_workflow_registry.py — structural smoke test over the live registry
# =============================================================================
# Parametrized at COLLECTION time (not inside a fixture) by calling
# workflow_registry() directly at module level — this is what makes it cover
# a workflow file added after this test was written with zero changes here.
#
# Deliberately does NOT invoke any workflow's real run() — several existing
# workflows (lead_gen_outreach, business_intro, calendar_booking,
# delete_calendar_events) have real Gmail/Calendar side effects. This only
# validates structure, the same two checks router.load_workflows() itself
# already enforces before registering a workflow into the live system.
# =============================================================================

import pytest

from dynamic_invoke import workflow_registry

_REGISTRY = workflow_registry()


def test_at_least_one_workflow_loaded():
    assert _REGISTRY, "No workflows discovered in workflows/ — router.load_workflows() returned nothing"


@pytest.mark.parametrize("name", sorted(_REGISTRY.keys()))
def test_workflow_has_valid_structure(name):
    info = _REGISTRY[name]
    assert callable(info["run"]), f"{name}'s run is not callable"
    description = info["meta"].get("description")
    assert isinstance(description, str) and len(description) > 10, (
        f"{name}'s WORKFLOW_META description is missing or suspiciously short"
    )
