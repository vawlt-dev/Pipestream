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

# =============================================================================
# CALENDAR (optional, for future use)
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
