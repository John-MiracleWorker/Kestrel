import sys
import os
import subprocess
import json

LOG_FILE = "/Users/tiuni/little bird alt/mcp-servers/gmail/debug.log"

def log(msg):
    with open(LOG_FILE, "a") as f:
        f.write(msg + "\n")

log("Starting Gmail MCP server...")

# Path to the actual server script
SERVER_SCRIPT = "/Users/tiuni/little bird alt/mcp-servers/gmail/server.py"

try:
    # Run the server script and pipe stdin/stdout
    process = subprocess.Popen(
        [sys.executable, SERVER_SCRIPT],
        stdin=sys.stdin,
        stdout=sys.stdout,
        stderr=subprocess.PIPE,
        text=True
    )
    
    # We need to handle stderr separately to avoid blocking
    # But for debugging, let's just wait and see if it crashes
    _, stderr = process.communicate()
    if stderr:
        log(f"Server stderr: {stderr}")
    log(f"Server exited with code: {process.returncode}")

except Exception as e:
    log(f"Exception in wrapper: {e}")
