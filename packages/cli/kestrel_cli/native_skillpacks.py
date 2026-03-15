from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from .native_shared import LOGGER, KestrelPaths
from .native_storage import SQLiteStateStore

_SHARED_PATH = Path(__file__).resolve().parents[2] / "shared"
if str(_SHARED_PATH) not in sys.path:
    sys.path.append(str(_SHARED_PATH))

from skillpacks import (  # type: ignore
    SkillPack,
    build_prompt_block,
    discover_marketplace_packs,
    default_pack_roots,
    download_remote_skill_archive,
    discover_skill_packs,
    expand_pack_dependencies,
    load_skill_pack,
    pack_snapshot_id,
    score_skill_candidate,
    select_skill_packs,
    unpack_skill_archive,
    write_inferred_manifest,
)


class NativeSkillPackManager:
    def __init__(
        self,
        *,
        paths: KestrelPaths,
        config: dict[str, Any],
        state_store: SQLiteStateStore,
        workspace_root: Path | None = None,
    ) -> None:
        self.paths = paths
        self.config = config
        self.state_store = state_store
        self.workspace_root = (workspace_root or Path.cwd()).resolve()
        self.repo_root = Path(__file__).resolve().parents[3]

    def _roots(self) -> dict[str, Path]:
        return default_pack_roots(
            repo_root=self.repo_root,
            kestrel_home=self.paths.home,
            workspace_root=self.workspace_root,
        )

    def _skill_config(self) -> dict[str, Any]:
        skill_config = self.config.get("skills") or {}
        return skill_config if isinstance(skill_config, dict) else {}

    def _marketplace_urls(self) -> list[str]:
        configured = self._skill_config().get("marketplace_urls") or []
        urls: list[str] = []
        if isinstance(configured, (list, tuple, set)):
            urls.extend(str(item).strip() for item in configured if str(item).strip())
        elif isinstance(configured, str) and configured.strip():
            urls.extend(part.strip() for part in configured.split(",") if part.strip())
        env_urls = os.getenv("KESTREL_SKILL_MARKETPLACE_URLS", "")
        if env_urls.strip():
            urls.extend(part.strip() for part in env_urls.split(",") if part.strip())
        deduped: list[str] = []
        seen: set[str] = set()
        for item in urls:
            if item in seen:
                continue
            seen.add(item)
            deduped.append(item)
        return deduped

    def _discover_marketplace(self) -> list[dict[str, Any]]:
        urls = self._marketplace_urls()
        if not urls:
            return []
        return discover_marketplace_packs(urls)

    def _synchronize_catalog(self) -> list[SkillPack]:
        packs = discover_skill_packs(self._roots())
        if bool(self._skill_config().get("auto_enable_bundled", True)):
            for pack in packs:
                if pack.root_kind != "bundled":
                    continue
                row = self.state_store.get_skill_pack(pack.pack_id)
                if row and row.get("removed_at"):
                    continue
                self.state_store.upsert_skill_pack(
                    pack_id=pack.pack_id,
                    version=pack.version,
                    scope="bundled",
                    source_path=str(pack.path),
                    source_type=pack.source_type,
                    enabled=True if row is None else bool(row.get("enabled", True)),
                    trusted=True,
                    manifest=pack.to_public_dict(),
                )
        return packs

    def _pack_state_map(self) -> dict[str, dict[str, Any]]:
        return {
            row["pack_id"]: row
            for row in self.state_store.list_skill_packs()
        }

    def _synthetic_custom_tool_packs(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        if not self.paths.tools_dir.exists():
            return results
        for tool_dir in sorted(path for path in self.paths.tools_dir.iterdir() if path.is_dir()):
            manifest_path = tool_dir / "tool.json"
            if not manifest_path.exists():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            name = str(manifest.get("name") or tool_dir.name)
            results.append(
                {
                    "pack_id": f"native-custom-{name}",
                    "name": name,
                    "version": str(manifest.get("version") or "1.0.0"),
                    "description": str(manifest.get("description") or f"Native custom tool {name}"),
                    "path": str(tool_dir),
                    "root_kind": "synthetic",
                    "source_type": "native_custom_tool",
                    "enabled": True,
                    "trusted": True,
                    "installed": True,
                    "components": [
                        {
                            "type": "native_tool",
                            "name": name,
                            "runtime": str(manifest.get("runtime") or "python"),
                            "entrypoint": str(manifest.get("entrypoint") or ""),
                            "input_schema": manifest.get("input_schema") or {"type": "object", "properties": {}},
                        }
                    ],
                    "compat": {"source_format": "tool_json", "synthetic": True},
                }
            )
        return results

    def _resolved_catalog_entries(self, *, include_marketplace: bool) -> tuple[list[SkillPack], list[dict[str, Any]]]:
        discovered = self._synchronize_catalog()
        marketplace: list[dict[str, Any]] = []
        if include_marketplace:
            local_ids = {pack.pack_id for pack in discovered}
            marketplace = [
                item
                for item in self._discover_marketplace()
                if str(item.get("pack_id") or "") not in local_ids
            ]
        return discovered, marketplace

    def catalog(self, *, include_synthetic: bool = True, include_marketplace: bool = True) -> dict[str, Any]:
        discovered, marketplace = self._resolved_catalog_entries(include_marketplace=include_marketplace)
        state_map = self._pack_state_map()
        items: list[dict[str, Any]] = []
        for pack in discovered:
            state = state_map.get(pack.pack_id)
            item = pack.to_public_dict()
            item["installed"] = state is not None or pack.root_kind == "bundled"
            item["enabled"] = bool(state.get("enabled", pack.root_kind == "bundled")) if state else pack.root_kind == "bundled"
            item["trusted"] = bool(state.get("trusted", pack.root_kind == "bundled")) if state else pack.root_kind == "bundled"
            item["scope"] = str(state.get("scope") or pack.root_kind)
            item["source_path"] = str(state.get("source_path") or pack.path)
            items.append(item)
        for item in marketplace:
            state = state_map.get(str(item.get("pack_id") or ""))
            payload = dict(item)
            payload["installed"] = bool(state)
            payload["enabled"] = bool((state or {}).get("enabled", False))
            payload["trusted"] = bool((state or {}).get("trusted", False))
            payload["scope"] = str((state or {}).get("scope") or "marketplace")
            payload["source_path"] = str((state or {}).get("source_path") or payload.get("source_path") or "")
            items.append(payload)
        if include_synthetic:
            items.extend(self._synthetic_custom_tool_packs())
        snapshot = pack_snapshot_id(discovered)
        return {"snapshot_id": snapshot, "packs": items}

    def search(self, query: str, *, include_marketplace: bool = True) -> dict[str, Any]:
        discovered, marketplace = self._resolved_catalog_entries(include_marketplace=include_marketplace)
        results: list[dict[str, Any]] = []
        for pack in discovered:
            score = score_skill_candidate(pack, query)
            if score <= 0:
                continue
            payload = pack.to_public_dict()
            payload["score"] = score
            results.append(payload)
        for item in marketplace:
            score = score_skill_candidate(item, query)
            if score <= 0:
                continue
            payload = dict(item)
            payload["score"] = score
            results.append(payload)
        results.sort(key=lambda item: (-float(item.get("score", 0)), str(item.get("name") or "").lower()))
        return {"query": query, "results": results[:25], "total": len(results)}

    def inspect(self, pack_id: str) -> dict[str, Any] | None:
        discovered, marketplace = self._resolved_catalog_entries(include_marketplace=True)
        discovered_map = {pack.pack_id: pack for pack in discovered}
        marketplace_map = {str(item.get("pack_id") or ""): item for item in marketplace}
        pack = discovered_map.get(pack_id)
        if pack:
            payload = pack.to_public_dict()
            state = self.state_store.get_skill_pack(pack_id) or {}
            payload["installed"] = bool(state) or pack.root_kind == "bundled"
            payload["enabled"] = bool(state.get("enabled", pack.root_kind == "bundled")) if state else pack.root_kind == "bundled"
            payload["trusted"] = bool(state.get("trusted", pack.root_kind == "bundled")) if state else pack.root_kind == "bundled"
            payload["scope"] = str(state.get("scope") or pack.root_kind)
            payload["source_path"] = str(state.get("source_path") or pack.path)
            payload["prompt_preview"] = build_prompt_block([pack], max_chars=3_000)
            return payload
        remote = marketplace_map.get(pack_id)
        if remote:
            payload = dict(remote)
            state = self.state_store.get_skill_pack(pack_id) or {}
            payload["installed"] = bool(state)
            payload["enabled"] = bool(state.get("enabled", False))
            payload["trusted"] = bool(state.get("trusted", False))
            payload["scope"] = str(state.get("scope") or "marketplace")
            payload["source_path"] = str(state.get("source_path") or payload.get("source_path") or "")
            return payload
        for synthetic in self._synthetic_custom_tool_packs():
            if synthetic["pack_id"] == pack_id:
                return synthetic
        return None

    def _workspace_skill_dir(self) -> Path:
        return self.workspace_root / ".kestrel" / "skills"

    def _copy_pack_dir(self, source_dir: Path, *, scope: str) -> SkillPack:
        target_root = self.paths.skills_dir if scope != "workspace" else self._workspace_skill_dir()
        target_root.mkdir(parents=True, exist_ok=True)
        source_pack = load_skill_pack(source_dir, root_kind="user")
        target_dir = target_root / source_pack.pack_id
        if target_dir.exists():
            shutil.rmtree(target_dir)
        shutil.copytree(source_dir, target_dir)
        if (target_dir / "SKILL.md").exists() and not any((target_dir / name).exists() for name in ("skill.yaml", "skill.yml", "skill.json", "manifest.json")):
            write_inferred_manifest(target_dir)
        return load_skill_pack(target_dir, root_kind="workspace" if scope == "workspace" else "user")

    def _download_and_unpack_remote(self, source_url: str) -> Path:
        archive_path = download_remote_skill_archive(source_url, self.paths.cache_dir / "skillpacks")
        return unpack_skill_archive(archive_path, self.paths.cache_dir / "skillpacks")

    def _install_pack_dependencies(
        self,
        pack: SkillPack,
        *,
        scope: str,
        trusted: bool,
        discovered: dict[str, SkillPack],
        marketplace: dict[str, dict[str, Any]],
        seen: set[str],
    ) -> list[str]:
        installed: list[str] = []
        for dependency in pack.dependencies:
            if dependency.pack_id in seen:
                continue
            existing = self.state_store.get_skill_pack(dependency.pack_id)
            if existing and existing.get("enabled") and existing.get("trusted"):
                seen.add(dependency.pack_id)
                continue
            if existing and not existing.get("enabled"):
                self.state_store.set_skill_pack_enabled(dependency.pack_id, True)
                seen.add(dependency.pack_id)
                installed.append(dependency.pack_id)
                continue
            if dependency.pack_id in discovered:
                result = self.install(
                    pack_id=dependency.pack_id,
                    scope=scope,
                    trusted=trusted,
                    _seen=seen,
                )
                installed.extend(list(result.get("dependencies_installed") or []))
                installed.append(dependency.pack_id)
                continue
            remote = marketplace.get(dependency.pack_id)
            if remote:
                result = self.install(
                    pack_id=dependency.pack_id,
                    scope=scope,
                    trusted=trusted,
                    _seen=seen,
                )
                installed.extend(list(result.get("dependencies_installed") or []))
                installed.append(dependency.pack_id)
                continue
            if dependency.source_path:
                result = self.install(
                    source_path=dependency.source_path,
                    scope=scope,
                    trusted=trusted,
                    _seen=seen,
                )
                installed.extend(list(result.get("dependencies_installed") or []))
                installed.append(str(result.get("pack", {}).get("pack_id") or dependency.pack_id))
                continue
            if dependency.source_url:
                result = self.install(
                    source_url=dependency.source_url,
                    scope=scope,
                    trusted=trusted,
                    _seen=seen,
                )
                installed.extend(list(result.get("dependencies_installed") or []))
                installed.append(str(result.get("pack", {}).get("pack_id") or dependency.pack_id))
                continue
            if dependency.optional:
                continue
            raise RuntimeError(f"Missing dependency for {pack.pack_id}: {dependency.pack_id}")
        return list(dict.fromkeys(item for item in installed if item and item != pack.pack_id))

    def install(
        self,
        *,
        pack_id: str = "",
        source_path: str = "",
        source_url: str = "",
        scope: str = "user",
        trusted: bool = True,
        _seen: set[str] | None = None,
    ) -> dict[str, Any]:
        discovered_list, marketplace_list = self._resolved_catalog_entries(include_marketplace=True)
        discovered = {pack.pack_id: pack for pack in discovered_list}
        marketplace = {str(item.get("pack_id") or ""): item for item in marketplace_list}
        installed_pack: SkillPack
        resolved_scope = "workspace" if scope == "workspace" else "user"
        seen = _seen or set()
        if pack_id:
            pack = discovered.get(pack_id)
            remote = marketplace.get(pack_id)
            if pack is None and remote is None:
                raise RuntimeError(f"Unknown skill pack: {pack_id}")
            if pack is not None:
                if pack.root_kind == "bundled":
                    dependency_ids = self._install_pack_dependencies(
                        pack,
                        scope=resolved_scope,
                        trusted=trusted,
                        discovered=discovered,
                        marketplace=marketplace,
                        seen=seen | {pack.pack_id},
                    )
                    row = self.state_store.upsert_skill_pack(
                        pack_id=pack.pack_id,
                        version=pack.version,
                        scope="bundled",
                        source_path=str(pack.path),
                        source_type=pack.source_type,
                        enabled=True,
                        trusted=True,
                        manifest=pack.to_public_dict(),
                    )
                    return {"pack": row, "action": "enabled_bundled", "dependencies_installed": dependency_ids}
                dependency_ids = self._install_pack_dependencies(
                    pack,
                    scope=resolved_scope,
                    trusted=trusted,
                    discovered=discovered,
                    marketplace=marketplace,
                    seen=seen | {pack.pack_id},
                )
                installed_pack = self._copy_pack_dir(pack.path, scope=resolved_scope)
            else:
                install_url = str(remote.get("install_url") or "").strip()
                if not install_url:
                    raise RuntimeError(f"Marketplace pack {pack_id} does not expose an installable archive.")
                unpacked = self._download_and_unpack_remote(install_url)
                remote_pack = load_skill_pack(unpacked, root_kind="user")
                dependency_ids = self._install_pack_dependencies(
                    remote_pack,
                    scope=resolved_scope,
                    trusted=trusted,
                    discovered=discovered,
                    marketplace=marketplace,
                    seen=seen | {remote_pack.pack_id},
                )
                installed_pack = self._copy_pack_dir(unpacked, scope=resolved_scope)
        else:
            dependency_ids = []
            if source_url:
                unpacked = self._download_and_unpack_remote(source_url)
                pack_preview = load_skill_pack(unpacked, root_kind="user")
                dependency_ids = self._install_pack_dependencies(
                    pack_preview,
                    scope=resolved_scope,
                    trusted=trusted,
                    discovered=discovered,
                    marketplace=marketplace,
                    seen=seen | {pack_preview.pack_id},
                )
                installed_pack = self._copy_pack_dir(unpacked, scope=resolved_scope)
            else:
                source = Path(source_path).expanduser().resolve()
                if not source.exists():
                    raise FileNotFoundError(f"Skill pack source not found: {source}")
                if source.is_file():
                    unpacked = unpack_skill_archive(source, self.paths.cache_dir / "skillpacks")
                    pack_preview = load_skill_pack(unpacked, root_kind="user")
                    dependency_ids = self._install_pack_dependencies(
                        pack_preview,
                        scope=resolved_scope,
                        trusted=trusted,
                        discovered=discovered,
                        marketplace=marketplace,
                        seen=seen | {pack_preview.pack_id},
                    )
                    installed_pack = self._copy_pack_dir(unpacked, scope=resolved_scope)
                else:
                    pack_preview = load_skill_pack(source, root_kind="user")
                    dependency_ids = self._install_pack_dependencies(
                        pack_preview,
                        scope=resolved_scope,
                        trusted=trusted,
                        discovered=discovered,
                        marketplace=marketplace,
                        seen=seen | {pack_preview.pack_id},
                    )
                    installed_pack = self._copy_pack_dir(source, scope=resolved_scope)
        row = self.state_store.upsert_skill_pack(
            pack_id=installed_pack.pack_id,
            version=installed_pack.version,
            scope=resolved_scope,
            source_path=str(installed_pack.path),
            source_type=installed_pack.source_type,
            enabled=True,
            trusted=trusted,
            manifest=installed_pack.to_public_dict(),
        )
        return {"pack": row, "action": "installed", "dependencies_installed": dependency_ids}

    def import_pack(self, *, source_path: str, scope: str = "user") -> dict[str, Any]:
        return self.install(source_path=source_path, scope=scope, trusted=True)

    def enable(self, pack_id: str) -> dict[str, Any]:
        row = self.state_store.set_skill_pack_enabled(pack_id, True)
        if row is None:
            raise RuntimeError(f"Unknown skill pack: {pack_id}")
        return {"pack": row, "action": "enabled"}

    def disable(self, pack_id: str) -> dict[str, Any]:
        row = self.state_store.set_skill_pack_enabled(pack_id, False)
        if row is None:
            raise RuntimeError(f"Unknown skill pack: {pack_id}")
        return {"pack": row, "action": "disabled"}

    def remove(self, pack_id: str) -> dict[str, Any]:
        row = self.state_store.get_skill_pack(pack_id)
        if row is None:
            raise RuntimeError(f"Unknown skill pack: {pack_id}")
        if str(row.get("scope") or "") == "bundled":
            disabled = self.state_store.set_skill_pack_enabled(pack_id, False)
            return {"pack": disabled or row, "action": "disabled_bundled"}
        source_path = Path(str(row.get("source_path") or "")).expanduser()
        roots = self._roots()
        safe_roots = {roots["user"].resolve(), roots["workspace"].resolve()}
        try:
            resolved_source = source_path.resolve()
        except FileNotFoundError:
            resolved_source = source_path
        if row.get("scope") in {"user", "workspace"} and resolved_source.exists():
            for safe_root in safe_roots:
                if str(resolved_source).startswith(str(safe_root)):
                    shutil.rmtree(resolved_source, ignore_errors=True)
                    break
        removed = self.state_store.remove_skill_pack(pack_id)
        return {"pack": removed or row, "action": "removed"}

    def enabled_packs(self) -> list[SkillPack]:
        discovered = {pack.pack_id: pack for pack in self._synchronize_catalog()}
        enabled: list[SkillPack] = []
        for row in self.state_store.list_skill_packs():
            if not row.get("enabled") or not row.get("trusted"):
                continue
            pack = discovered.get(row["pack_id"])
            if pack is None:
                source_path = Path(str(row.get("source_path") or "")).expanduser()
                if not source_path.exists():
                    continue
                try:
                    pack = load_skill_pack(source_path, root_kind=str(row.get("scope") or "user"))
                except Exception:
                    continue
            enabled.append(pack)
        return enabled

    def select_packs(self, goal: str, *, history: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        limit = max(1, int(self._skill_config().get("max_active_per_task", 5)))
        enabled = self.enabled_packs()
        selected = select_skill_packs(
            enabled,
            goal,
            history=history,
            limit=limit,
        )
        selected = expand_pack_dependencies(enabled, selected, limit=limit + 10)
        return {
            "snapshot_id": pack_snapshot_id(enabled),
            "packs": [pack.to_public_dict() for pack in selected],
            "prompt_block": build_prompt_block(selected),
        }

    def enabled_tool_components(self) -> list[tuple[SkillPack, dict[str, Any]]]:
        components: list[tuple[SkillPack, dict[str, Any]]] = []
        for pack in self.enabled_packs():
            for component in pack.tool_components():
                if component.type != "native_tool":
                    continue
                payload = component.to_dict()
                payload["pack_id"] = pack.pack_id
                components.append((pack, payload))
        return components
