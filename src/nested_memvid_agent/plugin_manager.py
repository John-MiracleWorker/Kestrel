from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess  # nosec B404
import tempfile
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .models import EvidenceRef, MemoryKind, MemoryLayer, MemoryRecord
from .skill_manager import validate_skill_manifest
from .state_store import AgentStateStore

PLUGIN_NAMESPACE = "plugin"
PLUGIN_SOURCE_DIR = "source"
PLUGIN_GENERATED_DIR = "generated"
DEFAULT_PLUGIN_RISK = "medium"
GIT_TIMEOUT_SECONDS = 60

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
        command = ["git", "clone", "--depth", "1"]
        if ref and not _looks_like_sha(ref):
            command.extend(["--branch", ref])
        command.extend([source.clone_url, str(destination)])
        _run_git(command)
        if ref and _looks_like_sha(ref):
            _run_git(["git", "-C", str(destination), "fetch", "--depth", "1", "origin", ref])
            _run_git(["git", "-C", str(destination), "checkout", "--detach", ref])
        return _run_git(["git", "-C", str(destination), "rev-parse", "HEAD"]).strip()


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
        tmp_parent = self.root / ".tmp"
        tmp_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="plugin-", dir=tmp_parent) as tmp_name:
            repo_path = Path(tmp_name) / "repo"
            commit_sha = self.fetcher.fetch(parsed_source, repo_path, normalized_ref)
            initial_manifest = load_plugin_manifest(repo_path)
            if expected_plugin_id is not None and initial_manifest.id != expected_plugin_id:
                raise PluginError(f"Plugin manifest id changed during update: expected {expected_plugin_id}, got {initial_manifest.id}")
            plugin_dir = _safe_plugin_dir(self.root, initial_manifest.id)
            if plugin_dir.exists() and not overwrite:
                raise FileExistsError(f"Plugin already installed: {initial_manifest.id}")
            if plugin_dir.exists():
                shutil.rmtree(plugin_dir)
            plugin_dir.mkdir(parents=True, exist_ok=True)
            source_dir = plugin_dir / PLUGIN_SOURCE_DIR
            shutil.move(str(repo_path), str(source_dir))

        manifest = load_plugin_manifest(source_dir)
        risk_report = _risk_report(
            manifest,
            source=parsed_source.display_url,
            commit_sha=commit_sha,
            source_dir=source_dir,
        )
        row = self.state.upsert_plugin(
            {
                "id": manifest.id,
                "name": manifest.name,
                "description": manifest.description,
                "source_url": parsed_source.display_url,
                "source_ref": normalized_ref,
                "commit_sha": commit_sha,
                "install_path": str(plugin_dir),
                "manifest": manifest.to_state_payload(),
                "capabilities": _capabilities(manifest),
                "enabled": enable,
                "risk_report": risk_report,
                "install_status": "installed",
                "format": manifest.format,
            }
        )
        self.sync_plugin(row["id"])
        return self.state.get_plugin(row["id"])

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
        self.state.set_plugin_enabled(plugin_id, enabled)
        self.sync_plugin(plugin_id)
        return self.state.get_plugin(plugin_id)

    def remove(self, plugin_id: str) -> dict[str, Any]:
        row = self.state.get_plugin(plugin_id)
        self._delete_extension_rows(plugin_id)
        install_path = Path(str(row["install_path"]))
        if install_path.exists():
            shutil.rmtree(install_path)
        self.state.delete_plugin(plugin_id)
        return {"removed": True, "plugin_id": plugin_id}

    def sync_all(self) -> None:
        for row in self.state.list_plugins():
            self._sync_plugin_row(row)

    def sync_plugin(self, plugin_id: str) -> None:
        self._sync_plugin_row(self.state.get_plugin(plugin_id))

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

    def _sync_plugin_row(self, row: dict[str, Any]) -> None:
        manifest = dict(row["manifest"])
        enabled = bool(row["enabled"])
        plugin_id = str(row["id"])
        skills = [dict(item) for item in manifest.get("skills", []) if isinstance(item, dict)]
        mcp_servers = [dict(item) for item in manifest.get("mcp_servers", []) if isinstance(item, dict)]
        desired_skill_ids = {str(item["namespaced_id"]) for item in skills}
        desired_mcp_ids = {str(item["namespaced_id"]) for item in mcp_servers}
        self._delete_stale_extension_rows(plugin_id, desired_skill_ids, desired_mcp_ids)

        for skill in skills:
            skill_manifest, skill_path = self._materialize_skill(row, skill)
            self.state.upsert_skill(
                {
                    "id": skill["namespaced_id"],
                    "name": skill_manifest.get("name", skill["namespaced_id"]),
                    "description": skill_manifest.get("description", ""),
                    "path": str(skill_path),
                    "manifest": skill_manifest,
                    "enabled": enabled and bool(skill.get("enabled", True)),
                }
            )

        for server in mcp_servers:
            payload = dict(server["config"])
            payload["id"] = server["namespaced_id"]
            payload["name"] = payload.get("name") or f"{row['name']} {server['id']}"
            payload["enabled"] = enabled and bool(payload.get("enabled", True))
            payload.setdefault("risk_policy", "approval_by_default")
            payload.setdefault("tools", [])
            self.state.upsert_mcp_server(payload)

    def _materialize_skill(self, row: dict[str, Any], skill: dict[str, Any]) -> tuple[dict[str, Any], Path]:
        install_path = Path(str(row["install_path"]))
        skill_path = _safe_child_path(
            install_path,
            f"{PLUGIN_GENERATED_DIR}/skills/{skill['id']}",
            must_exist=False,
        )
        skill_path.mkdir(parents=True, exist_ok=True)
        manifest = dict(skill["manifest"])
        manifest["id"] = skill["namespaced_id"]
        manifest.setdefault("name", skill.get("name") or skill["namespaced_id"])
        manifest.setdefault("description", skill.get("description") or row.get("description") or "Plugin skill.")
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
        (skill_path / "skill.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (skill_path / "SKILL.md").write_text(instructions, encoding="utf-8")
        return manifest, skill_path

    def _delete_stale_extension_rows(
        self,
        plugin_id: str,
        desired_skill_ids: set[str],
        desired_mcp_ids: set[str],
    ) -> None:
        prefix = f"{PLUGIN_NAMESPACE}.{plugin_id}."
        for skill in self.state.list_skills():
            skill_id = str(skill["id"])
            if skill_id.startswith(prefix) and skill_id not in desired_skill_ids:
                self.state.delete_skill(skill_id)
        for server in self.state.list_mcp_servers():
            server_id = str(server["id"])
            if server_id.startswith(prefix) and server_id not in desired_mcp_ids:
                self.state.delete_mcp_server(server_id)

    def _delete_extension_rows(self, plugin_id: str) -> None:
        self._delete_stale_extension_rows(plugin_id, set(), set())


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
        raw = json.loads(kestrel_manifest.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise PluginError("kestrel.plugin.json must contain a JSON object.")
        return _normalize_kestrel_manifest(raw, repo_root)

    for filename in ("plugin.yaml", "plugin.yml"):
        hermes_manifest = repo_root / filename
        if hermes_manifest.exists():
            raw = _load_yaml_manifest(hermes_manifest)
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
    warnings: list[str] = []
    skills = tuple(_normalize_skill(plugin_id, item, repo_root, risk) for item in _dict_list(raw.get("skills", []), "skills"))
    mcp_servers = tuple(_normalize_mcp_server(plugin_id, item) for item in _dict_list(raw.get("mcp_servers", []), "mcp_servers"))
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
    mcp_servers = tuple(_normalize_mcp_server(plugin_id, item) for item in _dict_list(mcp_raw, "mcp_servers"))
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
                loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    source_manifest.update(loaded)
            if instructions_path.exists():
                instructions = instructions_path.read_text(encoding="utf-8")
        elif skill_path.is_file():
            instructions = skill_path.read_text(encoding="utf-8")
    if raw.get("instructions_path"):
        instructions = _safe_child_path(repo_root, str(raw["instructions_path"])).read_text(encoding="utf-8")
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


def _normalize_mcp_server(plugin_id: str, raw: dict[str, Any]) -> dict[str, Any]:
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
        _validate_plugin_stdio_command(command, args)
        vetting = dict(config.get("vetting", {}) or {})
        vetting["stdio_command_hash"] = _stdio_command_hash(command, args)
        vetting["connect_requires_approval"] = True
        vetting["plugin_source"] = plugin_id
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


def _validate_plugin_stdio_command(command: str, args: list[str]) -> None:
    if not command:
        return
    command_name = Path(command).name
    allowed = {"npx", "uvx", "python", "python3", "node", "bunx", "deno"}
    if command_name not in allowed:
        raise PluginError(f"Plugin MCP command is not allowed: {command_name}")
    if command_name in {"python", "python3"}:
        if len(args) < 2 or args[0] != "-m" or not _valid_python_module(args[1]):
            raise PluginError("Plugin MCP python commands must use `python -m <module>` with a valid module name.")
    if command_name in {"npx", "uvx", "bunx"}:
        if not args or not _valid_package_name(args[0]):
            raise PluginError(f"Plugin MCP {command_name} commands must name a valid package.")
    if command_name == "node":
        if not args or any(_has_shell_metacharacters(part) for part in args):
            raise PluginError("Plugin MCP node args contain unsupported shell metacharacters.")
    if command_name == "deno":
        if not args or any(_has_shell_metacharacters(part) for part in args):
            raise PluginError("Plugin MCP deno args contain unsupported shell metacharacters.")


def _stdio_command_hash(command: str, args: list[str]) -> str:
    payload = json.dumps({"command": command, "args": args}, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _valid_python_module(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*", value))


def _valid_package_name(value: str) -> bool:
    return bool(re.fullmatch(r"(@[A-Za-z0-9_.-]+/)?[A-Za-z0-9_.-]+", value))


def _has_shell_metacharacters(value: str) -> bool:
    return any(char in value for char in ";&|`$><")


def _load_yaml_manifest(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
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
) -> dict[str, Any]:
    return {
        "risk": manifest.risk,
        "permissions": list(manifest.permissions),
        "requires_env": list(manifest.requires_env),
        "warnings": list(manifest.warnings),
        "unsupported_features": list(manifest.unsupported_features),
        "source_url": source,
        "commit_sha": commit_sha,
        "approval_policy": "approval_by_default",
        "tree_sha256": _tree_sha256(source_dir),
    }


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
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file() and ".git" not in item.parts):
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _safe_plugin_dir(root: Path, plugin_id: str) -> Path:
    return _safe_child_path(root, plugin_id, must_exist=False)


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


def _run_git(command: list[str]) -> str:
    completed = subprocess.run(  # noqa: S603 - list argv only, no shell  # nosec B603
        command,
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_SECONDS,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"git exited {completed.returncode}"
        raise PluginError(detail)
    return completed.stdout
