# =============================================================================
# workflows.py — Orchestrated Workflows
# =============================================================================
# THIS IS THE KEY FILE — Code controls flow, LLM just does the thinking.
#
# The business_intro_workflow:
#   1. Parse the user input (extract company name, email)
#   2. Research the company (multiple web searches)
#   3. Extract key info (company summary, recent news, key people)
#   4. Draft personalized email
#   5. Submit for user approval
#   6. Wait for confirmation
#   7. Send email
#
# Each step is a separate LLM call with a specific, focused prompt.
# The LLM doesn't decide what to do next — the code does.
# =============================================================================

import os
import re
import time
import json
import dateparser
from datetime import datetime, timedelta

from langchain_openai import ChatOpenAI
from tools_web import web_search, scrape_url
from tools_google import send_email, create_calendar_event

# Sender identity — set these in .env
SENDER_NAME    = os.getenv("SENDER_NAME", "")
SENDER_TITLE   = os.getenv("SENDER_TITLE", "")
SENDER_WEBSITE = os.getenv("SENDER_WEBSITE", "")
SENDER_BIO     = os.getenv("SENDER_BIO", "")

# =============================================================================
# LLM SETUP
# =============================================================================

llm = ChatOpenAI(
    model=os.getenv("OPENAI_MODEL", "local-model"),
    base_url=os.getenv("OPENAI_API_BASE", "http://host.docker.internal:1234/v1"),
    api_key=os.getenv("OPENAI_API_KEY", "not-needed"),
    temperature=0.7  # Slightly creative for email writing
)

def llm_call(prompt: str) -> str:
    """Simple LLM call — just text in, text out."""
    response = llm.invoke(prompt)
    return response.content

def wait_for_input(task_id: str, question: str, client, timeout: int = 300) -> str | None:
    """
    Post a question to the user and wait for their reply.

    Sets status to awaiting_input, clears any previous user_input, then polls
    until the user submits an answer via the web UI. Returns the answer string,
    or None on timeout or cancellation.
    """
    # Check trust flag before asking — skip the question entirely if trusted
    task = client.get_task(task_id)
    if task and task.get("trusted"):
        client.log(task_id, f"💬 (trusted) Skipping: {question}", "info")
        client.log(task_id, "✅ Proceeding autonomously", "success")
        return "yes"

    client.update_status(task_id, "awaiting_input", pending_question=question, user_input="")
    client.log(task_id, f"💬 {question}", "info")

    waited = 0
    while waited < timeout:
        time.sleep(5)
        waited += 5

        task = client.get_task(task_id)
        if not task:
            return None

        # Trust may have been granted while we were waiting
        if task.get("trusted") and task.get("status") == "running":
            client.log(task_id, "✅ Trust granted — proceeding autonomously", "success")
            return "yes"

        status = task.get("status")

        if status == "running":
            answer = task.get("user_input", "")
            if answer:
                client.log(task_id, f"💬 You replied: {answer}", "info")
                return answer

        if status in ("cancelled", "failed"):
            return None

    client.log(task_id, "⏰ Timed out waiting for reply", "error")
    client.update_status(task_id, "failed", error_message="Timed out waiting for user reply")
    return None

# =============================================================================
# SHARED HELPERS
# =============================================================================

def check_cancelled(task_id: str, client) -> bool:
    """Returns True if the task has been cancelled externally."""
    task = client.get_task(task_id)
    return bool(task and task.get("status") in ("cancelled", "failed"))

def extract_field(text: str, field: str) -> str:
    """Pull a labelled field from LLM-structured output."""
    match = re.search(rf"{field}:\s*(.+)", text, re.IGNORECASE)
    return match.group(1).strip() if match else "not found"

def parse_duration(duration_str: str) -> int:
    """Convert a duration string to minutes. Defaults to 60."""
    if not duration_str or duration_str.lower() in ("not specified", "not found"):
        return 60
    s = duration_str.lower()
    hours = re.search(r'(\d+(?:\.\d+)?)\s*h', s)
    mins  = re.search(r'(\d+)\s*m', s)
    total = 0
    if hours:
        total += int(float(hours.group(1)) * 60)
    if mins:
        total += int(mins.group(1))
    return total if total > 0 else 60

# =============================================================================
# DEEP RESEARCH
# =============================================================================

def deep_research(company_name: str, task_id: str, client, log) -> dict:
    """
    Multi-round recursive research:
      1. Discovery searches → collect URLs
      2. LLM selects best URLs to scrape
      3. Scrape selected pages
      4. LLM evaluates gaps → issues targeted follow-up searches
      5. Scrape follow-up URLs if budget allows
      6. Final structured extraction

    Returns dict: company_summary, recent_news, key_person, contact_email,
                  confidence, raw (extracted text), all_content (full corpus).
    Returns empty dict if task is cancelled mid-research.
    """
    MAX_SCRAPES = 4

    all_snippets: list[str] = []
    all_scraped:  list[str] = []
    scrape_count  = 0

    # -------------------------------------------------------------------------
    # Round 1 — Discovery searches
    # -------------------------------------------------------------------------
    log("🔬 Round 1: Discovery searches", "info")

    discovery_queries = [
        f"{company_name} New Zealand",
        f"{company_name} about leadership team",
        f"{company_name} contact email",
    ]

    candidate_urls: list[str] = []

    for query in discovery_queries:
        log(f"🔍 {query}", "tool_call")
        result = web_search(query, max_results=5)
        all_snippets.append(f"Search: {query}\n{result}")
        urls = re.findall(r'^(https?://\S+)', result, re.MULTILINE)
        candidate_urls.extend(urls)
        log(f"Got {len(result)} chars, {len(urls)} URLs", "tool_result")
        if check_cancelled(task_id, client):
            return {}

    # -------------------------------------------------------------------------
    # LLM picks best URLs to scrape
    # -------------------------------------------------------------------------
    unique_urls = list(dict.fromkeys(candidate_urls))[:15]
    url_list = "\n".join(f"- {u}" for u in unique_urls)

    select_prompt = f"""From these URLs found while researching "{company_name}", select up to 3 that are most likely to contain rich information (company website, About/Team page, news article, contact page). Prefer official company domains.

URLs:
{url_list}

Reply with ONLY a JSON array of up to 3 URLs:
["https://...", "https://..."]"""

    try:
        selected_raw = llm_call(select_prompt)
        json_match = re.search(r'\[.*?\]', selected_raw, re.DOTALL)
        selected_urls: list[str] = json.loads(json_match.group())[:3] if json_match else unique_urls[:3]
    except Exception:
        selected_urls = unique_urls[:3]

    # -------------------------------------------------------------------------
    # Scrape selected URLs
    # -------------------------------------------------------------------------
    for url in selected_urls:
        if scrape_count >= MAX_SCRAPES:
            break
        log(f"🌐 Scraping: {url}", "tool_call")
        content = scrape_url(url, max_chars=4000)
        all_scraped.append(f"FROM {url}:\n{content}")
        scrape_count += 1
        log(f"Got {len(content)} chars", "tool_result")
        if check_cancelled(task_id, client):
            return {}

    # -------------------------------------------------------------------------
    # Round 2 — LLM evaluates gaps, issues follow-up searches
    # -------------------------------------------------------------------------
    log("🔬 Round 2: Evaluating research gaps", "info")

    combined = "\n\n---\n\n".join(all_snippets + all_scraped)

    evaluate_prompt = f"""You are researching "{company_name}" to write a personalized intro email. Evaluate what you know and what's still missing.

Research so far:
{combined[:10000]}

Respond in this exact format:
CONFIDENCE: <high/medium/low>
WHAT_WE_KNOW: <one sentence summary of what is clear>
MISSING: <what is still unknown, e.g. "key person name", "recent news", "contact email", or "nothing">
FOLLOW_UP_SEARCHES: <up to 2 specific search queries to fill the gaps, comma-separated, or "none">"""

    evaluation = llm_call(evaluate_prompt)
    log(f"Gap analysis:\n{evaluation}", "agent")
    if check_cancelled(task_id, client):
        return {}

    confidence    = extract_field(evaluation, "CONFIDENCE")
    follow_up_raw = extract_field(evaluation, "FOLLOW_UP_SEARCHES")

    # -------------------------------------------------------------------------
    # Follow-up searches (only if confidence isn't already high)
    # -------------------------------------------------------------------------
    if confidence.lower() != "high" and follow_up_raw.lower() not in ("none", "not found", ""):
        follow_up_queries = [
            q.strip().strip('"').strip("'")
            for q in re.split(r',|\n', follow_up_raw)
            if q.strip() and q.strip().lower() not in ("none", "not found")
        ][:2]

        for query in follow_up_queries:
            log(f"🔍 Follow-up: {query}", "tool_call")
            result = web_search(query, max_results=4)
            all_snippets.append(f"Follow-up search: {query}\n{result}")
            log(f"Got {len(result)} chars", "tool_result")
            if check_cancelled(task_id, client):
                return {}

            # Scrape the top URL from each follow-up if budget allows
            if scrape_count < MAX_SCRAPES:
                follow_urls = re.findall(r'^(https?://\S+)', result, re.MULTILINE)
                if follow_urls:
                    url = follow_urls[0]
                    log(f"🌐 Scraping follow-up: {url}", "tool_call")
                    content = scrape_url(url, max_chars=3000)
                    all_scraped.append(f"FROM {url}:\n{content}")
                    scrape_count += 1
                    log(f"Got {len(content)} chars", "tool_result")
                    if check_cancelled(task_id, client):
                        return {}

    # -------------------------------------------------------------------------
    # Final structured extraction
    # -------------------------------------------------------------------------
    log("📋 Final extraction pass", "info")

    all_content = "\n\n---\n\n".join(all_snippets + all_scraped)

    extract_prompt = f"""Based on all this research about "{company_name}", extract the following fields precisely.

COMPANY_SUMMARY: What does this company do? (1-2 sentences)
RECENT_NEWS: Any recent news, achievements, funding, or milestones? (1 sentence, or "none found")
KEY_PERSON: Name and title of the most senior person found (CEO, founder, director)? (or "not found")
CONTACT_EMAIL: Any contact email address? (or "not found")
CONFIDENCE: Overall confidence in accuracy of the above (high/medium/low)

Research:
{all_content[:12000]}

Respond in the exact format above:"""

    extracted = llm_call(extract_prompt)
    log(f"Research complete:\n{extracted}", "agent")

    return {
        "company_summary": extract_field(extracted, "COMPANY_SUMMARY"),
        "recent_news":     extract_field(extracted, "RECENT_NEWS"),
        "key_person":      extract_field(extracted, "KEY_PERSON"),
        "contact_email":   extract_field(extracted, "CONTACT_EMAIL"),
        "confidence":      extract_field(extracted, "CONFIDENCE"),
        "raw":             extracted,
        "all_content":     all_content,
    }

# =============================================================================
# BUSINESS INTRO WORKFLOW
# =============================================================================

def business_intro_workflow(task_id: str, input_text: str, client):
    """
    Main workflow: Research company, draft email, get approval, send.

    Args:
        task_id: Unique task identifier
        input_text: User's original request
        client: VPSClient for logging and status updates
    """

    def log(msg: str, log_type: str = "info"):
        print(f"  [{log_type.upper()}] {msg}")
        client.log(task_id, msg, log_type)

    # =========================================================================
    # STEP 1: Parse the user input
    # =========================================================================
    log("Step 1: Parsing input...", "info")

    parse_prompt = f"""Extract the following from this request:
- Company name to research
- Email address to send to (if provided)

Request: "{input_text}"

Respond in this exact format:
COMPANY: <company name>
EMAIL: <email address or "not provided">

Examples:
Request: "Research Acme Corp and send intro to bob@acme.com"
COMPANY: Acme Corp
EMAIL: bob@acme.com

Request: "Look up TechStartup Wellington"
COMPANY: TechStartup Wellington
EMAIL: not provided

Now parse the request:"""

    parsed = llm_call(parse_prompt)
    log(f"Parsed: {parsed}", "agent")

    company_match = re.search(r"COMPANY:\s*(.+)", parsed, re.IGNORECASE)
    email_match   = re.search(r"EMAIL:\s*(.+)", parsed, re.IGNORECASE)

    company_name = company_match.group(1).strip() if company_match else None
    target_email = email_match.group(1).strip() if email_match else None

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
    # STEP 2: Deep recursive research
    # =========================================================================
    log("Step 2: Starting deep research...", "info")

    research = deep_research(company_name, task_id, client, log)
    if not research:
        return  # cancelled mid-research

    company_summary = research["company_summary"]
    recent_news     = research["recent_news"]
    key_person      = research["key_person"]
    found_email     = research["contact_email"]
    confidence      = research["confidence"]

    # Use found email if we didn't have one
    if not target_email and found_email and found_email.lower() != "not found":
        if "@" in found_email:
            target_email = found_email
            log(f"Found email from research: {target_email}", "success")

    if not target_email:
        log("No email address found or provided", "error")
        client.update_status(task_id, "failed",
                             error_message="Could not find email address. Please provide one.",
                             company_research=research["raw"])
        return

    client.update_status(task_id, "running", company_research=research["raw"])
    
    # =========================================================================
    # STEP 3: Draft personalized email
    # =========================================================================
    log("Step 3: Drafting email...", "info")

    if confidence.lower() == "low":
        log("⚠️ Low confidence in research results", "info")
    
    draft_prompt = f"""Write a short, professional introduction email to {company_name}.

Context:
- Company: {company_name}
- What they do: {company_summary}
- Recent news: {recent_news}
- Key person: {key_person}
- Sending to: {target_email}

About me (the sender):
- My name is {SENDER_NAME}
- {SENDER_BIO}

Guidelines:
- Keep it SHORT: 3-5 sentences max
- Be professional but warm, not stiff or corporate
- If there's recent news, briefly acknowledge it
- Don't be sycophantic or over-the-top
- End with a soft call to action (open to chat, happy to discuss, etc.)
- Don't use phrases like "I hope this email finds you well"
- Ensure to NOT include a signature or "regards" or anything like that
- Ensure to NOT include a "Hello" or "Hi" or any greeting

Write ONLY the email body (no subject line, no "Dear X", no signature — I'll add those):"""

    draft_body = llm_call(draft_prompt)
    log(f"Draft body:\n{draft_body}", "agent")
    
    # Generate subject line
    subject_prompt = f"""Write a short, compelling email subject line for an introduction email to {company_name}.
    
The email mentions: {recent_news if recent_news.lower() != "none found" else company_summary}

Keep it under 50 characters. Be specific, not generic. No emojis.
Write ONLY the subject line, nothing else:"""

    subject_line = llm_call(subject_prompt).strip().strip('"')
    log(f"Subject: {subject_line}", "agent")
    
    # Compose full email
    greeting = f"Hi"
    if key_person and key_person.lower() != "not found":
        # Try to extract first name
        first_name = key_person.split()[0] if " " in key_person else key_person
        if first_name.lower() not in ["ceo", "founder", "manager", "director", "not"]:
            greeting = f"Hi {first_name}"
    
    full_email = f"""{greeting},

{draft_body.strip()}

Best regards,
{SENDER_NAME}
{SENDER_TITLE}
{SENDER_WEBSITE}"""

    log(f"Full email composed", "success")
    
    # =========================================================================
    # STEP 4: Submit for approval
    # =========================================================================
    log("Step 4: Submitting for your approval...", "info")
    
    client.update_status(
        task_id, 
        "awaiting_confirmation",
        draft_email=full_email,
        draft_subject=subject_line,
        draft_to=target_email
    )
    
    log("📧 Email draft ready — check the website to approve or cancel", "success")
    
    # =========================================================================
    # STEP 5: Wait for confirmation
    # =========================================================================
    log("Waiting for your confirmation...", "info")
    
    max_wait = 300  # 5 minutes
    waited = 0
    
    while waited < max_wait:
        time.sleep(5)
        waited += 5
        
        # Check task status
        task = client.get_task(task_id)
        if not task:
            log("Task not found", "error")
            return
        
        status = task.get("status")
        
        if status == "confirmed":
            log("✅ User confirmed — sending email!", "success")
            break
        elif status == "cancelled":
            log("❌ User cancelled — not sending", "info")
            client.update_status(task_id, "cancelled", result="Cancelled by user")
            return
        elif status not in ("awaiting_confirmation", "confirmed"):
            log(f"Unexpected status: {status}", "error")
            return
    
    if waited >= max_wait:
        log("⏰ Timed out waiting for confirmation", "error")
        client.update_status(task_id, "failed", error_message="Timed out waiting for user confirmation")
        return
    
    # =========================================================================
    # STEP 6: Send the email
    # =========================================================================
    log("Step 6: Sending email via Gmail...", "info")
    log(f"🔧 send_email(to={target_email}, subject={subject_line})", "tool_call")
    
    try:
        result = send_email(target_email, subject_line, full_email)
        log(f"📤 {result}", "tool_result")
        
        if "✅" in result:
            log("🎉 Email sent successfully!", "success")
            client.update_status(task_id, "completed", 
                               result=f"Email sent to {target_email}")
        else:
            log(f"Email sending issue: {result}", "error")
            client.update_status(task_id, "failed", error_message=result)
            
    except Exception as e:
        log(f"Failed to send email: {e}", "error")
        client.update_status(task_id, "failed", error_message=str(e))

# =============================================================================
# CALENDAR BOOKING WORKFLOW
# =============================================================================

def calendar_booking_workflow(task_id: str, input_text: str, client):
    """
    Conversational calendar booking.
    Parses the request, resolves the date, confirms with user, creates event.
    Retries up to 3 times if the user provides corrections.
    """

    TIMEZONE = os.getenv("TIMEZONE", "Pacific/Auckland")
    MAX_RETRIES = 3

    def log(msg: str, log_type: str = "info"):
        print(f"  [{log_type.upper()}] {msg}")
        client.log(task_id, msg, log_type)

    log("📅 Starting calendar booking workflow", "info")

    original_input = input_text
    dt = None
    event_name = "Appointment"
    duration_mins = 60
    location = ""
    formatted_dt = ""

    for attempt in range(MAX_RETRIES):

        # =====================================================================
        # STEP 1: Parse booking details
        # =====================================================================
        log(f"Step 1: Parsing booking details (attempt {attempt + 1})...", "info")

        parse_prompt = f"""Extract booking details from this request.

Request: "{input_text}"

Respond in this exact format:
EVENT_NAME: <name of the event or appointment>
DATE_TIME: <date and time exactly as stated, e.g. "next Monday at 3pm">
DURATION: <duration if stated, e.g. "1 hour", "30 minutes", or "not specified">
LOCATION: <location if stated, or "not specified">

Now parse the request:"""

        parsed = llm_call(parse_prompt)
        log(f"Parsed: {parsed}", "agent")
        if check_cancelled(task_id, client): return

        event_name    = extract_field(parsed, "EVENT_NAME")
        date_time_text = extract_field(parsed, "DATE_TIME")
        duration_str  = extract_field(parsed, "DURATION")
        location_str  = extract_field(parsed, "LOCATION")

        if event_name.lower() == "not found":
            event_name = "Appointment"
        location = "" if location_str.lower() in ("not found", "not specified") else location_str
        duration_mins = parse_duration(duration_str)

        # =====================================================================
        # STEP 2: Resolve natural language date to absolute datetime
        # =====================================================================
        log(f"Step 2: Resolving date — '{date_time_text}'", "info")

        dt = dateparser.parse(
            date_time_text,
            settings={
                'PREFER_DATES_FROM': 'future',
                'TIMEZONE': TIMEZONE,
                'RETURN_AS_TIMEZONE_AWARE': False,
            }
        )

        if not dt:
            log(f"Could not parse date: {date_time_text}", "error")
            answer = wait_for_input(
                task_id,
                f"I couldn't understand the date \"{date_time_text}\". "
                f"When would you like to schedule \"{event_name}\"? "
                f"(e.g. \"next Monday at 3pm\")",
                client
            )
            if not answer:
                return
            input_text = f"{original_input} — date clarification: {answer}"
            continue

        log(f"Resolved: {dt.strftime('%A %d %B %Y at %H:%M')}", "info")
        if check_cancelled(task_id, client): return

        # =====================================================================
        # STEP 3: Confirm with user
        # =====================================================================
        day_str  = dt.strftime("%A %d %B").lstrip("0")
        time_str = dt.strftime("%I:%M %p").lstrip("0")
        formatted_dt = f"{day_str} at {time_str}"

        if duration_mins >= 60 and duration_mins % 60 == 0:
            hrs = duration_mins // 60
            duration_label = f"{hrs} hr{'s' if hrs > 1 else ''}"
        else:
            duration_label = f"{duration_mins} min"

        confirm_q = f'Book "{event_name}" — {formatted_dt} ({duration_label})'
        if location:
            confirm_q += f" at {location}"
        confirm_q += "?"

        answer = wait_for_input(task_id, confirm_q, client)
        if not answer:
            return

        # =====================================================================
        # STEP 4: Check if confirmed or a correction
        # =====================================================================
        is_yes_prompt = f'Is this response a "yes" or confirmation?\nResponse: "{answer}"\nReply with only YES or NO:'
        is_confirmed = "YES" in llm_call(is_yes_prompt).upper()

        if is_confirmed:
            log("✅ Confirmed — creating event", "success")
            break

        log(f"Got correction: {answer}", "info")
        log("Re-parsing with correction...", "info")
        input_text = f"{original_input} — correction: {answer}"

    else:
        log("Could not confirm booking after multiple attempts", "error")
        client.update_status(
            task_id, "failed",
            error_message="Could not confirm booking details. Please try again with more detail."
        )
        return

    # =========================================================================
    # STEP 5: Create the calendar event
    # =========================================================================
    log("Step 5: Creating calendar event...", "info")

    dt_end = dt + timedelta(minutes=duration_mins)
    log(f"🔧 create_calendar_event({event_name}, {dt.isoformat()}, {dt_end.isoformat()})", "tool_call")

    result = create_calendar_event(
        summary=event_name,
        start_time=dt.isoformat(),
        end_time=dt_end.isoformat(),
        description="Booked via Pipestream",
        location=location
    )

    log(f"📤 {result}", "tool_result")

    if "✅" in result:
        log("🎉 Event created successfully!", "success")
        client.update_status(task_id, "completed",
                             result=f'"{event_name}" booked for {formatted_dt}')
    else:
        log(f"Failed to create event: {result}", "error")
        client.update_status(task_id, "failed", error_message=result)
