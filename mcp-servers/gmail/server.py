import sys
import json
import os
import base64
import subprocess
import importlib

# Get the directory of the current script
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Ensure dependencies are installed
required_packages = {
    'google.oauth2.credentials': 'google-auth',
    'google_auth_oauthlib.flow': 'google-auth-oauthlib',
    'google.auth.transport.requests': 'google-auth-httplib2',
    'googleapiclient.discovery': 'google-api-python-client'
}

for module, package in required_packages.items():
    try:
        importlib.import_module(module.split('.')[0])
    except ImportError:
        print(f"Installing missing package {package}...", file=sys.stderr)
        subprocess.check_call([sys.executable, "-m", "pip", "install", package, "--quiet"])

from email.mime.text import MIMEText
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# If modifying these SCOPES, delete the file token.json.
SCOPES = ['https://www.googleapis.com/auth/gmail.modify']

def get_service():
    creds = None
    token_path = os.path.join(BASE_DIR, 'token.json')
    creds_path = os.path.join(BASE_DIR, 'credentials.json')
    
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(creds_path):
                raise RuntimeError(f"credentials.json not found at {creds_path}. Please set up Gmail API OAuth credentials and save them to credentials.json.")
            flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, 'w') as token:
            token.write(creds.to_json())
    return build('gmail', 'v1', credentials=creds)

def list_messages(query='', max_results=10):
    service = get_service()
    results = service.users().messages().list(userId='me', q=query, maxResults=max_results).execute()
    messages = results.get('messages', [])
    return messages

def get_message(message_id):
    service = get_service()
    message = service.users().messages().get(userId='me', id=message_id, format='full').execute()
    return message

def send_message(to, subject, body):
    service = get_service()
    message = MIMEText(body)
    message['to'] = to
    message['subject'] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    return service.users().messages().send(userId='me', body={'raw': raw}).execute()

def handle_request(request):
    method = request.get('method')
    params = request.get('params', {})
    
    if method == 'initialize':
        return {
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "tools": {
                    "list_messages": {"description": "List Gmail messages"},
                    "get_message": {"description": "Get a specific Gmail message"},
                    "send_message": {"description": "Send a Gmail message"},
                    "search_messages": {"description": "Search Gmail messages"}
                }
            },
            "serverInfo": {"name": "gmail-mcp", "version": "0.1.0"}
        }
    elif method == 'tools/list':
        return {
            "tools": [
                {
                    "name": "gmail_list_messages",
                    "description": "List messages in the user's mailbox",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "Search query"},
                            "max_results": {"type": "integer", "default": 10}
                        }
                    }
                },
                {
                    "name": "gmail_get_message",
                    "description": "Retrieve a specific message by ID",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "message_id": {"type": "string", "description": "The ID of the message to retrieve"}
                        },
                        "required": ["message_id"]
                    }
                },
                {
                    "name": "gmail_send_message",
                    "description": "Send a new email message",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "to": {"type": "string", "description": "Recipient email address"},
                            "subject": {"type": "string", "description": "Email subject"},
                            "body": {"type": "string", "description": "Email body content"}
                        },
                        "required": ["to", "subject", "body"]
                    }
                }
            ]
        }
    elif method == 'tools/call':
        name = params.get('name')
        args = params.get('arguments', {})
        if name == 'gmail_list_messages':
            return {"content": [{"type": "text", "text": json.dumps(list_messages(args.get('query', ''), args.get('max_results', 10)))}]}
        elif name == 'gmail_get_message':
            return {"content": [{"type": "text", "text": json.dumps(get_message(args.get('message_id')))}]}
        elif name == 'gmail_send_message':
            return {"content": [{"type": "text", "text": json.dumps(send_message(args.get('to'), args.get('subject'), args.get('body')))}]}
    
    return {"error": {"code": -32601, "message": f"Method {method} not found"}}

def main():
    for line in sys.stdin:
        try:
            request = json.loads(line)
            response = handle_request(request)
            out = json.dumps({"jsonrpc": "2.0", "id": request.get('id'), "result": response})
            sys.stdout.write(out + "\n")
            sys.stdout.flush()
        except Exception as e:
            out = json.dumps({"jsonrpc": "2.0", "id": request.get('id'), "error": {"code": -32603, "message": str(e)}})
            sys.stdout.write(out + "\n")
            sys.stdout.flush()

if __name__ == '__main__':
    main()
