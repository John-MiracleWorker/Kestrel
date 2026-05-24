from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import AgentConfig
from .llm.model_catalog import PROVIDER_OPTIONS

PROVIDER_CHOICES = set(PROVIDER_OPTIONS)
BACKEND_CHOICES = {"memory", "memvid"}
AUTONOMY_CHOICES = {"background", "manual", "autonomous"}
TOOL_PERMISSION_FIELDS = {
    "allow_shell",
    "allow_file_write",
    "allow_codex_cli",
    "allow_plugin_install",
    "allow_git_commit",
    "allow_memory_import",
    "allow_executable_skills",
    "allow_web",
    "allow_self_modification",
    "enable_auto_activate_low_risk_deltas",
    "enable_auto_skill_materialization",
    "enable_auto_consolidation_shadow",
    "enable_auto_consolidation_apply",
    "enable_diagnosis_to_patch",
}


@dataclass(frozen=True)
class RuntimeSettings:
    provider: str
    model: str
    backend: str
    memory_dir: str
    workspace: str
    temperature: float
    max_tool_rounds: int
    stream: bool
    require_api_auth: bool
    autonomy_mode: str = "background"
    allow_shell: bool = False
    allow_file_write: bool = False
    allow_codex_cli: bool = False
    allow_plugin_install: bool = False
    allow_git_commit: bool = False
    allow_memory_import: bool = False
    allow_executable_skills: bool = False
    allow_web: bool = False
    allow_self_modification: bool = False
    enable_auto_activate_low_risk_deltas: bool = False
    enable_auto_skill_materialization: bool = False
    enable_auto_consolidation_shadow: bool = False
    enable_auto_consolidation_apply: bool = False
    enable_diagnosis_to_patch: bool = False
    updated_at: str | None = None

    @classmethod
    def from_config(cls, config: AgentConfig, *, autonomy_mode: str = "background") -> RuntimeSettings:
        return cls(
            provider=config.provider,
            model=config.model,
            backend=config.backend,
            memory_dir=str(config.memory_dir),
            workspace=str(config.workspace),
            temperature=config.temperature,
            max_tool_rounds=config.max_tool_rounds,
            stream=config.stream,
            require_api_auth=config.require_api_auth,
            autonomy_mode=autonomy_mode,
            allow_shell=config.allow_shell,
            allow_file_write=config.allow_file_write,
            allow_codex_cli=config.allow_codex_cli,
            allow_plugin_install=config.allow_plugin_install,
            allow_git_commit=config.allow_git_commit,
            allow_memory_import=config.allow_memory_import,
            allow_executable_skills=config.allow_executable_skills,
            allow_web=config.allow_web,
            allow_self_modification=config.allow_self_modification,
            enable_auto_activate_low_risk_deltas=config.enable_auto_activate_low_risk_deltas,
            enable_auto_skill_materialization=config.enable_auto_skill_materialization,
            enable_auto_consolidation_shadow=config.enable_auto_consolidation_shadow,
            enable_auto_consolidation_apply=config.enable_auto_consolidation_apply,
            enable_diagnosis_to_patch=config.enable_diagnosis_to_patch,
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
        temperature=settings.temperature,
        backend=settings.backend,
        memory_dir=Path(settings.memory_dir),
        workspace=Path(settings.workspace),
        max_tool_rounds=settings.max_tool_rounds,
        stream=settings.stream,
        allow_shell=settings.allow_shell,
        allow_file_write=settings.allow_file_write,
        allow_codex_cli=settings.allow_codex_cli,
        allow_plugin_install=settings.allow_plugin_install,
        allow_git_commit=settings.allow_git_commit,
        allow_memory_import=settings.allow_memory_import,
        allow_executable_skills=settings.allow_executable_skills,
        allow_web=settings.allow_web,
        allow_self_modification=settings.allow_self_modification,
        enable_auto_activate_low_risk_deltas=settings.enable_auto_activate_low_risk_deltas,
        enable_auto_skill_materialization=settings.enable_auto_skill_materialization,
        enable_auto_consolidation_shadow=settings.enable_auto_consolidation_shadow,
        enable_auto_consolidation_apply=settings.enable_auto_consolidation_apply,
        enable_diagnosis_to_patch=settings.enable_diagnosis_to_patch,
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
        "temperature",
        "max_tool_rounds",
        "stream",
        "autonomy_mode",
        *TOOL_PERMISSION_FIELDS,
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
        temperature=_clean_temperature(settings.temperature),
        max_tool_rounds=_clean_int_range(settings.max_tool_rounds, "max_tool_rounds", minimum=0, maximum=50),
        stream=_clean_bool(settings.stream),
        require_api_auth=_clean_bool(settings.require_api_auth),
        autonomy_mode=autonomy_mode,
        allow_shell=_clean_bool(settings.allow_shell),
        allow_file_write=_clean_bool(settings.allow_file_write),
        allow_codex_cli=_clean_bool(settings.allow_codex_cli),
        allow_plugin_install=_clean_bool(settings.allow_plugin_install),
        allow_git_commit=_clean_bool(settings.allow_git_commit),
        allow_memory_import=_clean_bool(settings.allow_memory_import),
        allow_executable_skills=_clean_bool(settings.allow_executable_skills),
        allow_web=_clean_bool(settings.allow_web),
        allow_self_modification=_clean_bool(settings.allow_self_modification),
        enable_auto_activate_low_risk_deltas=_clean_bool(settings.enable_auto_activate_low_risk_deltas),
        enable_auto_skill_materialization=_clean_bool(settings.enable_auto_skill_materialization),
        enable_auto_consolidation_shadow=_clean_bool(settings.enable_auto_consolidation_shadow),
        enable_auto_consolidation_apply=_clean_bool(settings.enable_auto_consolidation_apply),
        enable_diagnosis_to_patch=_clean_bool(settings.enable_diagnosis_to_patch),
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


def _clean_temperature(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise ValueError("temperature must be a number")
    try:
        temperature = float(value)
    except ValueError as exc:
        raise ValueError("temperature must be a number") from exc
    if not math.isfinite(temperature):
        raise ValueError("temperature must be finite")
    if temperature < 0 or temperature > 2:
        raise ValueError("temperature must be between 0 and 2")
    return temperature


def _clean_int_range(value: object, field: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise ValueError(f"{field} must be an integer")
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an integer") from exc
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}")
    return parsed
