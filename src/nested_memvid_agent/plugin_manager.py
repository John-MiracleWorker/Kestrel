from __future__ import annotations

import binascii
import hashlib
import json
import re
import subprocess  # nosec B404
import tempfile
from base64 import b64decode
from dataclasses import dataclass
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .extension_transaction import (
    DirectoryRemoval,
    DirectorySwap,
    ExtensionCleanupIncompleteError,
    ExtensionTransactionError,
    copy_regular_tree,
    create_sibling_stage,
    ensure_real_directory,
    extension_lock,
    fsync_tree,
    path_exists,
    read_regular_file,
    read_regular_text,
    remove_tree_verified,
    write_regular_file,
)
from .models import EvidenceRef, MemoryKind, MemoryLayer, MemoryRecord
from .repair_integrity import (
    hardened_readonly_git_command,
    hardened_readonly_git_environment,
)
from .security_boundary import redact_secrets
from .skill_validation import validate_skill_manifest
from .state_store import AgentStateStore

PLUGIN_NAMESPACE = "plugin"
PLUGIN_SOURCE_DIR = "source"
PLUGIN_GENERATED_DIR = "generated"
DEFAULT_PLUGIN_RISK = "medium"
GIT_TIMEOUT_SECONDS = 60
PLUGIN_DEPENDENCY_KINDS = ("python", "node", "system")
PLUGIN_MANIFEST_SIGNATURE_SCHEMA = "kestrel.plugin.signature.v1"

_GITHUB_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
_PLUGIN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_COMPONENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/+-]{0,255}$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


class PluginError(ValueError):
    """Raised when plugin source, manifest, or install state is invalid."""


@dataclass(frozen=True)
class GitHubPluginSource:
    owner: str
    repo: str

    @property
    def display_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}"

    @property
    def clone_url(self) -> str:
        return f"{self.display_url}.git"


@dataclass(frozen=True)
class PluginManifest:
    id: str
    name: str
    version: str
    description: str
    format: str
    risk: str
    permissions: tuple[str, ...]
    requires_env: tuple[str, ...]
    dependencies: dict[str, list[str]]
    dependency_lock: dict[str, list[dict[str, str]]]
    compatibility: dict[str, str]
    signature: dict[str, str] | None
    isolation: dict[str, Any]
    skills: tuple[dict[str, Any], ...]
    mcp_servers: tuple[dict[str, Any], ...]
    warnings: tuple[str, ...]
    unsupported_features: tuple[str, ...]
    raw: dict[str, Any]

    def to_state_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "format": self.format,
            "risk": self.risk,
            "permissions": list(self.permissions),
            "requires_env": list(self.requires_env),
            "dependencies": self.dependencies,
            "dependency_lock": self.dependency_lock,
            "compatibility": self.compatibility,
            "signature": self.signature,
            "isolation": self.isolation,
            "skills": list(self.skills),
            "mcp_servers": list(self.mcp_servers),
            "warnings": list(self.warnings),
            "unsupported_features": list(self.unsupported_features),
            "raw": self.raw,
        }


class GitPluginFetcher:
    """Fetch public GitHub repositories with git, without executing plugin code."""

    def fetch(self, source: GitHubPluginSource, destination: Path, ref: str | None = None) -> str:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="kestrel-git-fetch-") as git_cwd_name:
            git_cwd = Path(git_cwd_name)
            command = [
                "git",
                "clone",
                f"--template={git_cwd}",
                "--depth",
                "1",
            ]
            if ref and not _looks_like_sha(ref):
                command.extend(["--branch", ref])
            command.extend([source.clone_url, str(destination)])
            _run_git(command, workspace=git_cwd)
            if ref and _looks_like_sha(ref):
                _run_git(
                    [
                        "git",
                        "-C",
                        str(destination),
                        "fetch",
                        "--depth",
                        "1",
                        "origin",
                        ref,
                    ],
                    workspace=destination,
                )
                _run_git(
                    ["git", "-C", str(destination), "checkout", "--detach", ref],
                    workspace=destination,
                )
            return _run_git(
                ["git", "-C", str(destination), "rev-parse", "HEAD"],
                workspace=destination,
            ).strip()


class PluginManager:
    """Installs, vets, and materializes GitHub plugins into Kestrel surfaces."""

    def __init__(
        self,
        root: Path,
        state: AgentStateStore,
        *,
        fetcher: GitPluginFetcher | None = None,
    ) -> None:
        self.root = root
        self.state = state
        self.fetcher = fetcher or GitPluginFetcher()
        self.root.mkdir(parents=True, exist_ok=True)

    def review(self, source: str, *, ref: str | None = None) -> dict[str, Any]:
        parsed_source = parse_github_plugin_source(source)
        normalized_ref = _normalize_ref(ref)
        tmp_parent = self.root / ".tmp"
        tmp_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="plugin-review-", dir=tmp_parent) as tmp_name:
            repo_path = Path(tmp_name) / "repo"
            commit_sha = self.fetcher.fetch(parsed_source, repo_path, normalized_ref)
            manifest = load_plugin_manifest(repo_path)
            risk_report = _risk_report(
                manifest,
                source=parsed_source.display_url,
                commit_sha=commit_sha,
                source_dir=repo_path,
            )
            return _review_payload(
                manifest,
                source=parsed_source.display_url,
                ref=normalized_ref,
                commit_sha=commit_sha,
                risk_report=risk_report,
            )

    def review_update(
        self,
        plugin_id: str,
        *,
        ref: str | None = None,
    ) -> dict[str, Any]:
        current = self.state.get_plugin(plugin_id)
        parsed_source = parse_github_plugin_source(str(current["source_url"]))
        normalized_ref = _normalize_ref(ref or current.get("source_ref"))
        tmp_parent = self.root / ".tmp"
        tmp_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="plugin-update-review-",
            dir=tmp_parent,
        ) as tmp_name:
            repo_path = Path(tmp_name) / "repo"
            commit_sha = self.fetcher.fetch(parsed_source, repo_path, normalized_ref)
            manifest = load_plugin_manifest(repo_path)
            if manifest.id != plugin_id:
                raise PluginError(
                    "Plugin manifest id changed during update: "
                    f"expected {plugin_id}, got {manifest.id}"
                )
            risk_report = _risk_report(
                manifest,
                source=parsed_source.display_url,
                commit_sha=commit_sha,
                source_dir=repo_path,
                prior_manifest=dict(current["manifest"]),
            )
            payload = _review_payload(
                manifest,
                source=parsed_source.display_url,
                ref=normalized_ref,
                commit_sha=commit_sha,
                risk_report=risk_report,
            )
            payload["current_commit_sha"] = current["commit_sha"]
            return payload

    def install(
        self,
        source: str,
        *,
        ref: str | None = None,
        enable: bool = False,
        overwrite: bool = False,
        expected_plugin_id: str | None = None,
    ) -> dict[str, Any]:
        parsed_source = parse_github_plugin_source(source)
        normalized_ref = _normalize_ref(ref)
        with extension_lock(self.root, ".plugin-manager.lock"):
            stage = create_sibling_stage(self.root, prefix="plugin")
            try:
                staged_source = stage / PLUGIN_SOURCE_DIR
                commit_sha = self.fetcher.fetch(
                    parsed_source,
                    staged_source,
                    normalized_ref,
                )
                initial_manifest = load_plugin_manifest(staged_source)
                if (
                    expected_plugin_id is not None
                    and initial_manifest.id != expected_plugin_id
                ):
                    raise PluginError(
                        "Plugin manifest id changed during update: "
                        f"expected {expected_plugin_id}, got {initial_manifest.id}"
                    )
                plugin_dir = _safe_plugin_dir(self.root, initial_manifest.id)
                if path_exists(plugin_dir) and not overwrite:
                    raise FileExistsError(f"Plugin already installed: {initial_manifest.id}")
                current: dict[str, Any] | None = None
                if overwrite:
                    try:
                        current = self.state.get_plugin(initial_manifest.id)
                    except KeyError:
                        current = None
                risk_report = _risk_report(
                    initial_manifest,
                    source=parsed_source.display_url,
                    commit_sha=commit_sha,
                    source_dir=staged_source,
                    prior_manifest=(
                        dict(current["manifest"]) if current is not None else None
                    ),
                )
                authority_delta = dict(risk_report["authority_delta"])
                if (
                    current is not None
                    and bool(current["enabled"])
                    and bool(authority_delta["expands_authority"])
                ):
                    added = ", ".join(str(item) for item in authority_delta["added"])
                    raise PluginError(
                        "Plugin update expands authority while enabled; disable and "
                        f"review the update first: {added}"
                    )
                _ensure_plugin_enable_allowed(
                    initial_manifest,
                    risk_report,
                    enable=enable,
                )
                row = _plugin_state_row(
                    initial_manifest,
                    source=parsed_source.display_url,
                    ref=normalized_ref,
                    commit_sha=commit_sha,
                    plugin_dir=plugin_dir,
                    risk_report=risk_report,
                    enabled=enable,
                )
                self._desired_extension_rows(
                    row,
                    tree_root=stage,
                    materialize_skills=True,
                    refresh_launch_vetting=False,
                )

                # Re-read every manifest-derived field only after the complete
                # candidate tree exists. A changed or unreadable source never
                # reaches the live path.
                staged_manifest = load_plugin_manifest(staged_source)
                if staged_manifest.to_state_payload() != initial_manifest.to_state_payload():
                    raise PluginError("Plugin manifest changed during staged validation.")
                staged_risk_report = _risk_report(
                    staged_manifest,
                    source=parsed_source.display_url,
                    commit_sha=commit_sha,
                    source_dir=staged_source,
                    prior_manifest=(
                        dict(current["manifest"]) if current is not None else None
                    ),
                )
                if staged_risk_report != risk_report:
                    raise PluginError("Plugin source changed during staged validation.")
                fsync_tree(stage)

                swap = DirectorySwap(live=plugin_dir, stage=stage)
                quiesce = self.state.quiesce_plugin_bundle(initial_manifest.id)
                state_committed = False
                try:
                    swap.publish()
                    try:
                        published_manifest = load_plugin_manifest(
                            plugin_dir / PLUGIN_SOURCE_DIR
                        )
                        if (
                            published_manifest.to_state_payload()
                            != staged_manifest.to_state_payload()
                        ):
                            raise PluginError("Published plugin manifest failed validation.")
                        skills, mcp_servers = self._desired_extension_rows(
                            row,
                            tree_root=plugin_dir,
                            materialize_skills=False,
                            refresh_launch_vetting=True,
                        )
                        installed = self.state.replace_plugin_bundle(
                            row,
                            skills=skills,
                            mcp_servers=mcp_servers,
                        )
                        state_committed = True
                    except BaseException:
                        swap.restore()
                        raise
                    swap.finalize()
                except BaseException:
                    if (
                        not state_committed
                        and quiesce is not None
                        and not swap.displaced
                        and not swap.published
                    ):
                        self._restore_quiesce(quiesce)
                    raise
                return installed
            finally:
                if path_exists(stage):
                    remove_tree_verified(stage)

    def update(self, plugin_id: str, *, ref: str | None = None) -> dict[str, Any]:
        current = self.state.get_plugin(plugin_id)
        return self.install(
            str(current["source_url"]),
            ref=ref or current.get("source_ref"),
            enable=bool(current["enabled"]),
            overwrite=True,
            expected_plugin_id=plugin_id,
        )

    def list_plugins(self) -> list[dict[str, Any]]:
        return self.state.list_plugins()

    def get_plugin(self, plugin_id: str) -> dict[str, Any]:
        return self.state.get_plugin(plugin_id)

    def set_enabled(self, plugin_id: str, enabled: bool) -> dict[str, Any]:
        with extension_lock(self.root, ".plugin-manager.lock"):
            row = self.state.get_plugin(plugin_id)
            desired = dict(row)
            desired["enabled"] = enabled
            if not enabled:
                prefix = f"{PLUGIN_NAMESPACE}.{plugin_id}."
                skills = [
                    {**skill, "enabled": False}
                    for skill in self.state.list_skills()
                    if str(skill["id"]).startswith(prefix)
                ]
                mcp_servers = [
                    {**server, "enabled": False}
                    for server in self.state.list_mcp_servers()
                    if str(server["id"]).startswith(prefix)
                ]
                return self.state.replace_plugin_bundle(
                    desired,
                    skills=skills,
                    mcp_servers=mcp_servers,
                )

            _ensure_plugin_enable_allowed_from_row(row)
            plugin_dir = _installed_plugin_dir(self.root, row)
            self._validate_installed_source(row, plugin_dir)
            skills, mcp_servers = self._desired_extension_rows(
                desired,
                tree_root=plugin_dir,
                materialize_skills=False,
                refresh_launch_vetting=True,
            )
            return self.state.replace_plugin_bundle(
                desired,
                skills=skills,
                mcp_servers=mcp_servers,
            )

    def remove(self, plugin_id: str) -> dict[str, Any]:
        with extension_lock(self.root, ".plugin-manager.lock"):
            row = self.state.get_plugin(plugin_id)
            install_path = _installed_plugin_dir(self.root, row)
            removal = DirectoryRemoval(install_path)
            quiesce = self.state.quiesce_plugin_bundle(plugin_id)
            state_committed = False
            try:
                removal.hide()
                try:
                    self.state.delete_plugin_bundle(plugin_id)
                    state_committed = True
                except BaseException:
                    removal.restore()
                    raise
                removal.finalize()
            except BaseException:
                if (
                    not state_committed
                    and quiesce is not None
                    and not removal.displaced
                ):
                    self._restore_quiesce(quiesce)
                raise
            return {"removed": True, "plugin_id": plugin_id}

    def sync_all(self) -> None:
        for row in self.state.list_plugins():
            self.sync_plugin(str(row["id"]))

    def sync_plugin(self, plugin_id: str) -> None:
        with extension_lock(self.root, ".plugin-manager.lock"):
            row = self.state.get_plugin(plugin_id)
            plugin_dir = _installed_plugin_dir(self.root, row)
            self._validate_installed_source(
                row,
                plugin_dir,
                require_source_digest=False,
            )
            try:
                skills, mcp_servers = self._desired_extension_rows(
                    row,
                    tree_root=plugin_dir,
                    materialize_skills=False,
                    refresh_launch_vetting=True,
                )
            except (ExtensionTransactionError, OSError, PluginError):
                self._rebuild_generated_extensions(row, plugin_dir)
                return
            self.state.replace_plugin_bundle(row, skills=skills, mcp_servers=mcp_servers)

    def write_audit_memory(self, memory: Any, *, action: str, plugin: dict[str, Any]) -> str:
        payload = {
            "action": action,
            "plugin_id": plugin["id"],
            "name": plugin["name"],
            "source_url": plugin["source_url"],
            "source_ref": plugin.get("source_ref"),
            "commit_sha": plugin["commit_sha"],
            "enabled": plugin["enabled"],
            "capabilities": plugin.get("capabilities", []),
            "risk_report": plugin.get("risk_report", {}),
        }
        record = MemoryRecord(
            layer=MemoryLayer.EPISODIC,
            kind=MemoryKind.EVENT,
            title=f"Plugin {action}: {plugin['id']}",
            content=json.dumps(payload, indent=2, sort_keys=True),
            confidence=0.85,
            importance=0.45,
            tags={"plugin_id": str(plugin["id"]), "source": "plugin_manager"},
            metadata={"plugin_id": plugin["id"], "action": action, "validation_status": "audit_only"},
            evidence=[EvidenceRef(source="plugin_registry", locator=str(plugin["id"]))],
        )
        record_id = memory.put(record)
        memory.seal_all()
        return str(record_id)

    def _desired_extension_rows(
        self,
        row: dict[str, Any],
        *,
        tree_root: Path,
        materialize_skills: bool,
        refresh_launch_vetting: bool,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        manifest = dict(row["manifest"])
        enabled = bool(row["enabled"])
        skill_definitions = [
            dict(item) for item in manifest.get("skills", []) if isinstance(item, dict)
        ]
        server_definitions = [
            dict(item)
            for item in manifest.get("mcp_servers", [])
            if isinstance(item, dict)
        ]
        skill_rows: list[dict[str, Any]] = []
        server_rows: list[dict[str, Any]] = []

        for skill in skill_definitions:
            skill_manifest, skill_path = self._prepare_skill(
                row,
                skill,
                tree_root=tree_root,
                materialize=materialize_skills,
            )
            skill_rows.append(
                {
                    "id": skill["namespaced_id"],
                    "name": skill_manifest.get("name", skill["namespaced_id"]),
                    "description": skill_manifest.get("description", ""),
                    "path": str(skill_path),
                    "manifest": skill_manifest,
                    "enabled": enabled and bool(skill.get("enabled", True)),
                }
            )

        for server in server_definitions:
            payload = dict(server["config"])
            payload["id"] = server["namespaced_id"]
            payload["name"] = payload.get("name") or f"{row['name']} {server['id']}"
            payload["enabled"] = enabled and bool(payload.get("enabled", True))
            payload.setdefault("risk_policy", "approval_by_default")
            payload.setdefault("tools", [])
            payload = self._materialize_mcp_launch(
                row,
                payload,
                tree_root=tree_root,
            )
            if refresh_launch_vetting:
                from .mcp_manager import refresh_stdio_launch_vetting

                try:
                    current_server = self.state.get_mcp_server(str(payload["id"]))
                except KeyError:
                    current_server = None
                payload = refresh_stdio_launch_vetting(
                    payload,
                    current_row=current_server,
                )
            server_rows.append(payload)
        return skill_rows, server_rows

    def _materialize_mcp_launch(
        self,
        row: dict[str, Any],
        payload: dict[str, Any],
        *,
        tree_root: Path,
    ) -> dict[str, Any]:
        """Bind plugin script launchers to the installed, reviewed source tree."""

        materialized = dict(payload)
        vetting = dict(materialized.get("vetting", {}) or {})
        relative = vetting.get("plugin_artifact_relative_path")
        if not isinstance(relative, str) or not relative:
            return materialized
        staged_source_root = tree_root / PLUGIN_SOURCE_DIR
        staged_artifact = _safe_child_path(staged_source_root, relative, must_exist=True)
        read_regular_file(staged_artifact)
        live_source_root = Path(str(row["install_path"])) / PLUGIN_SOURCE_DIR
        live_artifact = live_source_root / relative
        args = [str(item) for item in materialized.get("args", [])]
        command_name = Path(str(materialized.get("command") or "")).name.lower()
        index = 1 if command_name == "deno" else 0
        if len(args) <= index:
            raise PluginError("Plugin MCP launch artifact argument is missing.")
        args[index] = str(live_artifact)
        materialized["args"] = args
        vetting["plugin_artifact_root"] = str(live_source_root.resolve())
        materialized["vetting"] = vetting
        return materialized

    def _prepare_skill(
        self,
        row: dict[str, Any],
        skill: dict[str, Any],
        *,
        tree_root: Path,
        materialize: bool,
    ) -> tuple[dict[str, Any], Path]:
        tree_skill_path = _safe_child_path(
            tree_root,
            f"{PLUGIN_GENERATED_DIR}/skills/{skill['id']}",
            must_exist=False,
        )
        live_skill_path = (
            Path(str(row["install_path"]))
            / PLUGIN_GENERATED_DIR
            / "skills"
            / str(skill["id"])
        )
        manifest = dict(skill["manifest"])
        manifest["id"] = skill["namespaced_id"]
        manifest.setdefault("name", skill.get("name") or skill["namespaced_id"])
        manifest.setdefault(
            "description",
            skill.get("description") or row.get("description") or "Plugin skill.",
        )
        manifest.setdefault("risk", row["manifest"].get("risk", DEFAULT_PLUGIN_RISK))
        manifest.setdefault("runtime", {"type": "instruction"})
        manifest.setdefault("capabilities", ["plugin", "skill"])
        manifest["provenance"] = {
            "plugin_id": row["id"],
            "source_url": row["source_url"],
            "commit_sha": row["commit_sha"],
            "format": row["format"],
        }
        instructions = str(skill["instructions"])
        manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
        instruction_bytes = instructions.encode()
        if materialize:
            tree_skill_path.mkdir(parents=True, exist_ok=False, mode=0o700)
            write_regular_file(tree_skill_path / "skill.json", manifest_bytes)
            write_regular_file(tree_skill_path / "SKILL.md", instruction_bytes)
        if read_regular_file(tree_skill_path / "skill.json") != manifest_bytes:
            raise PluginError(f"Materialized plugin skill changed: {skill['id']}")
        if read_regular_file(tree_skill_path / "SKILL.md") != instruction_bytes:
            raise PluginError(f"Materialized plugin instructions changed: {skill['id']}")
        persisted_manifest = json.loads(read_regular_text(tree_skill_path / "skill.json"))
        validation = validate_skill_manifest(persisted_manifest)
        if validation["errors"]:
            raise PluginError(
                f"Materialized plugin skill is invalid: {skill['id']}: "
                + ", ".join(validation["errors"])
            )
        return manifest, live_skill_path

    def _validate_installed_source(
        self,
        row: dict[str, Any],
        plugin_dir: Path,
        *,
        require_source_digest: bool = True,
    ) -> None:
        expected_digest = dict(row.get("risk_report", {})).get("tree_sha256")
        if not expected_digest:
            # Compatibility for registry rows created before source-tree
            # attestation was introduced. Their declarative generated files are
            # still rebuilt through the same staged swap below.
            return
        source_dir = plugin_dir / PLUGIN_SOURCE_DIR
        manifest = load_plugin_manifest(source_dir)
        if manifest.id != str(row["id"]):
            raise PluginError("Installed plugin manifest id does not match its state row.")
        if manifest.to_state_payload() != dict(row["manifest"]):
            raise PluginError("Installed plugin manifest differs from its reviewed state.")
        actual_digest = _tree_sha256(source_dir)
        if require_source_digest and actual_digest != expected_digest:
            raise PluginError("Installed plugin source integrity check failed.")

    def _rebuild_generated_extensions(
        self,
        row: dict[str, Any],
        plugin_dir: Path,
    ) -> None:
        stage = create_sibling_stage(self.root, prefix=str(row["id"]))
        try:
            copy_regular_tree(plugin_dir, stage)
            generated = stage / PLUGIN_GENERATED_DIR
            if path_exists(generated):
                remove_tree_verified(generated)
            self._desired_extension_rows(
                row,
                tree_root=stage,
                materialize_skills=True,
                refresh_launch_vetting=False,
            )
            fsync_tree(stage)
            swap = DirectorySwap(live=plugin_dir, stage=stage)
            quiesce = self.state.quiesce_plugin_bundle(str(row["id"]))
            state_committed = False
            try:
                swap.publish()
                try:
                    skills, mcp_servers = self._desired_extension_rows(
                        row,
                        tree_root=plugin_dir,
                        materialize_skills=False,
                        refresh_launch_vetting=True,
                    )
                    self.state.replace_plugin_bundle(
                        row,
                        skills=skills,
                        mcp_servers=mcp_servers,
                    )
                    state_committed = True
                except BaseException:
                    swap.restore()
                    raise
                swap.finalize()
            except BaseException:
                if (
                    not state_committed
                    and quiesce is not None
                    and not swap.displaced
                    and not swap.published
                ):
                    self._restore_quiesce(quiesce)
                raise
        finally:
            if path_exists(stage):
                remove_tree_verified(stage)

    def _restore_quiesce(self, token: dict[str, Any]) -> None:
        try:
            self.state.restore_quiesced_plugin_bundle(token)
        except BaseException as exc:
            raise ExtensionCleanupIncompleteError(
                "Plugin rollback completed with unresolved fail-closed state rows."
            ) from exc


def _plugin_state_row(
    manifest: PluginManifest,
    *,
    source: str,
    ref: str | None,
    commit_sha: str,
    plugin_dir: Path,
    risk_report: dict[str, Any],
    enabled: bool,
) -> dict[str, Any]:
    return {
        "id": manifest.id,
        "name": manifest.name,
        "description": manifest.description,
        "source_url": source,
        "source_ref": ref,
        "commit_sha": commit_sha,
        "install_path": str(plugin_dir),
        "manifest": manifest.to_state_payload(),
        "capabilities": _capabilities(manifest),
        "enabled": enabled,
        "risk_report": risk_report,
        "install_status": "installed",
        "format": manifest.format,
    }


def _installed_plugin_dir(root: Path, row: dict[str, Any]) -> Path:
    expected = _safe_plugin_dir(root, str(row["id"]))
    configured = Path(str(row["install_path"])).expanduser()
    if not configured.is_absolute():
        configured = configured.absolute()
    if configured != expected:
        raise PluginError("Plugin install path does not match its registry id.")
    if path_exists(expected):
        ensure_real_directory(expected)
    return expected


def parse_github_plugin_source(raw_source: str) -> GitHubPluginSource:
    source = raw_source.strip()
    if not source:
        raise PluginError("Plugin source is required.")
    if source.startswith("git@") or "://" in source:
        parsed = urlparse(source)
        if parsed.scheme != "https":
            raise PluginError("Only https://github.com/owner/repo plugin URLs are supported.")
        if parsed.hostname != "github.com":
            raise PluginError("Only github.com plugin URLs are supported.")
        if parsed.username or parsed.password:
            raise PluginError("Credential-bearing GitHub URLs are not allowed.")
        if parsed.query or parsed.fragment:
            raise PluginError("GitHub plugin URLs cannot include query strings or fragments.")
        parts = [part for part in parsed.path.split("/") if part]
    else:
        parts = source.split("/")
    if len(parts) != 2:
        raise PluginError("Plugin source must be owner/repo or https://github.com/owner/repo.")
    owner, repo = parts
    repo = repo.removesuffix(".git")
    if not _GITHUB_NAME_RE.match(owner) or not _GITHUB_NAME_RE.match(repo):
        raise PluginError("GitHub owner and repo names contain unsupported characters.")
    return GitHubPluginSource(owner=owner, repo=repo)


def load_plugin_manifest(repo_root: Path) -> PluginManifest:
    kestrel_manifest = repo_root / "kestrel.plugin.json"
    if kestrel_manifest.exists():
        raw = json.loads(read_regular_text(kestrel_manifest))
        if not isinstance(raw, dict):
            raise PluginError("kestrel.plugin.json must contain a JSON object.")
        if redact_secrets(raw) != raw:
            raise PluginError(
                "Plugin manifests must use secret references or environment names, "
                "not raw secret material."
            )
        return _normalize_kestrel_manifest(raw, repo_root)

    for filename in ("plugin.yaml", "plugin.yml"):
        hermes_manifest = repo_root / filename
        if hermes_manifest.exists():
            raw = _load_yaml_manifest(hermes_manifest)
            if redact_secrets(raw) != raw:
                raise PluginError(
                    "Plugin manifests must use secret references or environment names, "
                    "not raw secret material."
                )
            return _normalize_hermes_manifest(raw, repo_root)
    raise PluginError("Plugin repo must include kestrel.plugin.json or plugin.yaml.")


def _normalize_kestrel_manifest(raw: dict[str, Any], repo_root: Path) -> PluginManifest:
    plugin_id = _validated_plugin_id(str(raw.get("id", "")).strip())
    name = str(raw.get("name") or plugin_id).strip()
    description = str(raw.get("description") or "").strip()
    if not description:
        raise PluginError("Plugin manifest description is required.")
    risk = _risk(str(raw.get("risk", DEFAULT_PLUGIN_RISK)))
    permissions = _string_tuple(raw.get("permissions", []), "permissions")
    requires_env = _env_tuple(raw.get("requires_env", []))
    dependencies = _dependency_map(raw.get("dependencies", {}))
    dependency_lock = _dependency_lock(raw.get("dependency_lock", {}))
    compatibility = _compatibility(raw.get("compatibility", {}))
    signature = _manifest_signature(raw.get("signature"))
    isolation = _isolation(raw.get("isolation", {}))
    warnings: list[str] = []
    skills = tuple(_normalize_skill(plugin_id, item, repo_root, risk) for item in _dict_list(raw.get("skills", []), "skills"))
    mcp_servers = tuple(
        _normalize_mcp_server(plugin_id, item, repo_root)
        for item in _dict_list(raw.get("mcp_servers", []), "mcp_servers")
    )
    if not skills and not mcp_servers:
        warnings.append("plugin_has_no_declarative_capabilities")
    return PluginManifest(
        id=plugin_id,
        name=name,
        version=str(raw.get("version", "0.0.0")),
        description=description,
        format="kestrel",
        risk=risk,
        permissions=permissions,
        requires_env=requires_env,
        dependencies=dependencies,
        dependency_lock=dependency_lock,
        compatibility=compatibility,
        signature=signature,
        isolation=isolation,
        skills=skills,
        mcp_servers=mcp_servers,
        warnings=tuple(warnings),
        unsupported_features=(),
        raw=raw,
    )


def _normalize_hermes_manifest(raw: dict[str, Any], repo_root: Path) -> PluginManifest:
    raw_name = str(raw.get("name") or raw.get("id") or repo_root.name)
    plugin_id = _validated_plugin_id(_slugify(str(raw.get("id") or raw_name)))
    description = str(raw.get("description") or f"Hermes-compatible plugin {raw_name}.").strip()
    risk = _risk(str(raw.get("risk", DEFAULT_PLUGIN_RISK)))
    warnings = ["hermes_compatibility_limited"]
    unsupported: list[str] = []
    if (repo_root / "__init__.py").exists() or any(key in raw for key in ("hooks", "entrypoint", "module")):
        unsupported.append("python_hooks_ignored")
    skills_raw = raw.get("skills", [])
    if isinstance(skills_raw, dict):
        skills_raw = list(skills_raw.values())
    skills = tuple(_normalize_skill(plugin_id, item, repo_root, risk) for item in _dict_list(skills_raw, "skills"))
    mcp_raw = raw.get("mcp_servers", raw.get("mcp", []))
    mcp_servers = tuple(
        _normalize_mcp_server(plugin_id, item, repo_root)
        for item in _dict_list(mcp_raw, "mcp_servers")
    )
    if raw.get("tools") and not mcp_servers:
        unsupported.append("python_tool_registration_ignored")
    if not skills and not mcp_servers:
        warnings.append("plugin_has_no_declarative_capabilities")
    return PluginManifest(
        id=plugin_id,
        name=raw_name.strip() or plugin_id,
        version=str(raw.get("version", "0.0.0")),
        description=description,
        format="hermes",
        risk=risk,
        permissions=_string_tuple(raw.get("permissions", []), "permissions"),
        requires_env=_env_tuple(raw.get("requires_env", [])),
        dependencies=_dependency_map(raw.get("dependencies", {})),
        dependency_lock=_dependency_lock(raw.get("dependency_lock", {})),
        compatibility=_compatibility(raw.get("compatibility", {})),
        signature=_manifest_signature(raw.get("signature")),
        isolation=_isolation(raw.get("isolation", {})),
        skills=skills,
        mcp_servers=mcp_servers,
        warnings=tuple(warnings),
        unsupported_features=tuple(sorted(set(unsupported))),
        raw=raw,
    )


def _normalize_skill(plugin_id: str, raw: dict[str, Any], repo_root: Path, plugin_risk: str) -> dict[str, Any]:
    skill_id = _validated_component_id(str(raw.get("id", "")).strip())
    namespaced_id = f"{PLUGIN_NAMESPACE}.{plugin_id}.{skill_id}"
    source_manifest: dict[str, Any] = {}
    instructions = str(raw.get("instructions", ""))
    if raw.get("path"):
        skill_path = _safe_child_path(repo_root, str(raw["path"]))
        if skill_path.is_dir():
            manifest_path = skill_path / "skill.json"
            instructions_path = skill_path / "SKILL.md"
            if manifest_path.exists():
                loaded = json.loads(read_regular_text(manifest_path))
                if isinstance(loaded, dict):
                    source_manifest.update(loaded)
            if instructions_path.exists():
                instructions = read_regular_text(instructions_path)
        elif skill_path.is_file():
            instructions = read_regular_text(skill_path)
    if raw.get("instructions_path"):
        instructions = read_regular_text(
            _safe_child_path(repo_root, str(raw["instructions_path"]))
        )
    source_manifest.update(dict(raw.get("manifest", {})) if isinstance(raw.get("manifest", {}), dict) else {})
    source_manifest["id"] = namespaced_id
    source_manifest.setdefault("name", raw.get("name") or source_manifest.get("name") or skill_id)
    source_manifest.setdefault("description", raw.get("description") or source_manifest.get("description") or "")
    source_manifest.setdefault("risk", _risk(str(raw.get("risk", source_manifest.get("risk", plugin_risk)))))
    source_manifest.setdefault("runtime", raw.get("runtime") if isinstance(raw.get("runtime"), dict) else {"type": "instruction"})
    if raw.get("parameters") and isinstance(raw["parameters"], dict):
        source_manifest["parameters"] = raw["parameters"]
    if raw.get("capabilities") and isinstance(raw["capabilities"], list):
        source_manifest["capabilities"] = raw["capabilities"]
    if not instructions.strip():
        raise PluginError(f"Plugin skill {skill_id} is missing instructions.")
    validation = validate_skill_manifest(source_manifest)
    if validation["errors"]:
        raise PluginError(f"Plugin skill {skill_id} is invalid: {', '.join(validation['errors'])}")
    return {
        "id": skill_id,
        "namespaced_id": namespaced_id,
        "name": str(source_manifest.get("name", skill_id)),
        "description": str(source_manifest.get("description", "")),
        "enabled": bool(raw.get("enabled", True)),
        "manifest": source_manifest,
        "instructions": instructions,
    }


def _normalize_mcp_server(
    plugin_id: str,
    raw: dict[str, Any],
    repo_root: Path,
) -> dict[str, Any]:
    server_id = _validated_component_id(str(raw.get("id", "")).strip())
    namespaced_id = f"{PLUGIN_NAMESPACE}.{plugin_id}.{server_id}"
    config = dict(raw)
    config.pop("id", None)
    config["id"] = namespaced_id
    config.setdefault("name", server_id)
    config.setdefault("transport", "stdio")
    config.setdefault("args", [])
    config.setdefault("env", {})
    config.setdefault("enabled", True)
    config["risk_policy"] = "approval_by_default"
    if str(config.get("transport", "stdio")) == "stdio":
        command = str(config.get("command") or "").strip()
        args = [str(item) for item in config.get("args", [])]
        artifact = _validate_plugin_stdio_command(command, args, repo_root=repo_root)
        from .mcp_manager import _stdio_command_hash

        vetting = dict(config.get("vetting", {}) or {})
        for approval_field in (
            "connect_approved",
            "connect_approved_at",
            "connect_approved_command_hash",
            "connect_approved_launch_digest",
            "stdio_launch_snapshot",
        ):
            vetting.pop(approval_field, None)
        vetting["stdio_command_hash"] = _stdio_command_hash(command, args)
        vetting["connect_requires_approval"] = True
        vetting["plugin_source"] = plugin_id
        if artifact is not None:
            vetting["plugin_artifact_relative_path"] = artifact
        config["vetting"] = vetting
    tools = _dict_list(config.get("tools", []), "mcp_servers.tools")
    config["tools"] = [_normalize_mcp_tool(namespaced_id, tool, str(config["risk_policy"])) for tool in tools]
    return {"id": server_id, "namespaced_id": namespaced_id, "config": config}


def _normalize_mcp_tool(server_id: str, raw: dict[str, Any], risk_policy: str) -> dict[str, Any]:
    remote_name = str(raw.get("remote_name") or raw.get("name") or "").strip()
    if not remote_name:
        raise PluginError("MCP tool names are required.")
    risk = _risk(str(raw.get("risk", "medium")))
    requires_approval = bool(raw.get("requires_approval", risk in {"medium", "high"}))
    if risk_policy != "trust_manifest":
        if risk == "low":
            risk = "medium"
        requires_approval = True
    return {
        "name": f"mcp.{server_id}.{remote_name}",
        "remote_name": remote_name,
        "description": str(raw.get("description", "")),
        "parameters": dict(raw.get("parameters") or raw.get("inputSchema") or {"type": "object", "properties": {}}),
        "risk": risk,
        "requires_approval": requires_approval,
        "capabilities": list(raw.get("capabilities", ["mcp"])) if isinstance(raw.get("capabilities", ["mcp"]), list) else ["mcp"],
        "produces_validation": bool(raw.get("produces_validation", False)),
    }


def _validate_plugin_stdio_command(
    command: str,
    args: list[str],
    *,
    repo_root: Path,
) -> str | None:
    if not command:
        return None
    command_name = Path(command).name.lower()
    allowed = {"npx", "uvx", "python", "python3", "node", "bunx", "deno"}
    if command_name not in allowed:
        raise PluginError(f"Plugin MCP command is not allowed: {command_name}")
    if command_name in {"python", "python3"}:
        if not args or args[0].startswith("-") or not args[0].lower().endswith(".py"):
            raise PluginError(
                "Plugin MCP `python -m` launchers are mutable and are not allowed; "
                "configure a plugin-relative `.py` script instead."
            )
        return _validated_plugin_artifact(repo_root, args[0], suffixes=(".py",))
    if command_name in {"npx", "uvx", "bunx"}:
        raise PluginError(
            "Plugin MCP package runners are disabled because registry coordinates do not "
            "prove the bytes that will execute; include a reviewed plugin-relative script."
        )
    if command_name == "node":
        if (
            not args
            or not args[0].lower().endswith((".js", ".cjs", ".mjs"))
            or any(_has_shell_metacharacters(part) for part in args)
        ):
            raise PluginError("Plugin MCP node args contain unsupported shell metacharacters.")
        return _validated_plugin_artifact(
            repo_root,
            args[0],
            suffixes=(".js", ".cjs", ".mjs"),
        )
    if command_name == "deno":
        if (
            len(args) < 2
            or args[0] != "run"
            or any(_has_shell_metacharacters(part) for part in args)
        ):
            raise PluginError("Plugin MCP deno args contain unsupported shell metacharacters.")
        return _validated_plugin_artifact(
            repo_root,
            args[1],
            suffixes=(".js", ".mjs", ".ts", ".mts"),
        )
    return None


def _validated_plugin_artifact(
    repo_root: Path,
    raw_path: str,
    *,
    suffixes: tuple[str, ...],
) -> str:
    candidate = Path(raw_path)
    if candidate.is_absolute() or candidate.suffix.lower() not in suffixes:
        raise PluginError("Plugin MCP launch artifacts must use a plugin-relative script path.")
    artifact = _safe_child_path(repo_root, raw_path, must_exist=True)
    try:
        read_regular_file(artifact)
    except (ExtensionTransactionError, OSError, UnicodeError) as exc:
        raise PluginError("Plugin MCP launch artifact must be a regular file.") from exc
    if not artifact.is_file():
        raise PluginError("Plugin MCP launch artifact must be a regular file.")
    return artifact.relative_to(repo_root.resolve()).as_posix()


def _has_shell_metacharacters(value: str) -> bool:
    return any(char in value for char in ";&|`$><")


def _load_yaml_manifest(path: Path) -> dict[str, Any]:
    text = read_regular_text(path)
    try:
        yaml = import_module("yaml")
    except Exception:
        parsed = _parse_simple_yaml(text)
    else:
        parsed = yaml.safe_load(text)
    if not isinstance(parsed, dict):
        raise PluginError("plugin.yaml must contain a mapping.")
    return parsed


def _parse_simple_yaml(text: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    current_key: str | None = None
    current_list: list[Any] | None = None
    current_item: dict[str, Any] | None = None
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if not line.startswith(" "):
            current_item = None
            key, sep, value = line.partition(":")
            if not sep:
                raise PluginError(f"Unsupported plugin.yaml line: {raw_line}")
            current_key = key.strip()
            if value.strip():
                result[current_key] = _parse_yaml_scalar(value.strip())
                current_list = None
            else:
                current_list = []
                result[current_key] = current_list
            continue
        if current_key is None or current_list is None:
            raise PluginError(f"Unsupported plugin.yaml indentation: {raw_line}")
        stripped = line.strip()
        if stripped.startswith("- "):
            item_text = stripped[2:].strip()
            if ":" in item_text and not item_text.startswith(("'", '"')):
                key, _, value = item_text.partition(":")
                current_item = {key.strip(): _parse_yaml_scalar(value.strip())}
                current_list.append(current_item)
            else:
                current_item = None
                current_list.append(_parse_yaml_scalar(item_text))
            continue
        if current_item is not None and ":" in stripped:
            key, _, value = stripped.partition(":")
            current_item[key.strip()] = _parse_yaml_scalar(value.strip())
            continue
        raise PluginError(f"Unsupported plugin.yaml line: {raw_line}")
    return result


def _parse_yaml_scalar(value: str) -> Any:
    if value in {"true", "True"}:
        return True
    if value in {"false", "False"}:
        return False
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        return [] if not inner else [_parse_yaml_scalar(part.strip()) for part in inner.split(",")]
    return value.strip("'\"")


def _risk_report(
    manifest: PluginManifest,
    *,
    source: str,
    commit_sha: str,
    source_dir: Path,
    prior_manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tree_sha256 = _tree_sha256(source_dir)
    dependency_review = _dependency_review(
        manifest.dependencies,
        manifest.dependency_lock,
    )
    isolation_review = _isolation_review(manifest.isolation)
    compatibility_review = _compatibility_review(manifest.compatibility)
    provenance_review = _provenance_review(
        manifest,
        commit_sha=commit_sha,
        tree_sha256=tree_sha256,
    )
    authority_delta = _authority_delta(
        manifest,
        prior_manifest=prior_manifest,
    )
    enable_blockers = _enable_blockers(
        dependency_review,
        isolation_review,
        compatibility_review,
        provenance_review,
    )
    install_receipt = {
        "schema": "kestrel.plugin.install-receipt.v1",
        "source_url": source,
        "commit_sha": commit_sha,
        "tree_sha256": tree_sha256,
        "manifest_sha256": provenance_review["manifest_sha256"],
        "dependency_lock_sha256": dependency_review["lock_digest"],
        "compatibility": compatibility_review,
        "provenance": provenance_review,
        "authority_digest": authority_delta["authority_digest"],
        "source_reproducible": bool(
            provenance_review["verified"]
            and compatibility_review["compatible"]
            and dependency_review["lock_complete"]
        ),
        "runtime_reproducible": bool(
            provenance_review["verified"]
            and compatibility_review["compatible"]
            and dependency_review["lock_complete"]
            and (
                not dependency_review["requires_install"]
                or dependency_review["managed"]
            )
        ),
    }
    install_receipt["reproducible"] = install_receipt["runtime_reproducible"]
    return {
        "risk": manifest.risk,
        "permissions": list(manifest.permissions),
        "requires_env": list(manifest.requires_env),
        "dependency_review": dependency_review,
        "isolation_review": isolation_review,
        "compatibility_review": compatibility_review,
        "provenance_review": provenance_review,
        "authority_delta": authority_delta,
        "install_receipt": install_receipt,
        "enable_blockers": enable_blockers,
        "warnings": list(manifest.warnings),
        "unsupported_features": list(manifest.unsupported_features),
        "source_url": source,
        "commit_sha": commit_sha,
        "approval_policy": "approval_by_default",
        "tree_sha256": tree_sha256,
    }


def _review_payload(
    manifest: PluginManifest,
    *,
    source: str,
    ref: str | None,
    commit_sha: str,
    risk_report: dict[str, Any],
) -> dict[str, Any]:
    return {
        "source_url": source,
        "source_ref": ref,
        "commit_sha": commit_sha,
        "manifest": manifest.to_state_payload(),
        "capabilities": _capabilities(manifest),
        "risk_report": risk_report,
        "dependency_review": risk_report["dependency_review"],
        "isolation_review": risk_report["isolation_review"],
        "compatibility_review": risk_report["compatibility_review"],
        "provenance_review": risk_report["provenance_review"],
        "authority_delta": risk_report["authority_delta"],
        "install_receipt": risk_report["install_receipt"],
        "enable_blockers": risk_report["enable_blockers"],
        "warnings": list(manifest.warnings),
        "unsupported_features": list(manifest.unsupported_features),
    }


def _ensure_plugin_enable_allowed(manifest: PluginManifest, risk_report: dict[str, Any], *, enable: bool) -> None:
    if enable and risk_report.get("enable_blockers"):
        blockers = ", ".join(str(item) for item in risk_report["enable_blockers"])
        raise PluginError(f"Plugin enable blocked: {manifest.id}: {blockers}")


def _ensure_plugin_enable_allowed_from_row(row: dict[str, Any]) -> None:
    risk_report = dict(row.get("risk_report", {}) or {})
    blockers = [str(item) for item in risk_report.get("enable_blockers", [])]
    if blockers:
        raise PluginError(f"Plugin enable blocked: {row['id']}: {', '.join(blockers)}")


def _provenance_review(
    manifest: PluginManifest,
    *,
    commit_sha: str,
    tree_sha256: str | None,
) -> dict[str, Any]:
    message = _canonical_manifest_bytes(manifest.raw)
    manifest_sha256 = hashlib.sha256(message).hexdigest()
    signature = manifest.signature
    if signature is None:
        source_pinned = bool(
            re.fullmatch(r"[0-9a-fA-F]{40}", commit_sha)
            and tree_sha256
            and re.fullmatch(r"[0-9a-f]{64}", tree_sha256)
        )
        return {
            "status": "source_pinned" if source_pinned else "unverified",
            "verified": source_pinned,
            "signature_verified": False,
            "algorithm": None,
            "key_id": None,
            "manifest_sha256": manifest_sha256,
            "commit_pinned": source_pinned,
            "tree_attested": bool(tree_sha256),
        }
    try:
        public_key_bytes = b64decode(signature["public_key"], validate=True)
        signature_bytes = b64decode(signature["signature"], validate=True)
        if len(public_key_bytes) != 32 or len(signature_bytes) != 64:
            raise ValueError("invalid Ed25519 key or signature length")
    except (binascii.Error, KeyError, ValueError) as exc:
        raise PluginError("Plugin manifest signature verification failed.") from exc
    try:
        ed25519 = import_module(
            "cryptography.hazmat.primitives.asymmetric.ed25519"
        )
    except ImportError as exc:
        raise PluginError(
            "Plugin manifest signature verification requires cryptography."
        ) from exc
    try:
        ed25519.Ed25519PublicKey.from_public_bytes(public_key_bytes).verify(
            signature_bytes,
            message,
        )
    except Exception as exc:
        raise PluginError("Plugin manifest signature verification failed.") from exc
    return {
        "status": "verified",
        "verified": True,
        "signature_verified": True,
        "algorithm": "ed25519",
        "key_id": signature.get("key_id"),
        "manifest_sha256": manifest_sha256,
        "commit_pinned": bool(re.fullmatch(r"[0-9a-fA-F]{40}", commit_sha)),
        "tree_attested": bool(tree_sha256),
    }


def _canonical_manifest_bytes(raw: dict[str, Any]) -> bytes:
    unsigned = dict(raw)
    unsigned.pop("signature", None)
    return json.dumps(
        unsigned,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _compatibility_review(compatibility: dict[str, str]) -> dict[str, Any]:
    try:
        current_version = version("nested-memvid-agent")
    except PackageNotFoundError:
        current_version = "0.5.1"
    kestrel_spec = compatibility.get("kestrel", "*")
    compatible = _version_matches(current_version, kestrel_spec)
    return {
        "current_kestrel": current_version,
        "required_kestrel": kestrel_spec,
        "compatible": compatible,
        "status": "compatible" if compatible else "incompatible",
    }


def _authority_delta(
    manifest: PluginManifest,
    *,
    prior_manifest: dict[str, Any] | None,
) -> dict[str, Any]:
    current = _authority_set(manifest.to_state_payload())
    prior = _authority_set(prior_manifest) if prior_manifest is not None else set()
    added = sorted(current - prior)
    removed = sorted(prior - current)
    authority_payload = json.dumps(
        sorted(current),
        separators=(",", ":"),
    ).encode("utf-8")
    initial_install = prior_manifest is None
    return {
        "initial_install": initial_install,
        "added": added,
        "removed": removed,
        "unchanged_count": len(current & prior),
        "expands_authority": bool(added) and not initial_install,
        "authority_digest": hashlib.sha256(authority_payload).hexdigest(),
    }


def _authority_set(manifest: dict[str, Any] | None) -> set[str]:
    if manifest is None:
        return set()
    authority = {"plugin"}
    for permission in manifest.get("permissions", []):
        authority.add(f"permission:{permission}")
    for env_name in manifest.get("requires_env", []):
        authority.add(f"secret-env:{env_name}")
    dependencies = manifest.get("dependencies", {})
    if isinstance(dependencies, dict):
        for kind in PLUGIN_DEPENDENCY_KINDS:
            for dependency in dependencies.get(kind, []):
                authority.add(f"dependency:{kind}:{dependency}")
    isolation = manifest.get("isolation", {})
    if isinstance(isolation, dict):
        authority.add(
            "isolation:"
            f"{isolation.get('mode', 'shared')}:"
            f"{bool(isolation.get('required', False))}"
        )
    for raw_skill in manifest.get("skills", []):
        if not isinstance(raw_skill, dict):
            continue
        skill_id = str(raw_skill.get("namespaced_id") or raw_skill.get("id") or "")
        if skill_id:
            authority.add(f"skill:{skill_id}")
        skill_manifest = raw_skill.get("manifest", {})
        if isinstance(skill_manifest, dict):
            for capability in skill_manifest.get("capabilities", []):
                authority.add(f"skill-capability:{skill_id}:{capability}")
    for raw_server in manifest.get("mcp_servers", []):
        if not isinstance(raw_server, dict):
            continue
        server_id = str(raw_server.get("namespaced_id") or raw_server.get("id") or "")
        if server_id:
            authority.add(f"mcp:{server_id}")
        config = raw_server.get("config", {})
        if not isinstance(config, dict):
            continue
        command = str(config.get("command") or "")
        if command:
            authority.add(f"mcp-launch:{server_id}:{Path(command).name}")
        for tool in config.get("tools", []):
            if not isinstance(tool, dict):
                continue
            tool_name = str(tool.get("remote_name") or tool.get("name") or "")
            authority.add(f"mcp-tool:{server_id}:{tool_name}")
            for capability in tool.get("capabilities", []):
                authority.add(
                    f"mcp-tool-capability:{server_id}:{tool_name}:{capability}"
                )
    return authority


def _dependency_review(
    dependencies: dict[str, list[str]],
    dependency_lock: dict[str, list[dict[str, str]]],
) -> dict[str, Any]:
    declared = {kind: list(dependencies.get(kind, [])) for kind in PLUGIN_DEPENDENCY_KINDS}
    locked = {
        kind: [dict(item) for item in dependency_lock.get(kind, [])]
        for kind in PLUGIN_DEPENDENCY_KINDS
    }
    requires_install = any(declared[kind] for kind in PLUGIN_DEPENDENCY_KINDS)
    missing_from_lock: dict[str, list[str]] = {}
    extra_in_lock: dict[str, list[str]] = {}
    for kind in PLUGIN_DEPENDENCY_KINDS:
        declared_names = {_dependency_name(item, kind=kind) for item in declared[kind]}
        locked_names = {item["name"] for item in locked[kind]}
        missing = sorted(declared_names - locked_names)
        extra = sorted(locked_names - declared_names)
        if missing:
            missing_from_lock[kind] = missing
        if extra:
            extra_in_lock[kind] = extra
    lock_payload = json.dumps(
        locked,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    lock_complete = not missing_from_lock and not extra_in_lock
    return {
        "declared": declared,
        "locked": locked,
        "requires_install": requires_install,
        "managed": False,
        "lock_complete": lock_complete,
        "missing_from_lock": missing_from_lock,
        "extra_in_lock": extra_in_lock,
        "lock_digest": hashlib.sha256(lock_payload).hexdigest(),
        "status": "unmanaged" if requires_install else "none",
    }


def _isolation_review(isolation: dict[str, Any]) -> dict[str, Any]:
    mode = str(isolation.get("mode", "shared"))
    required = bool(isolation.get("required", False))
    return {"mode": mode, "required": required, "available": mode == "shared" or not required}


def _enable_blockers(
    dependency_review: dict[str, Any],
    isolation_review: dict[str, Any],
    compatibility_review: dict[str, Any],
    provenance_review: dict[str, Any],
) -> list[str]:
    blockers: list[str] = []
    if dependency_review["requires_install"] and not dependency_review["managed"]:
        blockers.append("plugin_dependencies_unmanaged")
    if isolation_review["required"] and not isolation_review["available"]:
        blockers.append("plugin_isolation_unavailable")
    if not compatibility_review["compatible"]:
        blockers.append("plugin_kestrel_version_incompatible")
    if not provenance_review["verified"]:
        blockers.append("plugin_source_unverified")
    return blockers


def _capabilities(manifest: PluginManifest) -> list[str]:
    capabilities = {"plugin"}
    if manifest.skills:
        capabilities.add("skill")
    if manifest.mcp_servers:
        capabilities.add("mcp")
    for permission in manifest.permissions:
        capabilities.add(f"permission:{permission}")
    return sorted(capabilities)


def _tree_sha256(root: Path) -> str | None:
    if not root.exists():
        return None
    fsync_tree(root)
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file() and ".git" not in item.parts):
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(read_regular_file(path))
    return digest.hexdigest()


def _safe_plugin_dir(root: Path, plugin_id: str) -> Path:
    _validated_plugin_id(plugin_id)
    return root.resolve() / plugin_id


def _safe_child_path(root: Path, relative: str, *, must_exist: bool = True) -> Path:
    if Path(relative).is_absolute():
        raise PluginError(f"Plugin path must be relative: {relative}")
    target = (root / relative).resolve()
    root_resolved = root.resolve()
    if target != root_resolved and root_resolved not in target.parents:
        raise PluginError(f"Plugin path escapes its root: {relative}")
    if must_exist and not target.exists():
        raise PluginError(f"Plugin path does not exist: {relative}")
    return target


def _validated_plugin_id(value: str) -> str:
    if not value or not _PLUGIN_ID_RE.match(value):
        raise PluginError("Plugin id must start with an alphanumeric character and contain only alphanumerics, '_' or '-'.")
    return value


def _validated_component_id(value: str) -> str:
    if not value or not _COMPONENT_ID_RE.match(value):
        raise PluginError("Plugin component ids must start with an alphanumeric character and contain only alphanumerics, '_' or '-'.")
    return value


def _normalize_ref(ref: str | None) -> str | None:
    if ref is None:
        return None
    value = ref.strip()
    if not value:
        return None
    if value.startswith("-") or not _REF_RE.match(value):
        raise PluginError("Git ref contains unsupported characters.")
    return value


def _risk(value: str) -> str:
    risk = value.strip().lower() or DEFAULT_PLUGIN_RISK
    if risk not in {"low", "medium", "high"}:
        raise PluginError("Plugin risk must be low, medium, or high.")
    return risk


def _string_tuple(value: object, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise PluginError(f"Plugin manifest {field} must be a list of strings.")
    return tuple(str(item) for item in value)


def _env_tuple(value: object) -> tuple[str, ...]:
    env = _string_tuple(value, "requires_env")
    bad = [item for item in env if not _ENV_NAME_RE.match(item)]
    if bad:
        raise PluginError(f"Invalid environment variable names: {', '.join(bad)}")
    return env


def _dependency_map(value: object) -> dict[str, list[str]]:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise PluginError("Plugin dependencies must be an object.")
    unknown = sorted(str(key) for key in value if str(key) not in PLUGIN_DEPENDENCY_KINDS)
    if unknown:
        raise PluginError(f"Unsupported plugin dependency kinds: {', '.join(unknown)}")
    dependencies: dict[str, list[str]] = {kind: [] for kind in PLUGIN_DEPENDENCY_KINDS}
    for kind in PLUGIN_DEPENDENCY_KINDS:
        raw_items = value.get(kind, [])
        if raw_items is None:
            raw_items = []
        if not isinstance(raw_items, list) or not all(isinstance(item, str) for item in raw_items):
            raise PluginError(f"Plugin dependencies.{kind} must be a list of strings.")
        dependencies[kind] = [item.strip() for item in raw_items if item.strip()]
    return dependencies


def _dependency_lock(value: object) -> dict[str, list[dict[str, str]]]:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise PluginError("Plugin dependency_lock must be an object.")
    unknown = sorted(
        str(key) for key in value if str(key) not in PLUGIN_DEPENDENCY_KINDS
    )
    if unknown:
        raise PluginError(
            f"Unsupported plugin dependency lock kinds: {', '.join(unknown)}"
        )
    normalized: dict[str, list[dict[str, str]]] = {
        kind: [] for kind in PLUGIN_DEPENDENCY_KINDS
    }
    for kind in PLUGIN_DEPENDENCY_KINDS:
        raw_items = value.get(kind, [])
        if raw_items is None:
            raw_items = []
        if not isinstance(raw_items, list):
            raise PluginError(f"Plugin dependency_lock.{kind} must be a list.")
        seen: set[str] = set()
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                raise PluginError(
                    f"Plugin dependency_lock.{kind} entries must be objects."
                )
            unknown_fields = sorted(
                str(key)
                for key in raw_item
                if str(key) not in {"name", "version", "sha256"}
            )
            if unknown_fields:
                raise PluginError(
                    f"Plugin dependency_lock.{kind} has unsupported fields: "
                    + ", ".join(unknown_fields)
                )
            name = str(raw_item.get("name") or "").strip()
            locked_version = str(raw_item.get("version") or "").strip()
            sha256 = str(raw_item.get("sha256") or "").strip().lower()
            if not re.fullmatch(r"[A-Za-z0-9@][A-Za-z0-9@._/+:-]{0,199}", name):
                raise PluginError(
                    f"Plugin dependency_lock.{kind} contains an invalid name."
                )
            if (
                not locked_version
                or len(locked_version) > 128
                or any(char.isspace() for char in locked_version)
            ):
                raise PluginError(
                    f"Plugin dependency_lock.{kind} contains an invalid version."
                )
            if not re.fullmatch(r"[0-9a-f]{64}", sha256):
                raise PluginError(
                    f"Plugin dependency_lock.{kind} sha256 must be 64 hex characters."
                )
            if name in seen:
                raise PluginError(
                    f"Plugin dependency_lock.{kind} contains duplicate name {name}."
                )
            seen.add(name)
            normalized[kind].append(
                {"name": name, "version": locked_version, "sha256": sha256}
            )
        normalized[kind].sort(key=lambda item: item["name"].casefold())
    return normalized


def _dependency_name(value: str, *, kind: str) -> str:
    stripped = value.strip()
    if kind == "node" and stripped.startswith("@") and "/" in stripped:
        package_end = stripped.find("@", stripped.find("/") + 1)
        return stripped if package_end < 0 else stripped[:package_end]
    name = re.split(r"\s|[<>=!~@]", stripped, maxsplit=1)[0].strip()
    if not name:
        raise PluginError(f"Plugin dependency declaration has no package name: {value}")
    return name


def _compatibility(value: object) -> dict[str, str]:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise PluginError("Plugin compatibility must be an object.")
    unknown = sorted(str(key) for key in value if str(key) != "kestrel")
    if unknown:
        raise PluginError(
            f"Unsupported plugin compatibility targets: {', '.join(unknown)}"
        )
    kestrel = str(value.get("kestrel", "*")).strip() or "*"
    if len(kestrel) > 128:
        raise PluginError("Plugin compatibility.kestrel is too long.")
    try:
        _version_matches("0.5.1", kestrel)
    except ValueError as exc:
        raise PluginError("Plugin compatibility.kestrel is invalid.") from exc
    return {"kestrel": kestrel}


def _manifest_signature(value: object) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise PluginError("Plugin signature must be an object.")
    allowed = {"schema", "algorithm", "key_id", "public_key", "signature"}
    unknown = sorted(str(key) for key in value if str(key) not in allowed)
    if unknown:
        raise PluginError(
            f"Plugin signature has unsupported fields: {', '.join(unknown)}"
        )
    schema = str(value.get("schema") or "").strip()
    algorithm = str(value.get("algorithm") or "").strip().lower()
    public_key = str(value.get("public_key") or "").strip()
    signature = str(value.get("signature") or "").strip()
    key_id = str(value.get("key_id") or "").strip()
    if schema != PLUGIN_MANIFEST_SIGNATURE_SCHEMA:
        raise PluginError(
            f"Plugin signature schema must be {PLUGIN_MANIFEST_SIGNATURE_SCHEMA}."
        )
    if algorithm != "ed25519":
        raise PluginError("Plugin signature algorithm must be ed25519.")
    if not public_key or not signature:
        raise PluginError("Plugin signature public_key and signature are required.")
    normalized = {
        "schema": schema,
        "algorithm": algorithm,
        "public_key": public_key,
        "signature": signature,
    }
    if key_id:
        normalized["key_id"] = key_id
    return normalized


def _version_matches(current: str, raw_spec: str) -> bool:
    spec = raw_spec.strip()
    if spec in {"", "*"}:
        return True
    current_version = _version_tuple(current)
    for raw_clause in spec.split(","):
        clause = raw_clause.strip()
        if not clause:
            raise ValueError("empty version clause")
        if clause.startswith("^"):
            lower = _version_tuple(clause[1:])
            upper = _caret_upper_bound(lower)
            if not (current_version >= lower and current_version < upper):
                return False
            continue
        if clause.startswith("~="):
            lower = _version_tuple(clause[2:])
            parts = _version_components(clause[2:])
            upper = (
                (lower[0] + 1, 0, 0)
                if len(parts) == 1
                else (lower[0], lower[1] + 1, 0)
            )
            if not (current_version >= lower and current_version < upper):
                return False
            continue
        wildcard = re.fullmatch(r"v?(\d+(?:\.\d+){0,2})\.\*", clause)
        if wildcard:
            prefix = tuple(int(part) for part in wildcard.group(1).split("."))
            if current_version[: len(prefix)] != prefix:
                return False
            continue
        match = re.fullmatch(
            r"(>=|<=|==|!=|>|<)?(v?\d+(?:\.\d+){0,2})",
            clause,
        )
        if not match:
            raise ValueError(f"invalid version clause: {clause}")
        operator = match.group(1) or "=="
        target = _version_tuple(match.group(2))
        comparisons = {
            ">=": current_version >= target,
            "<=": current_version <= target,
            "==": current_version == target,
            "!=": current_version != target,
            ">": current_version > target,
            "<": current_version < target,
        }
        if not comparisons[operator]:
            return False
    return True


def _version_components(value: str) -> tuple[int, ...]:
    match = re.fullmatch(r"v?(\d+(?:\.\d+){0,2})", value.strip())
    if not match:
        raise ValueError(f"invalid version: {value}")
    return tuple(int(part) for part in match.group(1).split("."))


def _version_tuple(value: str) -> tuple[int, int, int]:
    match = re.match(r"^v?(\d+(?:\.\d+){0,2})", value.strip())
    if not match:
        raise ValueError(f"invalid version: {value}")
    parts = [int(part) for part in match.group(1).split(".")]
    padded = (parts + [0, 0])[:3]
    return padded[0], padded[1], padded[2]


def _caret_upper_bound(lower: tuple[int, int, int]) -> tuple[int, int, int]:
    if lower[0] > 0:
        return (lower[0] + 1, 0, 0)
    if lower[1] > 0:
        return (0, lower[1] + 1, 0)
    return (0, 0, lower[2] + 1)


def _isolation(value: object) -> dict[str, Any]:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise PluginError("Plugin isolation must be an object.")
    mode = str(value.get("mode", "shared")).strip().lower() or "shared"
    if mode not in {"shared", "process", "container"}:
        raise PluginError("Plugin isolation.mode must be shared, process, or container.")
    required = value.get("required", False)
    if not isinstance(required, bool):
        raise PluginError("Plugin isolation.required must be a boolean.")
    return {"mode": mode, "required": required}


def _dict_list(value: object, field: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise PluginError(f"Plugin manifest {field} must be a list of objects.")
    return [dict(item) for item in value]


def _slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-_")
    return slug or "plugin"


def _looks_like_sha(value: str) -> bool:
    return bool(_SHA_RE.match(value))


def _run_git(command: list[str], *, workspace: Path) -> str:
    if not command or Path(command[0]).name.casefold() not in {"git", "git.exe"}:
        raise PluginError("Expected a structured Git command.")
    completed = subprocess.run(  # noqa: S603 - list argv only, no shell  # nosec B603
        hardened_readonly_git_command(command[1:], workspace=workspace),
        cwd=workspace,
        capture_output=True,
        text=True,
        env=hardened_readonly_git_environment(),
        stdin=subprocess.DEVNULL,
        timeout=GIT_TIMEOUT_SECONDS,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"git exited {completed.returncode}"
        raise PluginError(detail)
    return completed.stdout
