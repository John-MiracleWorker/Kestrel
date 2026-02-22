"""
Moltbook tools — connect to the AI agent social network.

Provides Moltbook integration (registration, posting, commenting, voting,
feed browsing, search) so Kestrel can participate in the agent community.
All activity is logged to the moltbook_activity table for human visibility.
"""

import json
import logging
import os
from typing import Optional

import httpx

from agent.types import RiskLevel, ToolDefinition

logger = logging.getLogger("brain.agent.tools.moltbook")

# Module-level workspace context — set before execution by the tool registry
_current_workspace_id: Optional[str] = None

BASE_URL = "https://www.moltbook.com/api/v1"
CREDS_PATH = os.path.expanduser("~/.config/moltbook/credentials.json")


def _load_api_key() -> Optional[str]:
    """Load Moltbook API key from env or credentials file."""
    key = os.environ.get("MOLTBOOK_API_KEY")
    if key:
        return key
    try:
        if os.path.exists(CREDS_PATH):
            with open(CREDS_PATH) as f:
                creds = json.load(f)
            return creds.get("api_key")
    except Exception:
        pass
    return None


def _save_credentials(api_key: str, agent_name: str, claim_url: str = ""):
    """Save Moltbook credentials to disk."""
    os.makedirs(os.path.dirname(CREDS_PATH), exist_ok=True)
    creds = {
        "api_key": api_key,
        "agent_name": agent_name,
        "claim_url": claim_url,
    }
    with open(CREDS_PATH, "w") as f:
        json.dump(creds, f, indent=2)
    logger.info(f"Moltbook credentials saved to {CREDS_PATH}")


def _headers(api_key: str = "") -> dict:
    """Build request headers with auth."""
    key = api_key or _load_api_key()
    h = {"Content-Type": "application/json"}
    if key:
        h["Authorization"] = f"Bearer {key}"
    return h


async def _log_activity(
    action: str,
    title: str = "",
    content: str = "",
    submolt: str = "",
    post_id: str = "",
    url: str = "",
    result: dict = None,
) -> None:
    """Log Moltbook activity to the database for the UI feed."""
    try:
        # Import the brain's DB pool
        import server as _server
        pool = await _server.get_pool()
        await pool.execute(
            """INSERT INTO moltbook_activity
               (workspace_id, action, title, content, submolt, post_id, url, result)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8)""",
            _current_workspace_id,
            action,
            title[:500] if title else None,
            content[:1000] if content else None,
            submolt or None,
            post_id or None,
            url or None,
            json.dumps(result) if result else None,
        )
    except Exception as e:
        logger.warning(f"Failed to log moltbook activity: {e}")


def register_moltbook_tools(registry) -> None:
    """Register Moltbook social network tools."""

    registry.register(
        definition=ToolDefinition(
            name="moltbook",
            description=(
                "Interact with Moltbook, the social network for AI agents. "
                "Use action='register' to create your account, 'post' to share content, "
                "'feed' to browse, 'comment' to reply, 'upvote' to vote, "
                "'search' to find content, 'profile' to check your profile, "
                "or 'submolts' to list communities. "
                "Your human can see all your Moltbook activity."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "register", "status", "profile",
                            "feed", "post", "comment", "upvote",
                            "search", "submolts",
                        ],
                        "description": "The Moltbook action to perform",
                    },
                    "submolt": {
                        "type": "string",
                        "description": "Submolt (community) name for posting (e.g. 'general')",
                    },
                    "title": {
                        "type": "string",
                        "description": "Post title (for action='post')",
                    },
                    "content": {
                        "type": "string",
                        "description": "Post or comment content",
                    },
                    "post_id": {
                        "type": "string",
                        "description": "Post ID (for comment/upvote actions)",
                    },
                    "query": {
                        "type": "string",
                        "description": "Search query (for action='search')",
                    },
                    "sort": {
                        "type": "string",
                        "enum": ["hot", "new", "top", "rising"],
                        "description": "Feed sort order (default: hot)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Number of results to return (default: 10)",
                    },
                    "name": {
                        "type": "string",
                        "description": "Agent name for registration (default: Kestrel)",
                    },
                },
                "required": ["action"],
            },
            risk_level=RiskLevel.MEDIUM,
            timeout_seconds=30,
            category="social",
        ),
        handler=moltbook_action,
    )


async def moltbook_action(
    action: str,
    submolt: str = "general",
    title: str = "",
    content: str = "",
    post_id: str = "",
    query: str = "",
    sort: str = "hot",
    limit: int = 10,
    name: str = "Kestrel",
) -> dict:
    """Route to the appropriate Moltbook action."""
    actions = {
        "register": _register,
        "status": _check_status,
        "profile": _get_profile,
        "feed": _get_feed,
        "post": _create_post,
        "comment": _add_comment,
        "upvote": _upvote,
        "search": _search,
        "submolts": _list_submolts,
    }

    handler = actions.get(action)
    if not handler:
        return {"error": f"Unknown action: {action}. Use one of: {list(actions.keys())}"}

    # Check if we need an API key (everything except register)
    if action != "register":
        api_key = _load_api_key()
        if not api_key:
            return {
                "error": "Not registered on Moltbook yet. Use action='register' first.",
                "hint": "Call moltbook with action='register' to create your account.",
            }

    try:
        result = await handler(
            submolt=submolt, title=title, content=content,
            post_id=post_id, query=query, sort=sort, limit=limit,
            name=name,
        )

        # Log activity for the UI feed
        await _log_activity(
            action=action,
            title=title,
            content=content,
            submolt=submolt,
            post_id=result.get("post_id", post_id),
            url=result.get("url", result.get("claim_url", "")),
            result=result,
        )

        return result
    except Exception as e:
        logger.error(f"Moltbook {action} error: {e}", exc_info=True)
        error_msg = str(e).strip() or repr(e) or f"Moltbook {action} failed"
        return {"error": error_msg, "action": action}


async def _register(name: str = "Kestrel", **kwargs) -> dict:
    """Register Kestrel on Moltbook."""
    # Check if already registered locally
    existing_key = _load_api_key()
    if existing_key:
        return {
            "status": "already_registered",
            "message": "Already registered on Moltbook! Use action='status' to check claim status.",
            "api_key_present": True,
        }

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            f"{BASE_URL}/agents/register",
            json={
                "name": name,
                "description": (
                    "Autonomous AI agent from the Libre Bird platform. "
                    "I plan, use tools, reflect, and learn. Privacy-focused."
                ),
            },
            headers={"Content-Type": "application/json"},
        )

        # 409 means already registered on the server side
        if resp.status_code == 409:
            # Try to extract API key from the response body
            try:
                conflict_data = resp.json()
                api_key = conflict_data.get("api_key", conflict_data.get("agent", {}).get("api_key", ""))
                if api_key:
                    _save_credentials(api_key, "Kestrel")
                    return {
                        "status": "already_registered",
                        "message": "Already registered on Moltbook server. Credentials recovered.",
                        "api_key_saved": True,
                    }
            except Exception:
                pass
            return {
                "status": "already_registered",
                "message": "Already registered on Moltbook! Check MOLTBOOK_API_KEY env var or use action='status'.",
            }

        # 429 means rate limited
        if resp.status_code == 429:
            try:
                limit_data = resp.json()
                retry_after = limit_data.get("retry_after_seconds", "unknown")
            except Exception:
                retry_after = "unknown"
            return {
                "status": "rate_limited",
                "message": f"Registration rate limited by Moltbook. Try again later (retry after ~{retry_after}s).",
                "hint": "The Moltbook API has a rate limit on registrations. Wait and try again.",
            }

        resp.raise_for_status()
        data = resp.json()

    agent_data = data.get("agent", data)
    api_key = agent_data.get("api_key", "")
    claim_url = agent_data.get("claim_url", "")
    verification_code = agent_data.get("verification_code", "")

    if api_key:
        _save_credentials(api_key, "Kestrel", claim_url)

    return {
        "status": "registered",
        "message": "🦞 Registered on Moltbook! Send the claim URL to your human.",
        "claim_url": claim_url,
        "verification_code": verification_code,
        "important": "Your human needs to visit the claim URL and verify via tweet.",
        "api_key_saved": bool(api_key),
    }


async def _check_status(**kwargs) -> dict:
    """Check claim/account status."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{BASE_URL}/agents/status",
            headers=_headers(),
        )
        resp.raise_for_status()
        return resp.json()


async def _get_profile(**kwargs) -> dict:
    """Get own profile."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{BASE_URL}/agents/me",
            headers=_headers(),
        )
        resp.raise_for_status()
        return resp.json()


async def _get_feed(sort: str = "hot", limit: int = 10, submolt: str = "", **kwargs) -> dict:
    """Get the Moltbook feed."""
    limit = min(limit, 25)
    params = {"sort": sort, "limit": str(limit)}

    url = f"{BASE_URL}/posts"
    if submolt:
        url = f"{BASE_URL}/submolts/{submolt}/feed"

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, params=params, headers=_headers())
        resp.raise_for_status()
        data = resp.json()

    # Format posts for readability
    posts = data.get("data", data.get("posts", []))
    formatted = []
    for p in posts[:limit]:
        formatted.append({
            "id": p.get("id", p.get("_id", "")),
            "title": p.get("title", ""),
            "content": (p.get("content", "") or "")[:200],
            "author": p.get("author", {}).get("name", p.get("author_name", "unknown")),
            "submolt": p.get("submolt", ""),
            "upvotes": p.get("upvotes", 0),
            "comments": p.get("comment_count", p.get("comments", 0)),
            "url": f"https://www.moltbook.com/m/{p.get('submolt', 'general')}/{p.get('id', p.get('_id', ''))}",
        })

    return {
        "feed": formatted,
        "count": len(formatted),
        "sort": sort,
        "submolt": submolt or "all",
    }


async def _create_post(submolt: str = "general", title: str = "", content: str = "", **kwargs) -> dict:
    """Create a post on Moltbook."""
    if not title:
        return {"error": "A title is required to create a post."}

    payload = {"submolt": submolt, "title": title}
    if content:
        payload["content"] = content

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            f"{BASE_URL}/posts",
            json=payload,
            headers=_headers(),
        )
        resp.raise_for_status()
        data = resp.json()

    # Handle verification challenge
    verification = data.get("verification")
    if verification:
        challenge = verification.get("challenge", "")
        verify_id = verification.get("id", "")
        logger.info(f"Moltbook verification challenge: {challenge}")

        # Auto-solve math challenge
        answer = _solve_challenge(challenge)
        if answer is not None:
            verify_resp = await client.post(
                f"{BASE_URL}/posts/verify",
                json={"id": verify_id, "answer": answer},
                headers=_headers(),
            )
            if verify_resp.status_code == 200:
                data["verification_result"] = "✅ Challenge solved!"
            else:
                data["verification_result"] = f"❌ Challenge failed: {verify_resp.text}"

    post_data = data.get("data", data.get("post", data))
    post_id = post_data.get("id", post_data.get("_id", ""))

    return {
        "status": "posted",
        "post_id": post_id,
        "title": title,
        "submolt": submolt,
        "url": f"https://www.moltbook.com/m/{submolt}/{post_id}",
        "message": f"🦞 Posted '{title}' to m/{submolt}",
        "verification": data.get("verification_result", "no challenge"),
    }


async def _add_comment(post_id: str = "", content: str = "", **kwargs) -> dict:
    """Add a comment to a post."""
    if not post_id:
        return {"error": "post_id is required to comment."}
    if not content:
        return {"error": "content is required for a comment."}

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{BASE_URL}/posts/{post_id}/comments",
            json={"content": content},
            headers=_headers(),
        )
        resp.raise_for_status()
        data = resp.json()

    # Handle verification challenge for comments too
    verification = data.get("verification")
    if verification:
        challenge = verification.get("challenge", "")
        verify_id = verification.get("id", "")
        answer = _solve_challenge(challenge)
        if answer is not None:
            verify_resp = await client.post(
                f"{BASE_URL}/comments/verify",
                json={"id": verify_id, "answer": answer},
                headers=_headers(),
            )
            if verify_resp.status_code == 200:
                data["verification_result"] = "✅ Challenge solved!"

    return {
        "status": "commented",
        "post_id": post_id,
        "content": content[:100],
        "message": f"💬 Commented on post {post_id}",
    }


async def _upvote(post_id: str = "", **kwargs) -> dict:
    """Upvote a post."""
    if not post_id:
        return {"error": "post_id is required to upvote."}

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{BASE_URL}/posts/{post_id}/upvote",
            headers=_headers(),
        )
        resp.raise_for_status()
        return {
            "status": "upvoted",
            "post_id": post_id,
            "message": f"👍 Upvoted post {post_id}",
        }


async def _search(query: str = "", limit: int = 10, **kwargs) -> dict:
    """Semantic search on Moltbook."""
    if not query:
        return {"error": "query is required for search."}

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{BASE_URL}/search",
            params={"q": query, "limit": str(min(limit, 25))},
            headers=_headers(),
        )
        resp.raise_for_status()
        data = resp.json()

    results = data.get("data", data.get("results", []))
    formatted = []
    for r in results:
        formatted.append({
            "id": r.get("id", r.get("_id", "")),
            "title": r.get("title", ""),
            "content": (r.get("content", "") or "")[:200],
            "author": r.get("author", {}).get("name", r.get("author_name", "")),
            "score": r.get("score", r.get("similarity", 0)),
            "type": r.get("type", "post"),
        })

    return {
        "query": query,
        "results": formatted,
        "count": len(formatted),
    }


async def _list_submolts(**kwargs) -> dict:
    """List available submolts (communities)."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{BASE_URL}/submolts",
            headers=_headers(),
        )
        resp.raise_for_status()
        data = resp.json()

    submolts = data.get("data", data.get("submolts", []))
    formatted = []
    for s in submolts:
        formatted.append({
            "name": s.get("name", ""),
            "description": s.get("description", ""),
            "members": s.get("member_count", s.get("subscribers", 0)),
            "url": f"https://www.moltbook.com/m/{s.get('name', '')}",
        })

    return {
        "submolts": formatted,
        "count": len(formatted),
    }


def _solve_challenge(challenge: str) -> Optional[int]:
    """
    Auto-solve Moltbook verification challenges.
    These are simple math problems like "What is 7 + 3?"
    """
    import re
    # Match patterns like "What is 123 + 456?" or "123+456" or "123 * 456"
    match = re.search(r'(\d+)\s*([+\-*×÷/])\s*(\d+)', challenge)
    if not match:
        logger.warning(f"Could not parse challenge: {challenge}")
        return None

    a, op, b = int(match.group(1)), match.group(2), int(match.group(3))

    if op in ('+',):
        return a + b
    elif op in ('-',):
        return a - b
    elif op in ('*', '×'):
        return a * b
    elif op in ('/', '÷'):
        return a // b if b != 0 else None

    return None
