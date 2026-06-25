# =============================================================================
# Workflow: Email Triage
# =============================================================================
# Fetches the 10 most recent non-promotional inbox emails (Gmail category
# filtering excludes Promotions / Updates / Social automatically), classifies
# each as reply_needed / book_appointment / fyi / handled, then drafts and
# sends replies or books calendar events as needed.
# =============================================================================

import re
import dateparser

from core import (
    llm_structured,
    wait_for_input, check_cancelled,
    SENDER_NAME, SENDER_TITLE, SENDER_WEBSITE,
)
from schemas import s_object, s_string, s_enum, s_array
from tools_google import get_recent_emails, get_thread_text, send_reply

WORKFLOW_META = {
    "name": "email_triage",
    "description": (
        "Read, classify, and action recent emails. Fetches the 10 most recent "
        "non-promotional inbox emails, classifies each as reply_needed, "
        "book_appointment, fyi, or handled, drafts replies for approval, and "
        "hands scheduling requests off to calendar_booking automatically. "
        "Use when the request mentions checking email, triaging inbox, reading "
        "messages, or catching up on email."
    ),
}

ACTIONS = ("reply_needed", "book_appointment", "reply_and_book", "fyi", "handled")

_CLASSIFICATION_SCHEMA = s_object({"classifications": s_array(s_enum(list(ACTIONS)))})

_BOOKING_EXTRACT_SCHEMA = s_object({
    "event_name": s_string(),
    "date_time":  s_string(),
    "duration":   s_string(),
    "location":   s_string(),
    "confidence": s_enum(["high", "medium", "low"]),
})

_CLARIFICATION_EXTRACT_SCHEMA = s_object({
    "event_name": s_string(),
    "date_time":  s_string(),
    "duration":   s_string(),
})

_REPLY_INTENT_SCHEMA = s_object({"action": s_enum(["YES", "SKIP", "EDIT"])})
_DRAFT_BODY_SCHEMA   = s_object({"body": s_string()})

_NOT_SPECIFIED = ('not found', 'not specified', '')


# =============================================================================
# MAIN WORKFLOW
# =============================================================================

def run(task_id: str, input_text: str, client) -> None:

    def log(msg: str, log_type: str = 'info'):
        print(f'  [{log_type.upper()}] {msg}')
        client.log(task_id, msg, log_type)

    # =========================================================================
    # STEP 1: Fetch 10 recent non-promotional emails
    # =========================================================================
    log('📬 Fetching 10 recent inbox emails (excluding promotions)...', 'info')

    try:
        emails = get_recent_emails(max_results=10)
    except Exception as e:
        log(f'Failed to fetch emails: {e}', 'error')
        client.update_status(task_id, 'failed', error_message=str(e))
        return

    log(f'Fetched {len(emails)} emails', 'tool_result')

    if not emails:
        client.update_status(task_id, 'completed', result='No emails found.')
        return

    if check_cancelled(task_id, client):
        return

    # =========================================================================
    # STEP 2: Classify all emails in one batch
    # =========================================================================
    log('📊 Classifying emails...', 'info')

    lines = '\n'.join(
        f'{i}. {e["sender"]} | {e["subject"]}' for i, e in enumerate(emails, 1)
    )
    classify_prompt = f"""Classify each email. Use only: reply_needed, book_appointment, reply_and_book, fyi, handled.
reply_needed: real person expecting a reply, no scheduling involved.
book_appointment: automated booking notification (from a booking system) with clear date/time in the email.
reply_and_book: real person requesting a meeting/call/catchup — needs both a reply AND a calendar event booked (e.g. "can we meet Thursday at 3pm?").
fyi: informational only, no reply needed.
handled: already replied or no action needed.

{lines}

List the action for each email above, in the same order."""

    classification_result = llm_structured(classify_prompt, _CLASSIFICATION_SCHEMA, schema_name="email_classifications")
    log(f'Classifications: {classification_result}', 'agent')

    classifications = classification_result.get('classifications') or []
    actions = list(classifications[:len(emails)]) + ['fyi'] * max(0, len(emails) - len(classifications))
    for email, action in zip(emails, actions):
        email['action'] = action if action in ACTIONS else 'fyi'

    tally = {a: 0 for a in ACTIONS}
    for e in emails:
        tally[e['action']] += 1

    log(
        f"📊 {tally['reply_needed']} reply | "
        f"{tally['book_appointment']} book | "
        f"{tally['fyi']} fyi | "
        f"{tally['handled']} handled",
        'info'
    )

    if check_cancelled(task_id, client):
        return

    # =========================================================================
    # STEP 3: Process action items
    # =========================================================================
    action_emails = [e for e in emails if e['action'] in ('reply_needed', 'book_appointment', 'reply_and_book')]

    if not action_emails:
        client.update_status(
            task_id, 'completed',
            result=f'Checked {len(emails)} emails — nothing needs action.'
        )
        return

    log(f'Processing {len(action_emails)} action item(s)...', 'info')

    replies_sent  = 0
    bookings_made = 0
    skipped       = 0

    for email in action_emails:
        if check_cancelled(task_id, client):
            return

        sender  = email['sender']
        subject = email['subject']
        action  = email['action']

        log(f'─── {action.upper()}: {subject[:60]} (from {sender})', 'info')

        log('🌐 Fetching thread...', 'tool_call')
        try:
            thread_text = get_thread_text(email['thread_id'])
            log(f'Got {len(thread_text)} chars', 'tool_result')
        except Exception as e:
            log(f'Could not fetch thread: {e}', 'error')
            skipped += 1
            continue

        # =====================================================================
        # Book appointment
        # =====================================================================
        if action == 'book_appointment':
            log('📅 Extracting booking details...', 'info')

            extract_prompt = f"""Extract booking details from this email thread.

Thread:
{thread_text}

event_name: what is being booked, or "not found"
date_time: date and time as stated, or "not found"
duration: if mentioned, or "not specified"
location: if mentioned, or "not specified"
confidence: high, medium, or low"""

            extracted     = llm_structured(extract_prompt, _BOOKING_EXTRACT_SCHEMA, schema_name="booking_extract")
            log(f'Extracted: {extracted}', 'agent')

            event_name    = str(extracted.get('event_name') or '').strip()
            date_time_str = str(extracted.get('date_time') or '').strip()
            duration_str  = str(extracted.get('duration') or '').strip()
            location_str  = str(extracted.get('location') or '').strip()

            location      = '' if location_str.lower() in _NOT_SPECIFIED else location_str

            from core import parse_duration
            duration_mins = parse_duration(duration_str)

            dt = dateparser.parse(
                date_time_str,
                settings={'PREFER_DATES_FROM': 'future', 'RETURN_AS_TIMEZONE_AWARE': False},
            ) if date_time_str.lower() not in _NOT_SPECIFIED else None

            missing = []
            if not dt:
                missing.append('date and time')
            if event_name.lower() in _NOT_SPECIFIED:
                missing.append('event name')

            if missing:
                answer = wait_for_input(
                    task_id,
                    f'Scheduling request from {sender} re "{subject}" — '
                    f'missing: {", ".join(missing)}. '
                    f'Please provide (e.g. "Team standup, Monday 9am"):',
                    client,
                )
                if not answer:
                    return

                # "yes" means trusted auto-skip — no real details provided
                if answer.strip().lower() == 'yes':
                    log(f'Missing booking details for "{subject}" — skipping', 'info')
                    skipped += 1
                    continue

                re_extract = llm_structured(
                    f'Extract booking details from this clarification.\n'
                    f'Clarification: "{answer}"\n'
                    f'Original context: {event_name}, {date_time_str}\n\n'
                    f'event_name: the event name\n'
                    f'date_time: the date and time\n'
                    f'duration: the duration, or "not specified"',
                    _CLARIFICATION_EXTRACT_SCHEMA,
                    schema_name="booking_clarification",
                )
                event_name    = str(re_extract.get('event_name') or '').strip() or event_name
                date_time_str = str(re_extract.get('date_time') or '').strip()
                duration_mins = parse_duration(str(re_extract.get('duration') or ''))
                dt = dateparser.parse(
                    date_time_str,
                    settings={'PREFER_DATES_FROM': 'future', 'RETURN_AS_TIMEZONE_AWARE': False},
                )

            if not dt:
                log(f'Could not resolve date for "{subject}" — skipping', 'error')
                skipped += 1
                continue

            if check_cancelled(task_id, client):
                return

            import importlib.util, os as _os
            spec = importlib.util.spec_from_file_location(
                'calendar_booking',
                _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'calendar_booking.py')
            )
            cal_mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cal_mod)

            success = cal_mod.book_directly(
                task_id=task_id,
                event_name=event_name,
                dt=dt,
                duration_mins=duration_mins,
                location=location,
                client=client,
            )
            bookings_made += 1 if success else 0
            if not success:
                skipped += 1

        # =====================================================================
        # Reply needed
        # =====================================================================
        elif action == 'reply_needed':
            log(f'✍️  Drafting reply to {sender}...', 'info')

            draft_result = llm_structured(
                f'Draft a reply to this email thread on behalf of {SENDER_NAME}.\n\n'
                f'Thread:\n{thread_text}\n\n'
                f'Guidelines:\n'
                f'- Match the tone and formality of the conversation\n'
                f'- Be concise — answer what is being asked, nothing extra\n'
                f'- Do NOT include a greeting or sign-off\n\n'
                f'body: the reply body only',
                _DRAFT_BODY_SCHEMA,
                schema_name="reply_draft",
            )
            draft_body = str(draft_result.get('body') or '')

            email_match = re.search(r'[\w.+-]+@[\w.-]+\.[a-z]{2,}', sender)
            reply_to    = email_match.group() if email_match else sender
            full_reply  = f"{draft_body.strip()}\n\n{SENDER_NAME}\n{SENDER_TITLE}\n{SENDER_WEBSITE}"

            answer = wait_for_input(
                task_id,
                f'Reply to {sender}\nRe: "{subject}"\n\n'
                f'{"─" * 40}\n{full_reply}\n{"─" * 40}\n\n'
                f'Send? (yes / skip / paste edit instructions)',
                client,
            )
            if not answer:
                return

            intent_result = llm_structured(
                f'Classify this as YES, SKIP, or EDIT:\n"{answer}"',
                _REPLY_INTENT_SCHEMA,
                schema_name="reply_intent",
            )
            intent = str(intent_result.get('action') or '').upper()

            if intent == 'EDIT':
                log(f'Re-drafting with edits...', 'info')
                draft_result = llm_structured(
                    f'Rewrite this email reply based on the instructions.\n\n'
                    f'Original:\n{draft_body}\n\n'
                    f'Instructions: {answer}\n\n'
                    f'body: the revised body only',
                    _DRAFT_BODY_SCHEMA,
                    schema_name="reply_draft_revision",
                )
                draft_body = str(draft_result.get('body') or '')
                full_reply = f"{draft_body.strip()}\n\n{SENDER_NAME}\n{SENDER_TITLE}\n{SENDER_WEBSITE}"
            elif intent != 'YES':
                log(f'Skipped reply to {sender}', 'info')
                skipped += 1
                continue

            if check_cancelled(task_id, client):
                return

            log(f'📤 Sending reply to {reply_to}...', 'tool_call')
            result = send_reply(
                to=reply_to,
                subject=subject,
                body=full_reply,
                thread_id=email['thread_id'],
                in_reply_to=email.get('message_id_header', ''),
            )
            log(result, 'tool_result' if '✅' in result else 'error')

            if '✅' in result:
                replies_sent += 1
            else:
                skipped += 1

        # =====================================================================
        # Reply and book — real person requesting a meeting; do both
        # =====================================================================
        elif action == 'reply_and_book':
            log('📅 Extracting booking details for reply_and_book...', 'info')

            from core import parse_duration
            import importlib.util, os as _os

            extracted = llm_structured(
                f'Extract booking details from this email thread.\n\n'
                f'Thread:\n{thread_text}\n\n'
                f'event_name: what is being booked, or "not found"\n'
                f'date_time: date and time as stated, or "not found"\n'
                f'duration: if mentioned, or "not specified"\n'
                f'location: if mentioned, or "not specified"\n'
                f'confidence: high, medium, or low',
                _BOOKING_EXTRACT_SCHEMA,
                schema_name="booking_extract",
            )
            log(f'Extracted: {extracted}', 'agent')

            event_name    = str(extracted.get('event_name') or '').strip()
            date_time_str = str(extracted.get('date_time') or '').strip()
            duration_str  = str(extracted.get('duration') or '').strip()
            location_str  = str(extracted.get('location') or '').strip()
            location      = '' if location_str.lower() in _NOT_SPECIFIED else location_str
            duration_mins = parse_duration(duration_str)

            dt = dateparser.parse(
                date_time_str,
                settings={'PREFER_DATES_FROM': 'future', 'RETURN_AS_TIMEZONE_AWARE': False},
            ) if date_time_str.lower() not in _NOT_SPECIFIED else None

            # Book the event if details are complete
            booking_note = ''
            if dt and event_name.lower() not in _NOT_SPECIFIED:
                spec = importlib.util.spec_from_file_location(
                    'calendar_booking',
                    _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'calendar_booking.py')
                )
                cal_mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(cal_mod)

                success = cal_mod.book_directly(
                    task_id=task_id,
                    event_name=event_name,
                    dt=dt,
                    duration_mins=duration_mins,
                    location=location,
                    client=client,
                )
                if success:
                    bookings_made += 1
                    booking_note = f'Calendar event "{event_name}" has been added.'
                    log(f'✅ Booked: {event_name}', 'info')
                else:
                    log(f'Booking failed for "{subject}"', 'error')
            else:
                log(f'Not enough details to book — will still reply', 'info')

            if check_cancelled(task_id, client):
                return

            # Draft reply (mentioning the booking if it happened)
            log(f'✍️  Drafting reply to {sender}...', 'info')
            draft_result = llm_structured(
                f'Draft a reply to this email thread on behalf of {SENDER_NAME}.\n\n'
                f'Thread:\n{thread_text}\n\n'
                + (f'Note: {booking_note} Mention that the meeting is confirmed.\n\n' if booking_note else '')
                + f'Guidelines:\n'
                f'- Match the tone and formality of the conversation\n'
                f'- Be concise — answer what is being asked, nothing extra\n'
                f'- Do NOT include a greeting or sign-off\n\n'
                f'body: the reply body only',
                _DRAFT_BODY_SCHEMA,
                schema_name="reply_draft",
            )
            draft_body = str(draft_result.get('body') or '')

            email_match = re.search(r'[\w.+-]+@[\w.-]+\.[a-z]{2,}', sender)
            reply_to    = email_match.group() if email_match else sender
            full_reply  = f"{draft_body.strip()}\n\n{SENDER_NAME}\n{SENDER_TITLE}\n{SENDER_WEBSITE}"

            answer = wait_for_input(
                task_id,
                f'Reply to {sender}\nRe: "{subject}"\n\n'
                f'{"─" * 40}\n{full_reply}\n{"─" * 40}\n\n'
                f'Send? (yes / skip / paste edit instructions)',
                client,
            )
            if not answer:
                return

            intent_result = llm_structured(
                f'Classify this as YES, SKIP, or EDIT:\n"{answer}"',
                _REPLY_INTENT_SCHEMA,
                schema_name="reply_intent",
            )
            intent = str(intent_result.get('action') or '').upper()

            if intent == 'EDIT':
                log(f'Re-drafting with edits...', 'info')
                draft_result = llm_structured(
                    f'Rewrite this email reply based on the instructions.\n\n'
                    f'Original:\n{draft_body}\n\n'
                    f'Instructions: {answer}\n\n'
                    f'body: the revised body only',
                    _DRAFT_BODY_SCHEMA,
                    schema_name="reply_draft_revision",
                )
                draft_body = str(draft_result.get('body') or '')
                full_reply = f"{draft_body.strip()}\n\n{SENDER_NAME}\n{SENDER_TITLE}\n{SENDER_WEBSITE}"
            elif intent != 'YES':
                log(f'Skipped reply to {sender}', 'info')
                skipped += 1
                continue

            if check_cancelled(task_id, client):
                return

            log(f'📤 Sending reply to {reply_to}...', 'tool_call')
            result = send_reply(
                to=reply_to,
                subject=subject,
                body=full_reply,
                thread_id=email['thread_id'],
                in_reply_to=email.get('message_id_header', ''),
            )
            log(result, 'tool_result' if '✅' in result else 'error')

            if '✅' in result:
                replies_sent += 1
            else:
                skipped += 1

    # =========================================================================
    # Summary
    # =========================================================================
    summary = (
        f'Checked {len(emails)} emails — '
        f'{replies_sent} repl{"ies" if replies_sent != 1 else "y"} sent, '
        f'{bookings_made} appointment{"s" if bookings_made != 1 else ""} booked, '
        f'{tally["fyi"]} FYI, '
        f'{tally["handled"] + skipped} handled/skipped.'
    )
    log(f'✅ {summary}', 'success')
    client.update_status(task_id, 'completed', result=summary)
