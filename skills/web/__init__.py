"""
Web & Code Skill
Web search, URL reader, Python code execution, shell commands.
"""

import json
import os
import subprocess
import sys
import tempfile
import traceback


# ---------------------------------------------------------------------------
# Tool Definitions
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web using DuckDuckGo to find current information. Use this when you need up-to-date facts, news, or any information not in your training data.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_url",
            "description": "Read and extract the main content from a URL (web page, article, etc.). Use when you need to read a specific web page the user mentions or linked from a search.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The URL to read content from"},
                    "max_length": {"type": "integer", "description": "Maximum number of characters to return (default 10000)"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_code",
            "description": "Execute Python code in a sandboxed environment and return the output. Use for calculations, data processing, file analysis, or generating outputs. The code runs in a temporary file with access to standard libraries.",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Python code to execute"},
                    "timeout": {"type": "integer", "description": "Maximum execution time in seconds (default 30)"},
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "shell_command",
            "description": "CAUTION: Run a shell command on the user's Mac. Only use for safe, read-only commands like 'ls', 'cat', 'grep', 'find', 'which', 'brew list', 'df -h'. Never run destructive commands (rm, mkfs, dd, sudo) unless the user explicitly requests it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command to execute"},
                    "timeout": {"type": "integer", "description": "Maximum execution time in seconds (default 10)"},
                },
                "required": ["command"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Tool Implementations
# ---------------------------------------------------------------------------

def tool_web_search(query: str) -> dict:
    try:
        import urllib.request
        import urllib.parse

        url = f"https://api.duckduckgo.com/?q={urllib.parse.quote(query)}&format=json&no_html=1&skip_disambig=1"
        req = urllib.request.Request(url, headers={"User-Agent": "LibreBird/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())

        results = []
        if data.get("AbstractText"):
            results.append({"title": data.get("Heading", "Result"), "snippet": data["AbstractText"],
                            "url": data.get("AbstractURL", "")})
        for item in data.get("RelatedTopics", [])[:5]:
            if isinstance(item, dict) and "Text" in item:
                results.append({"title": item.get("Text", "")[:80], "snippet": item.get("Text", ""),
                                "url": item.get("FirstURL", "")})

        if not results:
            try:
                from duckduckgo_search import DDGS
                with DDGS() as ddgs:
                    for r in ddgs.text(query, max_results=5):
                        results.append({"title": r.get("title", ""), "snippet": r.get("body", ""),
                                        "url": r.get("href", "")})
            except ImportError:
                pass

        if not results:
            return {"query": query, "results": [], "message": "No results found. Try a different query."}
        return {"query": query, "results": results}
    except Exception as e:
        return {"query": query, "error": f"Search failed: {str(e)}"}


def tool_read_url(url: str, max_length: int = 10000) -> dict:
    try:
        import urllib.request
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) LibreBird/1.0"
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            content_type = resp.headers.get("Content-Type", "")
            raw = resp.read(200000)
            charset = "utf-8"
            if "charset=" in content_type:
                charset = content_type.split("charset=")[-1].split(";")[0].strip()
            text = raw.decode(charset, errors="replace")

        # Try to extract main content
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(text, "html.parser")
            for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
                tag.decompose()
            article = soup.find("article") or soup.find("main") or soup.find("body")
            if article:
                text = article.get_text(separator="\n", strip=True)
            else:
                text = soup.get_text(separator="\n", strip=True)
        except ImportError:
            import re
            text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"\s+", " ", text).strip()

        if len(text) > max_length:
            text = text[:max_length] + "\n\n[... truncated ...]"
        return {"url": url, "content": text, "char_count": len(text)}
    except Exception as e:
        return {"url": url, "error": f"Failed to read URL: {str(e)}"}


def tool_run_code(code: str, timeout: int = 30) -> dict:
    blocked = ["shutil.rmtree", "os.remove", "os.rmdir", "subprocess.call",
               "__import__('os').system", "exec(", "eval("]
    for b in blocked:
        if b in code:
            return {"error": f"Blocked operation: {b} — code execution is sandboxed for safety."}

    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(code)
            tmp_path = f.name
        try:
            result = subprocess.run(
                [sys.executable, tmp_path],
                capture_output=True, text=True, timeout=timeout,
                env={**os.environ, "PYTHONPATH": os.path.dirname(__file__)}
            )
            output = result.stdout
            errors = result.stderr
            if len(output) > 10000:
                output = output[:10000] + "\n[... output truncated ...]"
            if len(errors) > 5000:
                errors = errors[:5000] + "\n[... errors truncated ...]"
            return {
                "exit_code": result.returncode,
                "output": output,
                "errors": errors if errors else None,
                "status": "success" if result.returncode == 0 else "error",
            }
        finally:
            os.unlink(tmp_path)
    except subprocess.TimeoutExpired:
        return {"error": f"Code execution timed out after {timeout} seconds"}
    except Exception as e:
        return {"error": f"Code execution failed: {str(e)}"}


def tool_shell_command(command: str, timeout: int = 10) -> dict:
    dangerous = ["rm -rf", "mkfs", "dd if=", ":(){ :|:& };:", "sudo rm",
                  "> /dev/sd", "chmod -R 777 /", "curl | sh", "wget | sh"]
    cmd_lower = command.lower().strip()
    for d in dangerous:
        if d in cmd_lower:
            return {"error": f"Blocked dangerous command pattern: {d}"}

    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=timeout, cwd=os.path.expanduser("~")
        )
        output = result.stdout
        if len(output) > 10000:
            output = output[:10000] + "\n[... truncated ...]"
        return {
            "command": command,
            "exit_code": result.returncode,
            "output": output,
            "errors": result.stderr[:3000] if result.stderr else None,
        }
    except subprocess.TimeoutExpired:
        return {"error": f"Command timed out after {timeout} seconds"}
    except Exception as e:
        return {"error": f"Command execution failed: {str(e)}"}


# ---------------------------------------------------------------------------
# Tool Handlers
# ---------------------------------------------------------------------------

TOOL_HANDLERS = {
    "web_search": lambda args: tool_web_search(args.get("query", "")),
    "read_url": lambda args: tool_read_url(args.get("url", ""), args.get("max_length", 10000)),
    "run_code": lambda args: tool_run_code(args.get("code", ""), args.get("timeout", 30)),
    "shell_command": lambda args: tool_shell_command(args.get("command", ""), args.get("timeout", 10)),
}
