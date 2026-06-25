# =============================================================================
# Workflow: Calendar Booking
# =============================================================================
# Drop this file in the workflows/ directory to enable it.
#
# What it does:
#   Parses a natural-language booking request, resolves the date/time,
#   confirms with the user conversationally, then creates a Google Calendar event.
# =============================================================================

import os
import re
import dateparser
from datetime import timedelta

from core import llm_structured, wait_for_input, check_cancelled, parse_duration
from schemas import s_object, s_string, s_bool

from tools_google import create_calendar_event

_PARSED_BOOKING_SCHEMA = s_object({
    "event_name": s_string(),
    "date_time":  s_string(),
    "duration":   s_string(),
    "location":   s_string(),
})

_CONFIRMATION_SCHEMA = s_object({"confirmed": s_bool()})

WORKFLOW_META = {
    "name": "calendar_booking",
    "description": (
        "Schedule, book, or add a calendar event or appointment. Use this when the "
        "request mentions booking, scheduling, adding to calendar, setting a meeting, "
        "or any time-based event. "
        "Can also be called programmatically by other workflows via book_directly(task_id, "
        "event_name, dt, duration_mins, location, client) when all details are already "
        "known — skips all parsing and confirmation questions."
    ),
}


def book_directly(
    task_id: str,
    event_name: str,
    dt,
    duration_mins: int = 60,
    location: str = '',
    client = None,
) -> bool:
    """
    Programmatic entry point — skips all parsing and confirmation.
    Call this from other workflows when you already have structured booking data.

    Args:
        task_id:       Parent task ID (for logging)
        event_name:    Title of the event
        dt:            datetime object for the start time
        duration_mins: Length in minutes (default 60)
        location:      Optional location string
        client:        VPSClient instance

    Returns:
        True on success, False on failure.
    """
    from datetime import timedelta

    def log(msg: str, log_type: str = 'info'):
        print(f'  [{log_type.upper()}] {msg}')
        if client:
            client.log(task_id, msg, log_type)

    dt_end = dt + timedelta(minutes=duration_mins)
    log(f'📅 Booking directly: "{event_name}" at {dt.isoformat()}', 'info')
    log(f'🔧 create_calendar_event({event_name}, {dt.isoformat()}, {dt_end.isoformat()})', 'tool_call')

    result = create_calendar_event(
        summary=event_name,
        start_time=dt.isoformat(),
        end_time=dt_end.isoformat(),
        description='Booked via Pipestream',
        location=location,
    )

    log(f'📤 {result}', 'tool_result')

    if '✅' in result:
        log(f'🎉 "{event_name}" booked successfully', 'success')
        return True
    else:
        log(f'Failed to book "{event_name}": {result}', 'error')
        return False


def run(task_id: str, input_text: str, client) -> None:

    TIMEZONE   = os.getenv("TIMEZONE", "Pacific/Auckland")
    MAX_RETRIES = 3

    def log(msg: str, log_type: str = "info"):
        print(f"  [{log_type.upper()}] {msg}")
        client.log(task_id, msg, log_type)

    log("📅 Starting calendar booking workflow", "info")

    original_input = input_text
    dt             = None
    event_name     = "Appointment"
    duration_mins  = 60
    location       = ""
    formatted_dt   = ""

    for attempt in range(MAX_RETRIES):

        # =====================================================================
        # STEP 1: Parse booking details
        # =====================================================================
        log(f"Step 1: Parsing booking details (attempt {attempt + 1})...", "info")

        parse_prompt = f"""Extract booking details from this request.

Request: "{input_text}"

event_name: name of the event or appointment, or "not found"
date_time: date and time exactly as stated, e.g. "next Monday at 3pm", or "not found"
duration: duration if stated, e.g. "1 hour", "30 minutes", or "not specified"
location: location if stated, or "not specified" """

        parsed = llm_structured(parse_prompt, _PARSED_BOOKING_SCHEMA, schema_name="parsed_booking")
        log(f"Parsed: {parsed}", "agent")
        if check_cancelled(task_id, client):
            return

        event_name     = str(parsed.get("event_name") or "").strip()
        date_time_text = str(parsed.get("date_time") or "").strip()
        duration_str   = str(parsed.get("duration") or "").strip()
        location_str   = str(parsed.get("location") or "").strip()

        if not event_name or event_name.lower() == "not found":
            event_name = "Appointment"
        location      = "" if location_str.lower() in ("not found", "not specified", "") else location_str
        duration_mins = parse_duration(duration_str)

        # =====================================================================
        # STEP 2: Resolve natural language date to absolute datetime
        # =====================================================================
        log(f"Step 2: Resolving date — '{date_time_text}'", "info")

        dt = dateparser.parse(
            date_time_text,
            settings={
                "PREFER_DATES_FROM": "future",
                "TIMEZONE":           TIMEZONE,
                "RETURN_AS_TIMEZONE_AWARE": False,
            },
        )

        if not dt:
            log(f"Could not parse date: {date_time_text}", "error")
            answer = wait_for_input(
                task_id,
                f"I couldn't understand the date \"{date_time_text}\". "
                f"When would you like to schedule \"{event_name}\"? "
                f"(e.g. \"next Monday at 3pm\")",
                client,
            )
            if not answer:
                return
            input_text = f"{original_input} — date clarification: {answer}"
            continue

        log(f"Resolved: {dt.strftime('%A %d %B %Y at %H:%M')}", "info")
        if check_cancelled(task_id, client):
            return

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
        confirm_result = llm_structured(
            f'Is this response a "yes" or confirmation?\nResponse: "{answer}"',
            _CONFIRMATION_SCHEMA,
            schema_name="booking_confirmation",
        )
        is_confirmed = bool(confirm_result.get("confirmed"))

        if is_confirmed:
            log("✅ Confirmed — creating event", "success")
            break

        log(f"Got correction: {answer} — re-parsing", "info")
        input_text = f"{original_input} — correction: {answer}"

    else:
        log("Could not confirm booking after multiple attempts", "error")
        client.update_status(
            task_id, "failed",
            error_message="Could not confirm booking details. Please try again with more detail.",
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
        location=location,
    )

    log(f"📤 {result}", "tool_result")

    if "✅" in result:
        log("🎉 Event created successfully!", "success")
        client.update_status(task_id, "completed",
                             result=f'"{event_name}" booked for {formatted_dt}')
    else:
        log(f"Failed to create event: {result}", "error")
        client.update_status(task_id, "failed", error_message=result)
