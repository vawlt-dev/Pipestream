# =============================================================================
# Workflow: Business Introduction Email
# =============================================================================
# Drop this file in the workflows/ directory to enable it.
#
# What it does:
#   Researches a company, identifies the best angle for a cold intro email,
#   drafts it, gets user approval, then sends via Gmail.
# =============================================================================

from core import (
    llm_structured, gather_info, format_answers_as_context,
    clean_email_draft, clean_subject_line, extract_greeting_name,
    check_draft_appropriateness, wait_for_input, check_cancelled,
    SENDER_NAME, SENDER_TITLE, SENDER_WEBSITE, SENDER_BIO,
)
from schemas import s_object, s_string
from research import find_contact_email
from tools_google import send_email
from memory import memory_record_outreach

_PARSED_REQUEST_SCHEMA = s_object({"company": s_string(), "email": s_string()})
_ANGLE_SCHEMA          = s_object({"angle": s_string()})
_DRAFT_BODY_SCHEMA     = s_object({"body": s_string()})
_SUBJECT_LINE_SCHEMA   = s_object({"subject": s_string()})

WORKFLOW_META = {
    "name": "business_intro",
    "description": (
        "Research a company or person, then draft and send a personalized "
        "introduction or cold outreach email. Use this when the request mentions "
        "emailing, reaching out, introducing yourself, or sending a message to a company."
    ),
}


def run(task_id: str, input_text: str, client) -> None:

    def log(msg: str, log_type: str = "info"):
        print(f"  [{log_type.upper()}] {msg}")
        client.log(task_id, msg, log_type)

    # =========================================================================
    # STEP 1: Parse company name and target email from input
    # =========================================================================
    log("Step 1: Parsing input...", "info")

    parse_prompt = f"""Extract the following from this request:
- Company name to research
- Email address to send to (if provided)

Request: "{input_text}"

company: the company name to research
email: the email address to send to, or "not provided" if none is given"""

    parsed = llm_structured(parse_prompt, _PARSED_REQUEST_SCHEMA, schema_name="parsed_request")
    log(f"Parsed: {parsed}", "agent")

    company_name = str(parsed.get("company") or "").strip() or None
    target_email = str(parsed.get("email") or "").strip() or None

    if target_email and target_email.lower() == "not provided":
        target_email = None

    if not company_name:
        log("Could not identify company name from input", "error")
        client.update_status(task_id, "failed", error_message="Could not identify company name")
        return

    log(f"Company: {company_name}", "info")
    log(f"Email: {target_email or 'Not provided — will search'}", "info")
    client.update_status(task_id, "running", company_name=company_name)

    # =========================================================================
    # STEP 2: Gather information
    # =========================================================================
    log("Step 2: Gathering info...", "info")

    info = gather_info(
        company_name, "prospect", task_id, client, log,
        context=f"Writing a personalized intro email to {company_name}. Task: {input_text}",
    )
    if not info:
        return  # cancelled mid-research

    research_context = format_answers_as_context(info)

    # Best-effort — only available if Q3 (stakeholders) happened to be one of
    # the 3 questions selected for this topic. Falls back to a generic
    # greeting in STEP 4 if not.
    key_person = next(
        (a["answer"] for a in info["answers"] if a["question_id"] == "Q3" and a["was_answered"]),
        "not found",
    )

    if not target_email:
        log("🔍 Looking for a contact email...", "info")
        target_email = find_contact_email(company_name, task_id, client, log)
        if target_email:
            log(f"Found email from research: {target_email}", "success")

    if not target_email:
        log("No email address found or provided", "error")
        client.update_status(task_id, "failed",
                             error_message="Could not find email address. Please provide one.",
                             company_research=research_context)
        return

    client.update_status(task_id, "running", company_research=research_context)

    # =========================================================================
    # STEP 3: Identify the best angle for this specific company (Phase 5)
    # =========================================================================
    log("Step 3: Identifying email angle...", "info")

    angle_prompt = f"""Based on this research about {company_name}, identify the single most compelling and specific angle for a cold intro email from {SENDER_NAME} ({SENDER_BIO}).

The angle should be rooted in something concrete from the research — a recent milestone, what makes this company distinctive, a challenge they're working on, or an opportunity they're chasing. Not generic flattery.

Research:
{research_context}

angle: a short phrase describing the angle (e.g. "their recent expansion into healthcare", "the supply chain problem they're solving for NZ manufacturers"), no quotes"""

    angle_result = llm_structured(angle_prompt, _ANGLE_SCHEMA, schema_name="email_angle")
    angle = str(angle_result.get("angle") or "").strip().strip('"').strip("'")
    log(f"Email angle: {angle}", "agent")
    if check_cancelled(task_id, client):
        return

    # =========================================================================
    # STEP 4: Draft personalized email
    # =========================================================================
    log("Step 4: Drafting email...", "info")

    if not any(a["was_answered"] for a in info["answers"]):
        log("⚠️ Research came back thin — email may be less specific", "info")

    # Build greeting — only use a name if it's clearly identified as a person
    # (not just the first word of a "key people" answer, which is almost
    # always the company name itself, e.g. "Acme Corp is led by Jane Smith...")
    greeting_name = extract_greeting_name(key_person)
    greeting = f"Hi {greeting_name}" if greeting_name else "Hi"

    draft_prompt = f"""Write a short, professional introduction email to {company_name}.

The email should be anchored around this specific angle: {angle}

Context:
{research_context}
- Key person: {key_person}
- Sending to: {target_email}

About me (the sender):
- Name: {SENDER_NAME}
- {SENDER_BIO}

Guidelines:
- 3-5 sentences max — short and punchy
- Open by acknowledging the angle naturally, don't lead with "I noticed..."
- Be warm and direct, not stiff or corporate
- End with a soft call to action (open to a chat, happy to connect, etc.)
- No "I hope this email finds you well", no hollow compliments
- Do NOT include a greeting, signature, or subject line — those are added separately
- Do NOT invent or guess at names, emails, phone numbers, or other contact
  details that aren't given to you above — omit them rather than making them up

body: the email body only"""

    draft_result = llm_structured(draft_prompt, _DRAFT_BODY_SCHEMA, schema_name="email_draft_body")
    draft_body = clean_email_draft(str(draft_result.get("body") or ""))
    log(f"Draft:\n{draft_body}", "agent")

    # Content-safety gate, separate from the no-fabrication instruction above
    # — that guards against invented contact details, this guards against
    # presumptuous/offensive claims. This workflow auto-SENDS after human
    # confirmation (not just drafts, like lead_gen_outreach.py), so catching
    # this before the confirmation step matters even more here.
    safety = check_draft_appropriateness(draft_body, company_name)
    if not safety["appropriate"]:
        log(f"⚠️ Draft flagged: {safety['concern']} — regenerating once", "info")
        draft_result = llm_structured(
            draft_prompt + f"\n\nPREVIOUS ATTEMPT HAD THIS PROBLEM — avoid it this time: {safety['concern']}",
            _DRAFT_BODY_SCHEMA, schema_name="email_draft_body_retry",
        )
        draft_body = clean_email_draft(str(draft_result.get("body") or ""))
        safety = check_draft_appropriateness(draft_body, company_name)
        if not safety["appropriate"]:
            log(f"✗ Still flagged after retry: {safety['concern']} — aborting", "error")
            client.update_status(
                task_id, "failed",
                error_message=f"Draft repeatedly flagged for review: {safety['concern']}",
            )
            return

    subject_prompt = f"""Write a short email subject line for an introduction to {company_name}.

The email is about: {angle}

Under 50 characters. Be specific to this company, not generic. No emojis.

subject: the subject line only"""

    subject_result = llm_structured(subject_prompt, _SUBJECT_LINE_SCHEMA, schema_name="subject_line")
    subject_line = clean_subject_line(str(subject_result.get("subject") or ""))
    log(f"Subject: {subject_line}", "agent")

    full_email = f"""{greeting},

{draft_body}

Best regards,
{SENDER_NAME}
{SENDER_TITLE}
{SENDER_WEBSITE}"""

    # =========================================================================
    # STEP 5: Submit for approval
    # =========================================================================
    log("Step 5: Submitting for your approval...", "info")

    client.update_status(
        task_id,
        "awaiting_confirmation",
        draft_email=full_email,
        draft_subject=subject_line,
        draft_to=target_email,
    )
    log("📧 Email draft ready — check the website to approve or cancel", "success")

    # =========================================================================
    # STEP 6: Wait for confirmation
    # =========================================================================
    log("Waiting for your confirmation...", "info")

    waited = 0
    while waited < 300:
        import time
        time.sleep(5)
        waited += 5

        task = client.get_task(task_id)
        if not task:
            return

        status = task.get("status")
        if status == "confirmed":
            log("✅ Confirmed — sending email!", "success")
            break
        elif status == "cancelled":
            log("❌ Cancelled by user", "info")
            return
        elif status not in ("awaiting_confirmation", "confirmed"):
            log(f"Unexpected status: {status}", "error")
            return
    else:
        log("⏰ Timed out waiting for confirmation", "error")
        client.update_status(task_id, "failed", error_message="Timed out waiting for confirmation")
        return

    # =========================================================================
    # STEP 7: Send
    # =========================================================================
    log("Step 7: Sending email via Gmail...", "info")
    log(f"🔧 send_email(to={target_email}, subject={subject_line})", "tool_call")

    try:
        result = send_email(target_email, subject_line, full_email)
        log(f"📤 {result}", "tool_result")

        if "✅" in result:
            log("🎉 Email sent successfully!", "success")
            # SENDER_NAME is this workflow's only sender identity (no
            # per-request sender company the way lead_gen_outreach.py has) —
            # known limitation: the two workflows only share dedup/reply
            # history for the same real-world sender if these identity
            # strings happen to match, since there's no unified
            # signed-in-user identity yet.
            memory_record_outreach(
                SENDER_NAME, company_name, "drafted",
                target_email=target_email, subject_line=subject_line,
                angle=angle, draft_body=full_email,
            )
            client.update_status(task_id, "completed", result=f"Email sent to {target_email}")
        else:
            log(f"Email sending issue: {result}", "error")
            client.update_status(task_id, "failed", error_message=result)

    except Exception as e:
        log(f"Failed to send email: {e}", "error")
        client.update_status(task_id, "failed", error_message=str(e))
