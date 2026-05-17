from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import AgentConfig

PROVIDER_CHOICES = {
    "mock",
    "openai",
    "openai-compatible",
    "openrouter",
    "ollama",
    "anthropic",
    "gemini",
    "codex-cli",
}
BACKEND_CHOICES = {"memory", "memvid"}
AUTONOMY_CHOICES = {"background", "manual", "autonomous"}


@dataclass(frozen=True)
class RuntimeSettings:
    provider: str
    model: str
    backend: str
    memory_dir: str
    workspace: str
    stream: bool
    require_api_auth: bool
    autonomy_mode: str = "background"
    updated_at: str | None = None

    @classmethod
    def from_config(cls, config: AgentConfig, *, autonomy_mode: str = "background") -> RuntimeSettings:
        return cls(
            provider=config.provider,
            model=config.model,
            backend=config.backend,
            memory_dir=str(config.memory_dir),
            workspace=str(config.workspace),
            stream=config.stream,
            require_api_auth=config.require_api_auth,
            autonomy_mode=autonomy_mode,
        )

    @classmethod
    def from_mapping(cls, raw: dict[str, Any], fallback: AgentConfig) -> RuntimeSettings:
        base = cls.from_config(fallback)
        values = asdict(base)
        for key in values:
            if key in raw:
                values[key] = raw[key]
        return _normalize_settings(cls(**values))

    def to_public_dict(self, *, path: Path, persisted: bool) -> dict[str, object]:
        payload = asdict(self)
        payload["path"] = str(path)
        payload["persisted"] = persisted
        return payload


class RuntimeSettingsStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def exists(self) -> bool:
        return self.path.exists()

    def load(self, fallback: AgentConfig) -> RuntimeSettings:
        if not self.path.exists():
            return RuntimeSettings.from_config(fallback)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("runtime settings file must contain a JSON object")
        return RuntimeSettings.from_mapping(raw, fallback)

    def save(self, settings: RuntimeSettings) -> RuntimeSettings:
        rendered = replace(settings, updated_at=datetime.now(UTC).isoformat())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        tmp_path.write_text(json.dumps(asdict(rendered), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp_path.replace(self.path)
        return rendered


def default_runtime_settings_path(config: AgentConfig) -> Path:
    state_parent = config.state_path.parent
    if state_parent.name == "state":
        return state_parent.parent / "config" / "runtime_settings.json"
    return state_parent / "runtime_settings.json"


def apply_runtime_settings(config: AgentConfig, settings: RuntimeSettings) -> AgentConfig:
    return replace(
        config,
        provider=settings.provider,
        model=settings.model,
        backend=settings.backend,
        memory_dir=Path(settings.memory_dir),
        workspace=Path(settings.workspace),
        stream=settings.stream,
        # `require_api_auth` is launch-time security policy and must not be
        # overridden by persisted runtime settings.
    )


def merge_runtime_settings(config: AgentConfig, current: RuntimeSettings, raw: dict[str, Any]) -> RuntimeSettings:
    values = asdict(current)
    for key in {
        "provider",
        "model",
        "backend",
        "memory_dir",
        "workspace",
        "stream",
        "autonomy_mode",
    }:
        if key in raw:
            values[key] = raw[key]
    return _normalize_settings(RuntimeSettings.from_mapping(values, config))


def _normalize_settings(settings: RuntimeSettings) -> RuntimeSettings:
    provider = _clean_required(settings.provider, "provider")
    if provider not in PROVIDER_CHOICES:
        raise ValueError(f"unsupported provider: {provider}")
    backend = _clean_required(settings.backend, "backend").lower()
    if backend not in BACKEND_CHOICES:
        raise ValueError(f"unsupported backend: {backend}")
    autonomy_mode = _clean_required(settings.autonomy_mode, "autonomy_mode")
    if autonomy_mode not in AUTONOMY_CHOICES:
        raise ValueError(f"unsupported autonomy_mode: {autonomy_mode}")
    model = _clean_required(settings.model, "model")
    workspace = _clean_required(settings.workspace, "workspace")
    memory_dir = _clean_required(settings.memory_dir, "memory_dir")
    return replace(
        settings,
        provider=provider,
        model=model,
        backend=backend,
        memory_dir=memory_dir,
        workspace=workspace,
        stream=_clean_bool(settings.stream),
        require_api_auth=_clean_bool(settings.require_api_auth),
        autonomy_mode=autonomy_mode,
        updated_at=str(settings.updated_at) if settings.updated_at else None,
    )


def _clean_required(value: object, field: str) -> str:
    rendered = str(value or "").strip()
    if not rendered:
        raise ValueError(f"{field} is required")
    return rendered


def _clean_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)
