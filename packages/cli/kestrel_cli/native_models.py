from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from . import native_storage as _native_storage

globals().update({name: value for name, value in vars(_native_storage).items() if not name.startswith("__")})

_LMSTUDIO_INFERENCE_LOCK: asyncio.Lock | None = None
_REASONING_STRONG_PATTERNS = (
    r"\bthink\s+(?:step by step|through this|carefully)\b",
    r"\bstep by step\b",
    r"\bshow (?:your|the) reasoning\b",
    r"\broot cause\b",
    r"\btrade-?offs?\b",
    r"\bcompare\b.+\b(?:versus|vs\.?|against)\b",
    r"\bdebug\b",
    r"\bprove\b",
    r"\bderive\b",
    r"\bbenchmark\b",
    r"\bspatial\b",
    r"\bresearch\b",
    r"\banaly[sz]e\b",
)
_REASONING_HINT_KEYWORDS = (
    "reasoning",
    "architecture",
    "investigate",
    "multi-step",
    "hard problem",
    "complex problem",
    "tradeoff",
    "trade-off",
    "compare",
    "evaluate",
    "why this failed",
    "what went wrong",
)


def _get_lmstudio_inference_lock() -> asyncio.Lock:
    global _LMSTUDIO_INFERENCE_LOCK
    if _LMSTUDIO_INFERENCE_LOCK is None:
        _LMSTUDIO_INFERENCE_LOCK = asyncio.Lock()
    return _LMSTUDIO_INFERENCE_LOCK


def _normalize_lmstudio_model_id(model_id: str) -> str:
    value = str(model_id or "").strip()
    return re.sub(r":\d+$", "", value)


def _model_preferences(config: dict[str, Any]) -> dict[str, Any]:
    models_cfg = config.get("models") or {}
    preferred_provider = str(models_cfg.get("preferred_provider") or "auto").strip().lower() or "auto"
    preferred_model = str(models_cfg.get("preferred_model") or "").strip()
    reasoning_provider = str(models_cfg.get("reasoning_provider") or preferred_provider or "auto").strip().lower() or "auto"
    reasoning_model = str(models_cfg.get("reasoning_model") or "").strip()
    reasoning_escalation = bool(models_cfg.get("reasoning_escalation", False))
    reasoning_auto_restore_primary = bool(models_cfg.get("reasoning_auto_restore_primary", True))
    return {
        "preferred_provider": preferred_provider,
        "preferred_model": preferred_model,
        "reasoning_provider": reasoning_provider,
        "reasoning_model": reasoning_model,
        "reasoning_escalation": reasoning_escalation,
        "reasoning_auto_restore_primary": reasoning_auto_restore_primary,
    }


def _first_known_model(provider_info: dict[str, Any]) -> str:
    for key in ("models", "available_models"):
        models = provider_info.get(key) or []
        if models:
            return str(models[0] or "")
    return ""


def _provider_has_model(provider_info: dict[str, Any], model_id: str) -> bool:
    normalized = str(model_id or "").strip()
    if not normalized:
        return False
    known = {
        str(item).strip()
        for item in (
            list(provider_info.get("models") or [])
            + list(provider_info.get("available_models") or [])
        )
        if str(item).strip()
    }
    return normalized in known if known else bool(provider_info.get("ready"))


def _messages_need_reasoning(messages: list[dict[str, Any]]) -> bool:
    text_parts: list[str] = []
    for message in messages[-4:]:
        if str(message.get("role") or "").strip().lower() != "user":
            continue
        content = str(message.get("content") or "").strip()
        if content:
            text_parts.append(content)
    text = "\n".join(text_parts).strip().lower()
    if not text:
        return False
    if any(re.search(pattern, text) for pattern in _REASONING_STRONG_PATTERNS):
        return True
    score = 0
    if len(text) >= 500:
        score += 1
    if text.count("\n") >= 4:
        score += 1
    score += sum(1 for keyword in _REASONING_HINT_KEYWORDS if keyword in text)
    if "```" in text:
        score += 1
    return score >= 2


def _should_use_reasoning_profile(
    *,
    runtime: dict[str, Any],
    messages: list[dict[str, Any]],
    enable_thinking: bool | None,
    model_role: str,
) -> bool:
    role = str(model_role or "auto").strip().lower()
    if role == "primary":
        return False
    if role == "reasoning":
        return True
    if enable_thinking is False:
        return False
    if not runtime.get("reasoning_escalation") or not runtime.get("reasoning_model"):
        return False
    if enable_thinking is True:
        return True
    return _messages_need_reasoning(messages)


def _resolve_local_model_selection(
    *,
    runtime: dict[str, Any],
    messages: list[dict[str, Any]],
    enable_thinking: bool | None,
    model_role: str,
) -> dict[str, Any]:
    provider = str(runtime.get("default_provider") or "").strip()
    model = str(runtime.get("default_model") or "").strip()
    profile = "primary"
    if _should_use_reasoning_profile(
        runtime=runtime,
        messages=messages,
        enable_thinking=enable_thinking,
        model_role=model_role,
    ):
        candidate_provider = str(runtime.get("reasoning_provider") or provider or "").strip()
        candidate_model = str(runtime.get("reasoning_model") or "").strip()
        provider_info = runtime.get("providers", {}).get(candidate_provider, {})
        if candidate_provider and candidate_model and _provider_has_model(provider_info, candidate_model):
            provider = candidate_provider
            model = candidate_model
            profile = "reasoning"
    return {
        "provider": provider,
        "model": model,
        "profile": profile,
    }


async def _lmstudio_active_models(base_url: str) -> list[str]:
    payload = await _http_get_json(f"{base_url}/v1/models", timeout_seconds=10)
    return [
        _normalize_lmstudio_model_id(item.get("id", ""))
        for item in payload.get("data", [])
        if item.get("id")
    ]


async def _wait_for_lmstudio_active_model(
    base_url: str,
    target_model: str,
    *,
    timeout_seconds: float = 240,
) -> None:
    deadline = time.time() + timeout_seconds
    normalized_target = _normalize_lmstudio_model_id(target_model)
    while time.time() < deadline:
        active = await _lmstudio_active_models(base_url)
        if normalized_target in active:
            return
        await asyncio.sleep(2)
    raise RuntimeError(f"Timed out waiting for LM Studio model {target_model} to become active.")


async def _wait_for_lmstudio_clear(base_url: str, *, timeout_seconds: float = 120) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        active = await _lmstudio_active_models(base_url)
        if not active:
            return
        await asyncio.sleep(2)
    raise RuntimeError("Timed out waiting for LM Studio to clear active models.")


async def _ensure_lmstudio_model_loaded(
    *,
    base_url: str,
    target_model: str,
    available_models: list[str] | None = None,
) -> dict[str, Any]:
    catalog = await _http_get_json(f"{base_url}/api/v1/models", timeout_seconds=10)
    llm_models = [item for item in catalog.get("models", []) if item.get("type") == "llm"]
    known_models = {str(item).strip() for item in (available_models or []) if str(item).strip()}
    if not known_models:
        known_models = {
            str(model.get("key") or "").strip()
            for model in llm_models
            if str(model.get("key") or "").strip()
        }
    normalized_target = str(target_model or "").strip()
    if normalized_target not in known_models:
        raise RuntimeError(f"LM Studio model {normalized_target} is not downloaded or available.")

    previous_active_models: list[str] = []
    unload_instance_ids: list[str] = []
    target_already_loaded = False
    for model in llm_models:
        key = str(model.get("key") or "").strip()
        loaded_instances = list(model.get("loaded_instances") or [])
        if loaded_instances:
            previous_active_models.append(key)
        if key == normalized_target and loaded_instances:
            target_already_loaded = True
        for instance in loaded_instances:
            instance_id = str(instance.get("id") or key).strip()
            if not instance_id:
                continue
            if key != normalized_target:
                unload_instance_ids.append(instance_id)

    for instance_id in unload_instance_ids:
        try:
            await _http_post_json(
                f"{base_url}/api/v1/models/unload",
                {"instance_id": instance_id},
                timeout_seconds=60,
            )
        except Exception as exc:
            raise RuntimeError(f"Failed to unload LM Studio model instance {instance_id}: {exc}") from exc

    if unload_instance_ids:
        await _wait_for_lmstudio_clear(base_url)

    if not target_already_loaded:
        await _http_post_json(
            f"{base_url}/api/v1/models/load",
            {"model": normalized_target},
            timeout_seconds=300,
        )

    await _wait_for_lmstudio_active_model(base_url, normalized_target)
    return {
        "previous_active_models": list(dict.fromkeys(previous_active_models)),
        "swapped": bool(unload_instance_ids) or not target_already_loaded,
    }


@asynccontextmanager
async def _local_model_session(
    *,
    config: dict[str, Any],
    runtime: dict[str, Any],
    messages: list[dict[str, Any]],
    enable_thinking: bool | None,
    model_role: str,
) -> Any:
    selection = _resolve_local_model_selection(
        runtime=runtime,
        messages=messages,
        enable_thinking=enable_thinking,
        model_role=model_role,
    )
    provider = selection["provider"]
    model = selection["model"]
    if not provider or not model:
        raise RuntimeError("No local model runtime is available. Start Ollama or LM Studio, then retry.")

    if provider != "lmstudio":
        provider_info = runtime.get("providers", {}).get(provider, {})
        yield {
            "provider": provider,
            "model": model,
            "profile": selection["profile"],
            "base_url": provider_info.get("base_url", ""),
        }
        return

    provider_info = runtime.get("providers", {}).get("lmstudio", {})
    base_url = str(provider_info.get("base_url") or "").rstrip("/")
    if not base_url:
        raise RuntimeError("LM Studio base URL is unavailable.")

    async with _get_lmstudio_inference_lock():
        if not bool(provider_info.get("supports_model_swap", False)):
            yield {
                "provider": provider,
                "model": model,
                "profile": selection["profile"],
                "base_url": base_url,
            }
            return
        restore_model = ""
        temporary_reasoning = selection["profile"] == "reasoning" and bool(runtime.get("reasoning_auto_restore_primary"))
        try:
            try:
                await _ensure_lmstudio_model_loaded(
                    base_url=base_url,
                    target_model=model,
                    available_models=list(provider_info.get("available_models") or []),
                )
            except Exception as exc:
                primary_model = str(runtime.get("default_model") or "").strip()
                if selection["profile"] != "reasoning" or not primary_model or primary_model == model:
                    raise
                LOGGER.warning(
                    "Reasoning model %s was unavailable; falling back to primary model %s (%s)",
                    model,
                    primary_model,
                    exc,
                )
                model = primary_model
                selection["profile"] = "primary"
                temporary_reasoning = False
                await _ensure_lmstudio_model_loaded(
                    base_url=base_url,
                    target_model=model,
                    available_models=list(provider_info.get("available_models") or []),
                )

            if temporary_reasoning:
                primary_model = str(runtime.get("default_model") or "").strip()
                if primary_model and primary_model != model:
                    restore_model = primary_model

            yield {
                "provider": provider,
                "model": model,
                "profile": selection["profile"],
                "base_url": base_url,
            }
        finally:
            if restore_model:
                try:
                    await _ensure_lmstudio_model_loaded(
                        base_url=base_url,
                        target_model=restore_model,
                        available_models=list(provider_info.get("available_models") or []),
                    )
                except Exception as exc:
                    LOGGER.warning("Failed to restore primary LM Studio model %s: %s", restore_model, exc)

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
    preferences = _model_preferences(config)
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

    lmstudio_info: dict[str, Any] = {
        "ready": False,
        "chat_ready": False,
        "catalog_ready": False,
        "models": [],
        "active_models": [],
        "available_models": [],
        "loaded_instances": [],
        "supports_model_swap": False,
        "base_url": lmstudio_url,
    }
    try:
        payload = await _http_get_json(f"{lmstudio_url}/v1/models")
        active_models = [item.get("id", "") for item in payload.get("data", []) if item.get("id")]
        lmstudio_info["ready"] = True
        lmstudio_info["chat_ready"] = True
        lmstudio_info["models"] = list(active_models)
        lmstudio_info["active_models"] = list(active_models)
    except Exception as exc:
        lmstudio_info["error"] = str(exc)

    try:
        payload = await _http_get_json(f"{lmstudio_url}/api/v1/models", timeout_seconds=10)
        llm_models = [item for item in payload.get("models", []) if item.get("type") == "llm"]
        available_models = [item.get("key", "") for item in llm_models if item.get("key")]
        loaded_instances = [
            {
                "key": str(item.get("key") or "").strip(),
                "id": str(instance.get("id") or item.get("key") or "").strip(),
            }
            for item in llm_models
            for instance in item.get("loaded_instances", [])
            if str(instance.get("id") or item.get("key") or "").strip()
        ]
        lmstudio_info["catalog_ready"] = True
        lmstudio_info["supports_model_swap"] = True
        lmstudio_info["available_models"] = available_models
        lmstudio_info["loaded_instances"] = loaded_instances
        if not lmstudio_info["models"]:
            lmstudio_info["models"] = [entry["key"] for entry in loaded_instances if entry["key"]]
        lmstudio_info["ready"] = lmstudio_info["ready"] or bool(available_models)
    except Exception as exc:
        if "error" not in lmstudio_info:
            lmstudio_info["error"] = str(exc)

    providers["lmstudio"] = lmstudio_info

    preferred_provider = preferences["preferred_provider"]
    preferred_model = preferences["preferred_model"]
    default_provider = ""
    default_model = ""
    if preferred_provider != "auto" and providers.get(preferred_provider, {}).get("ready"):
        default_provider = preferred_provider
        default_model = preferred_model or _first_known_model(providers[preferred_provider])
    else:
        for name in ("ollama", "lmstudio"):
            if providers.get(name, {}).get("ready") and _first_known_model(providers[name]):
                default_provider = name
                default_model = preferred_model or _first_known_model(providers[name])
                break
    return {
        "preferred_provider": preferred_provider,
        "preferred_model": preferred_model,
        "reasoning_provider": (
            preferences["reasoning_provider"]
            if preferences["reasoning_provider"] not in {"", "auto"}
            else default_provider
        ),
        "reasoning_model": preferences["reasoning_model"],
        "reasoning_escalation": preferences["reasoning_escalation"],
        "reasoning_auto_restore_primary": preferences["reasoning_auto_restore_primary"],
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
    source_dir: str = ""
    aliases: tuple[str, ...] = ()
    setup_notes: list[str] = field(default_factory=list)
    source: str = "builtin"
    pack_id: str = ""
    always_on: bool = False

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
            "source": self.source,
            "pack_id": self.pack_id,
            "always_on": self.always_on,
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

    def to_dict(self, *, compact: bool = False) -> dict[str, Any]:
        data_payload: dict[str, Any] = dict(self.data)
        if compact:
            data_payload = _compact_native_payload(data_payload)
        payload = {
            "tool_name": self.tool_name,
            "success": self.success,
            "message": self.message,
            "data": data_payload,
            "stdout": _truncate_text(self.stdout, 1200) if compact else self.stdout,
            "stderr": _truncate_text(self.stderr, 1200) if compact else self.stderr,
            "exit_code": self.exit_code,
            "artifacts": _compact_native_payload(list(self.artifacts)) if compact else list(self.artifacts),
            "risk_class": self.risk_class,
            "approval_required": self.approval_required,
            "approval_operation": self.approval_operation,
            "approval_payload": _compact_native_payload(dict(self.approval_payload)) if compact else dict(self.approval_payload),
            "metadata": _compact_native_payload(dict(self.metadata)) if compact else dict(self.metadata),
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


def _compact_native_payload(value: Any, *, key: str = "", depth: int = 0) -> Any:
    if isinstance(value, dict):
        items = list(value.items())
        if depth >= 2:
            compact: dict[str, Any] = {}
            preferred_keys = (
                "pack_id",
                "name",
                "version",
                "description",
                "installed",
                "enabled",
                "trusted",
                "scope",
                "source_type",
                "path",
                "snapshot_id",
                "query",
                "total",
                "mime_type",
                "binary",
                "size_bytes",
                "truncated",
            )
            for preferred in preferred_keys:
                if preferred in value:
                    compact[preferred] = _compact_native_payload(value[preferred], key=preferred, depth=depth + 1)
            if not compact:
                for name, child in items[:6]:
                    compact[name] = _compact_native_payload(child, key=name, depth=depth + 1)
            omitted = len(value) - len(compact)
            if omitted > 0:
                compact["_omitted_fields"] = omitted
            return compact

        compact = {
            name: _compact_native_payload(child, key=name, depth=depth + 1)
            for name, child in items[:10]
        }
        if len(items) > 10:
            compact["_omitted_fields"] = len(items) - 10
        return compact

    if isinstance(value, list):
        if key in {"packs", "results", "matches", "artifacts", "approvals"} or len(value) > 8:
            preview = [_compact_native_payload(item, depth=depth + 1) for item in value[:5]]
            payload = {
                "count": len(value),
                "preview": preview,
            }
            if len(value) > 5:
                payload["truncated_items"] = len(value) - 5
            return payload
        return [_compact_native_payload(item, depth=depth + 1) for item in value]

    if isinstance(value, str):
        return _truncate_text(value, 600 if depth == 0 else 240)

    return value


class StructuredModelOutputError(RuntimeError):
    def __init__(self, *, repair_label: str, response_text: str):
        self.repair_label = str(repair_label or "structured").strip() or "structured"
        self.response_text = str(response_text or "")
        sample = re.sub(r"\s+", " ", _strip_wrappers(self.response_text)).strip()
        if not sample:
            sample = "(empty response)"
        if len(sample) > 180:
            sample = f"{sample[:177]}..."
        self.response_sample = sample
        super().__init__(
            f"Failed to parse {self.repair_label} model response after repair. Sample: {self.response_sample}"
        )


def _strip_wrappers(text: str) -> str:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if len(lines) >= 2 and lines[-1].strip() == "```":
            cleaned = "\n".join(lines[1:-1]).strip()
        else:
            cleaned = "\n".join(lines[1:]).strip()
    return cleaned


def _strip_json_like_comments(raw: str) -> str:
    cleaned: list[str] = []
    in_string = False
    escape = False
    in_line_comment = False
    in_block_comment = False
    index = 0
    length = len(raw)
    while index < length:
        char = raw[index]
        next_char = raw[index + 1] if index + 1 < length else ""

        if in_line_comment:
            if char in "\r\n":
                in_line_comment = False
                cleaned.append(char)
            index += 1
            continue

        if in_block_comment:
            if char == "*" and next_char == "/":
                in_block_comment = False
                if cleaned and not cleaned[-1].isspace():
                    cleaned.append(" ")
                index += 2
                continue
            index += 1
            continue

        if in_string:
            cleaned.append(char)
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            index += 1
            continue

        if char == '"':
            cleaned.append(char)
            in_string = True
            index += 1
            continue
        if char == "/" and next_char == "/":
            in_line_comment = True
            index += 2
            continue
        if char == "/" and next_char == "*":
            in_block_comment = True
            index += 2
            continue

        cleaned.append(char)
        index += 1

    return "".join(cleaned)


def _strip_json_like_trailing_commas(raw: str) -> str:
    cleaned: list[str] = []
    in_string = False
    escape = False
    index = 0
    length = len(raw)
    while index < length:
        char = raw[index]
        if in_string:
            cleaned.append(char)
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            index += 1
            continue

        if char == '"':
            cleaned.append(char)
            in_string = True
            index += 1
            continue

        if char == ",":
            lookahead = index + 1
            while lookahead < length and raw[lookahead].isspace():
                lookahead += 1
            if lookahead < length and raw[lookahead] in "}]":
                index += 1
                continue

        cleaned.append(char)
        index += 1

    return "".join(cleaned)


def _normalize_json_like_text(raw: str) -> str:
    normalized = _strip_json_like_comments(raw)
    normalized = _strip_json_like_trailing_commas(normalized)
    return normalized.strip()


def _json_object_parse_candidates(raw: str) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()
    normalized = _normalize_json_like_text(raw)
    for candidate in (raw, normalized):
        value = str(candidate or "")
        if not value or value in seen:
            continue
        candidates.append(value)
        seen.add(value)
        repaired = _escape_json_string_control_chars(value)
        if repaired and repaired not in seen:
            candidates.append(repaired)
            seen.add(repaired)
    return candidates


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
    for candidate in _json_object_parse_candidates(raw):
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
    enable_thinking: bool | None = None,
    timeout_seconds: float = 120,
    model_role: str = "auto",
) -> dict[str, Any]:
    fake = os.getenv("KESTREL_FAKE_MODEL_RESPONSE")
    if fake:
        return {"provider": "fake", "model": "fake", "content": fake}

    runtime = await detect_local_model_runtime(config)
    async with _local_model_session(
        config=config,
        runtime=runtime,
        messages=messages,
        enable_thinking=enable_thinking,
        model_role=model_role,
    ) as session:
        provider = session["provider"]
        model = session["model"]
        base_url = str(session.get("base_url") or "").rstrip("/")
        if provider == "ollama":
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
                timeout_seconds=timeout_seconds,
            )
            content = ((payload.get("message") or {}).get("content") or "").strip()
        elif provider == "lmstudio":
            request_payload: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            if enable_thinking is False:
                request_payload["chat_template_kwargs"] = {"enable_thinking": False}
            payload = await _http_post_json(
                f"{base_url}/v1/chat/completions",
                request_payload,
                timeout_seconds=timeout_seconds,
            )
            choices = payload.get("choices") or []
            message = ((choices[0] or {}).get("message") or {}) if choices else {}
            content = (message.get("content") or message.get("reasoning_content") or "").strip()
        else:  # pragma: no cover - defensive
            raise RuntimeError(f"Unsupported local model provider: {provider}")

    if not content:
        raise RuntimeError(f"{provider} returned an empty completion")
    return {"provider": provider, "model": model, "content": content}


def _image_data_url_from_path(path: Path) -> str:
    expanded = path.expanduser().resolve()
    if not expanded.exists():
        raise RuntimeError(f"Image not found: {expanded}")
    mime_type = (mimetypes.guess_type(expanded.name)[0] or "").strip().lower()
    if not mime_type.startswith("image/"):
        raise RuntimeError(f"Unsupported image type for multimodal inspection: {expanded.name}")
    encoded = base64.b64encode(expanded.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


async def inspect_local_images(
    *,
    prompt: str,
    image_paths: list[str],
    config: dict[str, Any],
    history: list[dict[str, Any]] | None = None,
    temperature: float = 0.1,
    max_tokens: int = 2048,
    timeout_seconds: float = 180,
) -> dict[str, Any]:
    fake = os.getenv("KESTREL_FAKE_MODEL_RESPONSE")
    if fake:
        return {"provider": "fake", "model": "fake", "content": fake}

    clean_prompt = str(prompt or "").strip() or "Describe the attached image and answer the user directly."
    selected_paths = [str(Path(raw).expanduser()) for raw in image_paths if str(raw).strip()][:4]
    if not selected_paths:
        raise RuntimeError("At least one local image is required for multimodal inspection.")

    runtime = await detect_local_model_runtime(config)
    session_messages = [{"role": "user", "content": clean_prompt}]
    async with _local_model_session(
        config=config,
        runtime=runtime,
        messages=session_messages,
        enable_thinking=True,
        model_role="reasoning",
    ) as session:
        provider = session["provider"]
        model = session["model"]
        base_url = str(session.get("base_url") or "").rstrip("/")
        if provider != "lmstudio":
            raise RuntimeError("Local multimodal image inspection currently requires LM Studio.")

        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": (
                    "You are Kestrel on Telegram. Inspect the attached image input directly and answer the user "
                    "concisely and concretely. Do not claim that you cannot view images when image input is present."
                ),
            }
        ]
        for item in list(history or [])[-4:]:
            role = str(item.get("role") or "user").strip().lower()
            if role not in {"system", "assistant", "user"}:
                role = "user"
            content = item.get("content")
            if isinstance(content, list):
                continue
            text = str(content or "").strip()
            if text:
                messages.append({"role": role, "content": text})

        user_content: list[dict[str, Any]] = [{"type": "text", "text": clean_prompt}]
        for raw_path in selected_paths:
            data_url = await asyncio.to_thread(_image_data_url_from_path, Path(raw_path))
            user_content.append({"type": "image_url", "image_url": {"url": data_url}})
        payload = {
            "model": model,
            "messages": messages + [{"role": "user", "content": user_content}],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        response = await _http_post_json(
            f"{base_url}/v1/chat/completions",
            payload,
            timeout_seconds=timeout_seconds,
        )
        choices = response.get("choices") or []
        message = ((choices[0] or {}).get("message") or {}) if choices else {}
        content = (message.get("content") or message.get("reasoning_content") or "").strip()

    if not content:
        raise RuntimeError(f"{provider} returned an empty multimodal completion")
    return {"provider": provider, "model": model, "content": content}


def _normalize_local_image_route(
    payload: dict[str, Any],
    *,
    fallback_prompt: str,
    fallback_reply: str,
) -> dict[str, Any]:
    action = str(payload.get("action") or "").strip().lower()
    if action not in {"respond", "generate_image"}:
        action = "respond"

    reply_text = str(
        payload.get("reply_text")
        or payload.get("response")
        or payload.get("message")
        or ""
    ).strip()
    generation_prompt = str(
        payload.get("generation_prompt")
        or payload.get("prompt")
        or ""
    ).strip()
    negative_prompt = str(payload.get("negative_prompt") or "").strip()
    media_type = str(payload.get("media_type") or "image").strip().lower() or "image"
    init_image_creativity = payload.get("init_image_creativity")
    if media_type not in {"image", "video"}:
        media_type = "image"
    try:
        init_image_creativity = float(init_image_creativity)
    except (TypeError, ValueError):
        init_image_creativity = 0.6
    init_image_creativity = min(max(init_image_creativity, 0.0), 1.5)

    if action == "generate_image" and not generation_prompt:
        action = "respond"

    if action == "respond" and not reply_text:
        reply_text = fallback_reply.strip()

    return {
        "action": action,
        "reply_text": reply_text,
        "generation_prompt": generation_prompt or fallback_prompt,
        "negative_prompt": negative_prompt,
        "media_type": media_type,
        "init_image_creativity": init_image_creativity,
    }


async def route_local_image_request(
    *,
    prompt: str,
    image_paths: list[str],
    config: dict[str, Any],
    history: list[dict[str, Any]] | None = None,
    temperature: float = 0.1,
    max_tokens: int = 1200,
    timeout_seconds: float = 180,
) -> dict[str, Any]:
    fake = os.getenv("KESTREL_FAKE_MODEL_RESPONSE")
    clean_prompt = str(prompt or "").strip() or "Describe the attached image and answer the user directly."
    if fake:
        parsed = _load_possible_json_object(fake) or {"action": "respond", "reply_text": fake}
        normalized = _normalize_local_image_route(
            parsed,
            fallback_prompt=clean_prompt,
            fallback_reply=fake,
        )
        normalized.update({"provider": "fake", "model": "fake"})
        return normalized

    selected_paths = [str(Path(raw).expanduser()) for raw in image_paths if str(raw).strip()][:4]
    if not selected_paths:
        raise RuntimeError("At least one local image is required for multimodal routing.")

    runtime = await detect_local_model_runtime(config)
    session_messages = [{"role": "user", "content": clean_prompt}]
    async with _local_model_session(
        config=config,
        runtime=runtime,
        messages=session_messages,
        enable_thinking=True,
        model_role="reasoning",
    ) as session:
        provider = session["provider"]
        model = session["model"]
        base_url = str(session.get("base_url") or "").rstrip("/")
        if provider != "lmstudio":
            raise RuntimeError("Local multimodal image routing currently requires LM Studio.")

        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": (
                    "You are Kestrel's multimodal Telegram router. Look at the user's prompt and the attached "
                    "image input together, then decide the next action. Return exactly one JSON object with this "
                    'schema: {"action":"respond"|"generate_image","reply_text":"","generation_prompt":"",'
                    '"negative_prompt":"","media_type":"image"|"video","init_image_creativity":0.6}. '
                    "Choose action='generate_image' when the user wants a new generated asset derived from the "
                    "attached image, including transformations, stylizations, avatars, caricatures, edits, "
                    "memes, posters, or 'turn me into' style requests. When generating, write a self-contained "
                    "generation_prompt that includes the important visible traits from the attached image so a "
                    "text-to-image model can recreate the subject, and set init_image_creativity to indicate how "
                    "loosely the output should follow the source image. Lower values stay closer to the source; "
                    "higher values allow stronger transformation. Choose action='respond' when the user wants "
                    "description, identification, analysis, OCR-style reading, or discussion about the image, and "
                    "put the full user-facing answer in reply_text. Prefer respond if the request is ambiguous. "
                    "Never mention tool limitations or claim that you cannot see the image."
                ),
            }
        ]
        for item in list(history or [])[-4:]:
            role = str(item.get("role") or "user").strip().lower()
            if role not in {"system", "assistant", "user"}:
                role = "user"
            content = item.get("content")
            if isinstance(content, list):
                continue
            text = str(content or "").strip()
            if text:
                messages.append({"role": role, "content": text})

        user_content: list[dict[str, Any]] = [
            {"type": "text", "text": clean_prompt},
            {"type": "text", "text": "Return exactly one JSON object and nothing else."},
        ]
        for raw_path in selected_paths:
            data_url = await asyncio.to_thread(_image_data_url_from_path, Path(raw_path))
            user_content.append({"type": "image_url", "image_url": {"url": data_url}})
        payload = {
            "model": model,
            "messages": messages + [{"role": "user", "content": user_content}],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        response = await _http_post_json(
            f"{base_url}/v1/chat/completions",
            payload,
            timeout_seconds=timeout_seconds,
        )
        choices = response.get("choices") or []
        message = ((choices[0] or {}).get("message") or {}) if choices else {}
        content = (message.get("content") or message.get("reasoning_content") or "").strip()

    if not content:
        raise RuntimeError(f"{provider} returned an empty multimodal routing completion")

    try:
        routed = _extract_json_object(content)
    except Exception:
        repair_messages = [
            {
                "role": "system",
                "content": (
                    "Rewrite the previous multimodal routing result as exactly one valid JSON object with keys "
                    "action, reply_text, generation_prompt, negative_prompt, and media_type."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Original user prompt:\n{clean_prompt}\n\n"
                    f"Previous routing result:\n{content}\n\n"
                    "Return exactly one valid JSON object and nothing else."
                ),
            },
        ]
        repaired = await _generate_local_text_response(
            messages=repair_messages,
            config=config,
            temperature=temperature,
            max_tokens=600,
            enable_thinking=True,
            timeout_seconds=min(timeout_seconds, 90),
            model_role="reasoning",
        )
        provider = repaired["provider"]
        model = repaired["model"]
        try:
            routed = _extract_json_object(repaired["content"])
        except Exception as exc:
            raise StructuredModelOutputError(
                repair_label="multimodal router",
                response_text=repaired["content"],
            ) from exc

    normalized = _normalize_local_image_route(
        routed,
        fallback_prompt=clean_prompt,
        fallback_reply=content,
    )
    normalized.update({"provider": provider, "model": model})
    return normalized


async def _request_model_json(
    *,
    messages: list[dict[str, Any]],
    config: dict[str, Any],
    temperature: float = 0.1,
    max_tokens: int = 4096,
    repair_label: str,
    enable_thinking: bool = False,
    timeout_seconds: float = 90,
    model_role: str = "primary",
) -> tuple[dict[str, Any], str, str]:
    response = await _generate_local_text_response(
        messages=messages,
        config=config,
        temperature=temperature,
        max_tokens=max_tokens,
        enable_thinking=enable_thinking,
        timeout_seconds=timeout_seconds,
        model_role=model_role,
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
            enable_thinking=enable_thinking,
            timeout_seconds=timeout_seconds,
            model_role=model_role,
        )
        try:
            return _extract_json_object(repaired["content"]), repaired["provider"], repaired["model"]
        except Exception as exc:
            raise StructuredModelOutputError(
                repair_label=repair_label,
                response_text=repaired["content"],
            ) from exc


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
