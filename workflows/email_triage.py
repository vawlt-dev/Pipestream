# =============================================================================
# Workflow: Email Triage
# =============================================================================
# Fetches 500 inbox emails, learns importance patterns from starred/important
# markers, filters out automated/marketing email, then triages the most recent
# 50 survivors — classifying each as reply_needed, book_appointment, fyi, or
# handled. Drafts and sends replies with user approval. Calls calendar_booking
# as a subroutine for scheduling requests (no follow-up questions).
# =============================================================================

import re
import dateparser
from datetime import datetime

from core import (
    llm_call, wait_for_input, check_cancelled, extract_field,
    SENDER_NAME, SENDER_TITLE, SENDER_WEBSITE,
)
from tools_google import get_emails, get_thread_text, send_reply

WORKFLOW_META = {
    "name": "email_triage",
    "description": (
        "Read, classify, and action recent emails. Fetches inbox emails, filters "
        "out marketing and automated messages, then triages the most recent ones — "
        "identifying which need a reply, which contain scheduling requests (handed "
        "off to calendar_booking automatically), and which are informational only. "
        "Drafts replies for approval and sends them in-thread. Use when the request "
        "mentions checking email, triaging inbox, reading messages, or catching up."
    ),
}

# Actions the classifier can assign
ACTIONS = ("reply_needed", "book_appointment", "fyi", "handled")


# =============================================================================
# HELPERS
# =============================================================================

def _format_email_list(emails: list[dict], numbered: bool = True) -> str:
    lines = []
    for i, e in enumerate(emails, 1):
        prefix = f"{i}. " if numbered else "- "
        flags  = []
        if e.get('is_starred'):   flags.append('★')
        if e.get('is_important'): flags.append('!')
        if e.get('is_unread'):    flags.append('unread')
        flag_str = f" [{', '.join(flags)}]" if flags else ''
        lines.append(
            f"{prefix}From: {e['sender']}{flag_str}\n"
            f"   Subject: {e['subject']}\n"
            f"   {e['snippet'][:120]}"
        )
    return "\n\n".join(lines)


def _parse_classifications(llm_output: str, count: int) -> list[str]:
    """
    Parse batch classification output like:
        1: reply_needed — reason
        2: fyi — reason
    Returns a list of action strings, defaulting to "fyi" on parse failure.
    """
    results = []
    for i in range(1, count + 1):
        match = re.search(rf'{i}[.:]\s*(\w+)', llm_output, re.IGNORECASE)
        if match:
            raw = match.group(1).lower()
            action = next((a for a in ACTIONS if raw.startswith(a[:6])), 'fyi')
        else:
            action = 'fyi'
        results.append(action)
    return results


# =============================================================================
# MAIN WORKFLOW
# =============================================================================

def run(task_id: str, input_text: str, client) -> None:

    def log(msg: str, log_type: str = 'info'):
        print(f'  [{log_type.upper()}] {msg}')
        client.log(task_id, msg, log_type)

    # =========================================================================
    # STEP 1: Fetch 500 emails (metadata only — no full bodies yet)
    # =========================================================================
    log('📬 Step 1: Fetching inbox emails (this takes ~10s)...', 'info')

    try:
        emails = get_emails(max_results=500)
    except Exception as e:
        log(f'Failed to fetch emails: {e}', 'error')
        client.update_status(task_id, 'failed', error_message=str(e))
        return

    log(f'Fetched {len(emails)} emails from inbox', 'tool_result')
    if not emails:
        client.update_status(task_id, 'completed', result='Inbox is empty.')
        return

    if check_cancelled(task_id, client):
        return

    # =========================================================================
    # STEP 2: Learn importance patterns from starred / important emails
    # =========================================================================
    log('🧠 Step 2: Learning importance patterns...', 'info')

    engaged = [e for e in emails if e['is_starred'] or e['is_important']]
    ignored = [e for e in emails if not e['is_starred'] and not e['is_important']]

    if engaged:
        pattern_prompt = f"""Analyse these emails that were starred or marked important by the user, compared to the ones that were not, and identify what patterns define what this person cares about.

STARRED / IMPORTANT ({len(engaged[:40])} samples):
{_format_email_list(engaged[:40], numbered=False)}

IGNORED samples ({min(20, len(ignored))} of {len(ignored)}):
{_format_email_list(ignored[:20], numbered=False)}

Identify concise patterns. Respond in this exact format:
IMPORTANT_SENDERS: <domains or addresses that matter, e.g. "@client.com, boss@company.com">
IMPORTANT_KEYWORDS: <subject keywords that signal importance>
IMPORTANT_TYPES: <types of email they engage with, e.g. "direct questions, project updates">
NOISE_PATTERNS: <patterns that signal noise, e.g. "newsletters, automated receipts">"""

        importance_profile = llm_call(pattern_prompt)
        log(f'Importance profile:\n{importance_profile}', 'agent')
    else:
        importance_profile = 'No starred/important emails found — use general judgement.'
        log('No starred emails found — skipping pattern learning', 'info')

    if check_cancelled(task_id, client):
        return

    # =========================================================================
    # STEP 3: Filter out automated / marketing email
    # =========================================================================
    log('🔍 Step 3: Filtering automated and marketing email...', 'info')

    # Fast rule-based pass: List-Unsubscribe header = bulk sender
    rule_filtered = [e for e in emails if not e['is_automated']]
    removed_by_rule = len(emails) - len(rule_filtered)
    log(f'Rule filter removed {removed_by_rule} bulk/automated emails', 'tool_result')

    if check_cancelled(task_id, client):
        return

    # LLM filter pass — run in batches of 50 to avoid oversized prompts
    BATCH = 50
    surviving: list[dict] = []

    for batch_start in range(0, len(rule_filtered), BATCH):
        batch = rule_filtered[batch_start: batch_start + BATCH]

        filter_prompt = f"""You are filtering an email inbox. Mark each email as KEEP or REMOVE.

Importance profile for this user:
{importance_profile}

Rules:
- REMOVE: newsletters, marketing, automated notifications, password resets, OTP codes, order confirmations, receipts, social media alerts, company-wide announcements, unsubscribe-style emails
- KEEP: anything from a real person that could require action, a direct reply, a question, a scheduling request, or anything matching the user's importance profile
- When uncertain: KEEP (lean inclusive)

Emails (number them exactly as shown):
{_format_email_list(batch)}

For each, reply:
1: KEEP or REMOVE
2: KEEP or REMOVE
...{len(batch)}: KEEP or REMOVE"""

        filter_result = llm_call(filter_prompt)

        for i, email in enumerate(batch, 1):
            match = re.search(rf'{i}[.:]\s*(KEEP|REMOVE)', filter_result, re.IGNORECASE)
            decision = match.group(1).upper() if match else 'KEEP'
            if decision == 'KEEP':
                surviving.append(email)

        if check_cancelled(task_id, client):
            return

    log(f'After filtering: {len(surviving)} emails remain (from {len(emails)})', 'tool_result')

    # =========================================================================
    # STEP 4: Take most recent 50 survivors
    # =========================================================================
    pool = surviving[:50]
    log(f'Triaging most recent {len(pool)} emails', 'info')

    if not pool:
        client.update_status(task_id, 'completed',
                             result='All emails filtered as automated/marketing — nothing to triage.')
        return

    # =========================================================================
    # STEP 5: Pass 1 — batch classify all 50 (subject + sender + snippet only)
    # =========================================================================
    log('📊 Step 5: Classifying emails...', 'info')

    classify_prompt = f"""Classify each email. Definitions:
- reply_needed: A real person sent this and is likely waiting for a response. Check if the thread shows a reply was already sent — if so, use "handled". Read emails are NOT automatically handled.
- book_appointment: Contains a clear request to schedule a meeting, call, or event at a specific time.
- fyi: Informational only — no reply expected, no scheduling needed.
- handled: Already replied to, or clearly requires no action ever.

User's importance profile:
{importance_profile}

Emails:
{_format_email_list(pool)}

For each email reply exactly:
1: <action> — <one line reason>
2: <action> — <one line reason>
...
Use only: reply_needed, book_appointment, fyi, handled"""

    classification_output = llm_call(classify_prompt)
    log(f'Classifications:\n{classification_output}', 'agent')
    actions = _parse_classifications(classification_output, len(pool))

    # Attach actions to emails
    for email, action in zip(pool, actions):
        email['action'] = action

    if check_cancelled(task_id, client):
        return

    # Tally
    tally = {a: 0 for a in ACTIONS}
    for e in pool:
        tally[e['action']] += 1

    log(
        f"📊 Triage: {tally['reply_needed']} replies needed | "
        f"{tally['book_appointment']} appointments | "
        f"{tally['fyi']} FYI | "
        f"{tally['handled']} handled",
        'info'
    )

    # =========================================================================
    # STEP 6: Process action items (most recent → least recent)
    # =========================================================================
    replies_sent   = 0
    bookings_made  = 0
    skipped        = 0

    action_emails = [e for e in pool if e['action'] in ('reply_needed', 'book_appointment')]

    if not action_emails:
        log('No action items found — all done.', 'success')
        client.update_status(
            task_id, 'completed',
            result=f'Triaged {len(pool)} emails. Nothing required action.'
        )
        return

    log(f'Processing {len(action_emails)} action item(s)...', 'info')

    for email in action_emails:
        if check_cancelled(task_id, client):
            return

        sender  = email['sender']
        subject = email['subject']
        action  = email['action']

        log(f'─── {action.upper()}: {subject[:60]} (from {sender})', 'info')

        # Fetch full thread for context
        log(f'🌐 Fetching thread...', 'tool_call')
        try:
            thread_text = get_thread_text(email['thread_id'])
            log(f'Got {len(thread_text)} chars of thread', 'tool_result')
        except Exception as e:
            log(f'Could not fetch thread: {e}', 'error')
            skipped += 1
            continue

        # =====================================================================
        # Book appointment branch
        # =====================================================================
        if action == 'book_appointment':
            log('📅 Extracting booking details from thread...', 'info')

            extract_prompt = f"""Extract booking details from this email thread.

Thread:
{thread_text}

Respond in this exact format:
EVENT_NAME: <what is being booked, e.g. "Call with Sarah re Q3 review">
DATE_TIME: <date and time as stated, e.g. "Tuesday 3pm" or "next Monday at 10am">
DURATION: <if mentioned, e.g. "30 minutes", or "not specified">
LOCATION: <if mentioned, or "not specified">
CONFIDENCE: <high/medium/low — how sure are you all critical details are present>"""

            extracted = llm_call(extract_prompt)
            log(f'Extracted:\n{extracted}', 'agent')

            event_name    = extract_field(extracted, 'EVENT_NAME')
            date_time_str = extract_field(extracted, 'DATE_TIME')
            duration_str  = extract_field(extracted, 'DURATION')
            location_str  = extract_field(extracted, 'LOCATION')
            confidence    = extract_field(extracted, 'CONFIDENCE')

            location = '' if location_str.lower() in ('not found', 'not specified') else location_str

            # Parse duration
            from core import parse_duration
            duration_mins = parse_duration(duration_str)

            # Resolve date
            dt = dateparser.parse(
                date_time_str,
                settings={'PREFER_DATES_FROM': 'future', 'RETURN_AS_TIMEZONE_AWARE': False},
            ) if date_time_str.lower() not in ('not found', 'not specified') else None

            # If critical info is missing, ask the user ONCE here — not inside book_directly
            missing = []
            if not dt:
                missing.append('date and time')
            if event_name.lower() in ('not found', 'not specified'):
                missing.append('event name')

            if missing:
                question = (
                    f'I found a scheduling request in an email from {sender} '
                    f'(re: "{subject}") but I\'m missing: {", ".join(missing)}. '
                    f'Please provide the missing details (e.g. "Team standup, Monday 9am"):'
                )
                answer = wait_for_input(task_id, question, client)
                if not answer:
                    return

                # Re-extract with the user's clarification
                re_extract = llm_call(f"""Extract booking details from this clarification.
Clarification: "{answer}"
Original context: {event_name}, {date_time_str}

EVENT_NAME: <event name>
DATE_TIME: <date and time>
DURATION: <duration or "not specified">""")

                event_name    = extract_field(re_extract, 'EVENT_NAME') or event_name
                date_time_str = extract_field(re_extract, 'DATE_TIME')
                duration_mins = parse_duration(extract_field(re_extract, 'DURATION'))
                dt = dateparser.parse(
                    date_time_str,
                    settings={'PREFER_DATES_FROM': 'future', 'RETURN_AS_TIMEZONE_AWARE': False},
                )

            if not dt:
                log(f'Still could not resolve date for "{subject}" — skipping', 'error')
                skipped += 1
                continue

            if check_cancelled(task_id, client):
                return

            # Call book_directly — no follow-up questions
            import importlib.util, os, sys
            workflows_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)))
            spec = importlib.util.spec_from_file_location(
                'calendar_booking',
                os.path.join(workflows_dir, 'calendar_booking.py')
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
            else:
                skipped += 1

        # =====================================================================
        # Reply needed branch
        # =====================================================================
        elif action == 'reply_needed':
            log(f'✍️ Drafting reply to {sender}...', 'info')

            draft_prompt = f"""Draft a reply to this email thread on behalf of {SENDER_NAME}.

Thread (most recent last):
{thread_text}

Guidelines:
- Match the tone and formality of the conversation
- Be concise — answer what's being asked, nothing extra
- Do NOT include a greeting or sign-off — those are added separately
- Do NOT include "Re:" in your response

Write ONLY the reply body:"""

            draft_body = llm_call(draft_prompt)

            # Extract sender's email address from the From header
            email_match = re.search(r'[\w.+-]+@[\w.-]+\.[a-z]{2,}', sender)
            reply_to    = email_match.group() if email_match else sender

            full_reply = f"{draft_body.strip()}\n\n{SENDER_NAME}\n{SENDER_TITLE}\n{SENDER_WEBSITE}"

            question = (
                f'Reply to {sender}\nRe: "{subject}"\n\n'
                f'{"─" * 40}\n{full_reply}\n{"─" * 40}\n\n'
                f'Send this reply? (yes / skip / or paste edit instructions)'
            )

            answer = wait_for_input(task_id, question, client)
            if not answer:
                return

            # Check if yes, skip, or edit
            intent_check = llm_call(
                f'Classify this response as YES, SKIP, or EDIT:\n"{answer}"\nReply with only one word:'
            ).strip().upper()

            if 'SKIP' in intent_check or 'NO' in intent_check:
                log(f'Skipped reply to {sender}', 'info')
                skipped += 1
                continue

            if 'EDIT' in intent_check:
                log(f'Re-drafting with edits: {answer}', 'info')
                draft_body = llm_call(
                    f'Rewrite this email reply based on the following instructions.\n\n'
                    f'Original draft:\n{draft_body}\n\n'
                    f'Instructions: {answer}\n\n'
                    f'Write ONLY the revised body:'
                )
                full_reply = f"{draft_body.strip()}\n\n{SENDER_NAME}\n{SENDER_TITLE}\n{SENDER_WEBSITE}"

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
    # STEP 7: Final summary
    # =========================================================================
    summary = (
        f'Triaged {len(pool)} emails — '
        f'{replies_sent} repl{"ies" if replies_sent != 1 else "y"} sent, '
        f'{bookings_made} appointment{"s" if bookings_made != 1 else ""} booked, '
        f'{tally["fyi"]} FYI, '
        f'{tally["handled"] + skipped} handled/skipped.'
    )

    log(f'✅ {summary}', 'success')
    client.update_status(task_id, 'completed', result=summary)
