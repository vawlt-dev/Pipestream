# =============================================================================
# Workflow: Delete Calendar Events
# =============================================================================
# The user describes what they want deleted — a topic, a time range, a name,
# a day, a month, or any combination. The workflow fetches ALL events in the
# time window first, then uses the LLM to filter by theme — much more reliable
# than keyword-searching the Calendar API directly.
# =============================================================================

import re
import dateparser
from datetime import datetime, timezone, timedelta

from core import llm_structured, wait_for_input, check_cancelled
from schemas import s_object, s_string, s_enum, s_array
from tools_google import search_calendar_events, delete_calendar_event

_TIME_THEME_SCHEMA = s_object({"time_description": s_string(), "theme": s_string()})

_EVENT_MATCH_SCHEMA = s_object({
    "matches": s_array(s_object({
        "verdict": s_enum(["DELETE", "KEEP", "UNSURE"]),
        "reason":  s_string(),
    })),
})

WORKFLOW_META = {
    "name": "delete_calendar_events",
    "description": (
        "Delete one or more calendar events. The user describes what to delete — "
        "by topic, keyword, person's name, date, day of week, or month — and the "
        "workflow finds matching events, shows them for confirmation, then deletes "
        "the approved ones. Use when the request mentions deleting, removing, "
        "cancelling, or clearing calendar events or appointments."
    ),
}


# =============================================================================
# TIME RANGE PARSING
# =============================================================================

def _start_of_week(d: datetime) -> datetime:
    """Return the Monday of the week containing d."""
    return (d - timedelta(days=d.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def _month_range(year: int, month: int) -> tuple[datetime, datetime]:
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(year, month + 1, 1, tzinfo=timezone.utc)
    return start, end


def _parse_time_range(text: str) -> tuple[str, str]:
    """
    Parse a natural-language time description into (time_min, time_max) RFC3339 strings.

    Week boundaries are Monday–Sunday, not rolling 7-day windows.
    Falls back to the next 3 months if nothing matches.
    """
    now   = datetime.now(timezone.utc)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    lower = text.lower()

    mon_this  = _start_of_week(today)
    mon_last  = mon_this - timedelta(weeks=1)
    mon_next  = mon_this + timedelta(weeks=1)
    mon_after = mon_next + timedelta(weeks=1)

    # --- Weeks (Monday-bounded) ---
    if 'last week' in lower:
        return mon_last.isoformat(), mon_this.isoformat()
    if 'next week' in lower:
        return mon_next.isoformat(), mon_after.isoformat()
    if 'this week' in lower or 'current week' in lower:
        return mon_this.isoformat(), mon_next.isoformat()

    # --- Relative months ---
    if 'last month' in lower:
        m = now.month - 1 or 12
        y = now.year if now.month > 1 else now.year - 1
        s, e = _month_range(y, m)
        return s.isoformat(), e.isoformat()
    if 'next month' in lower:
        m = now.month % 12 + 1
        y = now.year + (1 if now.month == 12 else 0)
        s, e = _month_range(y, m)
        return s.isoformat(), e.isoformat()
    if 'this month' in lower or 'current month' in lower:
        s, e = _month_range(now.year, now.month)
        return s.isoformat(), e.isoformat()

    # --- Named months ---
    month_names = {
        'january': 1, 'february': 2, 'march': 3, 'april': 4,
        'may': 5, 'june': 6, 'july': 7, 'august': 8,
        'september': 9, 'october': 10, 'november': 11, 'december': 12,
    }
    for name, num in month_names.items():
        if name in lower:
            # Use current year; if that month is already past use next year
            year = now.year if num >= now.month else now.year + 1
            s, e = _month_range(year, num)
            return s.isoformat(), e.isoformat()

    # --- Specific days ---
    if 'today' in lower:
        return today.isoformat(), (today + timedelta(days=1)).isoformat()
    if 'tomorrow' in lower:
        t = today + timedelta(days=1)
        return t.isoformat(), (t + timedelta(days=1)).isoformat()
    if 'yesterday' in lower:
        y = today - timedelta(days=1)
        return y.isoformat(), today.isoformat()

    # --- "next N days/weeks" ---
    m = re.search(r'next\s+(\d+)\s+(day|week)', lower)
    if m:
        n    = int(m.group(1))
        unit = timedelta(days=n) if m.group(2) == 'day' else timedelta(weeks=n)
        return now.isoformat(), (now + unit).isoformat()

    # --- Try dateparser for anything else (e.g. "Monday", "25th May") ---
    parsed = dateparser.parse(text, settings={'PREFER_DATES_FROM': 'future',
                                              'RETURN_AS_TIMEZONE_AWARE': False})
    if parsed:
        s = parsed.replace(hour=0, minute=0, second=0, microsecond=0,
                           tzinfo=timezone.utc)
        return s.isoformat(), (s + timedelta(days=1)).isoformat()

    # --- Default: next 3 months ---
    return now.isoformat(), (now + timedelta(days=90)).isoformat()


# =============================================================================
# HELPERS
# =============================================================================

def _format_event_list(events: list[dict]) -> str:
    lines = []
    for i, e in enumerate(events, 1):
        start = e['start'][:16].replace('T', ' ') if 'T' in e['start'] else e['start']
        loc   = f"  @ {e['location']}" if e['location'] else ''
        lines.append(f"{i}. [{start}] {e['summary']}{loc}")
    return '\n'.join(lines)


# =============================================================================
# MAIN WORKFLOW
# =============================================================================

def run(task_id: str, input_text: str, client) -> None:

    def log(msg: str, log_type: str = 'info'):
        print(f'  [{log_type.upper()}] {msg}')
        client.log(task_id, msg, log_type)

    # =========================================================================
    # STEP 1: Extract time range and theme from input
    # =========================================================================
    log('🔍 Extracting search parameters...', 'info')

    intent = llm_structured(
        f'Extract the time range and theme from this calendar delete request.\n\n'
        f'Request: "{input_text}"\n\n'
        f'time_description: the time part, e.g. "last week", "June", "this Thursday", or "not specified"\n'
        f'theme: what kind of events to delete, e.g. "physio", "meetings with Jason", "all events"',
        _TIME_THEME_SCHEMA,
        schema_name="delete_intent",
    )
    log(f'Intent: {intent}', 'agent')

    time_desc = str(intent.get('time_description') or 'not specified').strip()
    theme     = str(intent.get('theme') or 'all events').strip()

    # Use the raw input as fallback for time parsing
    time_input = time_desc if time_desc.lower() not in ('not found', 'not specified') else input_text
    time_min, time_max = _parse_time_range(time_input)

    log(f'Window: {time_min[:10]} → {time_max[:10]} | Theme: "{theme}"', 'info')

    if check_cancelled(task_id, client):
        return

    # =========================================================================
    # STEP 2: Fetch ALL events in the window — no keyword filter
    # =========================================================================
    log('📅 Fetching calendar events in window...', 'tool_call')

    events = search_calendar_events(
        query    = '',        # intentionally blank — LLM does the filtering
        time_min = time_min,
        time_max = time_max,
        max_results = 100,
    )

    log(f'Found {len(events)} event(s) in window', 'tool_result')

    if not events:
        client.update_status(task_id, 'completed', result='No calendar events found in that time window.')
        return

    if check_cancelled(task_id, client):
        return

    # =========================================================================
    # STEP 3: LLM matches events to theme
    # =========================================================================
    log('🧠 Matching events to theme...', 'info')

    event_list_str = _format_event_list(events)

    match_result = llm_structured(
        f'The user wants to delete calendar events related to: "{theme}"\n\n'
        f'Events:\n{event_list_str}\n\n'
        f'For each event, in the same order as listed, give a verdict: DELETE, KEEP, '
        f'or UNSURE (use UNSURE when you cannot confidently tell if it matches). '
        f'Give a short reason — required for UNSURE, empty string is fine for DELETE/KEEP.',
        _EVENT_MATCH_SCHEMA,
        schema_name="event_matches",
    )
    log(f'Match results: {match_result}', 'agent')
    matches = match_result.get('matches') or []

    to_delete: list[dict] = []
    unsure:    list[tuple[dict, str]] = []   # (event, reason)

    for event, match in zip(events, matches):
        verdict = str(match.get('verdict') or '').upper()
        reason  = str(match.get('reason') or '').strip()

        if verdict == 'DELETE':
            to_delete.append(event)
        elif verdict == 'UNSURE':
            unsure.append((event, reason or 'unclear match'))

    log(f'{len(to_delete)} to delete, {len(unsure)} unsure', 'info')

    if check_cancelled(task_id, client):
        return

    # =========================================================================
    # STEP 4a: Resolve ambiguous events
    # =========================================================================
    if unsure:
        lines = []
        for i, (event, reason) in enumerate(unsure, 1):
            start = event['start'][:16].replace('T', ' ') if 'T' in event['start'] else event['start']
            lines.append(f'{i}. [{start}] {event["summary"]} — {reason}')

        unsure_block = '\n'.join(lines)

        answer = wait_for_input(
            task_id,
            f'I\'m not sure about {len(unsure)} event(s) — do any of these match "{theme}"?\n\n'
            f'{unsure_block}\n\n'
            f'Reply with the numbers to DELETE (e.g. "1, 3"), or "none".',
            client,
        )
        if not answer:
            return

        if answer.strip().lower() not in ('none', 'no', 'n'):
            confirmed = set(int(n) for n in re.findall(r'\d+', answer))
            for i, (event, _) in enumerate(unsure, 1):
                if i in confirmed:
                    to_delete.append(event)

    if not to_delete:
        client.update_status(
            task_id, 'completed',
            result=f'Found {len(events)} events in that window but none matched "{theme}".'
        )
        return

    if check_cancelled(task_id, client):
        return

    # =========================================================================
    # STEP 4b: Final confirmation
    # =========================================================================
    event_preview = _format_event_list(to_delete)

    answer = wait_for_input(
        task_id,
        f'Deleting {len(to_delete)} event(s):\n\n'
        f'{event_preview}\n\n'
        f'Confirm? (yes / no / or "keep 2, 3" to spare specific ones)',
        client,
    )
    if not answer:
        return

    answer_lower = answer.strip().lower()

    if answer_lower in ('no', 'n', 'cancel', 'stop'):
        log('Deletion cancelled', 'info')
        client.update_status(task_id, 'completed', result='Deletion cancelled.')
        return

    keep_nums = set(int(n) for n in re.findall(r'\d+', answer_lower)) if 'keep' in answer_lower else set()
    final_delete = [e for i, e in enumerate(to_delete, 1) if i not in keep_nums]

    if not final_delete:
        client.update_status(task_id, 'completed', result='No events deleted.')
        return

    if check_cancelled(task_id, client):
        return

    # =========================================================================
    # STEP 5: Delete
    # =========================================================================
    log(f'🗑️  Deleting {len(final_delete)} event(s)...', 'info')

    deleted = 0
    failed  = 0

    for event in final_delete:
        result = delete_calendar_event(event['id'])
        log(f'{result} — {event["summary"]}', 'tool_result' if '✅' in result else 'error')
        deleted += '✅' in result
        failed  += '✅' not in result
        if check_cancelled(task_id, client):
            return

    summary = f'Deleted {deleted} event{"s" if deleted != 1 else ""}'
    if failed:
        summary += f', {failed} failed'
    if keep_nums:
        summary += f', {len(keep_nums)} kept at your request'

    log(f'✅ {summary}', 'success')
    client.update_status(task_id, 'completed', result=summary)
