from __future__ import annotations

from . import native_models as _native_models

globals().update({name: value for name, value in vars(_native_models).items() if not name.startswith("__")})

class NativeToolRegistryCore:
    def __init__(
        self,
        *,
        paths: KestrelPaths,
        config: dict[str, Any],
        runtime_policy: RuntimePolicy,
        vector_store: VectorMemoryStore | None = None,
        workspace_root: Path | None = None,
    ) -> None:
        self.paths = paths
        self.config = config
        self.runtime_policy = runtime_policy
        self.vector_store = vector_store
        self.workspace_root = (workspace_root or Path.cwd()).resolve()
        self._specs: dict[str, NativeToolSpec] = {}
        self._aliases: dict[str, str] = {}
        self._builtin_handlers: dict[str, Callable[[NativeToolContext, dict[str, Any]], NativeExecutionResult]] = {
            "write_file": self._handle_write_file,
            "create_file": self._handle_write_file,
            "append_file": self._handle_append_file,
            "read_file": self._handle_read_file,
            "read_many_files": self._handle_read_many_files,
            "list_directory": self._handle_list_directory,
            "find_files": self._handle_find_files,
            "search_files": self._handle_search_files,
            "run_command": self._handle_run_command,
            "run_python": self._handle_run_python,
            "memory_search": self._handle_memory_search,
            "fetch_url": self._handle_fetch_url,
            "generate_image": self._handle_generate_image,
            "take_screenshot": self._handle_take_screenshot,
            "custom_tool_create": self._handle_custom_tool_create,
        }
        for spec in self._builtin_tool_specs():
            self._register_spec(spec)
        self.reload_custom_tools()

    def _builtin_tool_specs(self) -> list[NativeToolSpec]:
        return [
            NativeToolSpec(
                name="write_file",
                aliases=("create_file",),
                description="Create or overwrite a local text file.",
                category="file",
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Path to the file."},
                        "content": {"type": "string", "description": "UTF-8 text content to write."},
                    },
                    "required": ["path", "content"],
                },
                risk_class="mutating",
                approval_required=True,
            ),
            NativeToolSpec(
                name="append_file",
                description="Append UTF-8 text to an existing file or create it if missing.",
                category="file",
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                },
                risk_class="mutating",
                approval_required=True,
            ),
            NativeToolSpec(
                name="read_file",
                description="Read the contents of a local text file.",
                category="file",
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "max_chars": {"type": "integer", "default": 12000},
                    },
                    "required": ["path"],
                },
            ),
            NativeToolSpec(
                name="read_many_files",
                description="Read multiple local text files in one call.",
                category="file",
                input_schema={
                    "type": "object",
                    "properties": {
                        "paths": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Up to 20 file paths.",
                        },
                        "max_chars": {"type": "integer", "default": 8000},
                    },
                    "required": ["paths"],
                },
            ),
            NativeToolSpec(
                name="list_directory",
                description="List files and subdirectories under a local directory.",
                category="file",
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "limit": {"type": "integer", "default": 100},
                    },
                    "required": ["path"],
                },
            ),
            NativeToolSpec(
                name="find_files",
                description="Find files by glob pattern under a local root directory.",
                category="file",
                input_schema={
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string", "description": "Glob such as README* or *.py."},
                        "path": {"type": "string", "description": "Root directory to search."},
                        "limit": {"type": "integer", "default": 50},
                    },
                    "required": ["pattern"],
                },
            ),
            NativeToolSpec(
                name="search_files",
                description="Search local files for matching text.",
                category="file",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "path": {"type": "string"},
                        "limit": {"type": "integer", "default": 50},
                    },
                    "required": ["query"],
                },
            ),
            NativeToolSpec(
                name="run_command",
                description="Run a local shell command. Read-only commands run automatically; mutating commands require approval.",
                category="system",
                input_schema={
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "cwd": {"type": "string"},
                        "timeout_seconds": {"type": "integer", "default": 30},
                    },
                    "required": ["command"],
                },
            ),
            NativeToolSpec(
                name="run_python",
                description="Run a local Python snippet with JSON stdin. Requires approval.",
                category="system",
                input_schema={
                    "type": "object",
                    "properties": {
                        "code": {"type": "string"},
                        "stdin_json": {
                            "type": "object",
                            "description": "Optional JSON payload delivered on stdin.",
                        },
                    },
                    "required": ["code"],
                },
                risk_class="mutating",
                approval_required=True,
            ),
            NativeToolSpec(
                name="memory_search",
                description="Search Kestrel's local markdown memory index.",
                category="memory",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "namespace": {"type": "string", "default": "*"},
                        "limit": {"type": "integer", "default": 5},
                    },
                    "required": ["query"],
                },
            ),
            NativeToolSpec(
                name="fetch_url",
                description="Fetch a web page over HTTP(S) and return truncated text content.",
                category="web",
                input_schema={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "max_chars": {"type": "integer", "default": 12000},
                    },
                    "required": ["url"],
                },
            ),
            NativeToolSpec(
                name="generate_image",
                description="Generate an AI image or video using the configured SwarmUI GPU server.",
                category="media",
                input_schema={
                    "type": "object",
                    "properties": {
                        "prompt": {"type": "string"},
                        "negative_prompt": {"type": "string"},
                        "width": {"type": "integer"},
                        "height": {"type": "integer"},
                        "media_type": {"type": "string", "enum": ["image", "video"]},
                    },
                    "required": ["prompt"],
                },
            ),
            NativeToolSpec(
                name="take_screenshot",
                description="Capture the current desktop screen and save it to the local artifact store.",
                category="desktop",
                input_schema={
                    "type": "object",
                    "properties": {
                        "send_to_telegram": {"type": "boolean"},
                        "caption": {"type": "string"},
                    },
                },
            ),
            NativeToolSpec(
                name="custom_tool_create",
                description="Scaffold a reusable native custom tool under ~/.kestrel/tools. Requires approval.",
                category="custom",
                input_schema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "runtime": {"type": "string", "enum": ["python", "shell"]},
                        "entrypoint": {"type": "string"},
                        "input_schema": {"type": "object"},
                        "risk_class": {"type": "string"},
                        "approval_required": {"type": "boolean"},
                        "setup_notes": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "files": {
                            "type": "object",
                            "description": "Mapping of filename to file contents.",
                        },
                    },
                    "required": ["name", "description", "runtime", "entrypoint", "input_schema", "files"],
                },
                risk_class="mutating",
                approval_required=True,
            ),
        ]

    def _register_spec(self, spec: NativeToolSpec) -> None:
        self._specs[spec.name] = spec
        for alias in spec.aliases:
            self._aliases[alias] = spec.name

    def categories(self) -> tuple[str, ...]:
        seen: list[str] = []
        for spec in self._specs.values():
            if spec.category not in seen:
                seen.append(spec.category)
        return tuple(seen)

    def resolve_name(self, name: str) -> str | None:
        if name in self._specs:
            return name
        return self._aliases.get(name)

    def get(self, name: str) -> NativeToolSpec | None:
        canonical = self.resolve_name(name)
        return self._specs.get(canonical) if canonical else None

    def list_tools(self, categories: tuple[str, ...] | list[str] | None = None) -> list[NativeToolSpec]:
        allowed = {item.strip().lower() for item in (categories or []) if str(item).strip()}
        specs = list(self._specs.values())
        if not allowed:
            return specs
        return [spec for spec in specs if spec.category in allowed]

    def list_openai_tools(self, categories: tuple[str, ...] | list[str] | None = None) -> list[dict[str, Any]]:
        return [spec.to_openai_tool() for spec in self.list_tools(categories)]

    def reload_custom_tools(self) -> None:
        for name in list(self._specs):
            if self._specs[name].runtime != "builtin":
                self._specs.pop(name, None)
        self._aliases = {
            alias: name
            for name, spec in self._specs.items()
            for alias in spec.aliases
        }

        if not self.paths.tools_dir.exists():
            return

        for tool_dir in sorted(self.paths.tools_dir.iterdir()):
            manifest_path = tool_dir / "tool.json"
            if not tool_dir.is_dir() or not manifest_path.exists():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except Exception as exc:
                LOGGER.warning("Failed to load custom tool manifest %s: %s", manifest_path, exc)
                continue

            try:
                spec = NativeToolSpec(
                    name=_sanitize_tool_name(str(manifest["name"])),
                    description=str(manifest["description"]),
                    category="custom",
                    input_schema=dict(manifest["input_schema"]),
                    risk_class=str(manifest.get("risk_class") or "mutating"),
                    approval_required=bool(manifest.get("approval_required", False)),
                    runtime=str(manifest["runtime"]),
                    entrypoint=str(manifest["entrypoint"]),
                    setup_notes=list(manifest.get("setup_notes") or []),
                )
            except Exception as exc:
                LOGGER.warning("Invalid custom tool manifest %s: %s", manifest_path, exc)
                continue
            self._register_spec(spec)

    def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        task_id: str = "",
        approved: bool = False,
    ) -> NativeExecutionResult:
        canonical = self.resolve_name(name)
        if not canonical:
            return NativeExecutionResult(
                tool_name=name,
                success=False,
                message=f"Unknown tool: {name}",
                risk_class="read_only",
            )

        spec = self._specs[canonical]
        context = NativeToolContext(
            paths=self.paths,
            config=self.config,
            runtime_policy=self.runtime_policy,
            vector_store=self.vector_store,
            task_id=task_id,
            workspace_root=self.workspace_root,
            approved=approved,
        )

        if spec.runtime == "builtin":
            handler = self._builtin_handlers[canonical]
            return handler(context, dict(arguments))
        return self._execute_custom_tool(spec, context, dict(arguments))

    def _resolve_local_path(self, raw_path: str | None, *, workspace_root: Path) -> Path:
        candidate = Path(str(raw_path or "").strip() or ".").expanduser()
        if not candidate.is_absolute():
            candidate = (workspace_root / candidate).resolve()
        return candidate

    def _format_artifact(self, path: Path, artifact_type: str = "file") -> dict[str, Any]:
        return {
            "type": artifact_type,
            "path": str(path),
            "name": path.name,
            "size_bytes": path.stat().st_size if path.exists() else 0,
        }

