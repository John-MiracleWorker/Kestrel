from __future__ import annotations

from . import native_tool_registry_handlers as _native_tool_registry_handlers

globals().update({name: value for name, value in vars(_native_tool_registry_handlers).items() if not name.startswith("__")})

class NativeToolRegistryCustomMixin:
    def _handle_custom_tool_create(self, context: NativeToolContext, arguments: dict[str, Any]) -> NativeExecutionResult:
        name = _sanitize_tool_name(str(arguments.get("name") or "custom_tool"))
        description = str(arguments.get("description") or "").strip()
        runtime = str(arguments.get("runtime") or "python").strip().lower()
        entrypoint = str(arguments.get("entrypoint") or ("tool.py" if runtime == "python" else "tool.sh")).strip()
        input_schema = arguments.get("input_schema")
        if not isinstance(input_schema, dict):
            input_schema = {"type": "object", "properties": {}}
        setup_notes = arguments.get("setup_notes")
        if not isinstance(setup_notes, list):
            setup_notes = []
        files = arguments.get("files")
        if not isinstance(files, dict) or not files:
            files = _default_custom_tool_files(name=name, description=description or name, setup_notes=setup_notes)
        manifest = _custom_tool_manifest_template(
            name=name,
            description=description or name,
            runtime=runtime,
            entrypoint=entrypoint,
            input_schema=input_schema,
            risk_class=str(arguments.get("risk_class") or "mutating"),
            approval_required=bool(arguments.get("approval_required", True)),
            setup_notes=setup_notes,
        )

        if not context.approved:
            return NativeExecutionResult(
                tool_name="custom_tool_create",
                success=False,
                message=f"Approval required to scaffold custom tool {name}",
                risk_class="mutating",
                approval_required=True,
                approval_operation="custom_tool_create",
                approval_payload={
                    "summary": f"Create custom tool {name}",
                    "name": name,
                    "manifest": manifest,
                    "files": files,
                    "arguments": {
                        "name": name,
                        "description": description or name,
                        "runtime": runtime,
                        "entrypoint": entrypoint,
                        "input_schema": input_schema,
                        "risk_class": manifest["risk_class"],
                        "approval_required": manifest["approval_required"],
                        "setup_notes": setup_notes,
                        "files": files,
                    },
                },
            )

        tool_dir = context.paths.tools_dir / name
        tool_dir.mkdir(parents=True, exist_ok=True)
        (tool_dir / "tool.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        for filename, content in files.items():
            file_path = tool_dir / filename
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(str(content), encoding="utf-8")
            if runtime == "shell" and file_path.name.endswith(".sh"):
                file_path.chmod(0o755)
            if runtime == "python" and file_path.name.endswith(".py"):
                file_path.chmod(0o755)

        self.reload_custom_tools()
        artifacts = [self._format_artifact(tool_dir / "tool.json")]
        artifacts.extend(
            self._format_artifact(tool_dir / filename)
            for filename in files
            if (tool_dir / filename).exists()
        )
        note_lines = [f"Custom tool {name} scaffolded at {tool_dir}"]
        if setup_notes:
            note_lines.append("Prerequisites:")
            note_lines.extend(f"- {item}" for item in setup_notes)
        return NativeExecutionResult(
            tool_name="custom_tool_create",
            success=True,
            message="\n".join(note_lines),
            data={"tool_dir": str(tool_dir), "setup_notes": setup_notes, "manifest": manifest},
            artifacts=artifacts,
            risk_class="mutating",
        )

    def _execute_custom_tool(
        self,
        spec: NativeToolSpec,
        context: NativeToolContext,
        arguments: dict[str, Any],
    ) -> NativeExecutionResult:
        tool_dir = context.paths.tools_dir / spec.name
        entrypoint = self._resolve_local_path(spec.entrypoint, workspace_root=tool_dir)
        if not entrypoint.exists():
            return NativeExecutionResult(
                tool_name=spec.name,
                success=False,
                message=f"Custom tool entrypoint not found: {entrypoint}",
                risk_class=spec.risk_class,
            )
        if spec.approval_required and not context.approved:
            return NativeExecutionResult(
                tool_name=spec.name,
                success=False,
                message=f"Approval required to run custom tool {spec.name}",
                risk_class=spec.risk_class,
                approval_required=True,
                approval_operation="custom_tool_run",
                approval_payload={
                    "summary": f"Run custom tool {spec.name}",
                    "arguments": dict(arguments),
                    "tool_name": spec.name,
                },
            )
        if spec.runtime == "python":
            command = [sys.executable, str(entrypoint)]
        elif spec.runtime == "shell":
            command = ["/bin/sh", str(entrypoint)]
        else:
            return NativeExecutionResult(tool_name=spec.name, success=False, message=f"Unsupported custom tool runtime: {spec.runtime}")

        process = subprocess.run(
            command,
            input=json.dumps(arguments),
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(tool_dir),
        )
        stdout = (process.stdout or "").strip()
        stderr = (process.stderr or "").strip()
        data: dict[str, Any] = {}
        message = stdout or stderr or f"{spec.name} exited with {process.returncode}"
        if stdout:
            try:
                parsed = json.loads(stdout)
                if isinstance(parsed, dict):
                    data = parsed.get("data") if isinstance(parsed.get("data"), dict) else parsed
                    message = str(parsed.get("message") or message)
                    success = bool(parsed.get("success", process.returncode == 0))
                    artifacts = parsed.get("artifacts") if isinstance(parsed.get("artifacts"), list) else []
                    return NativeExecutionResult(
                        tool_name=spec.name,
                        success=success,
                        message=message,
                        data=data if isinstance(data, dict) else {"result": data},
                        stdout=_truncate_text(stdout, 8_000),
                        stderr=_truncate_text(stderr, 8_000),
                        exit_code=process.returncode,
                        artifacts=[artifact for artifact in artifacts if isinstance(artifact, dict)],
                        risk_class=spec.risk_class,
                    )
            except Exception:
                pass
        return NativeExecutionResult(
            tool_name=spec.name,
            success=process.returncode == 0,
            message=_truncate_text(message, 8_000),
            stdout=_truncate_text(stdout, 8_000),
            stderr=_truncate_text(stderr, 8_000),
            exit_code=process.returncode,
            artifacts=[],
            risk_class=spec.risk_class,
        )


