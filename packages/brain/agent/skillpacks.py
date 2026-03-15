from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from agent.skills import BLOCKED_MODULES, SAFE_GLOBALS
from agent.types import RiskLevel, ToolDefinition
from core.config import logger

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
    find_skill_pack_dir,
    load_skill_pack,
    pack_snapshot_id,
    resolve_marketplace_pack,
    score_skill_candidate,
    search_marketplace_packs,
    select_skill_packs,
    unpack_skill_archive,
    write_inferred_manifest,
)


class SkillPackManager:
    def __init__(self, pool):
        self._pool = pool
        self._repo_root = Path(__file__).resolve().parents[3]

    def _roots(self) -> dict[str, Path]:
        return default_pack_roots(
            repo_root=self._repo_root,
            kestrel_home=Path.home() / ".kestrel",
            workspace_root=Path.cwd(),
        )

    def _marketplace_urls(self) -> list[str]:
        raw = os.getenv("KESTREL_SKILL_MARKETPLACE_URLS", "").strip()
        if not raw:
            return []
        return [part.strip() for part in raw.split(",") if part.strip()]

    def _auto_connect_mcp_enabled(self) -> bool:
        value = os.getenv("KESTREL_SKILL_AUTO_CONNECT_MCP_PACKS", "true").strip().lower()
        return value not in {"0", "false", "no", "off"}

    async def _discover_marketplace(self) -> list[dict[str, Any]]:
        urls = self._marketplace_urls()
        if not urls:
            return []
        return await asyncio.to_thread(discover_marketplace_packs, urls)

    async def _search_marketplace(self, query: str) -> list[dict[str, Any]]:
        urls = self._marketplace_urls()
        if not urls:
            return []
        return await asyncio.to_thread(search_marketplace_packs, urls, query, limit=50)

    async def _resolve_marketplace_pack(self, pack_id: str) -> dict[str, Any] | None:
        urls = self._marketplace_urls()
        if not urls:
            return None
        return await asyncio.to_thread(resolve_marketplace_pack, urls, pack_id)

    async def _ensure_tables(self) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_skill_packs (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    workspace_id UUID REFERENCES workspaces(id) ON DELETE CASCADE,
                    pack_id TEXT NOT NULL,
                    version TEXT NOT NULL DEFAULT '',
                    scope TEXT NOT NULL DEFAULT 'user',
                    source_path TEXT NOT NULL,
                    source_type TEXT NOT NULL DEFAULT 'directory',
                    enabled BOOLEAN NOT NULL DEFAULT true,
                    trusted BOOLEAN NOT NULL DEFAULT false,
                    manifest_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    installed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    removed_at TIMESTAMPTZ
                )
                """
            )
            await conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_skill_packs_workspace
                    ON agent_skill_packs(workspace_id, pack_id)
                    WHERE removed_at IS NULL
                """
            )
            await conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_skill_packs_global
                    ON agent_skill_packs(pack_id)
                    WHERE workspace_id IS NULL AND removed_at IS NULL
                """
            )

    async def _discover(self) -> list[SkillPack]:
        return discover_skill_packs(self._roots())

    async def _resolved_catalog_entries(self, *, include_marketplace: bool) -> tuple[list[SkillPack], list[dict[str, Any]]]:
        discovered = await self._discover()
        marketplace: list[dict[str, Any]] = []
        if include_marketplace:
            local_ids = {pack.pack_id for pack in discovered}
            marketplace = [
                item
                for item in await self._discover_marketplace()
                if str(item.get("pack_id") or "") not in local_ids
            ]
        return discovered, marketplace

    async def _sync_bundled(self) -> None:
        await self._ensure_tables()
        discovered = await self._discover()
        async with self._pool.acquire() as conn:
            for pack in discovered:
                if pack.root_kind != "bundled":
                    continue
                await conn.execute(
                    """
                    INSERT INTO agent_skill_packs (
                        workspace_id, pack_id, version, scope, source_path, source_type,
                        enabled, trusted, manifest_json, installed_at, updated_at, removed_at
                    )
                    VALUES (NULL, $1, $2, 'bundled', $3, $4, true, true, $5::jsonb, now(), now(), NULL)
                    ON CONFLICT (pack_id) WHERE workspace_id IS NULL AND removed_at IS NULL
                    DO UPDATE SET
                        version = EXCLUDED.version,
                        source_path = EXCLUDED.source_path,
                        source_type = EXCLUDED.source_type,
                        trusted = true,
                        manifest_json = EXCLUDED.manifest_json,
                        updated_at = now(),
                        removed_at = NULL
                    """,
                    pack.pack_id,
                    pack.version,
                    str(pack.path),
                    pack.source_type,
                    json.dumps(pack.to_public_dict()),
                )

    async def _load_state_rows(self, workspace_id: str) -> list[dict[str, Any]]:
        await self._sync_bundled()
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT workspace_id, pack_id, version, scope, source_path, source_type,
                       enabled, trusted, manifest_json, installed_at, updated_at, removed_at
                FROM agent_skill_packs
                WHERE removed_at IS NULL
                  AND (workspace_id = $1 OR workspace_id IS NULL)
                ORDER BY workspace_id NULLS LAST, updated_at DESC
                """,
                workspace_id or None,
            )
        payloads: list[dict[str, Any]] = []
        for row in rows:
            payload = dict(row)
            payload["manifest"] = row["manifest_json"] if isinstance(row["manifest_json"], dict) else json.loads(row["manifest_json"])
            payloads.append(payload)
        merged: dict[str, dict[str, Any]] = {}
        for payload in payloads:
            pack_id = str(payload.get("pack_id") or "")
            existing = merged.get(pack_id)
            if existing is None or (payload.get("workspace_id") and not existing.get("workspace_id")):
                merged[pack_id] = payload
        return list(merged.values())

    async def _synthetic_dynamic_skill_packs(self, workspace_id: str) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, workspace_id, name, description, parameters, enabled, scope, state
                FROM agent_skills
                WHERE state = 'approved'
                  AND (workspace_id = $1 OR workspace_id IS NULL)
                ORDER BY name ASC
                """,
                workspace_id or None,
            )
        results: list[dict[str, Any]] = []
        for row in rows:
            results.append(
                {
                    "pack_id": f"dynamic-skill-{row['name']}",
                    "name": row["name"],
                    "version": "1.0.0",
                    "description": row["description"] or f"Dynamic skill {row['name']}",
                    "path": "",
                    "root_kind": "synthetic",
                    "source_type": "brain_dynamic_skill",
                    "enabled": bool(row["enabled"]),
                    "trusted": True,
                    "installed": True,
                    "scope": row.get("scope") or "workspace",
                    "components": [
                        {
                            "type": "brain_python_tool",
                            "name": f"skill_{row['name']}",
                            "parameters": row["parameters"] if isinstance(row["parameters"], dict) else {},
                        }
                    ],
                    "compat": {"synthetic": True},
                }
            )
        return results

    async def _synthetic_mcp_packs(self, workspace_id: str) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT name, description, server_url, transport, enabled
                FROM installed_tools
                WHERE workspace_id = $1
                ORDER BY name ASC
                """,
                workspace_id,
            )
        results: list[dict[str, Any]] = []
        for row in rows:
            results.append(
                {
                    "pack_id": f"mcp-recipe-{row['name']}",
                    "name": row["name"],
                    "version": "1.0.0",
                    "description": row["description"] or f"MCP recipe {row['name']}",
                    "path": "",
                    "root_kind": "synthetic",
                    "source_type": "mcp_recipe",
                    "enabled": bool(row["enabled"]),
                    "trusted": True,
                    "installed": True,
                    "scope": "workspace",
                    "components": [
                        {
                            "type": "mcp_recipe",
                            "name": row["name"],
                            "description": row["description"] or "",
                            "server_command": row["server_url"],
                            "transport": row["transport"] or "stdio",
                        }
                    ],
                    "compat": {"synthetic": True},
                }
            )
        return results

    async def catalog(self, workspace_id: str, *, include_synthetic: bool = True, include_marketplace: bool = True) -> dict[str, Any]:
        discovered_list, marketplace = await self._resolved_catalog_entries(include_marketplace=include_marketplace)
        discovered = {pack.pack_id: pack for pack in discovered_list}
        state_rows = await self._load_state_rows(workspace_id)
        items: list[dict[str, Any]] = []
        for pack in discovered.values():
            row = next((item for item in state_rows if item["pack_id"] == pack.pack_id), None)
            payload = pack.to_public_dict()
            payload["installed"] = row is not None or pack.root_kind == "bundled"
            payload["enabled"] = bool((row or {}).get("enabled", pack.root_kind == "bundled"))
            payload["trusted"] = bool((row or {}).get("trusted", pack.root_kind == "bundled"))
            payload["scope"] = str((row or {}).get("scope") or pack.root_kind)
            payload["source_path"] = str((row or {}).get("source_path") or pack.path)
            items.append(payload)
        state_map = {str(row.get("pack_id") or ""): row for row in state_rows}
        for item in marketplace:
            row = state_map.get(str(item.get("pack_id") or ""))
            if row is None:
                local_pack_id = str(((item.get("compat") or {}).get("local_pack_id") or "")).strip()
                if local_pack_id:
                    row = state_map.get(local_pack_id)
            payload = dict(item)
            payload["installed"] = bool(row)
            payload["enabled"] = bool((row or {}).get("enabled", False))
            payload["trusted"] = bool((row or {}).get("trusted", False))
            payload["scope"] = str((row or {}).get("scope") or "marketplace")
            payload["source_path"] = str((row or {}).get("source_path") or payload.get("source_path") or "")
            items.append(payload)
        if include_synthetic:
            items.extend(await self._synthetic_dynamic_skill_packs(workspace_id))
            items.extend(await self._synthetic_mcp_packs(workspace_id))
        return {"snapshot_id": pack_snapshot_id(list(discovered.values())), "packs": items}

    async def search(self, workspace_id: str, query: str, *, include_marketplace: bool = True) -> dict[str, Any]:
        discovered = await self._discover()
        marketplace = await self._search_marketplace(query) if include_marketplace else []
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

    async def inspect(self, workspace_id: str, pack_id: str) -> dict[str, Any] | None:
        catalog = await self.catalog(workspace_id, include_synthetic=True, include_marketplace=True)
        for pack in catalog["packs"]:
            if str(pack.get("pack_id") or "").lower() == pack_id.lower():
                if str(pack.get("path") or "").strip():
                    try:
                        loaded = load_skill_pack(str(pack["path"]), root_kind=str(pack.get("root_kind") or "user"))
                        pack["prompt_preview"] = build_prompt_block([loaded], max_chars=3_000)
                    except Exception:
                        pass
                return pack
        remote = await self._resolve_marketplace_pack(pack_id)
        if remote:
            state_rows = await self._load_state_rows(workspace_id)
            state_map = {str(row.get("pack_id") or ""): row for row in state_rows}
            row = state_map.get(pack_id)
            if row is None:
                local_pack_id = str(((remote.get("compat") or {}).get("local_pack_id") or "")).strip()
                if local_pack_id:
                    row = state_map.get(local_pack_id)
            payload = dict(remote)
            payload["installed"] = bool(row)
            payload["enabled"] = bool((row or {}).get("enabled", False))
            payload["trusted"] = bool((row or {}).get("trusted", False))
            payload["scope"] = str((row or {}).get("scope") or "marketplace")
            payload["source_path"] = str((row or {}).get("source_path") or payload.get("source_path") or "")
            return payload
        return None

    def _workspace_skill_dir(self) -> Path:
        return Path.cwd() / ".kestrel" / "skills"

    def _copy_pack(self, source_dir: Path, *, scope: str) -> SkillPack:
        target_root = Path.home() / ".kestrel" / "skills" if scope != "workspace" else self._workspace_skill_dir()
        target_root.mkdir(parents=True, exist_ok=True)
        pack = load_skill_pack(source_dir, root_kind="user")
        target_dir = target_root / pack.pack_id
        if target_dir.exists():
            shutil.rmtree(target_dir)
        shutil.copytree(source_dir, target_dir)
        if (target_dir / "SKILL.md").exists() and not any((target_dir / name).exists() for name in ("skill.yaml", "skill.yml", "skill.json", "manifest.json")):
            write_inferred_manifest(target_dir)
        return load_skill_pack(target_dir, root_kind="workspace" if scope == "workspace" else "user")

    async def _download_and_unpack_remote(self, source_url: str) -> Path:
        archive_path = await asyncio.to_thread(
            download_remote_skill_archive,
            source_url,
            Path.home() / ".kestrel" / "cache" / "skillpacks",
        )
        return unpack_skill_archive(archive_path, Path.home() / ".kestrel" / "cache" / "skillpacks")

    async def _upsert_row(
        self,
        *,
        workspace_id: str | None,
        pack: SkillPack,
        scope: str,
        enabled: bool,
        trusted: bool,
    ) -> dict[str, Any]:
        await self._ensure_tables()
        async with self._pool.acquire() as conn:
            if workspace_id is None:
                await conn.execute(
                    """
                    INSERT INTO agent_skill_packs (
                        workspace_id, pack_id, version, scope, source_path, source_type,
                        enabled, trusted, manifest_json, installed_at, updated_at, removed_at
                    )
                    VALUES (NULL, $1, $2, $3, $4, $5, $6, $7, $8::jsonb, now(), now(), NULL)
                    ON CONFLICT (pack_id) WHERE workspace_id IS NULL AND removed_at IS NULL
                    DO UPDATE SET
                        version = EXCLUDED.version,
                        scope = EXCLUDED.scope,
                        source_path = EXCLUDED.source_path,
                        source_type = EXCLUDED.source_type,
                        enabled = EXCLUDED.enabled,
                        trusted = EXCLUDED.trusted,
                        manifest_json = EXCLUDED.manifest_json,
                        updated_at = now(),
                        removed_at = NULL
                    """,
                    pack.pack_id,
                    pack.version,
                    scope,
                    str(pack.path),
                    pack.source_type,
                    enabled,
                    trusted,
                    json.dumps(pack.to_public_dict()),
                )
            else:
                await conn.execute(
                    """
                    INSERT INTO agent_skill_packs (
                        workspace_id, pack_id, version, scope, source_path, source_type,
                        enabled, trusted, manifest_json, installed_at, updated_at, removed_at
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, now(), now(), NULL)
                    ON CONFLICT (workspace_id, pack_id) WHERE removed_at IS NULL
                    DO UPDATE SET
                        version = EXCLUDED.version,
                        scope = EXCLUDED.scope,
                        source_path = EXCLUDED.source_path,
                        source_type = EXCLUDED.source_type,
                        enabled = EXCLUDED.enabled,
                        trusted = EXCLUDED.trusted,
                        manifest_json = EXCLUDED.manifest_json,
                        updated_at = now(),
                        removed_at = NULL
                    """,
                    workspace_id,
                    pack.pack_id,
                    pack.version,
                    scope,
                    str(pack.path),
                    pack.source_type,
                    enabled,
                    trusted,
                    json.dumps(pack.to_public_dict()),
        )
        return {"pack_id": pack.pack_id, "scope": scope, "enabled": enabled, "trusted": trusted}

    async def _install_pack_dependencies(
        self,
        *,
        workspace_id: str,
        pack: SkillPack,
        scope: str,
        discovered: dict[str, SkillPack],
        marketplace: dict[str, dict[str, Any]],
        seen: set[str],
    ) -> list[str]:
        installed: list[str] = []
        state_rows = {str(row.get("pack_id") or ""): row for row in await self._load_state_rows(workspace_id)}
        for dependency in pack.dependencies:
            if dependency.pack_id in seen:
                continue
            existing = state_rows.get(dependency.pack_id)
            if existing and existing.get("enabled") and existing.get("trusted"):
                seen.add(dependency.pack_id)
                continue
            if existing and not existing.get("enabled"):
                await self.enable(workspace_id, dependency.pack_id)
                seen.add(dependency.pack_id)
                installed.append(dependency.pack_id)
                continue
            if dependency.pack_id in discovered:
                result = await self.install(
                    workspace_id=workspace_id,
                    pack_id=dependency.pack_id,
                    scope=scope,
                    _seen=seen,
                )
                installed.extend(list(result.get("dependencies_installed") or []))
                installed.append(dependency.pack_id)
                continue
            remote_dependency = marketplace.get(dependency.pack_id)
            if remote_dependency is None:
                remote_dependency = await self._resolve_marketplace_pack(dependency.pack_id)
            if remote_dependency is not None:
                result = await self.install(
                    workspace_id=workspace_id,
                    pack_id=dependency.pack_id,
                    scope=scope,
                    _seen=seen,
                )
                installed.extend(list(result.get("dependencies_installed") or []))
                installed.append(dependency.pack_id)
                continue
            if dependency.source_path:
                result = await self.install(
                    workspace_id=workspace_id,
                    source_path=dependency.source_path,
                    scope=scope,
                    _seen=seen,
                )
                installed.extend(list(result.get("dependencies_installed") or []))
                installed.append(str(result.get("pack", {}).get("pack_id") or dependency.pack_id))
                continue
            if dependency.source_url:
                result = await self.install(
                    workspace_id=workspace_id,
                    source_url=dependency.source_url,
                    scope=scope,
                    _seen=seen,
                )
                installed.extend(list(result.get("dependencies_installed") or []))
                installed.append(str(result.get("pack", {}).get("pack_id") or dependency.pack_id))
                continue
            if dependency.optional:
                continue
            raise RuntimeError(f"Missing dependency for {pack.pack_id}: {dependency.pack_id}")
        return list(dict.fromkeys(item for item in installed if item and item != pack.pack_id))

    async def install(
        self,
        *,
        workspace_id: str,
        pack_id: str = "",
        source_path: str = "",
        source_url: str = "",
        scope: str = "user",
        _seen: set[str] | None = None,
    ) -> dict[str, Any]:
        discovered_list, marketplace_list = await self._resolved_catalog_entries(include_marketplace=True)
        discovered = {pack.pack_id: pack for pack in discovered_list}
        marketplace = {str(item.get("pack_id") or ""): item for item in marketplace_list}
        scope = "workspace" if scope == "workspace" else "user"
        target_workspace_id = workspace_id if scope == "workspace" else None
        seen = _seen or set()
        if pack_id:
            pack = discovered.get(pack_id)
            remote = marketplace.get(pack_id)
            if remote is None:
                remote = await self._resolve_marketplace_pack(pack_id)
            if pack is None and remote is None:
                raise RuntimeError(f"Unknown skill pack: {pack_id}")
            if pack is not None:
                dependency_ids = await self._install_pack_dependencies(
                    workspace_id=workspace_id,
                    pack=pack,
                    scope=scope,
                    discovered=discovered,
                    marketplace=marketplace,
                    seen=seen | {pack.pack_id},
                )
                if pack.root_kind == "bundled":
                    row = await self._upsert_row(
                        workspace_id=None,
                        pack=pack,
                        scope="bundled",
                        enabled=True,
                        trusted=True,
                    )
                    return {"pack": row, "action": "enabled_bundled", "dependencies_installed": dependency_ids}
                installed = self._copy_pack(pack.path, scope=scope)
            else:
                install_url = str(remote.get("install_url") or "").strip()
                if not install_url:
                    raise RuntimeError(f"Marketplace pack {pack_id} does not expose an installable archive.")
                unpacked = await self._download_and_unpack_remote(install_url)
                compat = remote.get("compat") if isinstance(remote.get("compat"), dict) else {}
                remote_dir = find_skill_pack_dir(
                    unpacked,
                    pack_id=str(compat.get("local_pack_id") or pack_id),
                    skill_dir=str(compat.get("skill_dir") or ""),
                )
                remote_pack = load_skill_pack(remote_dir, root_kind="user")
                dependency_ids = await self._install_pack_dependencies(
                    workspace_id=workspace_id,
                    pack=remote_pack,
                    scope=scope,
                    discovered=discovered,
                    marketplace=marketplace,
                    seen=seen | {remote_pack.pack_id},
                )
                installed = self._copy_pack(remote_dir, scope=scope)
        else:
            dependency_ids = []
            if source_url:
                unpacked = await self._download_and_unpack_remote(source_url)
                preview = load_skill_pack(unpacked, root_kind="user")
                dependency_ids = await self._install_pack_dependencies(
                    workspace_id=workspace_id,
                    pack=preview,
                    scope=scope,
                    discovered=discovered,
                    marketplace=marketplace,
                    seen=seen | {preview.pack_id},
                )
                installed = self._copy_pack(unpacked, scope=scope)
            else:
                source = Path(source_path).expanduser().resolve()
                if not source.exists():
                    raise FileNotFoundError(f"Skill pack source not found: {source}")
                if source.is_file():
                    unpacked = unpack_skill_archive(source, Path.home() / ".kestrel" / "cache" / "skillpacks")
                    preview = load_skill_pack(unpacked, root_kind="user")
                    dependency_ids = await self._install_pack_dependencies(
                        workspace_id=workspace_id,
                        pack=preview,
                        scope=scope,
                        discovered=discovered,
                        marketplace=marketplace,
                        seen=seen | {preview.pack_id},
                    )
                    installed = self._copy_pack(unpacked, scope=scope)
                else:
                    preview = load_skill_pack(source, root_kind="user")
                    dependency_ids = await self._install_pack_dependencies(
                        workspace_id=workspace_id,
                        pack=preview,
                        scope=scope,
                        discovered=discovered,
                        marketplace=marketplace,
                        seen=seen | {preview.pack_id},
                    )
                    installed = self._copy_pack(source, scope=scope)
        row = await self._upsert_row(
            workspace_id=target_workspace_id,
            pack=installed,
            scope=scope,
            enabled=True,
            trusted=True,
        )
        return {"pack": row, "action": "installed", "dependencies_installed": dependency_ids}

    async def import_pack(self, *, workspace_id: str, source_path: str, scope: str = "user") -> dict[str, Any]:
        return await self.install(workspace_id=workspace_id, source_path=source_path, scope=scope)

    async def _set_enabled(self, workspace_id: str, pack_id: str, enabled: bool) -> dict[str, Any]:
        await self._ensure_tables()
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE agent_skill_packs
                SET enabled = $3, updated_at = now()
                WHERE pack_id = $1 AND removed_at IS NULL AND (workspace_id = $2 OR workspace_id IS NULL)
                RETURNING pack_id, enabled, scope, trusted
                """,
                pack_id,
                workspace_id or None,
                enabled,
            )
        if row is None:
            raise RuntimeError(f"Unknown skill pack: {pack_id}")
        return dict(row)

    async def enable(self, workspace_id: str, pack_id: str) -> dict[str, Any]:
        return {"pack": await self._set_enabled(workspace_id, pack_id, True), "action": "enabled"}

    async def disable(self, workspace_id: str, pack_id: str) -> dict[str, Any]:
        return {"pack": await self._set_enabled(workspace_id, pack_id, False), "action": "disabled"}

    async def remove(self, workspace_id: str, pack_id: str) -> dict[str, Any]:
        await self._ensure_tables()
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT pack_id, scope, source_path
                FROM agent_skill_packs
                WHERE pack_id = $1 AND removed_at IS NULL AND (workspace_id = $2 OR workspace_id IS NULL)
                ORDER BY workspace_id NULLS LAST
                LIMIT 1
                """,
                pack_id,
                workspace_id or None,
            )
            if row is None:
                raise RuntimeError(f"Unknown skill pack: {pack_id}")
            if str(row["scope"] or "") == "bundled":
                await conn.execute(
                    """
                    UPDATE agent_skill_packs
                    SET enabled = false, updated_at = now()
                    WHERE pack_id = $1 AND removed_at IS NULL AND workspace_id IS NULL
                    """,
                    pack_id,
                )
                return {"pack": {"pack_id": pack_id, "scope": "bundled"}, "action": "disabled_bundled"}
            await conn.execute(
                """
                UPDATE agent_skill_packs
                SET enabled = false, removed_at = now(), updated_at = now()
                WHERE pack_id = $1 AND removed_at IS NULL AND (workspace_id = $2 OR workspace_id IS NULL)
                """,
                pack_id,
                workspace_id or None,
            )
        source_path = Path(str(row["source_path"] or "")).expanduser()
        safe_roots = {
            (Path.home() / ".kestrel" / "skills").resolve(),
            self._workspace_skill_dir().resolve(),
        }
        try:
            resolved = source_path.resolve()
        except FileNotFoundError:
            resolved = source_path
        if row["scope"] in {"user", "workspace"} and resolved.exists():
            for safe_root in safe_roots:
                if str(resolved).startswith(str(safe_root)):
                    shutil.rmtree(resolved, ignore_errors=True)
                    break
        return {"pack": {"pack_id": pack_id, "scope": row["scope"]}, "action": "removed"}

    async def enabled_packs(self, workspace_id: str) -> list[SkillPack]:
        discovered = {pack.pack_id: pack for pack in await self._discover()}
        enabled: list[SkillPack] = []
        for row in await self._load_state_rows(workspace_id):
            if not row.get("enabled") or not row.get("trusted"):
                continue
            pack = discovered.get(str(row["pack_id"]))
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

    async def select_packs(self, workspace_id: str, goal: str, *, history: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        enabled = await self.enabled_packs(workspace_id)
        selected = select_skill_packs(enabled, goal, history=history, limit=5)
        selected = expand_pack_dependencies(enabled, selected, limit=15)
        return {
            "snapshot_id": pack_snapshot_id(enabled),
            "packs": [pack.to_public_dict() for pack in selected],
            "prompt_block": build_prompt_block(selected),
        }

    async def auto_connect_selected_mcp(self, workspace_id: str, selected_packs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not self._auto_connect_mcp_enabled():
            return []
        discovered = {pack.pack_id: pack for pack in await self.enabled_packs(workspace_id)}
        from agent.tools.mcp_client import get_mcp_pool

        mcp_pool = get_mcp_pool()
        results: list[dict[str, Any]] = []
        for item in selected_packs:
            if not isinstance(item, dict):
                continue
            pack_id = str(item.get("pack_id") or "").strip().lower()
            pack = discovered.get(pack_id)
            if pack is None:
                continue
            for component in pack.mcp_components():
                payload = component.to_dict()
                transport = str(payload.get("transport") or "stdio").strip().lower() or "stdio"
                server_name = str(payload.get("server_name") or payload.get("name") or pack.pack_id).strip() or pack.pack_id
                command = str(
                    payload.get("server_command")
                    or payload.get("command")
                    or payload.get("server_url")
                    or payload.get("install")
                    or ""
                ).strip()
                if transport != "stdio":
                    results.append(
                        {
                            "pack_id": pack.pack_id,
                            "server_name": server_name,
                            "transport": transport,
                            "connected": False,
                            "error": f"Unsupported MCP transport for auto-connect: {transport}",
                        }
                    )
                    continue
                if not command:
                    results.append(
                        {
                            "pack_id": pack.pack_id,
                            "server_name": server_name,
                            "transport": transport,
                            "connected": False,
                            "error": "Missing server command for MCP recipe.",
                        }
                    )
                    continue
                env = payload.get("env") if isinstance(payload.get("env"), dict) else {}
                result = await mcp_pool.connect(server_name, command, env)
                results.append(
                    {
                        "pack_id": pack.pack_id,
                        "server_name": server_name,
                        "transport": transport,
                        "connected": "error" not in result,
                        "tools": result.get("tools", []),
                        "error": str(result.get("error") or ""),
                    }
                )
        return results

    def _make_pack_tool_handler(self, pack: SkillPack, component: dict[str, Any]):
        entrypoint = str(component.get("entrypoint") or "tool.py").strip() or "tool.py"
        source_path = (pack.path / entrypoint).resolve()

        async def _handler(**kwargs) -> dict[str, Any]:
            if not source_path.exists():
                return {"success": False, "error": f"Pack tool entrypoint not found: {source_path}"}
            code = source_path.read_text(encoding="utf-8")
            try:
                compiled = compile(code, str(source_path), "exec")
            except SyntaxError as exc:
                return {"success": False, "error": f"Skill pack tool syntax error: {exc}"}
            if any(token in code for token in (f"import {name}" for name in BLOCKED_MODULES)):
                return {"success": False, "error": "Skill pack tool imports a blocked module."}
            namespace = dict(SAFE_GLOBALS)
            namespace["args"] = kwargs
            loop = asyncio.get_running_loop()

            def _execute() -> dict[str, Any]:
                exec(compiled, namespace)
                run = namespace.get("run")
                if not callable(run):
                    raise RuntimeError("Pack tool must define run(args)")
                result = run(kwargs)
                if isinstance(result, dict):
                    return result
                return {"success": True, "output": result}

            try:
                return await loop.run_in_executor(None, _execute)
            except Exception as exc:
                return {"success": False, "error": str(exc)}

        return _handler

    async def register_selected_tools(self, registry, workspace_id: str, selected_packs: list[dict[str, Any]]) -> int:
        discovered = {pack.pack_id: pack for pack in await self._discover()}
        registered = 0
        for item in selected_packs:
            if not isinstance(item, dict):
                continue
            pack_id = str(item.get("pack_id") or "").strip().lower()
            pack = discovered.get(pack_id)
            if pack is None:
                continue
            for component in pack.tool_components():
                if component.type != "brain_python_tool":
                    continue
                payload = component.to_dict()
                definition = ToolDefinition(
                    name=str(payload.get("name") or f"pack_{pack.pack_id}"),
                    description=str(payload.get("description") or f"Skill pack tool from {pack.name}"),
                    parameters=dict(payload.get("parameters") or {"type": "object", "properties": {}}),
                    risk_level=RiskLevel(str(payload.get("risk_level") or "medium")),
                    requires_approval=bool(payload.get("approval_required", False)),
                    timeout_seconds=int(payload.get("timeout_seconds") or 30),
                    category="skill",
                    source="skill",
                    scope="workspace",
                    lifecycle_state="approved",
                    use_cases=tuple(pack.use_cases),
                )
                registry.register(definition, self._make_pack_tool_handler(pack, payload))
                registered += 1
        return registered
