"""
GitHub Integration Skill
Repos, issues, pull requests, and repo stats via the GitHub REST API.
Requires GITHUB_TOKEN in .env or environment.
"""

import json
import os
import urllib.request
import urllib.parse
import urllib.error

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

from typing import Optional
def _get_token() -> Optional[str]:
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    # Try loading from .env
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), ".env")
    if os.path.isfile(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("GITHUB_TOKEN="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def _api_request(path: str, method: str = "GET", data: dict = None) -> dict:
    token = _get_token()
    if not token:
        return {"error": "GITHUB_TOKEN not configured. Add GITHUB_TOKEN=ghp_xxx to your .env file."}
    url = f"https://api.github.com{path}" if path.startswith("/") else f"https://api.github.com/{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "LibreBird/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    if body:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else ""
        return {"error": f"GitHub API {e.code}: {error_body[:500]}"}
    except Exception as e:
        return {"error": f"GitHub API request failed: {str(e)}"}


# ---------------------------------------------------------------------------
# Tool Definitions
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "github_repos",
            "description": "List GitHub repositories. Without a username, lists your own repos. With a username, lists their public repos.",
            "parameters": {
                "type": "object",
                "properties": {
                    "username": {"type": "string", "description": "GitHub username to list repos for (omit for your own)"},
                    "sort": {"type": "string", "enum": ["updated", "created", "pushed", "full_name"],
                             "description": "Sort order (default: updated)"},
                    "limit": {"type": "integer", "description": "Max repos to return (default 10)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "github_issues",
            "description": "List or create issues in a GitHub repository.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "create", "get"],
                               "description": "'list' to list issues, 'create' to create a new issue, 'get' to get a specific issue"},
                    "repo": {"type": "string", "description": "Repository in 'owner/repo' format, e.g. 'octocat/Hello-World'"},
                    "title": {"type": "string", "description": "Issue title (for create)"},
                    "body": {"type": "string", "description": "Issue body/description (for create)"},
                    "issue_number": {"type": "integer", "description": "Issue number (for get)"},
                    "state": {"type": "string", "enum": ["open", "closed", "all"], "description": "Filter by state (for list)"},
                },
                "required": ["action", "repo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "github_pr_list",
            "description": "List pull requests for a GitHub repository.",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository in 'owner/repo' format"},
                    "state": {"type": "string", "enum": ["open", "closed", "all"], "description": "Filter by state"},
                    "limit": {"type": "integer", "description": "Max PRs to return (default 10)"},
                },
                "required": ["repo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "github_repo_stats",
            "description": "Get detailed stats for a GitHub repository: stars, forks, issues, language breakdown, latest release.",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository in 'owner/repo' format"},
                },
                "required": ["repo"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Tool Implementations
# ---------------------------------------------------------------------------

def tool_github_repos(username: str = None, sort: str = "updated", limit: int = 10) -> dict:
    path = f"/users/{username}/repos?sort={sort}&per_page={limit}" if username else f"/user/repos?sort={sort}&per_page={limit}"
    result = _api_request(path)
    if isinstance(result, dict) and "error" in result:
        return result
    repos = []
    for r in (result if isinstance(result, list) else [])[:limit]:
        repos.append({
            "name": r.get("full_name", ""),
            "description": (r.get("description") or "")[:100],
            "stars": r.get("stargazers_count", 0),
            "language": r.get("language"),
            "updated": r.get("updated_at", "")[:10],
            "url": r.get("html_url", ""),
            "private": r.get("private", False),
        })
    return {"repos": repos, "count": len(repos), "user": username or "(you)"}


def tool_github_issues(action: str, repo: str, title: str = None, body: str = None,
                       issue_number: int = None, state: str = "open") -> dict:
    if action == "list":
        result = _api_request(f"/repos/{repo}/issues?state={state}&per_page=10")
        if isinstance(result, dict) and "error" in result:
            return result
        issues = []
        for i in (result if isinstance(result, list) else []):
            if "pull_request" in i:
                continue  # Skip PRs from the issues endpoint
            issues.append({
                "number": i.get("number"),
                "title": i.get("title", ""),
                "state": i.get("state", ""),
                "author": i.get("user", {}).get("login", ""),
                "created": i.get("created_at", "")[:10],
                "comments": i.get("comments", 0),
                "labels": [l.get("name") for l in i.get("labels", [])],
                "url": i.get("html_url", ""),
            })
        return {"repo": repo, "state": state, "issues": issues, "count": len(issues)}
    elif action == "create":
        if not title:
            return {"error": "title is required to create an issue"}
        data = {"title": title}
        if body:
            data["body"] = body
        result = _api_request(f"/repos/{repo}/issues", method="POST", data=data)
        if isinstance(result, dict) and "error" in result:
            return result
        return {
            "action": "created", "number": result.get("number"),
            "title": result.get("title"), "url": result.get("html_url"),
        }
    elif action == "get":
        if not issue_number:
            return {"error": "issue_number is required for 'get'"}
        result = _api_request(f"/repos/{repo}/issues/{issue_number}")
        if isinstance(result, dict) and "error" in result:
            return result
        return {
            "number": result.get("number"), "title": result.get("title"),
            "state": result.get("state"), "body": (result.get("body") or "")[:3000],
            "author": result.get("user", {}).get("login"),
            "created": result.get("created_at", "")[:10],
            "comments": result.get("comments", 0),
            "labels": [l.get("name") for l in result.get("labels", [])],
            "url": result.get("html_url"),
        }
    return {"error": f"Unknown action: {action}"}


def tool_github_pr_list(repo: str, state: str = "open", limit: int = 10) -> dict:
    result = _api_request(f"/repos/{repo}/pulls?state={state}&per_page={limit}")
    if isinstance(result, dict) and "error" in result:
        return result
    prs = []
    for pr in (result if isinstance(result, list) else [])[:limit]:
        prs.append({
            "number": pr.get("number"),
            "title": pr.get("title", ""),
            "state": pr.get("state", ""),
            "author": pr.get("user", {}).get("login", ""),
            "created": pr.get("created_at", "")[:10],
            "merged": pr.get("merged_at") is not None,
            "draft": pr.get("draft", False),
            "url": pr.get("html_url", ""),
        })
    return {"repo": repo, "state": state, "prs": prs, "count": len(prs)}


def tool_github_repo_stats(repo: str) -> dict:
    result = _api_request(f"/repos/{repo}")
    if isinstance(result, dict) and "error" in result:
        return result
    stats = {
        "name": result.get("full_name"),
        "description": result.get("description"),
        "stars": result.get("stargazers_count", 0),
        "forks": result.get("forks_count", 0),
        "watchers": result.get("subscribers_count", 0),
        "open_issues": result.get("open_issues_count", 0),
        "language": result.get("language"),
        "license": (result.get("license") or {}).get("spdx_id"),
        "created": result.get("created_at", "")[:10],
        "updated": result.get("updated_at", "")[:10],
        "size_kb": result.get("size", 0),
        "default_branch": result.get("default_branch"),
        "url": result.get("html_url"),
    }
    # Try to get language breakdown
    langs = _api_request(f"/repos/{repo}/languages")
    if isinstance(langs, dict) and "error" not in langs:
        total = sum(langs.values()) or 1
        stats["languages"] = {k: f"{v/total*100:.1f}%" for k, v in langs.items()}
    return stats


# ---------------------------------------------------------------------------
# Tool Handlers
# ---------------------------------------------------------------------------

TOOL_HANDLERS = {
    "github_repos": lambda args: tool_github_repos(
        args.get("username"), args.get("sort", "updated"), args.get("limit", 10)
    ),
    "github_issues": lambda args: tool_github_issues(
        args.get("action", "list"), args.get("repo", ""), args.get("title"),
        args.get("body"), args.get("issue_number"), args.get("state", "open")
    ),
    "github_pr_list": lambda args: tool_github_pr_list(
        args.get("repo", ""), args.get("state", "open"), args.get("limit", 10)
    ),
    "github_repo_stats": lambda args: tool_github_repo_stats(args.get("repo", "")),
}
