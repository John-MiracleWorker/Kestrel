import sys
import os
import subprocess
import importlib

# Ensure dependencies are installed
required_packages = {
    'mcp': 'mcp',
    'google.oauth2.credentials': 'google-auth',
    'google_auth_oauthlib.flow': 'google-auth-oauthlib',
    'google.auth.transport.requests': 'google-auth-httplib2',
    'googleapiclient.discovery': 'google-api-python-client'
}

for module, package in required_packages.items():
    try:
        if module == 'mcp':
            importlib.import_module('mcp')
        else:
            importlib.import_module(module.split('.')[0])
    except ImportError:
        print(f"Installing missing package {package}...", file=sys.stderr)
        subprocess.check_call([sys.executable, "-m", "pip", "install", package, "--quiet"])

from mcp.server.fastmcp import FastMCP
import json
import base64
from email.mime.text import MIMEText
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_PATH = os.path.join(BASE_DIR, 'token.json')
SCOPES = ['https://www.googleapis.com/auth/gmail.modify']

# Create FastMCP server
mcp = FastMCP("Gmail")

def get_service():
    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            raise Exception("Authentication required. Please run auth.py first.")
        with open(TOKEN_PATH, 'w') as token:
            token.write(creds.to_json())
    return build('gmail', 'v1', credentials=creds)

@mcp.tool()
def list_emails(query: str = '', max_results: int = 5) -> str:
    """List recent emails from Gmail. Returns a JSON string of email summaries."""
    try:
        service = get_service()
        results = service.users().messages().list(userId='me', q=query, maxResults=max_results).execute()
        messages = results.get('messages', [])
        
        if not messages:
            return "No messages found."
            
        summaries = []
        for msg in messages:
            message = service.users().messages().get(userId='me', id=msg['id'], format='metadata', metadataHeaders=['Subject', 'From', 'Date']).execute()
            headers = message['payload']['headers']
            subject = next((h['value'] for h in headers if h['name'] == 'Subject'), 'No Subject')
            sender = next((h['value'] for h in headers if h['name'] == 'From'), 'Unknown Sender')
            date = next((h['value'] for h in headers if h['name'] == 'Date'), 'Unknown Date')
            summaries.append(f"ID: {msg['id']} | From: {sender} | Date: {date} | Subject: {subject}")
            
        return "\n".join(summaries)
    except Exception as e:
        return f"Error: {str(e)}"

@mcp.tool()
def read_email(message_id: str) -> str:
    """Read the full content of a specific email by ID."""
    try:
        service = get_service()
        message = service.users().messages().get(userId='me', id=message_id, format='full').execute()
        
        headers = message['payload']['headers']
        subject = next((h['value'] for h in headers if h['name'] == 'Subject'), 'No Subject')
        sender = next((h['value'] for h in headers if h['name'] == 'From'), 'Unknown Sender')
        date = next((h['value'] for h in headers if h['name'] == 'Date'), 'Unknown Date')
        
        body = ""
        def get_body(payload):
            if 'parts' in payload:
                for part in payload['parts']:
                    if part['mimeType'] == 'text/plain':
                        data = part['body'].get('data')
                        if data:
                            return base64.urlsafe_b64decode(data).decode('utf-8')
                    elif 'parts' in part:
                        res = get_body(part)
                        if res: return res
            else:
                data = payload['body'].get('data')
                if data:
                    return base64.urlsafe_b64decode(data).decode('utf-8')
            return ""

        body = get_body(message['payload'])
        
        return f"From: {sender}\nDate: {date}\nSubject: {subject}\n\n{body}"
    except Exception as e:
        return f"Error: {str(e)}"

if __name__ == "__main__":
    mcp.run()
