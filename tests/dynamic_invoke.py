# =============================================================================
# dynamic_invoke.py — generic "invoke anything anywhere" layer for tests
# =============================================================================
# Two capabilities, both deliberately avoiding static imports so tests keep
# working against code that didn't exist when the test was written:
#
#   call(path, *args, **kwargs)
#       Dynamically import and call any function by dotted path string
#       (e.g. "research.resolve_persona", "memory.memory_list_personas").
#       No import statement to update when a new module or function appears.
#
#   call_workflow(name, task_id, input_text, client)
#       Dynamically discover and invoke any workflow's run() by its
#       registered WORKFLOW_META["name"], reusing router.load_workflows() —
#       the exact same discovery mechanism agent_worker.py uses in
#       production. A workflow file dropped into workflows/ after this
#       module was written is invocable through this with zero test-file
#       changes, including ones that don't exist yet.
#
# Caution: several existing workflows (lead_gen_outreach, business_intro,
# calendar_booking, delete_calendar_events) have real Gmail/Calendar side
# effects. call_workflow() will happily invoke any of them for real — that's
# the point, it's a generic capability — but don't call it against those
# workflows from a test that runs automatically in a normal suite. See
# test_workflow_registry.py for the always-safe structural-only check.
# =============================================================================

import importlib


def call(path: str, *args, **kwargs):
    """Dynamically import and call any function by dotted path string."""
    module_path, func_name = path.rsplit(".", 1)
    module = importlib.import_module(module_path)
    func = getattr(module, func_name)
    return func(*args, **kwargs)


def workflow_registry() -> dict:
    """
    Fresh router.load_workflows() call — always reflects exactly what's
    currently in workflows/, including files that didn't exist when any test
    importing this was written.
    """
    from router import load_workflows
    return load_workflows()


def call_workflow(name: str, task_id: str, input_text: str, client) -> None:
    """Invoke any workflow's run() by its registered name."""
    registry = workflow_registry()
    if name not in registry:
        raise ValueError(f"No workflow named {name!r}. Available: {sorted(registry)}")
    registry[name]["run"](task_id, input_text, client)
