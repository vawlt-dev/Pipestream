import pickle
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    'https://www.googleapis.com/auth/gmail.send',
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/calendar',
]

flow = InstalledAppFlow.from_client_secrets_file('workspace/credentials.json', SCOPES)
creds = flow.run_local_server(port=0)

with open('workspace/token.pickle', 'wb') as f:
    pickle.dump(creds, f)

print("Done! token.pickle saved to workspace/token.pickle")
