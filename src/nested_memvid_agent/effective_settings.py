from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import AgentConfig
from .llm.model_catalog import PROVIDER_OPTIONS
from .runtime_settings import (
    RuntimeSettings,
    RuntimeSettingsStore,
    RuntimeSettingsUpdateResult,
)

# Category names are a public contract of the settings workspace; do not
# rename or reorder without a coordinated client change.
SETTING_CATEGORIES: tuple[str, ...] = (
    "General",
    "Models and providers",
    "Safety and permissions",
    "Storage and memory",
    "Containment",
    "Appearance",
    "Notifications",
    "Updates",
    "Diagnostics",
    "Advanced",
)

_NETWORK_CAPABILITY_KEY = "network"


@dataclass(frozen=True)
class SettingDescriptor:
    """Static metadata for one projected setting.

    Descriptors only describe how a value is read from (and written to) the
    existing stores; they never hold values themselves, so there is exactly
    one settings database: the runtime settings store plus the owning
    feature managers.
    """

    id: str
    key: str | None
    category: str
    type: str
    applies: str
    writable: bool = True
    authority_impact: str = "none"
    privacy_impact: str = "none"
    allowed_values: tuple[str, ...] | None = None
    allowed_range: tuple[float, float] | None = None
    requires_capability: str | None = None
    restart_required: bool = False


@dataclass(frozen=True)
class ProjectedSetting:
    """Configured/effective truth for one setting at one store revision."""

    descriptor: SettingDescriptor
    configured_value: Any
    effective_value: Any
    blockers: tuple[str, ...]
    revision: str | None
    provenance: str
    undo_available: bool

    @property
    def setting_id(self) -> str:
        return self.descriptor.id

    @property
    def key(self) -> str | None:
        return self.descriptor.key

    @property
    def category(self) -> str:
        return self.descriptor.category

    @property
    def type(self) -> str:
        return self.descriptor.type

    @property
    def applies(self) -> str:
        return self.descriptor.applies

    @property
    def writable(self) -> bool:
        return self.descriptor.writable

    @property
    def authority_impact(self) -> str:
        return self.descriptor.authority_impact

    @property
    def privacy_impact(self) -> str:
        return self.descriptor.privacy_impact

    @property
    def allowed_values(self) -> tuple[str, ...] | None:
        return self.descriptor.allowed_values

    @property
    def allowed_range(self) -> tuple[float, float] | None:
        return self.descriptor.allowed_range

    @property
    def requires_approval(self) -> bool:
        return self.descriptor.authority_impact == "grants_authority"

    @property
    def restart_required(self) -> bool:
        return self.descriptor.restart_required or self.descriptor.applies == "restart"

    def to_public_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.setting_id,
            "key": self.key,
            "category": self.category,
            "type": self.type,
            "configured_value": self.configured_value,
            "effective_value": self.effective_value,
            "blockers": list(self.blockers),
            "authority_impact": self.authority_impact,
            "privacy_impact": self.privacy_impact,
            "applies": self.applies,
            "revision": self.revision,
            "provenance": self.provenance,
            "undo_available": self.undo_available,
            "allowed_values": (
                list(self.allowed_values) if self.allowed_values is not None else None
            ),
            "allowed_range": (
                list(self.allowed_range) if self.allowed_range is not None else None
            ),
            "restart_required": self.restart_required,
            "writable": self.writable,
            "requires_approval": self.requires_approval,
        }
        return payload


@dataclass(frozen=True)
class SettingsProjection:
    settings: tuple[ProjectedSetting, ...]
    revision: str | None

    @property
    def categories(self) -> tuple[str, ...]:
        return SETTING_CATEGORIES

    def require(self, setting_id: str) -> ProjectedSetting:
        for setting in self.settings:
            if setting.setting_id == setting_id:
                return setting
        raise KeyError(f"unknown setting: {setting_id}")

    def to_public_dict(self) -> dict[str, Any]:
        items = [setting.to_public_dict() for setting in self.settings]
        return {
            "schema": "kestrel.effective_settings.v1",
            "revision": self.revision,
            "categories": list(SETTING_CATEGORIES),
            "items": items,
            "items_by_id": {item["id"]: item for item in items},
            "counts": {
                "total": len(items),
                "blocked": sum(1 for item in items if item["blockers"]),
                "restart_required": sum(1 for item in items if item["restart_required"]),
            },
        }


def _permission(
    setting_id: str,
    key: str,
    *,
    authority_impact: str,
    privacy_impact: str,
    requires_capability: str | None = None,
) -> SettingDescriptor:
    return SettingDescriptor(
        id=setting_id,
        key=key,
        category="Safety and permissions",
        type="boolean",
        applies="new_runs",
        authority_impact=authority_impact,
        privacy_impact=privacy_impact,
        requires_capability=requires_capability,
    )


SETTING_DESCRIPTORS: tuple[SettingDescriptor, ...] = (
    # General
    SettingDescriptor(
        id="general.autonomy_mode",
        key="autonomy_mode",
        category="General",
        type="enum",
        applies="new_runs",
        authority_impact="grants_authority",
        allowed_values=("background", "manual", "autonomous"),
    ),
    SettingDescriptor(
        id="general.stream",
        key="stream",
        category="General",
        type="boolean",
        applies="new_runs",
    ),
    # Models and providers
    SettingDescriptor(
        id="models.provider",
        key="provider",
        category="Models and providers",
        type="enum",
        applies="new_runs",
        allowed_values=tuple(PROVIDER_OPTIONS),
    ),
    SettingDescriptor(
        id="model",
        key="model",
        category="Models and providers",
        type="string",
        applies="new_runs",
    ),
    SettingDescriptor(
        id="models.base_url",
        key="base_url",
        category="Models and providers",
        type="string",
        applies="new_runs",
        privacy_impact="network_egress",
    ),
    SettingDescriptor(
        id="models.api_key_env",
        key="api_key_env",
        category="Models and providers",
        type="string",
        applies="new_runs",
    ),
    SettingDescriptor(
        id="models.temperature",
        key="temperature",
        category="Models and providers",
        type="number",
        applies="new_runs",
        allowed_range=(0.0, 2.0),
    ),
    SettingDescriptor(
        id="models.startup_probe",
        key="provider_startup_probe",
        category="Models and providers",
        type="boolean",
        applies="new_runs",
        privacy_impact="network_egress",
    ),
    # Safety and permissions
    _permission(
        "tools.web_search.enabled",
        "allow_web",
        authority_impact="grants_authority",
        privacy_impact="network_egress",
        requires_capability=_NETWORK_CAPABILITY_KEY,
    ),
    _permission(
        "tools.shell.enabled",
        "allow_shell",
        authority_impact="grants_authority",
        privacy_impact="local_execution",
    ),
    _permission(
        "tools.file_write.enabled",
        "allow_file_write",
        authority_impact="grants_authority",
        privacy_impact="local_write",
    ),
    _permission(
        "tools.codex_cli.enabled",
        "allow_codex_cli",
        authority_impact="grants_authority",
        privacy_impact="local_execution",
    ),
    _permission(
        "tools.plugin_install.enabled",
        "allow_plugin_install",
        authority_impact="grants_authority",
        privacy_impact="local_write",
    ),
    _permission(
        "tools.git_commit.enabled",
        "allow_git_commit",
        authority_impact="grants_authority",
        privacy_impact="local_write",
    ),
    _permission(
        "tools.memory_import.enabled",
        "allow_memory_import",
        authority_impact="grants_authority",
        privacy_impact="memory_write",
    ),
    _permission(
        "tools.executable_skills.enabled",
        "allow_executable_skills",
        authority_impact="grants_authority",
        privacy_impact="local_execution",
    ),
    _permission(
        "tools.self_modification.enabled",
        "allow_self_modification",
        authority_impact="grants_authority",
        privacy_impact="local_write",
    ),
    SettingDescriptor(
        id="server.require_api_auth",
        key="require_api_auth",
        category="Safety and permissions",
        type="boolean",
        applies="restart",
        writable=False,
        restart_required=True,
    ),
    # Storage and memory
    SettingDescriptor(
        id="storage.backend",
        key="backend",
        category="Storage and memory",
        type="enum",
        applies="new_runs",
        allowed_values=("memory", "memvid"),
    ),
    SettingDescriptor(
        id="storage.memory_dir",
        key="memory_dir",
        category="Storage and memory",
        type="path",
        applies="restart",
        restart_required=True,
        privacy_impact="local_write",
    ),
    SettingDescriptor(
        id="memory.enable_semantic_orchestration",
        key="enable_semantic_orchestration",
        category="Storage and memory",
        type="boolean",
        applies="new_runs",
    ),
    # Containment
    SettingDescriptor(
        id="containment.workspace",
        key="workspace",
        category="Containment",
        type="path",
        applies="new_runs",
        privacy_impact="local_write",
    ),
    SettingDescriptor(
        id="containment.max_tool_rounds",
        key="max_tool_rounds",
        category="Containment",
        type="number",
        applies="new_runs",
        allowed_range=(0.0, 50.0),
    ),
    # Appearance
    SettingDescriptor(
        id="appearance.theme",
        key=None,
        category="Appearance",
        type="enum",
        applies="immediate",
        writable=False,
        allowed_values=("system", "light", "dark"),
    ),
    # Notifications
    SettingDescriptor(
        id="notifications.desktop",
        key=None,
        category="Notifications",
        type="boolean",
        applies="immediate",
        writable=False,
    ),
    # Updates
    SettingDescriptor(
        id="updates.channel",
        key=None,
        category="Updates",
        type="enum",
        applies="restart",
        writable=False,
        restart_required=True,
        allowed_values=("stable",),
    ),
    # Diagnostics
    SettingDescriptor(
        id="diagnostics.provider_probe",
        key="provider_startup_probe",
        category="Diagnostics",
        type="boolean",
        applies="new_runs",
        privacy_impact="network_egress",
    ),
    # Advanced
    SettingDescriptor(
        id="advanced.auto_activate_low_risk_deltas",
        key="enable_auto_activate_low_risk_deltas",
        category="Advanced",
        type="boolean",
        applies="new_runs",
        authority_impact="grants_authority",
    ),
    SettingDescriptor(
        id="advanced.auto_skill_materialization",
        key="enable_auto_skill_materialization",
        category="Advanced",
        type="boolean",
        applies="new_runs",
        authority_impact="grants_authority",
    ),
    SettingDescriptor(
        id="advanced.auto_consolidation_shadow",
        key="enable_auto_consolidation_shadow",
        category="Advanced",
        type="boolean",
        applies="new_runs",
    ),
    SettingDescriptor(
        id="advanced.auto_consolidation_apply",
        key="enable_auto_consolidation_apply",
        category="Advanced",
        type="boolean",
        applies="new_runs",
        authority_impact="grants_authority",
    ),
    SettingDescriptor(
        id="advanced.diagnosis_to_patch",
        key="enable_diagnosis_to_patch",
        category="Advanced",
        type="boolean",
        applies="new_runs",
        authority_impact="grants_authority",
    ),
    SettingDescriptor(
        id="paths.state_path",
        key=None,
        category="Advanced",
        type="path",
        applies="restart",
        writable=False,
        restart_required=True,
    ),
)

_DESCRIPTOR_BY_ID: dict[str, SettingDescriptor] = {
    descriptor.id: descriptor for descriptor in SETTING_DESCRIPTORS
}

_STATIC_EVIDENCE_VALUES: dict[str, Any] = {
    "appearance.theme": "system",
    "notifications.desktop": False,
    "updates.channel": "stable",
}


def _clean_value(descriptor: SettingDescriptor, value: Any) -> Any:
    if descriptor.type == "boolean":
        if not isinstance(value, bool):
            raise ValueError(f"{descriptor.id} must be a boolean")
        return value
    if descriptor.type == "enum":
        rendered = str(value).strip()
        if descriptor.allowed_values is not None and rendered not in descriptor.allowed_values:
            raise ValueError(f"unsupported value for {descriptor.id}: {rendered}")
        return rendered
    if descriptor.type == "number":
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError(f"{descriptor.id} must be a number")
        numeric = float(value)
        if descriptor.allowed_range is not None:
            low, high = descriptor.allowed_range
            if numeric < low or numeric > high:
                raise ValueError(f"{descriptor.id} must be between {low} and {high}")
        return int(numeric) if float(numeric).is_integer() else numeric
    # string / path
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{descriptor.id} must be a non-empty string")
    return value.strip()


def _configured_value(
    descriptor: SettingDescriptor,
    runtime: RuntimeSettings,
    config: AgentConfig,
) -> Any:
    if descriptor.id == "paths.state_path":
        return str(config.state_path)
    if descriptor.id in _STATIC_EVIDENCE_VALUES:
        return _STATIC_EVIDENCE_VALUES[descriptor.id]
    if descriptor.key is None:
        raise KeyError(f"setting has no value source: {descriptor.id}")
    return getattr(runtime, descriptor.key)


def project_settings(
    *,
    runtime: RuntimeSettings,
    capabilities: Any = (),
    config: AgentConfig | None = None,
) -> SettingsProjection:
    """Project configured/effective settings truth from current stores.

    Values are read from the loaded runtime settings (and the live config for
    launch-controlled evidence); capability blockers come from the caller's
    capability catalog. No values are persisted here.
    """

    disabled_capabilities = {
        str(item.get("key"))
        for item in capabilities or ()
        if isinstance(item, dict) and not bool(item.get("effective_enabled", True))
    }
    fallback_config = config if config is not None else AgentConfig()
    projected: list[ProjectedSetting] = []
    for descriptor in SETTING_DESCRIPTORS:
        configured = _configured_value(descriptor, runtime, fallback_config)
        blockers: tuple[str, ...] = ()
        if (
            descriptor.requires_capability is not None
            and descriptor.requires_capability in disabled_capabilities
        ):
            blockers = (f"capability:{descriptor.requires_capability}_disabled",)
        if blockers and isinstance(configured, bool):
            effective: Any = False
        else:
            effective = configured
        provenance = (
            runtime.sources.get(descriptor.key, "launch")
            if descriptor.key is not None
            else "launch"
        )
        projected.append(
            ProjectedSetting(
                descriptor=descriptor,
                configured_value=configured,
                effective_value=effective,
                blockers=blockers,
                revision=runtime.revision,
                provenance=provenance,
                undo_available=True,
            )
        )
    return SettingsProjection(settings=tuple(projected), revision=runtime.revision)


def descriptor_for(setting_id: str) -> SettingDescriptor:
    try:
        return _DESCRIPTOR_BY_ID[setting_id]
    except KeyError as exc:
        raise KeyError(f"unknown setting: {setting_id}") from exc


def apply_setting_change(
    store: RuntimeSettingsStore,
    fallback: AgentConfig | Any,
    *,
    setting_id: str,
    value: Any,
    expected_revision: str,
    validate_config: Any | None = None,
    activate_config: Any | None = None,
    rollback_config: Any | None = None,
) -> RuntimeSettingsUpdateResult:
    """Commit one setting through the runtime settings store's CAS update.

    The revision compare-and-swap is enforced inside
    ``RuntimeSettingsStore.transactional_update``; this wrapper only maps the
    descriptor to its owning store field after validation.
    """

    descriptor = descriptor_for(setting_id)
    if not descriptor.writable or descriptor.key is None:
        raise ValueError(f"setting_is_read_only: {setting_id}")
    cleaned = _clean_value(descriptor, value)
    return store.transactional_update(
        fallback,
        {descriptor.key: cleaned},
        expected_revision=expected_revision,
        validate_config=validate_config,
        activate_config=activate_config,
        rollback_config=rollback_config,
    )
