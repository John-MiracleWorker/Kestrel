from __future__ import annotations

from . import native_tool_registry_core as _native_tool_registry_core

globals().update({name: value for name, value in vars(_native_tool_registry_core).items() if not name.startswith("__")})

class NativeToolRegistryHandlersMixin:
    def _native_send_file_to_telegram(self, file_path: Path, *, caption: str = "") -> tuple[bool, str]:
        from .native_chat_tools import _send_file_to_telegram

        return _send_file_to_telegram(file_path, caption=caption)

    def _native_resolve_telegram_delivery_targets(self) -> tuple[str, str]:
        from .native_chat_tools import _resolve_telegram_delivery_targets

        return _resolve_telegram_delivery_targets()

    def _native_capture_screenshot_to_file(self, output_path: Path) -> None:
        from .native_chat_tools import _capture_screenshot_to_file

        _capture_screenshot_to_file(output_path)

    def _handle_write_file(self, context: NativeToolContext, arguments: dict[str, Any]) -> NativeExecutionResult:
        path = self._resolve_local_path(arguments.get("path"), workspace_root=context.workspace_root)
        content = str(arguments.get("content") or "")
        if not context.approved:
            return NativeExecutionResult(
                tool_name="write_file",
                success=False,
                message=f"Approval required to write {path}",
                risk_class="mutating",
                approval_required=True,
                approval_operation="file_write",
                approval_payload={
                    "summary": f"Write {len(content)} characters to {path}",
                    "path": str(path),
                    "content_preview": content[:500],
                    "arguments": {"path": str(path), "content": content},
                },
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return NativeExecutionResult(
            tool_name="write_file",
            success=True,
            message=f"Wrote {len(content)} characters to {path}",
            data={"path": str(path), "bytes_written": len(content.encode('utf-8'))},
            artifacts=[self._format_artifact(path)],
            risk_class="mutating",
        )

    def _handle_append_file(self, context: NativeToolContext, arguments: dict[str, Any]) -> NativeExecutionResult:
        path = self._resolve_local_path(arguments.get("path"), workspace_root=context.workspace_root)
        content = str(arguments.get("content") or "")
        if not context.approved:
            return NativeExecutionResult(
                tool_name="append_file",
                success=False,
                message=f"Approval required to append to {path}",
                risk_class="mutating",
                approval_required=True,
                approval_operation="file_write",
                approval_payload={
                    "summary": f"Append {len(content)} characters to {path}",
                    "path": str(path),
                    "content_preview": content[:500],
                    "arguments": {"path": str(path), "content": content},
                },
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(content)
        return NativeExecutionResult(
            tool_name="append_file",
            success=True,
            message=f"Appended {len(content)} characters to {path}",
            data={"path": str(path), "bytes_appended": len(content.encode('utf-8'))},
            artifacts=[self._format_artifact(path)],
            risk_class="mutating",
        )

    def _handle_read_file(self, context: NativeToolContext, arguments: dict[str, Any]) -> NativeExecutionResult:
        path = self._resolve_local_path(arguments.get("path"), workspace_root=context.workspace_root)
        if not path.exists():
            return NativeExecutionResult(tool_name="read_file", success=False, message=f"File not found: {path}")
        content = path.read_text(encoding="utf-8", errors="replace")
        max_chars = max(1, int(arguments.get("max_chars") or 12_000))
        truncated = _truncate_text(content, max_chars)
        return NativeExecutionResult(
            tool_name="read_file",
            success=True,
            message=truncated,
            data={"path": str(path), "content": truncated, "truncated": truncated != content},
            artifacts=[self._format_artifact(path)],
        )

    def _handle_read_many_files(self, context: NativeToolContext, arguments: dict[str, Any]) -> NativeExecutionResult:
        paths = arguments.get("paths") or []
        max_chars = max(1, int(arguments.get("max_chars") or 8_000))
        files: list[dict[str, Any]] = []
        for raw_path in list(paths)[:20]:
            result = self._handle_read_file(
                context,
                {"path": raw_path, "max_chars": max_chars},
            )
            files.append(result.to_dict())
        return NativeExecutionResult(
            tool_name="read_many_files",
            success=True,
            message=f"Read {len(files)} file(s).",
            data={"files": files},
            artifacts=[
                artifact
                for item in files
                for artifact in item.get("artifacts", [])
                if isinstance(artifact, dict)
            ],
        )

    def _handle_list_directory(self, context: NativeToolContext, arguments: dict[str, Any]) -> NativeExecutionResult:
        path = self._resolve_local_path(arguments.get("path"), workspace_root=context.workspace_root)
        if not path.exists():
            return NativeExecutionResult(tool_name="list_directory", success=False, message=f"Directory not found: {path}")
        if not path.is_dir():
            return NativeExecutionResult(tool_name="list_directory", success=False, message=f"Not a directory: {path}")
        limit = max(1, int(arguments.get("limit") or 100))
        items = []
        for entry in sorted(path.iterdir(), key=lambda item: item.name.lower())[:limit]:
            items.append(
                {
                    "name": entry.name,
                    "path": str(entry),
                    "type": "directory" if entry.is_dir() else "file",
                }
            )
        return NativeExecutionResult(
            tool_name="list_directory",
            success=True,
            message=f"Listed {len(items)} item(s) under {path}",
            data={"path": str(path), "items": items},
        )

    def _handle_find_files(self, context: NativeToolContext, arguments: dict[str, Any]) -> NativeExecutionResult:
        pattern = str(arguments.get("pattern") or "").strip()
        search_root = self._resolve_local_path(arguments.get("path"), workspace_root=context.workspace_root)
        limit = max(1, int(arguments.get("limit") or 50))
        if not pattern:
            return NativeExecutionResult(tool_name="find_files", success=False, message="pattern is required")
        if not search_root.exists():
            return NativeExecutionResult(tool_name="find_files", success=False, message=f"Search root not found: {search_root}")
        matches: list[str] = []
        for candidate in search_root.rglob("*"):
            if fnmatch.fnmatch(candidate.name, pattern):
                matches.append(str(candidate))
                if len(matches) >= limit:
                    break
        return NativeExecutionResult(
            tool_name="find_files",
            success=True,
            message=f"Found {len(matches)} file(s) matching {pattern}",
            data={"pattern": pattern, "path": str(search_root), "matches": matches},
            artifacts=[{"type": "match_list", "path": item} for item in matches],
        )

    def _handle_search_files(self, context: NativeToolContext, arguments: dict[str, Any]) -> NativeExecutionResult:
        query = str(arguments.get("query") or "").strip()
        search_root = self._resolve_local_path(arguments.get("path"), workspace_root=context.workspace_root)
        limit = max(1, int(arguments.get("limit") or 50))
        if not query:
            return NativeExecutionResult(tool_name="search_files", success=False, message="query is required")
        if not search_root.exists():
            return NativeExecutionResult(tool_name="search_files", success=False, message=f"Search root not found: {search_root}")

        matches: list[dict[str, Any]] = []
        try:
            result = subprocess.run(
                [
                    "rg",
                    "-n",
                    "--hidden",
                    "--glob",
                    "!.git",
                    "--glob",
                    "!node_modules",
                    "--max-count",
                    str(limit),
                    query,
                    str(search_root),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode in {0, 1}:
                for line in result.stdout.splitlines():
                    if len(matches) >= limit:
                        break
                    path_text, line_no, content = (line.split(":", 2) + ["", ""])[:3]
                    matches.append(
                        {
                            "path": path_text,
                            "line": int(line_no) if str(line_no).isdigit() else 0,
                            "content": content,
                        }
                    )
        except FileNotFoundError:
            for file_path in search_root.rglob("*"):
                if not file_path.is_file():
                    continue
                try:
                    with file_path.open("r", encoding="utf-8", errors="replace") as handle:
                        for line_number, line in enumerate(handle, start=1):
                            if query.lower() in line.lower():
                                matches.append(
                                    {
                                        "path": str(file_path),
                                        "line": line_number,
                                        "content": line.rstrip(),
                                    }
                                )
                                if len(matches) >= limit:
                                    break
                    if len(matches) >= limit:
                        break
                except Exception:
                    continue

        return NativeExecutionResult(
            tool_name="search_files",
            success=True,
            message=f"Found {len(matches)} match(es) for {query}",
            data={"query": query, "path": str(search_root), "matches": matches},
        )

    def _handle_run_command(self, context: NativeToolContext, arguments: dict[str, Any]) -> NativeExecutionResult:
        command = str(arguments.get("command") or "").strip()
        if not command:
            return NativeExecutionResult(tool_name="run_command", success=False, message="command is required")
        decision = context.runtime_policy.evaluate_command(command)
        if not decision["allowed"]:
            return NativeExecutionResult(
                tool_name="run_command",
                success=False,
                message=f"Blocked by native runtime policy: {command}",
                risk_class=decision["risk_class"],
            )
        if decision["approval_required"] and not context.approved:
            return NativeExecutionResult(
                tool_name="run_command",
                success=False,
                message=f"Approval required for command: {command}",
                risk_class=decision["risk_class"],
                approval_required=True,
                approval_operation="shell_command",
                approval_payload={
                    "summary": f"Run shell command: {command}",
                    "command": command,
                    "arguments": {
                        "command": command,
                        "cwd": arguments.get("cwd"),
                        "timeout_seconds": int(arguments.get("timeout_seconds") or 30),
                    },
                },
            )
        cwd = self._resolve_local_path(arguments.get("cwd"), workspace_root=context.workspace_root)
        timeout_seconds = max(1, int(arguments.get("timeout_seconds") or 30))
        started = time.time()
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            cwd=str(cwd),
        )
        duration_ms = int((time.time() - started) * 1000)
        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()
        message = stdout or stderr or f"Command exited with {result.returncode}"
        return NativeExecutionResult(
            tool_name="run_command",
            success=result.returncode == 0,
            message=_truncate_text(message, 8_000),
            stdout=_truncate_text(stdout, 8_000),
            stderr=_truncate_text(stderr, 8_000),
            exit_code=result.returncode,
            risk_class=decision["risk_class"],
            data={
                "command": command,
                "cwd": str(cwd),
                "duration_ms": duration_ms,
                "exit_code": result.returncode,
            },
        )

    def _handle_run_python(self, context: NativeToolContext, arguments: dict[str, Any]) -> NativeExecutionResult:
        code = str(arguments.get("code") or "")
        stdin_json = arguments.get("stdin_json")
        if not code:
            return NativeExecutionResult(tool_name="run_python", success=False, message="code is required")
        if not context.approved:
            return NativeExecutionResult(
                tool_name="run_python",
                success=False,
                message="Approval required for Python execution.",
                risk_class="mutating",
                approval_required=True,
                approval_operation="python_run",
                approval_payload={
                    "summary": "Run a Python snippet locally",
                    "code_preview": code[:500],
                    "arguments": {
                        "code": code,
                        "stdin_json": stdin_json if isinstance(stdin_json, dict) else {},
                    },
                },
            )
        started = time.time()
        process = subprocess.run(
            [sys.executable, "-c", code],
            input=json.dumps(stdin_json or {}),
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(context.workspace_root),
        )
        duration_ms = int((time.time() - started) * 1000)
        stdout = (process.stdout or "").strip()
        stderr = (process.stderr or "").strip()
        return NativeExecutionResult(
            tool_name="run_python",
            success=process.returncode == 0,
            message=_truncate_text(stdout or stderr or f"Python exited with {process.returncode}", 8_000),
            stdout=_truncate_text(stdout, 8_000),
            stderr=_truncate_text(stderr, 8_000),
            exit_code=process.returncode,
            risk_class="mutating",
            data={"duration_ms": duration_ms, "exit_code": process.returncode},
        )

    def _handle_memory_search(self, context: NativeToolContext, arguments: dict[str, Any]) -> NativeExecutionResult:
        if context.vector_store is None:
            return NativeExecutionResult(tool_name="memory_search", success=False, message="Memory search is unavailable.")
        query = str(arguments.get("query") or "").strip()
        namespace = str(arguments.get("namespace") or "*").strip()
        limit = max(1, int(arguments.get("limit") or 5))
        hits = context.vector_store.search_text(namespace=namespace or "*", query=query, limit=limit)
        return NativeExecutionResult(
            tool_name="memory_search",
            success=True,
            message=f"Found {len(hits)} memory hit(s) for {query}",
            data={"query": query, "namespace": namespace or "*", "hits": hits},
        )

    def _handle_fetch_url(self, context: NativeToolContext, arguments: dict[str, Any]) -> NativeExecutionResult:
        if httpx is None:
            return NativeExecutionResult(tool_name="fetch_url", success=False, message="httpx is required for fetch_url.")
        url = str(arguments.get("url") or "").strip()
        if not url:
            return NativeExecutionResult(tool_name="fetch_url", success=False, message="url is required")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return NativeExecutionResult(tool_name="fetch_url", success=False, message=f"Unsupported URL scheme: {parsed.scheme or 'missing'}")
        hostname = (parsed.hostname or "").lower()
        if hostname in {"localhost", "127.0.0.1", "::1"} and not bool(context.config.get("runtime", {}).get("allow_loopback_http", False)):
            return NativeExecutionResult(tool_name="fetch_url", success=False, message=f"Loopback HTTP is disabled by native config: {url}")
        max_chars = max(1, int(arguments.get("max_chars") or 12_000))
        try:
            response = httpx.get(url, timeout=30, follow_redirects=True)
            response.raise_for_status()
        except Exception as exc:
            return NativeExecutionResult(tool_name="fetch_url", success=False, message=f"Failed to fetch {url}: {exc}")
        body = _truncate_text(response.text, max_chars)
        return NativeExecutionResult(
            tool_name="fetch_url",
            success=True,
            message=body,
            data={
                "url": url,
                "status_code": response.status_code,
                "content_type": response.headers.get("content-type", ""),
                "body": body,
            },
        )

    def _reload_lmstudio_models(self, lmstudio_url: str, model_ids: list[str]) -> bool:
        import httpx as _httpx

        if not model_ids:
            return True
        for model_id in model_ids:
            try:
                _httpx.post(f"{lmstudio_url}/api/v1/models/load", json={"model": model_id}, timeout=300)
            except Exception:
                pass
        for _wait in range(30):
            time.sleep(5)
            try:
                test_resp = _httpx.post(
                    f"{lmstudio_url}/v1/chat/completions",
                    json={
                        "model": model_ids[0],
                        "messages": [{"role": "user", "content": "hi"}],
                        "max_tokens": 1,
                    },
                    timeout=30,
                )
                if test_resp.status_code == 200:
                    return True
            except Exception:
                pass
        return False

    def _slugify_media_name(self, value: str, default: str = "generated") -> str:
        slug = re.sub(r"[^A-Za-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
        if not slug:
            return default
        return slug[:48]

    def _render_svg_to_png(self, svg_path: Path, png_path: Path) -> None:
        png_path.parent.mkdir(parents=True, exist_ok=True)
        sips_result = subprocess.run(
            ["sips", "-s", "format", "png", str(svg_path), "--out", str(png_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if sips_result.returncode == 0 and png_path.exists():
            return

        qlmanage_result = subprocess.run(
            ["qlmanage", "-t", "-s", "2048", "-o", str(png_path.parent), str(svg_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        quicklook_candidates = (
            png_path.parent / f"{svg_path.stem}.png",
            png_path.parent / f"{svg_path.name}.png",
        )
        for candidate in quicklook_candidates:
            if candidate.exists():
                if candidate != png_path:
                    png_path.write_bytes(candidate.read_bytes())
                    candidate.unlink()
                return
        stderr = (sips_result.stderr or qlmanage_result.stderr or "").strip()
        raise RuntimeError(stderr or "SVG rendering failed with both sips and qlmanage.")

    def _handle_generate_image(self, context: NativeToolContext, arguments: dict[str, Any]) -> NativeExecutionResult:
        import httpx as _httpx

        prompt_text = str(arguments.get("prompt") or "")
        negative = str(arguments.get("negative_prompt") or "")
        width = int(arguments.get("width", 1024) or 1024)
        height = int(arguments.get("height", 1024) or 1024)
        media_type = str(arguments.get("media_type", "image") or "image")
        send_to_telegram = bool(arguments.get("send_to_telegram", False))

        swarm_ip = os.getenv("SWARM_HOST_IP", "192.168.1.19")
        swarm_port = os.getenv("SWARM_PORT", "7801")
        swarm_base = f"http://{swarm_ip}:{swarm_port}"
        swarm_model = os.getenv("KESTREL_SWARM_MODEL", "Flux/flux1-dev-fp8.safetensors")
        swarm_video_model = os.getenv(
            "KESTREL_SWARM_VIDEO_MODEL",
            "wan2.2_t2v_low_noise_14B_fp8_scaled.safetensors",
        )
        lmstudio_url = os.getenv("LMSTUDIO_BASE_URL", f"http://{swarm_ip}:1234")
        output_dir = context.paths.artifacts_dir / "media"
        output_dir.mkdir(parents=True, exist_ok=True)

        try:
            models_resp = _httpx.get(f"{lmstudio_url}/api/v1/models", timeout=10)
            models_resp.raise_for_status()
            models_data = models_resp.json().get("models", [])
        except Exception as exc:
            return NativeExecutionResult(
                tool_name="generate_image",
                success=False,
                message=f"Could not inspect LM Studio before media generation: {exc}",
            )

        model_ids = [m.get("key", "") for m in models_data if m.get("key")]
        instance_ids = [
            inst.get("id", "")
            for model_info in models_data
            for inst in model_info.get("loaded_instances", [])
            if inst.get("id")
        ]
        unload_errors: list[str] = []
        for instance_id in instance_ids:
            try:
                unload_resp = _httpx.post(
                    f"{lmstudio_url}/api/v1/models/unload",
                    json={"instance_id": instance_id},
                    timeout=15,
                )
                if unload_resp.status_code >= 400:
                    unload_errors.append(f"{instance_id}: {unload_resp.status_code} {unload_resp.text[:160]}")
            except Exception as exc:
                unload_errors.append(f"{instance_id}: {exc}")

        vram_clear = False
        for _retry in range(10):
            time.sleep(4)
            try:
                check = _httpx.get(f"{lmstudio_url}/api/v1/models", timeout=10)
                check.raise_for_status()
                all_models = check.json().get("models", [])
                loaded_count = sum(
                    len(model_info.get("loaded_instances", []))
                    for model_info in all_models
                    if model_info.get("type") == "llm"
                )
                if loaded_count == 0:
                    vram_clear = True
                    break
            except Exception:
                pass
        if not vram_clear:
            reload_ok = self._reload_lmstudio_models(lmstudio_url, model_ids)
            detail = unload_errors[0] if unload_errors else "LM Studio kept one or more LLM instances loaded."
            return NativeExecutionResult(
                tool_name="generate_image",
                success=False,
                message=(
                    "Could not free LM Studio VRAM safely for media generation. "
                    f"{detail} LLM {'reloaded' if reload_ok else 'reload failed'}."
                ),
                metadata={
                    "reason": "vram_not_cleared",
                    "unload_errors": unload_errors,
                    "reload_ok": reload_ok,
                },
            )

        time.sleep(3)

        try:
            session_resp = _httpx.post(f"{swarm_base}/API/GetNewSession", json={}, timeout=15)
            if session_resp.status_code != 200:
                raise RuntimeError(f"SwarmUI session failed (status {session_resp.status_code})")
            session_id = session_resp.json().get("session_id", "")
            if not session_id:
                raise RuntimeError("SwarmUI returned no session_id")
        except Exception as exc:
            reload_ok = self._reload_lmstudio_models(lmstudio_url, model_ids)
            return NativeExecutionResult(
                tool_name="generate_image",
                success=False,
                message=f"Could not start the SwarmUI media session: {exc}. LLM {'reloaded' if reload_ok else 'reload failed'}.",
                metadata={"reason": "swarm_session_failed", "reload_ok": reload_ok},
            )

        is_video = media_type == "video"
        payload: dict[str, Any] = {
            "session_id": session_id,
            "prompt": prompt_text,
            "negativeprompt": negative,
            "images": 1,
            "steps": 30,
            "width": width,
            "height": height,
            "cfgscale": 1.0,
            "seed": -1,
            "model": swarm_model,
        }
        if is_video:
            payload.update(
                {
                    "videomodel": swarm_video_model,
                    "videoframes": 17,
                    "videosteps": 15,
                    "videocfg": 3.0,
                    "videofps": 24,
                    "videoformat": "mp4",
                }
            )

        try:
            gen_resp = _httpx.post(
                f"{swarm_base}/API/GenerateText2Image",
                json=payload,
                timeout=1800 if is_video else 300,
            )
        except Exception as exc:
            reload_ok = self._reload_lmstudio_models(lmstudio_url, model_ids)
            return NativeExecutionResult(
                tool_name="generate_image",
                success=False,
                message=f"SwarmUI media generation failed: {exc}. LLM {'reloaded' if reload_ok else 'reload failed'}.",
                metadata={"reason": "generation_request_failed", "reload_ok": reload_ok},
            )

        generation_error = None
        data = {}
        if gen_resp.status_code != 200:
            generation_error = f"SwarmUI returned {gen_resp.status_code}: {gen_resp.text[:300]}"
        else:
            data = gen_resp.json()
            if data.get("error"):
                generation_error = f"SwarmUI API error: {data['error']}"

        saved_paths: list[Path] = []
        if not generation_error:
            result_images = data.get("images", [])
            if not result_images:
                generation_error = "SwarmUI returned no images"
            else:
                for relative_url in result_images:
                    image_url = f"{swarm_base}/{relative_url}"
                    try:
                        image_resp = _httpx.get(image_url, timeout=60)
                        if image_resp.status_code != 200:
                            continue
                        content_bytes = image_resp.content
                        extension = ".png"
                        if content_bytes[:4] == b"\x89PNG":
                            extension = ".png"
                        elif content_bytes[:3] == b"\xff\xd8\xff":
                            extension = ".jpg"
                        elif content_bytes[:4] == b"RIFF":
                            extension = ".webp"
                        elif b"ftyp" in content_bytes[:12]:
                            extension = ".mp4"
                        elif content_bytes[:4] == b"\x1aE\xdf\xa3":
                            extension = ".webm"
                        file_path = output_dir / f"kestrel_{uuid.uuid4().hex[:8]}_{int(time.time())}{extension}"
                        file_path.write_bytes(content_bytes)
                        saved_paths.append(file_path)
                    except Exception:
                        continue

        try:
            for _round in range(5):
                free_resp = _httpx.post(f"{swarm_base}/API/FreeBackendMemory", json={"session_id": session_id}, timeout=15)
                freed = free_resp.json().get("count", 0) if free_resp.status_code == 200 else 0
                if freed == 0:
                    break
                time.sleep(2)
            time.sleep(8)
        except Exception:
            time.sleep(8)

        reload_ok = self._reload_lmstudio_models(lmstudio_url, model_ids)
        if generation_error:
            return NativeExecutionResult(
                tool_name="generate_image",
                success=False,
                message=f"{generation_error}. LLM {'reloaded' if reload_ok else 'reload failed'}.",
                artifacts=[self._format_artifact(path, "media") for path in saved_paths],
                data={"saved_paths": [str(path) for path in saved_paths]},
                metadata={"reason": "generation_failed", "reload_ok": reload_ok},
            )
        if not saved_paths:
            return NativeExecutionResult(
                tool_name="generate_image",
                success=False,
                message=f"Generated media could not be downloaded. LLM {'reloaded' if reload_ok else 'reload failed'}.",
                metadata={"reason": "download_failed", "reload_ok": reload_ok},
            )

        sent_to_telegram = False
        if send_to_telegram:
            tg_token, tg_chat = self._native_resolve_telegram_delivery_targets()
            if tg_token and tg_chat:
                for file_path in saved_paths:
                    sent, _delivery = self._native_send_file_to_telegram(file_path, caption=prompt_text[:1024])
                    if sent:
                        sent_to_telegram = True

        saved_path_strings = [str(path) for path in saved_paths]
        note = "" if reload_ok else " WARNING: LLM reload failed."
        if sent_to_telegram:
            note += " Sent to Telegram."
        return NativeExecutionResult(
            tool_name="generate_image",
            success=True,
            message=(
                f"Generated {len(saved_paths)} {media_type}(s).{note}\n"
                + "\n".join(saved_path_strings)
            ),
            data={"saved_paths": saved_path_strings, "sent_to_telegram": sent_to_telegram},
            artifacts=[self._format_artifact(path, "media") for path in saved_paths],
            metadata={"reload_ok": reload_ok},
        )

    def _handle_render_svg_asset(self, context: NativeToolContext, arguments: dict[str, Any]) -> NativeExecutionResult:
        svg_content = str(arguments.get("svg_content") or "").strip()
        if not svg_content:
            return NativeExecutionResult(
                tool_name="render_svg_asset",
                success=False,
                message="svg_content is required",
            )
        if "<svg" not in svg_content.lower():
            return NativeExecutionResult(
                tool_name="render_svg_asset",
                success=False,
                message="svg_content must contain SVG markup.",
            )

        prompt = str(arguments.get("prompt") or "").strip()
        base_name = self._slugify_media_name(arguments.get("base_name") or prompt or "generated-svg", "generated-svg")
        send_to_telegram = bool(arguments.get("send_to_telegram", False))
        caption = str(arguments.get("caption") or prompt or "Kestrel SVG render").strip()

        output_dir = context.paths.artifacts_dir / "media"
        output_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{base_name}-{int(time.time())}-{uuid.uuid4().hex[:8]}"
        svg_path = output_dir / f"{stem}.svg"
        png_path = output_dir / f"{stem}.png"

        try:
            svg_path.write_text(svg_content, encoding="utf-8")
            self._render_svg_to_png(svg_path, png_path)
        except Exception as exc:
            return NativeExecutionResult(
                tool_name="render_svg_asset",
                success=False,
                message=f"SVG render failed: {exc}",
                artifacts=[self._format_artifact(svg_path, "svg")] if svg_path.exists() else [],
            )

        sent_to_telegram = False
        delivery_note = ""
        if send_to_telegram:
            sent_to_telegram, delivery_note = self._native_send_file_to_telegram(
                png_path,
                caption=caption[:1024],
            )

        message = f"Rendered the SVG to PNG.\n{svg_path}\n{png_path}"
        if send_to_telegram:
            message = f"{message}\n{delivery_note}"
        return NativeExecutionResult(
            tool_name="render_svg_asset",
            success=True,
            message=message,
            data={
                "svg_path": str(svg_path),
                "png_path": str(png_path),
                "sent_to_telegram": sent_to_telegram,
            },
            artifacts=[
                self._format_artifact(svg_path, "svg"),
                self._format_artifact(png_path, "image"),
            ],
        )

    def _handle_take_screenshot(self, context: NativeToolContext, arguments: dict[str, Any]) -> NativeExecutionResult:
        output_dir = context.paths.artifacts_dir / "media"
        output_dir.mkdir(parents=True, exist_ok=True)
        file_path = output_dir / f"kestrel_screenshot_{int(time.time())}_{uuid.uuid4().hex[:8]}.png"
        send_to_telegram = bool(arguments.get("send_to_telegram", False))
        caption = str(arguments.get("caption") or "Kestrel screenshot")
        try:
            self._native_capture_screenshot_to_file(file_path)
        except Exception as exc:
            return NativeExecutionResult(
                tool_name="take_screenshot",
                success=False,
                message=f"Screenshot capture failed: {exc}",
            )
        delivery_note = "Telegram delivery was not requested."
        if send_to_telegram:
            sent, delivery_note = self._native_send_file_to_telegram(file_path, caption=caption)
            if not sent:
                delivery_note = f"{delivery_note} The screenshot is still available locally."
        return NativeExecutionResult(
            tool_name="take_screenshot",
            success=True,
            message=f"Screenshot captured successfully.\nSaved to: {file_path}\n{delivery_note}",
            data={"path": str(file_path), "sent_to_telegram": send_to_telegram},
            artifacts=[self._format_artifact(file_path, "image")],
        )
