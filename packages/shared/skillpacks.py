from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - optional import guard
    yaml = None


_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_PACK_MANIFEST_NAMES = ("skill.yaml", "skill.yml", "skill.json", "manifest.json")
_PRECEDENCE_RANK = {"workspace": 3, "user": 2, "bundled": 1}
_ALWAYS_ON_LIMIT = 5
_AGENT_SKILLS_API_URL = "https://www.agentskills.in/api/skills"
_AGENT_SKILLS_HOSTS = {
    "agentskills.in",
    "www.agentskills.in",
    "skills.sh",
    "www.skills.sh",
}
_AGENT_SKILLS_QUERY_STOP_WORDS = {
    "a",
    "an",
    "and",
    "any",
    "are",
    "as",
    "at",
    "be",
    "browse",
    "can",
    "could",
    "discover",
    "find",
    "for",
    "help",
    "i",
    "in",
    "is",
    "it",
    "list",
    "look",
    "marketplace",
    "me",
    "or",
    "recommend",
    "search",
    "show",
    "skill",
    "skills",
    "suggest",
    "that",
    "the",
    "to",
    "use",
    "with",
    "would",
    "you",
}


def _slugify(value: str) -> str:
    slug = _SLUG_RE.sub("-", str(value or "").strip().lower()).strip("-")
    return slug or "skill-pack"


def _safe_yaml_load(text: str) -> dict[str, Any]:
    if yaml is None:
        raise RuntimeError("PyYAML is required to parse skill pack manifests.")
    loaded = yaml.safe_load(text) or {}
    if not isinstance(loaded, dict):
        raise ValueError("Skill manifest must decode to an object.")
    return loaded


def _read_frontmatter(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    frontmatter = _safe_yaml_load(match.group(1))
    body = text[match.end():]
    return frontmatter, body


def _ensure_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _is_url(value: str) -> bool:
    parsed = urlparse(str(value or "").strip())
    return parsed.scheme in {"http", "https", "file"}


def _normalize_url(base_url: str, value: str) -> str:
    candidate = str(value or "").strip()
    if not candidate:
        return ""
    if _is_url(candidate):
        return candidate
    candidate_path = Path(candidate).expanduser()
    if candidate_path.is_absolute():
        return candidate_path.resolve().as_uri()
    if not base_url:
        return candidate
    base_path = Path(base_url).expanduser()
    if base_path.exists():
        return (base_path.parent / candidate).resolve().as_uri()
    return urljoin(base_url.rstrip("/") + "/", candidate)


def _marketplace_source_kind(source: str) -> tuple[str, str]:
    candidate = str(source or "").strip()
    if not candidate:
        return "catalog", ""
    parsed = urlparse(candidate)
    host = parsed.netloc.lower()
    path = parsed.path.rstrip("/")
    if host in _AGENT_SKILLS_HOSTS:
        if not path or path in {"/", "/docs", "/marketplace"}:
            return "agent_skills_api", _AGENT_SKILLS_API_URL
        if path == "/api/skills":
            return "agent_skills_api", _AGENT_SKILLS_API_URL
    return "catalog", candidate


def _agent_skills_request_url(api_url: str, **params: Any) -> str:
    parsed = urlparse(api_url)
    existing = parse_qs(parsed.query, keep_blank_values=True)
    for key, value in params.items():
        if value is None or value == "":
            continue
        existing[str(key)] = [str(value)]
    query = urlencode({key: values[-1] for key, values in existing.items()}, doseq=False)
    return parsed._replace(query=query).geturl()


def _agent_skills_fetch_json(api_url: str, **params: Any) -> dict[str, Any]:
    request = Request(
        _agent_skills_request_url(api_url, **params),
        headers={
            "Accept": "application/json",
            "User-Agent": "kestrel-skillpacks",
        },
    )
    with urlopen(request) as response:  # nosec B310 - explicit configured marketplace source
        text = response.read().decode("utf-8")
    payload = _safe_structured_load(text)
    skills = payload.get("skills")
    if not isinstance(skills, list):
        raise ValueError(f"Agent Skills API at {api_url} returned an invalid payload.")
    payload["skills"] = skills
    return payload


def _normalize_agent_skills_query(query: str) -> str:
    text = str(query or "").strip()
    if not text:
        return ""
    text = re.sub(r"[\"'`]", " ", text)
    text = re.sub(r"(?i)\b(?:or|and|not)\b", " ", text)
    text = re.sub(r"[\[\]{}()|,:;/\\]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _agent_skills_query_candidates(query: str, *, limit: int = 12) -> list[str]:
    seen: set[str] = set()
    candidates: list[str] = []

    def add(value: str) -> None:
        candidate = re.sub(r"\s+", " ", str(value or "").strip())
        if len(candidate) < 2:
            return
        key = candidate.lower()
        if key in seen:
            return
        seen.add(key)
        candidates.append(candidate)

    raw = re.sub(r"\s+", " ", str(query or "").strip())
    add(raw)
    for phrase in re.findall(r'"([^"]+)"|\'([^\']+)\'', raw):
        add(next((item for item in phrase if item), ""))

    normalized = _normalize_agent_skills_query(raw)
    add(normalized)

    tokens = [
        token
        for token in normalized.lower().split()
        if token and token not in _AGENT_SKILLS_QUERY_STOP_WORDS
    ]
    for size in (2, 3):
        phrases = [" ".join(tokens[index:index + size]) for index in range(len(tokens) - size + 1)]
        for phrase in reversed(phrases):
            add(phrase)
    for token in reversed(tokens):
        if len(token) >= 4:
            add(token)
    return candidates[: max(1, int(limit))]


def _agent_skill_dir_path(entry: dict[str, Any]) -> str:
    compat = entry.get("compat") if isinstance(entry.get("compat"), dict) else {}
    compat_dir = str(compat.get("skill_dir") or "").strip().replace("\\", "/")
    if compat_dir:
        return compat_dir
    path = str(entry.get("path") or "").strip().replace("\\", "/")
    if not path:
        return ""
    if path.lower().endswith("/skill.md"):
        return path[:-len("/SKILL.md")]
    return str(Path(path).parent).replace("\\", "/")


def _agent_skill_local_name(entry: dict[str, Any]) -> str:
    directory = _agent_skill_dir_path(entry)
    if directory:
        return Path(directory).name
    compat = entry.get("compat") if isinstance(entry.get("compat"), dict) else {}
    local_pack_id = str(compat.get("local_pack_id") or "").strip()
    if local_pack_id:
        return local_pack_id
    return str(entry.get("name") or entry.get("skill_name") or "skill").strip() or "skill"


def _agent_skill_branch(entry: dict[str, Any]) -> str:
    github_url = str(entry.get("githubUrl") or entry.get("github_url") or "").strip()
    match = re.search(r"github\.com/[^/]+/[^/]+/tree/([^/]+)/", github_url)
    if match:
        return match.group(1).strip() or "main"
    return str(entry.get("branch") or "main").strip() or "main"


def _agent_skill_repo_full_name(entry: dict[str, Any]) -> str:
    repo_full_name = str(entry.get("repoFullName") or entry.get("repo_full_name") or "").strip()
    if repo_full_name:
        return repo_full_name
    github_url = str(entry.get("githubUrl") or entry.get("github_url") or "").strip()
    match = re.search(r"github\.com/([^/]+/[^/]+)", github_url)
    return match.group(1).strip() if match else ""


def _agent_skill_pack_id(entry: dict[str, Any]) -> str:
    author = _slugify(str(entry.get("author") or "unknown").strip())
    directory = _agent_skill_dir_path(entry).strip("/")
    if directory:
        return f"agentskills:{author}:{directory}"
    return f"agentskills:{author}:{_slugify(_agent_skill_local_name(entry))}"


def _parse_agent_skill_pack_id(pack_id: str) -> tuple[str, str] | None:
    raw = str(pack_id or "").strip()
    match = re.fullmatch(r"agentskills:([^:]+):(.+)", raw)
    if not match:
        return None
    return match.group(1).strip(), match.group(2).strip().strip("/")


def _agent_skill_entry_from_api(item: dict[str, Any], api_url: str) -> dict[str, Any]:
    repo_full_name = _agent_skill_repo_full_name(item)
    branch = _agent_skill_branch(item)
    skill_dir = _agent_skill_dir_path(item)
    skill_path = str(item.get("path") or "").strip().replace("\\", "/")
    local_name = _agent_skill_local_name(item)
    raw_url = (
        str(item.get("rawUrl") or item.get("raw_url") or "").strip()
        or (
            f"https://raw.githubusercontent.com/{repo_full_name}/{branch}/{skill_path}"
            if repo_full_name and skill_path
            else ""
        )
    )
    install_url = (
        f"https://api.github.com/repos/{repo_full_name}/zipball/{branch}"
        if repo_full_name
        else ""
    )
    category = str(item.get("category") or "").strip()
    tags = [category] if category else []
    return {
        "pack_id": _agent_skill_pack_id(item),
        "name": local_name,
        "version": str(item.get("version") or "1.0.0").strip() or "1.0.0",
        "description": str(item.get("description") or "").strip() or f"Marketplace skill pack {local_name}",
        "tags": tags,
        "use_cases": [],
        "permissions": [],
        "components": [],
        "dependencies": [],
        "prompt_preview": "",
        "root_kind": "marketplace",
        "source_type": "agent_skills_api",
        "marketplace_name": "Agent Skills",
        "marketplace_url": api_url,
        "install_url": install_url,
        "manifest_url": raw_url,
        "source_path": "",
        "github_url": str(item.get("githubUrl") or item.get("github_url") or "").strip(),
        "scoped_name": str(item.get("scopedName") or item.get("scoped_name") or "").strip(),
        "author": str(item.get("author") or "").strip(),
        "stars": int(item.get("stars") or 0),
        "forks": int(item.get("forks") or 0),
        "repo_full_name": repo_full_name,
        "path": "",
        "compat": {
            "source_format": "agent_skills_api",
            "marketplace": True,
            "agent_skills": True,
            "branch": branch,
            "skill_path": skill_path,
            "skill_dir": skill_dir,
            "local_pack_id": _slugify(local_name),
            "api_id": str(item.get("id") or "").strip(),
        },
    }


def _agent_skill_exact_match(entry: dict[str, Any], *, author: str = "", skill_dir: str = "", local_pack_id: str = "") -> bool:
    if author and _slugify(str(entry.get("author") or "")) != _slugify(author):
        return False
    if skill_dir:
        return _agent_skill_dir_path(entry).strip("/").lower() == skill_dir.strip("/").lower()
    if local_pack_id:
        return _slugify(_agent_skill_local_name(entry)) == _slugify(local_pack_id)
    return False


def _search_agent_skills_marketplace(api_url: str, query: str, *, author: str = "", limit: int = 50) -> list[dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    for index, candidate in enumerate(_agent_skills_query_candidates(query)):
        payload = _agent_skills_fetch_json(
            api_url,
            search=candidate,
            author=str(author or "").strip() or None,
            limit=max(1, int(limit)),
            offset=0,
        )
        current: list[dict[str, Any]] = []
        for item in payload.get("skills") or []:
            if not isinstance(item, dict):
                continue
            entry = _agent_skill_entry_from_api(item, api_url)
            entries.setdefault(str(entry.get("pack_id") or ""), entry)
            current.append(entry)
        if index == 0 and current:
            break
        if entries and len(entries) >= max(1, int(limit)):
            break
    return list(entries.values())[: max(1, int(limit))]


def _safe_json_load(text: str) -> dict[str, Any]:
    loaded = json.loads(text or "{}")
    if not isinstance(loaded, dict):
        raise ValueError("Structured skill metadata must decode to an object.")
    return loaded


def _safe_structured_load(text: str) -> dict[str, Any]:
    try:
        return _safe_json_load(text)
    except Exception:
        return _safe_yaml_load(text)


def _normalize_component(component: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(component)
    normalized["type"] = str(component.get("type") or "prompt").strip().lower()
    if component.get("always_on") is not None:
        normalized["always_on"] = bool(component.get("always_on"))
    else:
        normalized["always_on"] = False
    normalized["permissions"] = _ensure_list(component.get("permissions"))
    return normalized


@dataclass(frozen=True)
class SkillComponent:
    type: str
    config: dict[str, Any]
    always_on: bool = False
    permissions: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload = dict(self.config)
        payload["type"] = self.type
        payload["always_on"] = self.always_on
        if self.permissions:
            payload["permissions"] = list(self.permissions)
        return payload


@dataclass(frozen=True)
class SkillDependency:
    pack_id: str
    version: str = ""
    optional: bool = False
    source_path: str = ""
    source_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "pack_id": self.pack_id,
            "version": self.version,
            "optional": self.optional,
        }
        if self.source_path:
            payload["source_path"] = self.source_path
        if self.source_url:
            payload["source_url"] = self.source_url
        return payload


@dataclass(frozen=True)
class SkillPack:
    pack_id: str
    name: str
    version: str
    description: str
    path: Path
    root_kind: str
    source_type: str
    tags: tuple[str, ...] = ()
    use_cases: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    components: tuple[SkillComponent, ...] = ()
    dependencies: tuple[SkillDependency, ...] = ()
    compat: dict[str, Any] = field(default_factory=dict)
    manifest: dict[str, Any] = field(default_factory=dict)
    score: float = 0.0

    def prompt_components(self) -> list[SkillComponent]:
        return [component for component in self.components if component.type in {"prompt", "knowledge"}]

    def tool_components(self) -> list[SkillComponent]:
        return [
            component
            for component in self.components
            if component.type in {"native_tool", "brain_python_tool"}
        ]

    def mcp_components(self) -> list[SkillComponent]:
        return [component for component in self.components if component.type == "mcp_recipe"]

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "pack_id": self.pack_id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "path": str(self.path),
            "root_kind": self.root_kind,
            "source_type": self.source_type,
            "tags": list(self.tags),
            "use_cases": list(self.use_cases),
            "permissions": list(self.permissions),
            "components": [component.to_dict() for component in self.components],
            "dependencies": [dependency.to_dict() for dependency in self.dependencies],
            "compat": dict(self.compat),
            "score": self.score,
        }


def _normalize_dependency_payloads(value: Any) -> tuple[SkillDependency, ...]:
    items = value
    if isinstance(value, dict):
        items = value.get("items") or value.get("packs") or []
    if isinstance(items, str):
        items = [items]
    dependencies: list[SkillDependency] = []
    for item in list(items or []):
        if isinstance(item, str):
            pack_id = _slugify(item)
            if pack_id:
                dependencies.append(SkillDependency(pack_id=pack_id))
            continue
        if not isinstance(item, dict):
            continue
        pack_id = _slugify(str(item.get("pack_id") or item.get("id") or item.get("name") or "").strip())
        if not pack_id:
            continue
        dependencies.append(
            SkillDependency(
                pack_id=pack_id,
                version=str(item.get("version") or "").strip(),
                optional=bool(item.get("optional", False)),
                source_path=str(item.get("source_path") or item.get("path") or "").strip(),
                source_url=str(item.get("source_url") or item.get("url") or item.get("archive_url") or "").strip(),
            )
        )
    return tuple(dependencies)


def _infer_manifest_from_skill_md(pack_dir: Path) -> dict[str, Any]:
    skill_md = pack_dir / "SKILL.md"
    if not skill_md.exists():
        raise FileNotFoundError(f"SKILL.md not found under {pack_dir}")
    frontmatter, body = _read_frontmatter(skill_md)
    name = str(frontmatter.get("name") or pack_dir.name).strip()
    description = str(frontmatter.get("description") or "").strip()
    metadata = frontmatter.get("metadata")
    tags = _ensure_list(frontmatter.get("tags"))
    use_cases = _ensure_list(frontmatter.get("use_cases"))
    if isinstance(metadata, dict):
        tags.extend(_ensure_list(metadata.get("tags")))
        use_cases.extend(_ensure_list(metadata.get("use_cases")))
        short_description = str(metadata.get("short-description") or metadata.get("short_description") or "").strip()
        if not description and short_description:
            description = short_description
    if not description:
        first_line = next((line.strip() for line in body.splitlines() if line.strip()), "")
        description = first_line[:240] if first_line else f"Imported SKILL.md pack {name}"
    return {
        "id": _slugify(name),
        "name": name,
        "version": "1.0.0",
        "description": description,
        "tags": list(dict.fromkeys(tags)),
        "use_cases": list(dict.fromkeys(use_cases)),
        "components": [
            {
                "type": "prompt",
                "path": "SKILL.md",
                "always_on": False,
            }
        ],
        "compat": {
            "source_format": "skill_md_only",
            "imported": True,
        },
    }


def _normalize_manifest(pack_dir: Path, root_kind: str, source_type: str, manifest: dict[str, Any]) -> SkillPack:
    raw_id = str(manifest.get("id") or manifest.get("name") or pack_dir.name).strip()
    pack_id = _slugify(raw_id)
    name = str(
        manifest.get("name")
        or manifest.get("display_name")
        or manifest.get("title")
        or pack_dir.name
    ).strip()
    description = str(manifest.get("description") or "").strip()
    version = str(manifest.get("version") or "1.0.0").strip()
    tags = tuple(dict.fromkeys(_ensure_list(manifest.get("tags"))))
    use_cases = tuple(dict.fromkeys(_ensure_list(manifest.get("use_cases"))))

    permissions_value = manifest.get("permissions")
    permissions: tuple[str, ...]
    if isinstance(permissions_value, dict):
        permissions = tuple(
            key
            for key, enabled in permissions_value.items()
            if bool(enabled)
        )
    else:
        permissions = tuple(dict.fromkeys(_ensure_list(permissions_value)))

    components_payload = manifest.get("components")
    components: list[SkillComponent] = []
    if isinstance(components_payload, list):
        for item in components_payload:
            if not isinstance(item, dict):
                continue
            normalized = _normalize_component(item)
            component_type = normalized.pop("type")
            always_on = bool(normalized.pop("always_on", False))
            component_permissions = tuple(dict.fromkeys(_ensure_list(normalized.pop("permissions", []))))
            components.append(
                SkillComponent(
                    type=component_type,
                    config=normalized,
                    always_on=always_on,
                    permissions=component_permissions,
                )
            )

    skill_md = pack_dir / "SKILL.md"
    if skill_md.exists() and not any(component.type == "prompt" for component in components):
        components.append(
            SkillComponent(
                type="prompt",
                config={"path": "SKILL.md"},
                always_on=False,
            )
        )

    dependencies = _normalize_dependency_payloads(
        manifest.get("dependencies") if manifest.get("dependencies") is not None else manifest.get("depends_on")
    )
    compat = manifest.get("compat") if isinstance(manifest.get("compat"), dict) else {}
    return SkillPack(
        pack_id=pack_id,
        name=name or pack_id,
        version=version or "1.0.0",
        description=description or f"Skill pack {name or pack_id}",
        path=pack_dir.resolve(),
        root_kind=root_kind,
        source_type=source_type,
        tags=tags,
        use_cases=use_cases,
        permissions=permissions,
        components=tuple(components),
        dependencies=dependencies,
        compat=dict(compat),
        manifest=dict(manifest),
    )


def load_skill_pack(pack_dir: str | Path, *, root_kind: str = "user") -> SkillPack:
    pack_path = Path(pack_dir).expanduser().resolve()
    if not pack_path.exists() or not pack_path.is_dir():
        raise FileNotFoundError(f"Skill pack directory not found: {pack_path}")

    for manifest_name in _PACK_MANIFEST_NAMES:
        manifest_path = pack_path / manifest_name
        if not manifest_path.exists():
            continue
        if manifest_name.endswith((".yaml", ".yml")):
            manifest = _safe_yaml_load(manifest_path.read_text(encoding="utf-8"))
            return _normalize_manifest(pack_path, root_kind, "manifest", manifest)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest_name == "skill.json":
            manifest = {
                "id": _slugify(str(manifest.get("name") or pack_path.name)),
                "name": str(manifest.get("display_name") or manifest.get("name") or pack_path.name),
                "version": str(manifest.get("version") or "1.0.0"),
                "description": str(manifest.get("description") or ""),
                "tags": _ensure_list(manifest.get("category")),
                "use_cases": _ensure_list(manifest.get("tools")) + _ensure_list(manifest.get("functions")),
                "components": manifest.get("components") or [],
                "compat": {
                    "source_format": "skill_json",
                    "legacy_manifest": True,
                },
                **{key: value for key, value in manifest.items() if key not in {"display_name", "tools", "functions"}},
            }
            return _normalize_manifest(pack_path, root_kind, "skill_json", manifest)
        return _normalize_manifest(
            pack_path,
            root_kind,
            "manifest_json",
            {
                **manifest,
                "compat": {
                    "source_format": "manifest_json",
                    "legacy_manifest": True,
                },
            },
        )

    inferred = _infer_manifest_from_skill_md(pack_path)
    return _normalize_manifest(pack_path, root_kind, "skill_md_only", inferred)


def discover_skill_packs(roots: dict[str, str | Path]) -> list[SkillPack]:
    discovered: dict[str, SkillPack] = {}
    for root_kind, root_path in roots.items():
        base = Path(root_path).expanduser()
        if not base.exists() or not base.is_dir():
            continue
        for pack_dir in sorted(path for path in base.iterdir() if path.is_dir()):
            try:
                pack = load_skill_pack(pack_dir, root_kind=root_kind)
            except Exception:
                continue
            current = discovered.get(pack.pack_id)
            if current is None or _PRECEDENCE_RANK.get(pack.root_kind, 0) >= _PRECEDENCE_RANK.get(current.root_kind, 0):
                discovered[pack.pack_id] = pack
    return sorted(discovered.values(), key=lambda pack: (pack.root_kind, pack.name.lower()))


def resolve_skill_prompt(pack: SkillPack) -> str:
    sections: list[str] = []
    for component in pack.prompt_components():
        relative_path = str(component.config.get("path") or "SKILL.md").strip() or "SKILL.md"
        prompt_path = (pack.path / relative_path).resolve()
        if not prompt_path.exists():
            continue
        text = prompt_path.read_text(encoding="utf-8")
        if prompt_path.name.lower() == "skill.md":
            _frontmatter, body = _read_frontmatter(prompt_path)
            text = body.strip()
        sections.append(text.strip())
    return "\n\n".join(section for section in sections if section).strip()


def build_prompt_block(selected_packs: list[SkillPack], *, max_chars: int = 20_000) -> str:
    sections: list[str] = []
    total = 0
    for pack in selected_packs[:_ALWAYS_ON_LIMIT]:
        prompt_text = resolve_skill_prompt(pack)
        if not prompt_text:
            continue
        section = f"## Skill Pack: {pack.name}\n{prompt_text.strip()}"
        remaining = max_chars - total
        if remaining <= 0:
            break
        if len(section) > remaining:
            section = section[:remaining].rstrip() + "\n..."
        sections.append(section)
        total += len(section)
    if not sections:
        return ""
    return "\n\n".join(
        [
            "## Active Skill Packs",
            "Use the following pack-specific instructions only when they are relevant to the current task.",
            *sections,
        ]
    )


def _pack_search_text(pack: SkillPack) -> str:
    parts = [
        pack.pack_id,
        pack.name,
        pack.description,
        " ".join(pack.tags),
        " ".join(pack.use_cases),
        " ".join(component.type for component in pack.components),
        " ".join(dependency.pack_id for dependency in pack.dependencies),
    ]
    try:
        prompt_text = resolve_skill_prompt(pack)
    except Exception:
        prompt_text = ""
    if prompt_text:
        parts.append(prompt_text[:4_000])
    return "\n".join(part for part in parts if part)


def score_skill_pack(pack: SkillPack, text: str) -> float:
    query = str(text or "").strip().lower()
    if not query:
        return 0.0
    haystack = _pack_search_text(pack).lower()
    score = 0.0
    for token in {token for token in re.findall(r"[a-z0-9_./-]+", query) if len(token) > 2}:
        if token == pack.pack_id:
            score += 8.0
        elif token in pack.name.lower():
            score += 6.0
        elif token in haystack:
            score += 2.0
    if query in pack.name.lower():
        score += 5.0
    if query in pack.description.lower():
        score += 3.0
    return score


def select_skill_packs(
    packs: list[SkillPack],
    goal: str,
    *,
    history: list[dict[str, Any]] | None = None,
    limit: int = 5,
) -> list[SkillPack]:
    history_text = " ".join(str(item.get("content") or "") for item in list(history or [])[-4:] if isinstance(item, dict))
    query = f"{goal}\n{history_text}".strip()
    scored: list[SkillPack] = []
    always_on: list[SkillPack] = []
    for pack in packs:
        if any(component.always_on for component in pack.components):
            always_on.append(pack)
            continue
        score = score_skill_pack(pack, query)
        if score <= 0:
            continue
        scored.append(
            SkillPack(
                pack_id=pack.pack_id,
                name=pack.name,
                version=pack.version,
            description=pack.description,
            path=pack.path,
            root_kind=pack.root_kind,
            source_type=pack.source_type,
            tags=pack.tags,
            use_cases=pack.use_cases,
            permissions=pack.permissions,
            components=pack.components,
            dependencies=pack.dependencies,
            compat=pack.compat,
            manifest=pack.manifest,
            score=score,
        )
        )
    ordered = always_on + sorted(scored, key=lambda pack: (-pack.score, pack.name.lower()))
    return ordered[: max(1, limit)]


def expand_pack_dependencies(
    packs: list[SkillPack],
    selected: list[SkillPack],
    *,
    include_optional: bool = False,
    limit: int = 25,
) -> list[SkillPack]:
    by_id = {pack.pack_id: pack for pack in packs}
    ordered: list[SkillPack] = []
    seen: set[str] = set()

    def _append(pack: SkillPack) -> None:
        if pack.pack_id in seen or len(ordered) >= max(1, limit):
            return
        seen.add(pack.pack_id)
        ordered.append(pack)
        for dependency in pack.dependencies:
            if dependency.optional and not include_optional:
                continue
            child = by_id.get(dependency.pack_id)
            if child is not None:
                _append(child)

    for pack in selected:
        _append(pack)
    return ordered[: max(1, limit)]


def pack_snapshot_id(packs: list[SkillPack]) -> str:
    digest = hashlib.sha256()
    for pack in sorted(packs, key=lambda item: item.pack_id):
        digest.update(pack.pack_id.encode("utf-8"))
        digest.update(pack.version.encode("utf-8"))
        digest.update(str(pack.path).encode("utf-8"))
        try:
            stat = pack.path.stat()
        except FileNotFoundError:
            continue
        digest.update(str(int(stat.st_mtime_ns)).encode("utf-8"))
    return digest.hexdigest()[:16]


def unpack_skill_archive(archive_path: str | Path, destination_root: str | Path) -> Path:
    archive = Path(archive_path).expanduser().resolve()
    destination = Path(destination_root).expanduser().resolve()
    if not archive.exists():
        raise FileNotFoundError(f"Archive not found: {archive}")
    if not shutil.which("unzip") and archive.suffix == ".zip":
        pass
    destination.mkdir(parents=True, exist_ok=True)
    temp_dir = destination / f".unpack-{archive.stem}"
    if temp_dir.exists():
        shutil.rmtree(temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)
    shutil.unpack_archive(str(archive), str(temp_dir))
    candidates = [path for path in temp_dir.iterdir() if path.is_dir()]
    return candidates[0] if len(candidates) == 1 else temp_dir


def download_remote_skill_archive(source_url: str, destination_root: str | Path) -> Path:
    destination = Path(destination_root).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    parsed = urlparse(source_url)
    suffix = Path(parsed.path).suffix or ".zip"
    handle, temp_name = tempfile.mkstemp(prefix="skillpack-", suffix=suffix, dir=str(destination))
    os.close(handle)
    target = Path(temp_name)
    try:
        with urlopen(source_url) as response, target.open("wb") as fh:  # nosec B310 - controlled by explicit install action
            fh.write(response.read())
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return target


def _marketplace_entry_search_text(entry: dict[str, Any]) -> str:
    parts = [
        str(entry.get("pack_id") or ""),
        str(entry.get("name") or ""),
        str(entry.get("description") or ""),
        str(entry.get("author") or ""),
        str(entry.get("scoped_name") or entry.get("scopedName") or ""),
        str(entry.get("repo_full_name") or entry.get("repoFullName") or ""),
        " ".join(_ensure_list(entry.get("tags"))),
        " ".join(_ensure_list(entry.get("use_cases"))),
        " ".join(
            str(item.get("pack_id") or "")
            for item in list(entry.get("dependencies") or [])
            if isinstance(item, dict)
        ),
    ]
    compat = entry.get("compat") if isinstance(entry.get("compat"), dict) else {}
    if compat:
        parts.extend(
            [
                str(compat.get("local_pack_id") or ""),
                str(compat.get("skill_dir") or ""),
                str(compat.get("skill_path") or ""),
            ]
        )
    prompt_preview = str(entry.get("prompt_preview") or "").strip()
    if prompt_preview:
        parts.append(prompt_preview[:4_000])
    return "\n".join(part for part in parts if part)


def score_skill_candidate(candidate: SkillPack | dict[str, Any], text: str) -> float:
    if isinstance(candidate, SkillPack):
        return score_skill_pack(candidate, text)
    query = str(text or "").strip().lower()
    if not query:
        return 0.0
    haystack = _marketplace_entry_search_text(candidate).lower()
    score = 0.0
    for token in {token for token in re.findall(r"[a-z0-9_./-]+", query) if len(token) > 2}:
        if token == str(candidate.get("pack_id") or "").lower():
            score += 8.0
        elif token in str(candidate.get("name") or "").lower():
            score += 6.0
        elif token in haystack:
            score += 2.0
    if query in str(candidate.get("name") or "").lower():
        score += 5.0
    if query in str(candidate.get("description") or "").lower():
        score += 3.0
    return score


def fetch_marketplace_catalog(source: str) -> dict[str, Any]:
    text: str
    if _is_url(source):
        with urlopen(source) as response:  # nosec B310 - explicit user/configured catalog source
            text = response.read().decode("utf-8")
    else:
        text = Path(source).expanduser().read_text(encoding="utf-8")
    catalog = _safe_structured_load(text)
    packs = catalog.get("packs")
    if not isinstance(packs, list):
        raise ValueError(f"Marketplace catalog at {source} does not contain a packs list.")
    catalog["packs"] = packs
    return catalog


def discover_marketplace_packs(urls: list[str]) -> list[dict[str, Any]]:
    discovered: dict[str, dict[str, Any]] = {}
    for source in [str(item).strip() for item in urls if str(item).strip()]:
        source_kind, url = _marketplace_source_kind(source)
        if source_kind != "catalog":
            continue
        try:
            catalog = fetch_marketplace_catalog(url)
        except Exception:
            continue
        catalog_name = str(catalog.get("name") or "Skill Marketplace").strip()
        for item in catalog.get("packs") or []:
            if not isinstance(item, dict):
                continue
            pack_id = _slugify(str(item.get("pack_id") or item.get("id") or item.get("name") or "").strip())
            if not pack_id or pack_id in discovered:
                continue
            dependencies = _normalize_dependency_payloads(
                item.get("dependencies") if item.get("dependencies") is not None else item.get("depends_on")
            )
            compat = item.get("compat") if isinstance(item.get("compat"), dict) else {}
            entry = {
                "pack_id": pack_id,
                "name": str(item.get("name") or pack_id).strip() or pack_id,
                "version": str(item.get("version") or "1.0.0").strip(),
                "description": str(item.get("description") or "").strip() or f"Marketplace skill pack {pack_id}",
                "tags": _ensure_list(item.get("tags")),
                "use_cases": _ensure_list(item.get("use_cases")),
                "permissions": _ensure_list(item.get("permissions")),
                "components": list(item.get("components") or []),
                "dependencies": [dependency.to_dict() for dependency in dependencies],
                "prompt_preview": str(item.get("prompt_preview") or item.get("prompt") or "").strip(),
                "root_kind": "marketplace",
                "source_type": "marketplace",
                "marketplace_name": catalog_name,
                "marketplace_url": url,
                "install_url": _normalize_url(url, str(item.get("install_url") or item.get("archive_url") or item.get("download_url") or item.get("source_url") or "")),
                "manifest_url": _normalize_url(url, str(item.get("manifest_url") or "")),
                "source_path": str(item.get("source_path") or ""),
                "compat": {
                    **compat,
                    "source_format": "marketplace_catalog",
                    "marketplace": True,
                },
            }
            discovered[pack_id] = entry
    return sorted(discovered.values(), key=lambda item: str(item.get("name") or "").lower())


def search_marketplace_packs(urls: list[str], query: str, *, limit: int = 50) -> list[dict[str, Any]]:
    discovered: dict[str, dict[str, Any]] = {}
    catalog_sources: list[str] = []
    for source in [str(item).strip() for item in urls if str(item).strip()]:
        source_kind, normalized = _marketplace_source_kind(source)
        if source_kind == "agent_skills_api":
            try:
                for item in _search_agent_skills_marketplace(normalized, query, limit=limit):
                    discovered[str(item.get("pack_id") or "")] = item
            except Exception:
                continue
        else:
            catalog_sources.append(normalized)

    for item in discover_marketplace_packs(catalog_sources):
        discovered.setdefault(str(item.get("pack_id") or ""), item)

    return sorted(discovered.values(), key=lambda item: str(item.get("name") or "").lower())


def resolve_marketplace_pack(urls: list[str], pack_id: str) -> dict[str, Any] | None:
    target = str(pack_id or "").strip()
    if not target:
        return None

    for item in discover_marketplace_packs(urls):
        if str(item.get("pack_id") or "").lower() == target.lower():
            return item

    parsed = _parse_agent_skill_pack_id(target)
    for source in [str(item).strip() for item in urls if str(item).strip()]:
        source_kind, normalized = _marketplace_source_kind(source)
        if source_kind != "agent_skills_api":
            continue
        try:
            if parsed:
                author, skill_dir = parsed
                query = Path(skill_dir).name or skill_dir
                matches = _search_agent_skills_marketplace(normalized, query, author=author, limit=100)
                for item in matches:
                    if _agent_skill_exact_match(item, author=author, skill_dir=skill_dir):
                        return item
            else:
                matches = _search_agent_skills_marketplace(normalized, target, limit=100)
                exact = [
                    item
                    for item in matches
                    if _agent_skill_exact_match(
                        item,
                        local_pack_id=target,
                    )
                ]
                if exact:
                    exact.sort(key=lambda item: (-float(item.get("stars") or 0), str(item.get("name") or "").lower()))
                    return exact[0]
        except Exception:
            continue
    return None


def find_skill_pack_dir(root_dir: str | Path, *, pack_id: str = "", skill_dir: str = "") -> Path:
    root = Path(root_dir).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise FileNotFoundError(f"Skill pack directory not found: {root}")

    normalized_dir = str(skill_dir or "").strip().replace("\\", "/").strip("/")
    if normalized_dir:
        for skill_md in root.rglob("SKILL.md"):
            candidate_dir = skill_md.parent
            relative_dir = candidate_dir.relative_to(root).as_posix()
            if relative_dir == normalized_dir or relative_dir.endswith("/" + normalized_dir):
                return candidate_dir

    normalized_pack_id = str(pack_id or "").strip().lower()
    if normalized_pack_id:
        for skill_md in root.rglob("SKILL.md"):
            candidate_dir = skill_md.parent
            if candidate_dir.name.lower() == normalized_pack_id:
                return candidate_dir
            try:
                candidate_pack = load_skill_pack(candidate_dir, root_kind="user")
            except Exception:
                continue
            if candidate_pack.pack_id.lower() == normalized_pack_id:
                return candidate_dir

    raise FileNotFoundError(
        f"Could not find a skill pack directory under {root}"
        + (f" for path {normalized_dir}" if normalized_dir else "")
        + (f" matching {pack_id}" if pack_id else "")
    )


def write_inferred_manifest(pack_dir: str | Path) -> Path:
    pack_path = Path(pack_dir).expanduser().resolve()
    manifest = _infer_manifest_from_skill_md(pack_path)
    manifest_path = pack_path / "skill.yaml"
    if yaml is None:
        raise RuntimeError("PyYAML is required to write inferred skill manifests.")
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    return manifest_path


def default_pack_roots(*, repo_root: str | Path, kestrel_home: str | Path | None = None, workspace_root: str | Path | None = None) -> dict[str, Path]:
    repo = Path(repo_root).resolve()
    home = Path(kestrel_home or os.getenv("KESTREL_HOME") or "~/.kestrel").expanduser()
    workspace = Path(workspace_root or Path.cwd()).resolve()
    return {
        "bundled": repo / "skills",
        "user": home / "skills",
        "workspace": workspace / ".kestrel" / "skills",
    }


__all__ = [
    "SkillDependency",
    "SkillComponent",
    "SkillPack",
    "build_prompt_block",
    "download_remote_skill_archive",
    "default_pack_roots",
    "discover_skill_packs",
    "discover_marketplace_packs",
    "expand_pack_dependencies",
    "fetch_marketplace_catalog",
    "find_skill_pack_dir",
    "load_skill_pack",
    "pack_snapshot_id",
    "resolve_marketplace_pack",
    "score_skill_candidate",
    "score_skill_pack",
    "search_marketplace_packs",
    "select_skill_packs",
    "unpack_skill_archive",
    "write_inferred_manifest",
]
