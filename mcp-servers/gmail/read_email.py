import os
import json
import base64
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

BASE_DIR = "/Users/tiuni/little bird alt/mcp-servers/gmail"
TOKEN_PATH = os.path.join(BASE_DIR, 'token.json')

def get_service():
    if not os.path.exists(TOKEN_PATH):
        print("Error: token.json not found")
        return None
    creds = Credentials.from_authorized_user_file(TOKEN_PATH, ['https://www.googleapis.com/auth/gmail.modify'])
    return build('gmail', 'v1', credentials=creds)

def read_latest_email():
    service = get_service()
    if not service: return
    
    # Get latest message
    results = service.users().messages().list(userId='me', maxResults=1).execute()
    messages = results.get('messages', [])
    
    if not messages:
        print("No messages found.")
        return
        
    msg_id = messages[0]['id']
    message = service.users().messages().get(userId='me', id=msg_id, format='full').execute()
    
    # Extract headers
    headers = message['payload']['headers']
    subject = next((h['value'] for h in headers if h['name'] == 'Subject'), 'No Subject')
    sender = next((h['value'] for h in headers if h['name'] == 'From'), 'Unknown Sender')
    date = next((h['value'] for h in headers if h['name'] == 'Date'), 'Unknown Date')
    
    # Extract body
    body = ""
    if 'parts' in message['payload']:
        for part in message['payload']['parts']:
            if part['mimeType'] == 'text/plain':
                data = part['body'].get('data')
                if data:
                    body = base64.urlsafe_b64decode(data).decode('utf-8')
                    break
    else:
        data = message['payload']['body'].get('data')
        if data:
            body = base64.urlsafe_b64decode(data).decode('utf-8')
            
    print(f"From: {sender}")
    print(f"Date: {date}")
    print(f"Subject: {subject}")
    print("-" * 40)
    print(body)

if __name__ == '__main__':
    read_latest_email()
