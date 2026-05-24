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
from concurrent.futures import ThreadPoolExecutor, as_completed

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
        service = build('gmail', 'v1', credentials=creds)
        
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

def get_emails(max_results: int = 500) -> list[dict]:
    """
    Fetch emails from inbox with metadata only (no full bodies).
    Parallelises the per-message metadata calls for speed.

    Each returned dict:
        id, thread_id, subject, sender, date, message_id_header,
        snippet, is_starred, is_important, is_unread, is_automated,
        internal_date (ms unix timestamp, use for sorting)
    """
    creds = get_google_credentials()

    # Use a single service to list message IDs (just one call)
    service  = build('gmail', 'v1', credentials=creds)
    response = service.users().messages().list(
        userId='me',
        maxResults=max_results,
        labelIds=['INBOX'],
    ).execute()

    stubs = response.get('messages', [])
    if not stubs:
        return []

    # Each thread builds its own service instance to avoid shared-state issues
    def fetch_meta(stub):
        svc    = build('gmail', 'v1', credentials=creds)
        detail = svc.users().messages().get(
            userId='me',
            id=stub['id'],
            format='metadata',
            metadataHeaders=['From', 'Subject', 'Date', 'List-Unsubscribe', 'Message-ID'],
        ).execute()
        headers   = {h['name']: h['value'] for h in detail.get('payload', {}).get('headers', [])}
        label_ids = detail.get('labelIds', [])
        return {
            'id':                stub['id'],
            'thread_id':         detail['threadId'],
            'subject':           headers.get('Subject', '(no subject)'),
            'sender':            headers.get('From', ''),
            'date':              headers.get('Date', ''),
            'message_id_header': headers.get('Message-ID', ''),
            'snippet':           detail.get('snippet', ''),
            'is_starred':        'STARRED'   in label_ids,
            'is_important':      'IMPORTANT' in label_ids,
            'is_unread':         'UNREAD'    in label_ids,
            'is_automated':      'List-Unsubscribe' in headers,
            'internal_date':     int(detail.get('internalDate', 0)),
        }

    results = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(fetch_meta, s) for s in stubs]
        for future in as_completed(futures, timeout=120):
            try:
                results.append(future.result())
            except Exception:
                pass

    results.sort(key=lambda x: x['internal_date'], reverse=True)
    return results


def _extract_body(payload: dict) -> str:
    """Recursively pull plain-text body from a Gmail message payload."""
    if payload.get('mimeType') == 'text/plain':
        data = payload.get('body', {}).get('data', '')
        if data:
            return base64.urlsafe_b64decode(data).decode('utf-8', errors='replace')
    for part in payload.get('parts', []):
        text = _extract_body(part)
        if text:
            return text
    return ''


def get_thread_text(thread_id: str, max_chars: int = 8000) -> str:
    """
    Fetch a full Gmail thread and return it as readable plain text.
    Each message is prefixed with direction (SENT/RECEIVED), sender, and date.
    """
    creds   = get_google_credentials()
    service = build('gmail', 'v1', credentials=creds)

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
        service = build('gmail', 'v1', credentials=creds)

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
        service = build('calendar', 'v3', credentials=creds)
        
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
