from __future__ import annotations

from . import native_storage as _native_storage

globals().update({name: value for name, value in vars(_native_storage).items() if not name.startswith("__")})

class NativeRuntimePolicy(RuntimePolicy):
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    def runtime_profile(self) -> dict[str, Any]:
        return {
            "runtime_mode": "native",
            "policy_name": "NativeRuntimePolicy",
            "policy_version": "1",
            "docker_enabled": False,
            "native_enabled": True,
            "hybrid_fallback_visible": False,
            "host_mounts": [{"path": str(Path.home()), "mode": "read-write"}],
            "runtime_capabilities": {
                "unix_socket_control": "true",
                "sqlite_wal": "true",
                "docker_required": "false",
                "loopback_http_primary": "false",
            },
        }

    def evaluate_command(self, command: str) -> dict[str, Any]:
        normalized = (command or "").strip().lower()
        destructive_markers = (
            "rm ",
            "mv ",
            "chmod ",
            "chown ",
            "git reset --hard",
            "diskutil",
            "launchctl unload",
            "killall",
        )
        mutating_markers = destructive_markers + (
            "touch ",
            "mkdir ",
            "rmdir ",
            "cp ",
            "git commit",
            "git checkout ",
            "git clean",
            "pip install",
            "npm install",
            "brew install",
        )
        broad_control = bool(self.config.get("permissions", {}).get("broad_local_control", True))
        require_approval = bool(
            self.config.get("permissions", {}).get("require_approval_for_mutations", True)
        )
        risk_class = "read_only"
        approval_required = False
        if any(marker in normalized for marker in destructive_markers):
            risk_class = "destructive"
            approval_required = True
        elif any(marker in normalized for marker in mutating_markers):
            risk_class = "mutating"
            approval_required = require_approval
        allowed = broad_control or risk_class == "read_only"
        return {
            "allowed": allowed,
            "risk_class": risk_class,
            "approval_required": approval_required,
        }


async def _http_get_json(url: str, timeout_seconds: float = 2.5) -> Any:
    if httpx is None:
        raise RuntimeError("httpx is required for model detection")
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.json()


async def _http_post_json(url: str, payload: dict[str, Any], timeout_seconds: float = 60) -> Any:
    if httpx is None:
        raise RuntimeError("httpx is required for model inference")
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        response = await client.post(url, json=payload)
        response.raise_for_status()
        return response.json()


async def detect_local_model_runtime(config: dict[str, Any]) -> dict[str, Any]:
    models_cfg = config.get("models", {})
    ollama_url = str(models_cfg.get("ollama_url") or DEFAULT_CONFIG["models"]["ollama_url"]).rstrip("/")
    lmstudio_url = str(models_cfg.get("lmstudio_url") or DEFAULT_CONFIG["models"]["lmstudio_url"]).rstrip("/")
    providers: dict[str, Any] = {}

    try:
        payload = await _http_get_json(f"{ollama_url}/api/tags")
        models = [item.get("name", "") for item in payload.get("models", []) if item.get("name")]
        providers["ollama"] = {"ready": True, "models": models, "base_url": ollama_url}
    except Exception as exc:
        providers["ollama"] = {"ready": False, "models": [], "base_url": ollama_url, "error": str(exc)}

    try:
        payload = await _http_get_json(f"{lmstudio_url}/v1/models")
        models = [item.get("id", "") for item in payload.get("data", []) if item.get("id")]
        providers["lmstudio"] = {"ready": True, "models": models, "base_url": lmstudio_url}
    except Exception as exc:
        providers["lmstudio"] = {"ready": False, "models": [], "base_url": lmstudio_url, "error": str(exc)}

    preferred_provider = models_cfg.get("preferred_provider", "auto")
    preferred_model = models_cfg.get("preferred_model", "")
    default_provider = ""
    default_model = ""
    if preferred_provider != "auto" and providers.get(preferred_provider, {}).get("ready"):
        default_provider = preferred_provider
        default_model = preferred_model or providers[preferred_provider]["models"][:1][0]
    else:
        for name in ("ollama", "lmstudio"):
            if providers.get(name, {}).get("ready") and providers[name]["models"]:
                default_provider = name
                default_model = preferred_model or providers[name]["models"][0]
                break
    return {
        "preferred_provider": preferred_provider,
        "preferred_model": preferred_model,
        "default_provider": default_provider,
        "default_model": default_model,
        "providers": providers,
    }


@dataclass(frozen=True)
class NativeToolSpec:
    name: str
    description: str
    category: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] = field(default_factory=dict)
    risk_class: str = "read_only"
    approval_required: bool = False
    runtime: str = "builtin"
    entrypoint: str = ""
    aliases: tuple[str, ...] = ()
    setup_notes: list[str] = field(default_factory=list)

    def to_openai_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }

    def to_prompt_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "risk_class": self.risk_class,
            "approval_required": self.approval_required,
            "runtime": self.runtime,
            "aliases": list(self.aliases),
            "input_schema": self.input_schema,
            "setup_notes": list(self.setup_notes),
        }


@dataclass
class NativeExecutionResult:
    tool_name: str
    success: bool
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    risk_class: str = "read_only"
    approval_required: bool = False
    approval_operation: str = ""
    approval_payload: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "tool_name": self.tool_name,
            "success": self.success,
            "message": self.message,
            "data": dict(self.data),
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_code": self.exit_code,
            "artifacts": list(self.artifacts),
            "risk_class": self.risk_class,
            "approval_required": self.approval_required,
            "approval_operation": self.approval_operation,
            "approval_payload": dict(self.approval_payload),
            "metadata": dict(self.metadata),
        }
        return payload

    def to_text(self) -> str:
        if self.approval_required:
            detail = self.approval_payload.get("summary") or self.message or "Approval required."
            return f"Approval required: {detail}"
        if self.message:
            return self.message
        if self.stdout or self.stderr:
            pieces = []
            if self.stdout:
                pieces.append(self.stdout)
            if self.stderr:
                pieces.append(f"[stderr] {self.stderr}")
            return "\n".join(piece for piece in pieces if piece).strip() or "(no output)"
        if self.data:
            return json.dumps(self.data, indent=2, sort_keys=True)
        return "Success" if self.success else "Error"


@dataclass
class NativePlanStep:
    id: str
    description: str
    success_criteria: str = ""
    preferred_tools: list[str] = field(default_factory=list)
    status: str = "pending"
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "success_criteria": self.success_criteria,
            "preferred_tools": list(self.preferred_tools),
            "status": self.status,
            "notes": self.notes,
        }


@dataclass
class NativePlan:
    goal: str
    summary: str
    steps: list[NativePlanStep]
    reasoning: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "summary": self.summary,
            "reasoning": self.reasoning,
            "steps": [step.to_dict() for step in self.steps],
        }


@dataclass
class NativeAgentOutcome:
    status: str
    message: str
    provider: str
    model: str
    plan: dict[str, Any] | None = None
    approval: dict[str, Any] | None = None
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "message": self.message,
            "provider": self.provider,
            "model": self.model,
            "plan": self.plan,
            "approval": self.approval,
            "artifacts": list(self.artifacts),
            "state": dict(self.state),
        }


@dataclass
class NativeToolContext:
    paths: KestrelPaths
    config: dict[str, Any]
    runtime_policy: RuntimePolicy
    vector_store: VectorMemoryStore | None = None
    task_id: str = ""
    workspace_root: Path = field(default_factory=Path.cwd)
    approved: bool = False


def _truncate_text(value: str, limit: int = 12_000) -> str:
    text = value or ""
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-(limit // 3) :]
    omitted = len(text) - len(head) - len(tail)
    return f"{head}\n\n... ({omitted} chars omitted) ...\n\n{tail}"


def _strip_wrappers(text: str) -> str:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if len(lines) >= 2 and lines[-1].strip() == "```":
            cleaned = "\n".join(lines[1:-1]).strip()
        else:
            cleaned = "\n".join(lines[1:]).strip()
    return cleaned


def _escape_json_string_control_chars(raw: str) -> str:
    repaired: list[str] = []
    in_string = False
    escape = False
    for char in raw:
        if in_string:
            if escape:
                repaired.append(char)
                escape = False
                continue
            if char == "\\":
                repaired.append(char)
                escape = True
                continue
            if char == '"':
                repaired.append(char)
                in_string = False
                continue
            if char == "\n":
                repaired.append("\\n")
                continue
            if char == "\r":
                repaired.append("\\r")
                continue
            if char == "\t":
                repaired.append("\\t")
                continue
            if ord(char) < 0x20:
                repaired.append(f"\\u{ord(char):04x}")
                continue
            repaired.append(char)
            continue
        repaired.append(char)
        if char == '"':
            in_string = True
    return "".join(repaired)


def _load_possible_json_object(raw: str) -> dict[str, Any] | None:
    candidates = [raw]
    repaired = _escape_json_string_control_chars(raw)
    if repaired != raw:
        candidates.append(repaired)
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _extract_json_object(text: str) -> dict[str, Any]:
    cleaned = _strip_wrappers(text)
    parsed = _load_possible_json_object(cleaned)
    if parsed is not None:
        return parsed

    start = cleaned.find("{")
    while start >= 0:
        depth = 0
        in_string = False
        escape = False
        for index in range(start, len(cleaned)):
            char = cleaned[index]
            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
                continue
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    snippet = cleaned[start : index + 1]
                    parsed = _load_possible_json_object(snippet)
                    if parsed is not None:
                        return parsed
                    break
        start = cleaned.find("{", start + 1)
    raise ValueError(f"Could not extract JSON object from model response: {cleaned[:200]!r}")


async def _generate_local_text_response(
    *,
    messages: list[dict[str, Any]],
    config: dict[str, Any],
    temperature: float = 0.2,
    max_tokens: int = 4096,
) -> dict[str, Any]:
    fake = os.getenv("KESTREL_FAKE_MODEL_RESPONSE")
    if fake:
        return {"provider": "fake", "model": "fake", "content": fake}

    runtime = await detect_local_model_runtime(config)
    provider = runtime.get("default_provider")
    model = runtime.get("default_model")
    if not provider or not model:
        raise RuntimeError("No local model runtime is available. Start Ollama or LM Studio, then retry.")

    if provider == "ollama":
        base_url = runtime["providers"]["ollama"]["base_url"]
        payload = await _http_post_json(
            f"{base_url}/api/chat",
            {
                "model": model,
                "stream": False,
                "messages": messages,
                "options": {
                    "temperature": temperature,
                    "num_predict": max_tokens,
                },
            },
            timeout_seconds=120,
        )
        content = ((payload.get("message") or {}).get("content") or "").strip()
    elif provider == "lmstudio":
        base_url = runtime["providers"]["lmstudio"]["base_url"]
        payload = await _http_post_json(
            f"{base_url}/v1/chat/completions",
            {
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
            timeout_seconds=120,
        )
        choices = payload.get("choices") or []
        message = ((choices[0] or {}).get("message") or {}) if choices else {}
        content = (message.get("content") or message.get("reasoning_content") or "").strip()
    else:  # pragma: no cover - defensive
        raise RuntimeError(f"Unsupported local model provider: {provider}")

    if not content:
        raise RuntimeError(f"{provider} returned an empty completion")
    return {"provider": provider, "model": model, "content": content}


async def _request_model_json(
    *,
    messages: list[dict[str, Any]],
    config: dict[str, Any],
    temperature: float = 0.1,
    max_tokens: int = 4096,
    repair_label: str,
) -> tuple[dict[str, Any], str, str]:
    response = await _generate_local_text_response(
        messages=messages,
        config=config,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    content = response["content"]
    try:
        return _extract_json_object(content), response["provider"], response["model"]
    except Exception:
        repair_messages = list(messages) + [
            {
                "role": "assistant",
                "content": content,
            },
            {
                "role": "user",
                "content": (
                    f"Your previous {repair_label} response was invalid. "
                    "Return exactly one valid JSON object and nothing else."
                ),
            },
        ]
        repaired = await _generate_local_text_response(
            messages=repair_messages,
            config=config,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return _extract_json_object(repaired["content"]), repaired["provider"], repaired["model"]


def _custom_tool_manifest_template(
    *,
    name: str,
    description: str,
    runtime: str,
    entrypoint: str,
    input_schema: dict[str, Any],
    risk_class: str,
    approval_required: bool,
    setup_notes: list[str],
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "runtime": runtime,
        "entrypoint": entrypoint,
        "input_schema": input_schema,
        "risk_class": risk_class,
        "approval_required": approval_required,
        "setup_notes": setup_notes,
    }


def _sanitize_tool_name(raw_name: str) -> str:
    cleaned = "".join(char if char.isalnum() or char == "_" else "_" for char in (raw_name or "").strip().lower())
    cleaned = cleaned.strip("_")
    return cleaned or "custom_tool"


def _default_custom_tool_files(
    *,
    name: str,
    description: str,
    setup_notes: list[str],
) -> dict[str, str]:
    notes_markdown = "\n".join(f"- {item}" for item in setup_notes) if setup_notes else "- Add setup notes here."
    python_code = f"""#!/usr/bin/env python3
import json
import sys
from pathlib import Path


def main() -> None:
    raw = sys.stdin.read().strip()
    args = json.loads(raw) if raw else {{}}
    result = {{
        "success": False,
        "message": "{description}",
        "data": {{
            "tool": "{name}",
            "received_args": args,
            "status": "scaffolded_only",
            "setup_notes_path": str(Path(__file__).with_name("SETUP.md")),
        }},
    }}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
"""
    return {
        "tool.py": python_code,
        "SETUP.md": f"# {name}\n\n## Description\n{description}\n\n## Setup Notes\n{notes_markdown}\n",
    }


def _gmail_custom_tool_blueprint(goal: str) -> dict[str, Any]:
    name = "gmail_tool"
    description = "Use Gmail through a local custom Kestrel tool once Google OAuth credentials are configured."
    setup_notes = [
        "Create a Google Cloud project and enable the Gmail API.",
        "Download desktop OAuth credentials to ~/.google/credentials.json.",
        "Install google-api-python-client, google-auth-httplib2, and google-auth-oauthlib in the Python environment used by Kestrel.",
        "Run the tool once to complete the local OAuth browser flow.",
    ]
    python_code = """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path


SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]


def main() -> None:
    raw = sys.stdin.read().strip()
    args = json.loads(raw) if raw else {}
    credentials_path = Path.home() / ".google" / "credentials.json"
    if not credentials_path.exists():
        result = {
            "success": False,
            "message": (
                "Missing prerequisite: create ~/.google/credentials.json before using the Gmail tool."
            ),
            "data": {
                "missing_prerequisites": [
                    "Google OAuth desktop credentials at ~/.google/credentials.json"
                ],
                "received_args": args,
            },
        }
        print(json.dumps(result))
        return

    result = {
        "success": False,
        "message": (
            "Gmail tool scaffold is installed, but live Gmail execution still requires OAuth setup and "
            "the Google client libraries in Kestrel's Python environment."
        ),
        "data": {
            "credentials_path": str(credentials_path),
            "received_args": args,
            "scopes": SCOPES,
            "next_steps": [
                "Install google-api-python-client google-auth-httplib2 google-auth-oauthlib",
                "Run the tool again after completing the browser OAuth flow",
            ],
        },
    }
    print(json.dumps(result))


if __name__ == "__main__":
    main()
"""
    return {
        "name": name,
        "description": description,
        "runtime": "python",
        "entrypoint": "tool.py",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "Gmail action such as list_messages, search_messages, read_message, or send_message.",
                },
                "query": {
                    "type": "string",
                    "description": "Optional Gmail search query.",
                },
            },
            "required": ["action"],
        },
        "risk_class": "mutating",
        "approval_required": True,
        "setup_notes": setup_notes,
        "files": {
            "tool.py": python_code,
            "SETUP.md": "# Gmail Tool\n\n## Setup Notes\n" + "\n".join(f"- {item}" for item in setup_notes) + "\n",
        },
        "goal": goal,
    }


def _build_custom_tool_blueprint(goal: str, proposal: dict[str, Any] | None = None) -> dict[str, Any]:
    proposal = dict(proposal or {})
    lowered = (goal or "").lower()
    if "gmail" in lowered:
        return _gmail_custom_tool_blueprint(goal)

    name = _sanitize_tool_name(proposal.get("name") or "custom_tool")
    description = str(proposal.get("description") or goal or f"Custom tool {name}").strip()
    setup_notes = proposal.get("setup_notes")
    if not isinstance(setup_notes, list) or not all(isinstance(item, str) for item in setup_notes):
        setup_notes = [
            "Review the generated tool scaffold.",
            "Fill in any external credentials or API dependencies that the tool needs.",
            "Retry the original task after the prerequisites are in place.",
        ]
    input_schema = proposal.get("input_schema")
    if not isinstance(input_schema, dict):
        input_schema = {
            "type": "object",
            "properties": {},
        }
    runtime = str(proposal.get("runtime") or "python").strip().lower()
    entrypoint = "tool.py" if runtime == "python" else "tool.sh"
    files = proposal.get("files")
    if not isinstance(files, dict) or not files:
        files = _default_custom_tool_files(name=name, description=description, setup_notes=setup_notes)
    return {
        "name": name,
        "description": description,
        "runtime": runtime,
        "entrypoint": entrypoint,
        "input_schema": input_schema,
        "risk_class": str(proposal.get("risk_class") or "mutating"),
        "approval_required": True,
        "setup_notes": setup_notes,
        "files": files,
        "goal": goal,
    }


