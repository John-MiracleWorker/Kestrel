"""
API Caller Skill
Make arbitrary HTTP requests to REST APIs.
Uses only stdlib (urllib) — zero dependencies.
"""

import json
import logging
import urllib.request
import urllib.parse
import urllib.error

logger = logging.getLogger("libre_bird.skills.api_caller")

_DEFAULT_TIMEOUT = 15
_MAX_RESPONSE = 10000  # chars


def _do_request(url: str, method: str = "GET", headers: dict = None,
                body: dict = None, timeout: int = _DEFAULT_TIMEOUT) -> dict:
    """Perform an HTTP request and return structured result."""
    if not url:
        return {"error": "url is required"}

    headers = headers or {}
    if "User-Agent" not in headers:
        headers["User-Agent"] = "LibreBird/1.0"

    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        if "Content-Type" not in headers:
            headers["Content-Type"] = "application/json"

    try:
        req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = resp.status
            resp_headers = dict(resp.getheaders())

            # Try to parse as JSON
            try:
                parsed = json.loads(raw)
                response_body = parsed
            except (json.JSONDecodeError, ValueError):
                # Truncate long text responses
                if len(raw) > _MAX_RESPONSE:
                    raw = raw[:_MAX_RESPONSE] + f"\n... [truncated at {_MAX_RESPONSE} chars]"
                response_body = raw

            return {
                "status_code": status,
                "headers": {k: v for k, v in resp_headers.items()
                           if k.lower() in ("content-type", "x-ratelimit-remaining", "x-request-id")},
                "body": response_body,
            }

    except urllib.error.HTTPError as e:
        error_body = ""
        try:
            error_body = e.read().decode("utf-8", errors="replace")[:2000]
        except Exception:
            pass
        return {"error": f"HTTP {e.code}: {e.reason}", "status_code": e.code, "body": error_body}
    except urllib.error.URLError as e:
        return {"error": f"Connection error: {e.reason}"}
    except Exception as e:
        return {"error": str(e)}


def tool_http_request(args: dict) -> dict:
    """Make a generic HTTP request with full control over method, headers, and body."""
    url = args.get("url", "").strip()
    method = args.get("method", "GET").upper()
    timeout = int(args.get("timeout", _DEFAULT_TIMEOUT))

    headers = args.get("headers")
    if isinstance(headers, str):
        try:
            headers = json.loads(headers)
        except (json.JSONDecodeError, ValueError):
            return {"error": "headers must be a JSON object"}

    body = args.get("body")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            return {"error": "body must be a JSON object"}

    return _do_request(url, method, headers, body, timeout)


def tool_http_get(args: dict) -> dict:
    """Simple GET request to a URL. Good for fetching API data."""
    url = args.get("url", "").strip()

    # Append query params if provided
    params = args.get("params")
    if params:
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except (json.JSONDecodeError, ValueError):
                pass
        if isinstance(params, dict):
            sep = "&" if "?" in url else "?"
            url += sep + urllib.parse.urlencode(params)

    headers = args.get("headers")
    if isinstance(headers, str):
        try:
            headers = json.loads(headers)
        except (json.JSONDecodeError, ValueError):
            headers = None

    return _do_request(url, "GET", headers)


def tool_http_post(args: dict) -> dict:
    """POST JSON data to a URL. Good for webhooks, form submissions, and API calls."""
    url = args.get("url", "").strip()

    body = args.get("body")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            return {"error": "body must be a JSON object"}

    headers = args.get("headers")
    if isinstance(headers, str):
        try:
            headers = json.loads(headers)
        except (json.JSONDecodeError, ValueError):
            headers = None

    return _do_request(url, "POST", headers, body)


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "http_request",
            "description": "Make an HTTP request to any URL with full control over method, headers, and body. Supports GET, POST, PUT, PATCH, DELETE.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Full URL to request"},
                    "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"], "description": "HTTP method (default GET)"},
                    "headers": {"type": "string", "description": "JSON object of request headers"},
                    "body": {"type": "string", "description": "JSON object for request body (POST/PUT/PATCH)"},
                    "timeout": {"type": "integer", "description": "Timeout in seconds (default 15)"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "http_get",
            "description": "Simple GET request to fetch data from a URL or API endpoint.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to fetch"},
                    "params": {"type": "string", "description": "JSON object of query parameters"},
                    "headers": {"type": "string", "description": "JSON object of request headers (e.g. for API keys)"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "http_post",
            "description": "POST JSON data to a URL. Useful for webhooks, Slack messages, API calls, and form submissions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to POST to"},
                    "body": {"type": "string", "description": "JSON object for the request body"},
                    "headers": {"type": "string", "description": "JSON object of request headers"},
                },
                "required": ["url", "body"],
            },
        },
    },
]

TOOL_HANDLERS = {
    "http_request": tool_http_request,
    "http_get": tool_http_get,
    "http_post": tool_http_post,
}
