# =============================================================================
# router.py — Dynamic Workflow Discovery and Dispatch
# =============================================================================
# Scans the workflows/ directory on every task, builds a live registry,
# asks the LLM to pick the right workflow, and runs it.
#
# Adding a new workflow:
#   1. Create a .py file in the workflows/ directory
#   2. Define WORKFLOW_META = {"name": "...", "description": "..."}
#   3. Define run(task_id, input_text, client) -> None
#   That's it — no changes needed here.
# =============================================================================

import os
import sys
import importlib.util

from core import llm_call, wait_for_input

WORKFLOWS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workflows")

# Ensure the app root is importable so workflow files can do `from core import ...`
_app_root = os.path.dirname(os.path.abspath(__file__))
if _app_root not in sys.path:
    sys.path.insert(0, _app_root)


def load_workflows() -> dict:
    """
    Scan workflows/ and import every valid .py file.
    A valid workflow file must expose:
        WORKFLOW_META: dict  — must have "name" and "description" keys
        run(task_id, input_text, client) -> None

    Reloads on every call so newly dropped files are picked up without restart.
    """
    registry: dict = {}

    if not os.path.isdir(WORKFLOWS_DIR):
        return registry

    for fname in sorted(os.listdir(WORKFLOWS_DIR)):
        if not fname.endswith(".py") or fname.startswith("_"):
            continue

        path = os.path.join(WORKFLOWS_DIR, fname)
        spec = importlib.util.spec_from_file_location(fname[:-3], path)
        mod  = importlib.util.module_from_spec(spec)

        try:
            spec.loader.exec_module(mod)
        except Exception as e:
            import traceback
            print(f"⚠️  Failed to load workflow '{fname}': {e}", flush=True)
            print(traceback.format_exc(), flush=True)
            continue

        meta = getattr(mod, "WORKFLOW_META", None)
        run  = getattr(mod, "run", None)

        if not (meta and isinstance(meta, dict) and "name" in meta and "description" in meta):
            print(f"⚠️  Skipping '{fname}': missing or invalid WORKFLOW_META", flush=True)
            continue

        if not callable(run):
            print(f"⚠️  Skipping '{fname}': no callable run() function", flush=True)
            continue

        print(f"  ✓ Loaded workflow: {meta['name']}  ({fname})", flush=True)
        registry[meta["name"]] = {"meta": meta, "run": run}

    return registry


def classify_intent(input_text: str, registry: dict) -> str:
    """
    Show the LLM the full description of every available workflow and ask it to pick one.
    Returns the workflow name, or "unknown" if none fit.
    """
    workflow_list = "\n".join(
        f'- "{name}": {info["meta"]["description"]}'
        for name, info in registry.items()
    )

    prompt = f"""Choose the best workflow to handle this request.

Available workflows:
{workflow_list}
- "unknown": none of the above fit

Request: "{input_text}"

Reply with ONLY the workflow name (e.g. business_intro) or "unknown":"""

    result = llm_call(prompt).strip().lower().strip('"').strip("'")
    result = result.replace('\\', '')  # Mistral sometimes markdown-escapes underscores as \_

    for name in registry:
        if name in result:
            return name
    return "unknown"


def route_workflow(task_id: str, input_text: str, client, _depth: int = 0) -> None:
    """
    Load the workflow registry, classify the request, and dispatch.
    On unknown intent, asks the user for clarification (one retry).
    """

    def log(msg: str, log_type: str = "info"):
        print(f"  [{log_type.upper()}] {msg}")
        client.log(task_id, msg, log_type)

    registry = load_workflows()
    print(f"\n  [ROUTER] Loaded {len(registry)} workflow(s): {list(registry.keys())}", flush=True)

    if not registry:
        log("No workflows found in workflows/ directory", "error")
        client.update_status(task_id, "failed",
                             error_message="No workflows are installed. Add .py files to the workflows/ directory.")
        return

    log(f"🔀 {len(registry)} workflow(s) loaded: {', '.join(registry)}", "info")

    workflow_name = classify_intent(input_text, registry)
    log(f"Routing to: {workflow_name}", "agent")

    if workflow_name in registry:
        log(f"▶ Running: {workflow_name}", "info")
        registry[workflow_name]["run"](task_id, input_text, client)
        return

    # Unknown intent — ask for clarification (once)
    if _depth > 0:
        log("Still couldn't determine intent after clarification", "error")
        client.update_status(
            task_id, "failed",
            error_message="Could not determine what to do. Please try again with more detail.",
        )
        return

    available = ", ".join(f'"{n}"' for n in registry)
    log("❓ Intent unclear — asking for clarification", "info")

    answer = wait_for_input(
        task_id,
        f"I'm not sure which workflow to use. "
        f"Available: {available}. "
        f"What would you like me to do?",
        client,
    )

    if not answer:
        return

    route_workflow(task_id, f"{input_text} — clarification: {answer}", client, _depth=1)
