# =============================================================================
# router.py — Intent Classification and Workflow Dispatch
# =============================================================================
# Reads the user's input, classifies it into a workflow type, and dispatches
# to the appropriate workflow function.
#
# Adding a new workflow:
#   1. Write the workflow function in workflows.py
#   2. Import it here
#   3. Add its type to the classify_intent prompt and the dispatch block below
# =============================================================================

from workflows import business_intro_workflow, calendar_booking_workflow, wait_for_input, llm_call

WORKFLOW_TYPES = ["email_outreach", "calendar_booking"]

def classify_intent(input_text: str) -> str:
    """Use LLM to classify the request into a workflow type."""
    prompt = f"""Classify this task request into exactly one of these workflow types:

- email_outreach: research a company or person and send or draft an outreach or introduction email
- calendar_booking: schedule, add, check, or modify a calendar appointment or event
- unknown: anything that doesn't clearly fit the above

Request: "{input_text}"

Respond with ONLY the workflow type (one of: email_outreach, calendar_booking, unknown):"""

    result = llm_call(prompt).strip().lower()

    for wf in WORKFLOW_TYPES:
        if wf in result:
            return wf
    return "unknown"


def route_workflow(task_id: str, input_text: str, client, _depth: int = 0):
    """
    Classify intent and dispatch to the correct workflow.
    On unknown intent, asks the user to clarify (one level deep).
    """

    def log(msg: str, log_type: str = "info"):
        print(f"  [{log_type.upper()}] {msg}")
        client.log(task_id, msg, log_type)

    log("🔀 Classifying intent...", "info")
    workflow_type = classify_intent(input_text)
    log(f"Detected: {workflow_type}", "agent")

    if workflow_type == "email_outreach":
        log("📧 Starting email outreach workflow", "info")
        business_intro_workflow(task_id, input_text, client)

    elif workflow_type == "calendar_booking":
        log("📅 Starting calendar booking workflow", "info")
        calendar_booking_workflow(task_id, input_text, client)

    else:
        if _depth > 0:
            log("Still couldn't determine intent after clarification", "error")
            client.update_status(task_id, "failed",
                                 error_message="Could not determine what to do. Please try again with more detail.")
            return

        log("❓ Intent unclear — asking for clarification", "info")
        answer = wait_for_input(
            task_id,
            "I'm not sure what you'd like me to do. "
            "Are you looking to (1) send an outreach email to a company, "
            "or (2) book a calendar appointment?",
            client
        )

        if not answer:
            return

        # Re-route once with the clarification appended
        combined = f"{input_text} — clarification: {answer}"
        route_workflow(task_id, combined, client, _depth=1)
