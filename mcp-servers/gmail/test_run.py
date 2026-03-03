import subprocess
import sys

try:
    result = subprocess.run(
        [sys.executable, "/Users/tiuni/little bird alt/mcp-servers/gmail/server.py"],
        capture_output=True,
        text=True,
        timeout=10
    )
    with open("/Users/tiuni/little bird alt/mcp-servers/gmail/test_output.txt", "w") as f:
        f.write("STDOUT:\n")
        f.write(result.stdout)
        f.write("\nSTDERR:\n")
        f.write(result.stderr)
except Exception as e:
    with open("/Users/tiuni/little bird alt/mcp-servers/gmail/test_output.txt", "w") as f:
        f.write(f"Error: {e}")
