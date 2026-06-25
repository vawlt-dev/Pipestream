# =============================================================================
# tools_google.py — Google API Tools (Gmail, Calendar)
# =============================================================================
# Reuses your existing Google OAuth setup.
# Requires credentials.json and token.pickle in the workspace.
# =============================================================================

import os
import base64
import pickle
from email.mime.text import MIMEText

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# =============================================================================
# CONFIG
# =============================================================================

WORK_DIR = os.getenv("WORK_DIR", "/workspace")
CREDENTIALS_FILE = os.path.join(WORK_DIR, "credentials.json")
TOKEN_FILE = os.path.join(WORK_DIR, "token.pickle")

# =============================================================================
# AUTH
# =============================================================================

def get_google_credentials():
    """Load existing Google credentials from token.pickle."""
    
    if not os.path.exists(TOKEN_FILE):
        raise FileNotFoundError(
            f"Missing {TOKEN_FILE}!\n"
            "Run auth_setup.py on your host machine first, then mount token.pickle."
        )
    
    with open(TOKEN_FILE, 'rb') as token:
        creds = pickle.load(token)
    
    # Refresh if expired
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(TOKEN_FILE, 'wb') as token:
            pickle.dump(creds, token)
    
    return creds

# =============================================================================
# GMAIL
# =============================================================================

def send_email(to: str, subject: str, body: str) -> str:
    """
    Send an email via Gmail.
    
    Args:
        to: Recipient email address
        subject: Email subject line
        body: Email body text (plain text)
    
    Returns:
        Success or error message
    """
    try:
        creds = get_google_credentials()
        service = build('gmail', 'v1', credentials=creds, cache_discovery=False)
        
        # Create the email
        message = MIMEText(body)
        message['to'] = to
        message['subject'] = subject
        
        # Encode it
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        
        # Send it
        sent = service.users().messages().send(
            userId='me',
            body={'raw': raw}
        ).execute()
        
        return f"✅ Email sent to {to} (ID: {sent['id']})"

    except FileNotFoundError as e:
        return f"❌ Auth error: {str(e)}"
    except Exception as e:
        return f"❌ Failed to send email: {str(e)}"


def create_email_draft(to: str, subject: str, body: str) -> str:
    """
    Create a Gmail draft — does NOT send. Visible in the Drafts folder for
    the user to review, edit, and send manually.

    Returns:
        Success or error message
    """
    try:
        creds   = get_google_credentials()
        service = build('gmail', 'v1', credentials=creds, cache_discovery=False)

        message = MIMEText(body)
        message['to']      = to
        message['subject'] = subject

        raw   = base64.urlsafe_b64encode(message.as_bytes()).decode()
        draft = service.users().drafts().create(
            userId='me',
            body={'message': {'raw': raw}},
        ).execute()

        return f"✅ Draft created for {to} (ID: {draft['id']})"

    except FileNotFoundError as e:
        return f"❌ Auth error: {str(e)}"
    except Exception as e:
        return f"❌ Failed to create draft: {str(e)}"


def get_recent_emails(max_results: int = 10) -> list[dict]:
    """
    Fetch the most recent non-promotional inbox emails.
    Uses Gmail's built-in category filtering to exclude Promotions, Updates,
    and Social tabs — no LLM filter pass required.

    Returns the same dict shape as get_emails().
    """
    return get_emails(max_results=max_results,
                      query='in:inbox -category:promotions -category:updates -category:social')


def get_emails(max_results: int = 500, query: str = '') -> list[dict]:
    """
    Fetch emails from inbox with metadata only (no full bodies).
    Parallelises the per-message metadata calls for speed.

    Each returned dict:
        id, thread_id, subject, sender, date, message_id_header,
        snippet, is_starred, is_important, is_unread, is_automated,
        internal_date (ms unix timestamp, use for sorting)
    """
    creds   = get_google_credentials()
    service = build('gmail', 'v1', credentials=creds, cache_discovery=False)

    # Step 1: get message ID stubs (single API call)
    list_kwargs = dict(userId='me', maxResults=max_results)
    if query:
        list_kwargs['q'] = query
    else:
        list_kwargs['labelIds'] = ['INBOX']
    response = service.users().messages().list(**list_kwargs).execute()

    stubs = response.get('messages', [])
    if not stubs:
        return []

    # Step 2: batch-fetch metadata — up to 100 requests per HTTP call.
    # This avoids threading entirely (httplib2 is not thread-safe).
    results: list[dict] = []

    def _on_meta(request_id, response, exception):
        if exception or not response:
            return
        headers   = {h['name']: h['value'] for h in response.get('payload', {}).get('headers', [])}
        label_ids = response.get('labelIds', [])
        results.append({
            'id':                response['id'],
            'thread_id':         response['threadId'],
            'subject':           headers.get('Subject', '(no subject)'),
            'sender':            headers.get('From', ''),
            'date':              headers.get('Date', ''),
            'message_id_header': headers.get('Message-ID', ''),
            'snippet':           response.get('snippet', ''),
            'is_starred':        'STARRED'   in label_ids,
            'is_important':      'IMPORTANT' in label_ids,
            'is_unread':         'UNREAD'    in label_ids,
            'is_automated':      'List-Unsubscribe' in headers,
            'internal_date':     int(response.get('internalDate', 0)),
        })

    BATCH_SIZE = 100  # Gmail API hard limit per batch request
    for i in range(0, len(stubs), BATCH_SIZE):
        batch = service.new_batch_http_request(callback=_on_meta)
        for stub in stubs[i:i + BATCH_SIZE]:
            batch.add(service.users().messages().get(
                userId='me',
                id=stub['id'],
                format='metadata',
                metadataHeaders=['From', 'Subject', 'Date', 'List-Unsubscribe', 'Message-ID'],
            ))
        batch.execute()

    results.sort(key=lambda x: x['internal_date'], reverse=True)
    return results


def _extract_mime(payload: dict, mime_type: str) -> str:
    """Recursively find the first part matching mime_type and return its decoded text."""
    if payload.get('mimeType') == mime_type:
        data = payload.get('body', {}).get('data', '')
        return base64.urlsafe_b64decode(data).decode('utf-8', errors='replace') if data else ''
    for part in payload.get('parts', []):
        result = _extract_mime(part, mime_type)
        if result:
            return result
    return ''


def _extract_body(payload: dict) -> str:
    """
    Recursively pull readable text from a Gmail message payload.
    Prefers text/plain; falls back to text/html with tags stripped.
    """
    import re as _re
    plain = _extract_mime(payload, 'text/plain')
    if plain:
        return plain
    html = _extract_mime(payload, 'text/html')
    if html:
        text = _re.sub(r'<[^>]+>', ' ', html)
        return _re.sub(r'\s+', ' ', text).strip()
    return ''


def get_thread_text(thread_id: str, max_chars: int = 8000) -> str:
    """
    Fetch a full Gmail thread and return it as readable plain text.
    Each message is prefixed with direction (SENT/RECEIVED), sender, and date.
    """
    creds   = get_google_credentials()
    service = build('gmail', 'v1', credentials=creds, cache_discovery=False)

    thread   = service.users().threads().get(userId='me', id=thread_id, format='full').execute()
    sections = []

    for msg in thread.get('messages', []):
        headers   = {h['name']: h['value'] for h in msg.get('payload', {}).get('headers', [])}
        label_ids = msg.get('labelIds', [])
        direction = 'YOU SENT' if 'SENT' in label_ids else 'RECEIVED'
        sender    = headers.get('From', 'Unknown')
        date      = headers.get('Date', '')
        body      = _extract_body(msg.get('payload', {})).strip()
        sections.append(f"[{direction}] {sender}  |  {date}\n{body}")

    full = "\n\n---\n\n".join(sections)
    return full[:max_chars] + ('\n... (truncated)' if len(full) > max_chars else '')


def send_reply(to: str, subject: str, body: str, thread_id: str, in_reply_to: str = '') -> str:
    """
    Send a reply inside an existing Gmail thread.

    Args:
        to:           Recipient address
        subject:      Original subject (Re: prefix added automatically if missing)
        body:         Reply body text
        thread_id:    Gmail thread ID to reply into
        in_reply_to:  Message-ID header value of the email being replied to
    """
    try:
        creds   = get_google_credentials()
        service = build('gmail', 'v1', credentials=creds, cache_discovery=False)

        reply_subject = subject if subject.lower().startswith('re:') else f'Re: {subject}'

        message = MIMEText(body)
        message['to']      = to
        message['subject'] = reply_subject
        if in_reply_to:
            message['In-Reply-To'] = in_reply_to
            message['References']  = in_reply_to

        raw  = base64.urlsafe_b64encode(message.as_bytes()).decode()
        sent = service.users().messages().send(
            userId='me',
            body={'raw': raw, 'threadId': thread_id},
        ).execute()

        return f'✅ Reply sent (ID: {sent["id"]})'

    except FileNotFoundError as e:
        return f'❌ Auth error: {str(e)}'
    except Exception as e:
        return f'❌ Failed to send reply: {str(e)}'


# =============================================================================
# CALENDAR
# =============================================================================

def create_calendar_event(summary: str, start_time: str, end_time: str, 
                          description: str = "", location: str = "") -> str:
    """Create a Google Calendar event."""
    try:
        import pytz
        TIMEZONE = os.getenv("TIMEZONE", "Pacific/Auckland")
        
        creds = get_google_credentials()
        service = build('calendar', 'v3', credentials=creds, cache_discovery=False)
        
        event = {
            'summary': summary,
            'location': location,
            'description': description,
            'start': {'dateTime': start_time, 'timeZone': TIMEZONE},
            'end': {'dateTime': end_time, 'timeZone': TIMEZONE},
        }
        
        event = service.events().insert(calendarId='primary', body=event).execute()
        return f"✅ Event created: {event.get('summary')}\n   Link: {event.get('htmlLink')}"

    except Exception as e:
        return f"❌ Failed to create event: {str(e)}"

def search_calendar_events(query: str = '', time_min: str = '', time_max: str = '',
                           max_results: int = 50) -> list[dict]:
    """
    Search calendar events within an optional time range.

    Returns a list of dicts:
        id, summary, start, end, location, description, html_link
    """
    try:
        creds   = get_google_credentials()
        service = build('calendar', 'v3', credentials=creds, cache_discovery=False)

        kwargs = dict(
            calendarId  = 'primary',
            maxResults  = max_results,
            singleEvents= True,
            orderBy     = 'startTime',
        )
        if query:
            kwargs['q'] = query
        if time_min:
            kwargs['timeMin'] = time_min
        if time_max:
            kwargs['timeMax'] = time_max

        result = service.events().list(**kwargs).execute()
        events = []
        for e in result.get('items', []):
            start = e.get('start', {})
            end   = e.get('end', {})
            events.append({
                'id':          e['id'],
                'summary':     e.get('summary', '(no title)'),
                'start':       start.get('dateTime', start.get('date', '')),
                'end':         end.get('dateTime',   end.get('date', '')),
                'location':    e.get('location', ''),
                'description': e.get('description', ''),
                'html_link':   e.get('htmlLink', ''),
            })
        return events

    except Exception as e:
        print(f"  [CALENDAR SEARCH ERROR] {e}", flush=True)
        return []


def delete_calendar_event(event_id: str) -> str:
    """Delete a calendar event by ID. Returns a status string."""
    try:
        creds   = get_google_credentials()
        service = build('calendar', 'v3', credentials=creds, cache_discovery=False)
        service.events().delete(calendarId='primary', eventId=event_id).execute()
        return f'✅ Deleted event {event_id}'
    except Exception as e:
        return f'❌ Failed to delete {event_id}: {e}'


# =============================================================================
# TEST
# =============================================================================

if __name__ == "__main__":
    print("Testing Google auth...")
    try:
        creds = get_google_credentials()
        print("✅ Google credentials loaded successfully")
    except Exception as e:
        print(f"❌ Auth failed: {e}")
