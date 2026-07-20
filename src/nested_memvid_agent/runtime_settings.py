from __future__ import annotations

import hashlib
import json
import math
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock, RLock
from typing import Any

from .config import AgentConfig
from .file_lock import lock_exclusive, unlock
from .llm.model_catalog import PROVIDER_OPTIONS
from .private_artifacts import (
    harden_private_file,
    open_private_file_descriptor,
    read_private_text,
    write_private_text,
)

PROVIDER_CHOICES = set(PROVIDER_OPTIONS)
BACKEND_CHOICES = {"memory", "memvid"}
AUTONOMY_CHOICES = {"background", "manual", "autonomous"}
RUNTIME_SETTINGS_SCHEMA_VERSION = 1
_SETTINGS_THREAD_LOCKS: dict[Path, RLock] = {}
_SETTINGS_THREAD_LOCKS_GUARD = Lock()


class RuntimeSettingsConflict(RuntimeError):
    """Raised when a runtime settings compare-and-swap revision is stale."""


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
    schema_version: int = RUNTIME_SETTINGS_SCHEMA_VERSION
    revision: str | None = None
    sources: dict[str, str] = field(default_factory=dict)
    base_url: str | None = None
    api_key_env: str | None = None
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
    provider_startup_probe: bool = False
    enable_semantic_orchestration: bool = False
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
            base_url=config.base_url,
            api_key_env=config.api_key_env,
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
            provider_startup_probe=config.provider_startup_probe,
            enable_semantic_orchestration=config.enable_semantic_orchestration,
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


@dataclass(frozen=True)
class RuntimeSettingsUpdateResult:
    settings: RuntimeSettings
    previous_config: AgentConfig
    config: AgentConfig
    activation_result: object | None = None


class RuntimeSettingsStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._thread_lock = _settings_thread_lock(self.path)

    def exists(self) -> bool:
        with self._thread_lock:
            return harden_private_file(self.path, missing_ok=True)

    def load(self, fallback: AgentConfig) -> RuntimeSettings:
        with self._thread_lock, self._settings_lock(exclusive=True):
            return self._load_unlocked(fallback, migrate=True)

    def save(
        self,
        settings: RuntimeSettings,
        *,
        expected_revision: str | None = None,
    ) -> RuntimeSettings:
        with self._thread_lock, self._settings_lock(exclusive=True):
            current_raw = self._read_raw_unlocked()
            if (
                expected_revision is not None
                and current_raw is not None
                and current_raw.get("revision") != expected_revision
            ):
                raise RuntimeSettingsConflict("runtime_settings_revision_conflict")
            rendered = _persisted_settings(settings)
            self._write_unlocked(rendered)
            return rendered

    def transactional_update(
        self,
        fallback: AgentConfig | Callable[[], AgentConfig],
        changes: dict[str, Any],
        *,
        expected_revision: str | None,
        validate_config: Callable[[AgentConfig], None] | None = None,
        activate_config: Callable[[AgentConfig, AgentConfig], object | None] | None = None,
        rollback_config: Callable[[AgentConfig], None] | None = None,
    ) -> RuntimeSettingsUpdateResult:
        """Serialize merge, validation, persistence, and live activation."""

        with self._thread_lock, self._settings_lock(exclusive=True):
            previous_config = fallback() if callable(fallback) else fallback
            had_persisted_settings = harden_private_file(self.path, missing_ok=True)
            current = self._load_unlocked(previous_config, migrate=True)
            if expected_revision is not None and current.revision != expected_revision:
                raise RuntimeSettingsConflict("runtime_settings_revision_conflict")
            merged = merge_runtime_settings(previous_config, current, changes)
            candidate_config = apply_runtime_settings(previous_config, merged)
            if validate_config is not None:
                validate_config(candidate_config)
            saved = _persisted_settings(merged)
            next_config = apply_runtime_settings(previous_config, saved)
            activation_started = False
            try:
                self._write_unlocked(saved)
                activation_started = True
                activation_result = (
                    activate_config(previous_config, next_config)
                    if activate_config is not None
                    else None
                )
            except BaseException:
                rollback_error: BaseException | None = None
                if activation_started and rollback_config is not None:
                    try:
                        rollback_config(previous_config)
                    except BaseException as exc:  # noqa: BLE001 - persistence rollback must still run
                        rollback_error = exc
                try:
                    if had_persisted_settings:
                        self._write_unlocked(current)
                    else:
                        self._remove_unlocked()
                except BaseException as exc:
                    raise RuntimeError("runtime_settings_persistence_rollback_failed") from exc
                if rollback_error is not None:
                    raise RuntimeError("runtime_settings_activation_rollback_failed") from rollback_error
                raise
            return RuntimeSettingsUpdateResult(
                settings=saved,
                previous_config=previous_config,
                config=next_config,
                activation_result=activation_result,
            )

    def _load_unlocked(self, fallback: AgentConfig, *, migrate: bool) -> RuntimeSettings:
        raw = self._read_raw_unlocked()
        if raw is None:
            return _finalize_settings(RuntimeSettings.from_config(fallback), source="launch")
        schema_version = raw.get("schema_version", RUNTIME_SETTINGS_SCHEMA_VERSION)
        if schema_version != RUNTIME_SETTINGS_SCHEMA_VERSION:
            raise ValueError(f"unsupported runtime settings schema: {schema_version}")
        settings = RuntimeSettings.from_mapping(raw, fallback)
        # This policy is controlled only by launch configuration, even when an
        # older persisted file contains a different historical value.
        settings = replace(settings, require_api_auth=fallback.require_api_auth)
        finalized = _finalize_settings(settings, source="persisted")
        stored_revision = raw.get("revision")
        if (
            stored_revision
            and stored_revision != finalized.revision
            and stored_revision != _mapping_revision(raw)
        ):
            raise ValueError("runtime settings revision mismatch")
        if migrate and stored_revision != finalized.revision:
            self._write_unlocked(finalized)
        return finalized

    def _read_raw_unlocked(self) -> dict[str, Any] | None:
        raw_text = read_private_text(self.path, missing_ok=True)
        if raw_text is None:
            return None
        raw = json.loads(raw_text)
        if not isinstance(raw, dict):
            raise ValueError("runtime settings file must contain a JSON object")
        return raw

    def _write_unlocked(self, settings: RuntimeSettings) -> None:
        rendered = json.dumps(asdict(settings), indent=2, sort_keys=True)
        write_private_text(self.path, f"{rendered}\n")

    def _remove_unlocked(self) -> None:
        if not harden_private_file(self.path, missing_ok=True):
            return
        self.path.unlink()

    @contextmanager
    def _settings_lock(self, *, exclusive: bool) -> Iterator[None]:
        lock_path = self.path.with_name(f".{self.path.name}.lock")
        descriptor = open_private_file_descriptor(lock_path)
        with os.fdopen(descriptor, "r+", encoding="utf-8") as lock_handle:
            if exclusive:
                lock_exclusive(lock_handle)
            else:
                # Settings reads currently use exclusive locking so accepted
                # legacy shapes can be migrated atomically.
                raise ValueError("shared runtime settings locks are unsupported")
            try:
                yield
            finally:
                unlock(lock_handle)


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
        base_url=settings.base_url,
        api_key_env=settings.api_key_env,
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
        provider_startup_probe=settings.provider_startup_probe,
        enable_semantic_orchestration=settings.enable_semantic_orchestration,
        enable_auto_activate_low_risk_deltas=settings.enable_auto_activate_low_risk_deltas,
        enable_auto_skill_materialization=settings.enable_auto_skill_materialization,
        enable_auto_consolidation_shadow=settings.enable_auto_consolidation_shadow,
        enable_auto_consolidation_apply=settings.enable_auto_consolidation_apply,
        enable_diagnosis_to_patch=settings.enable_diagnosis_to_patch,
        # `require_api_auth` is launch-time security policy and must not be
        # overridden by persisted runtime settings.
    )


def runtime_settings_snapshot(config: AgentConfig, *, source: str = "run_override") -> RuntimeSettings:
    """Return a canonical, versioned, non-secret effective settings snapshot."""
    return _finalize_settings(RuntimeSettings.from_config(config), source=source)


def merge_runtime_settings(config: AgentConfig, current: RuntimeSettings, raw: dict[str, Any]) -> RuntimeSettings:
    values = asdict(current)
    for key in {
        "provider",
        "model",
        "base_url",
        "api_key_env",
        "backend",
        "memory_dir",
        "workspace",
        "temperature",
        "max_tool_rounds",
        "stream",
        "autonomy_mode",
        "provider_startup_probe",
        "enable_semantic_orchestration",
        *TOOL_PERMISSION_FIELDS,
    }:
        if key in raw:
            values[key] = raw[key]
    return _normalize_settings(RuntimeSettings.from_mapping(values, config))


def _finalize_settings(settings: RuntimeSettings, *, source: str) -> RuntimeSettings:
    normalized = _normalize_settings(settings)
    source_map = {
        key: ("launch" if key == "require_api_auth" else source)
        for key in asdict(normalized)
        if key not in {"schema_version", "revision", "sources", "updated_at"}
    }
    without_revision = replace(
        normalized,
        schema_version=RUNTIME_SETTINGS_SCHEMA_VERSION,
        revision=None,
        sources=source_map,
    )
    canonical = json.dumps(asdict(without_revision), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return replace(without_revision, revision=hashlib.sha256(canonical.encode("utf-8")).hexdigest())


def _mapping_revision(raw: dict[str, Any]) -> str:
    payload = dict(raw)
    payload["revision"] = None
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _persisted_settings(settings: RuntimeSettings) -> RuntimeSettings:
    return _finalize_settings(
        replace(
            settings,
            schema_version=RUNTIME_SETTINGS_SCHEMA_VERSION,
            updated_at=datetime.now(UTC).isoformat(),
        ),
        source="persisted",
    )


def _settings_thread_lock(path: Path) -> RLock:
    key = Path(os.path.abspath(os.fspath(path)))
    with _SETTINGS_THREAD_LOCKS_GUARD:
        return _SETTINGS_THREAD_LOCKS.setdefault(key, RLock())


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
        base_url=_clean_optional(settings.base_url),
        api_key_env=_clean_optional(settings.api_key_env),
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
        provider_startup_probe=_clean_bool(settings.provider_startup_probe),
        enable_semantic_orchestration=_clean_bool(settings.enable_semantic_orchestration),
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


def _clean_optional(value: object) -> str | None:
    rendered = str(value or "").strip()
    return rendered or None


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
