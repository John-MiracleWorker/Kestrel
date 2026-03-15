from __future__ import annotations

from . import native_agent as _native_agent

globals().update({name: value for name, value in vars(_native_agent).items() if not name.startswith("__")})

############################################################
# Tool definitions for LLM function calling
############################################################

CHAT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "create_file",
            "description": "Create or overwrite a file at the specified path with the given content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute file path, e.g. /Users/tiuni/Desktop/hello.txt",
                    },
                    "content": {
                        "type": "string",
                        "description": "The text content to write to the file.",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file at the specified path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute file path to read.",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "List files and subdirectories in the specified directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute directory path to list.",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Execute a shell command and return its output. Use for tasks like installing packages, checking system info, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to execute.",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_image",
            "description": "Generate an AI image or video using the remote SwarmUI GPU server. Always use this tool when the user asks for an image, picture, photo, artwork, illustration, or video. Returns the file path of the generated media.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Detailed text description of the image or video to generate. Be descriptive and specific.",
                    },
                    "negative_prompt": {
                        "type": "string",
                        "description": "Things to avoid in the generation (e.g. 'blurry, low quality, deformed').",
                    },
                    "width": {
                        "type": "integer",
                        "description": "Output width in pixels. Default 1024.",
                    },
                    "height": {
                        "type": "integer",
                        "description": "Output height in pixels. Default 1024.",
                    },
                    "media_type": {
                        "type": "string",
                        "description": "Type of media to generate: 'image' or 'video'. Default 'image'.",
                        "enum": ["image", "video"],
                    },
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "take_screenshot",
            "description": (
                "Capture the current desktop screen and save it to Kestrel's local media artifacts. "
                "Use this when the user asks you to take, capture, grab, or send a screenshot."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "send_to_telegram": {
                        "type": "boolean",
                        "description": "If true, also send the screenshot to the configured Telegram chat when credentials are available.",
                    },
                    "caption": {
                        "type": "string",
                        "description": "Optional caption to include when sending the screenshot to Telegram.",
                    },
                },
                "required": [],
            },
        },
    },
]

CHAT_TOOL_CATEGORIES: dict[str, tuple[str, ...]] = {
    "file": ("create_file", "read_file", "list_directory"),
    "system": ("run_command",),
    "media": ("generate_image",),
    "desktop": ("take_screenshot",),
}


def get_chat_tools(enabled_categories: list[str] | tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    registry = NativeToolRegistry(
        paths=resolve_paths(),
        config=DEFAULT_CONFIG,
        runtime_policy=NativeRuntimePolicy(DEFAULT_CONFIG),
    )
    return registry.list_openai_tools(enabled_categories)


def resolve_chat_tool_categories(config: dict[str, Any]) -> tuple[str, ...]:
    registry = NativeToolRegistry(
        paths=resolve_paths(),
        config=config,
        runtime_policy=NativeRuntimePolicy(config),
    )
    known_categories = registry.categories()
    configured = (config.get("tools") or {}).get("enabled_categories")
    if not isinstance(configured, list):
        return known_categories

    resolved: list[str] = []
    seen: set[str] = set()
    for raw in configured:
        category = str(raw or "").strip().lower()
        if category in known_categories and category not in seen:
            resolved.append(category)
            seen.add(category)
    return tuple(resolved) or known_categories


def describe_chat_tool_categories(enabled_categories: list[str] | tuple[str, ...] | None = None) -> str:
    registry = NativeToolRegistry(
        paths=resolve_paths(),
        config=DEFAULT_CONFIG,
        runtime_policy=NativeRuntimePolicy(DEFAULT_CONFIG),
    )
    categories = tuple(enabled_categories or registry.categories())
    lines: list[str] = []
    for category in categories:
        tool_names = tuple(tool.name for tool in registry.list_tools((category,)))
        if not tool_names:
            continue
        lines.append(f"- {category}: {', '.join(tool_names)}")
    return "\n".join(lines)


def _resolve_telegram_delivery_targets() -> tuple[str, str]:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if token and chat_id:
        return token, chat_id

    state_path = Path.home() / ".kestrel" / "state" / "gateway-channels.json"
    if not state_path.exists():
        return token, chat_id

    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return token, chat_id

    telegram = payload.get("telegram") or {}
    config = telegram.get("config") or {}
    state = telegram.get("state") or {}
    mappings = state.get("mappings") or []

    resolved_token = token or str(config.get("token") or "").strip()
    resolved_chat_id = chat_id
    if not resolved_chat_id and mappings:
        candidate = mappings[0]
        if isinstance(candidate, dict):
            resolved_chat_id = str(candidate.get("chatId") or "").strip()

    return resolved_token, resolved_chat_id


def _send_file_to_telegram(file_path: Path, caption: str = "") -> tuple[bool, str]:
    token, chat_id = _resolve_telegram_delivery_targets()
    if not token or not chat_id:
        return False, "Telegram delivery is not configured."

    mime_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    is_video = file_path.suffix.lower() in {".mp4", ".webm", ".mov"}
    method = "sendVideo" if is_video else "sendDocument"
    field_name = "video" if is_video else "document"
    telegram_url = f"https://api.telegram.org/bot{token}/{method}"

    try:
        import httpx as _httpx

        with file_path.open("rb") as handle:
            files = {
                field_name: (
                    file_path.name,
                    handle,
                    mime_type,
                )
            }
            data = {
                "chat_id": chat_id,
            }
            if caption:
                data["caption"] = caption[:1024]
            response = _httpx.post(telegram_url, data=data, files=files, timeout=60)
        if response.status_code == 200:
            return True, f"Sent to Telegram chat {chat_id}."
        return False, f"Telegram upload failed with status {response.status_code}: {response.text[:200]}"
    except Exception as exc:
        return False, f"Telegram upload failed: {exc}"


def _capture_screenshot_to_file(output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if sys.platform == "darwin":
        result = subprocess.run(
            ["screencapture", "-x", str(output_path)],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            raise RuntimeError(
                "macOS screencapture failed. "
                f"{stderr or 'Grant Screen Recording permission to the terminal/daemon if needed.'}"
            )
    else:
        try:
            from PIL import ImageGrab  # type: ignore

            image = ImageGrab.grab()
            image.save(output_path)
        except Exception as exc:
            raise RuntimeError(
                "Screenshot capture is only implemented for macOS or environments with Pillow ImageGrab."
            ) from exc

    if not output_path.exists() or output_path.stat().st_size == 0:
        raise RuntimeError("Screenshot capture completed but no image file was produced.")


def _execute_tool(name: str, arguments: dict[str, Any]) -> str:
    """Execute a tool call and return the result as a string."""
    import subprocess
    import uuid as _uuid
    import time as _time

    try:
        if name == "create_file":
            file_path = Path(arguments["path"]).expanduser()
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(arguments["content"], encoding="utf-8")
            return f"File created successfully at {file_path} ({len(arguments['content'])} bytes)"

        elif name == "read_file":
            file_path = Path(arguments["path"]).expanduser()
            if not file_path.exists():
                return f"Error: File not found: {file_path}"
            content = file_path.read_text(encoding="utf-8")
            # Truncate very large files
            if len(content) > 10000:
                content = content[:10000] + f"\n... (truncated, {len(content)} total bytes)"
            return content

        elif name == "list_directory":
            dir_path = Path(arguments["path"]).expanduser()
            if not dir_path.exists():
                return f"Error: Directory not found: {dir_path}"
            if not dir_path.is_dir():
                return f"Error: Not a directory: {dir_path}"
            entries = sorted(dir_path.iterdir())
            lines = []
            for entry in entries[:100]:  # Cap at 100 entries
                kind = "dir" if entry.is_dir() else "file"
                lines.append(f"  [{kind}] {entry.name}")
            result = f"Contents of {dir_path} ({len(entries)} items):\n" + "\n".join(lines)
            if len(entries) > 100:
                result += f"\n  ... and {len(entries) - 100} more"
            return result

        elif name == "run_command":
            command = arguments["command"]
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(Path.home()),
            )
            output = ""
            if result.stdout:
                output += result.stdout.strip()
            if result.stderr:
                if output:
                    output += "\n"
                output += f"[stderr] {result.stderr.strip()}"
            if not output:
                output = "(no output)"
            return f"Exit code: {result.returncode}\n{output}"

        elif name == "generate_image":
            import httpx as _httpx

            prompt_text = arguments.get("prompt", "")
            negative = arguments.get("negative_prompt", "")
            width = int(arguments.get("width", 1024))
            height = int(arguments.get("height", 1024))
            media_type = arguments.get("media_type", "image")

            # SwarmUI config from environment or defaults
            swarm_ip = os.getenv("SWARM_HOST_IP", "192.168.1.19")
            swarm_port = os.getenv("SWARM_PORT", "7801")
            swarm_base = f"http://{swarm_ip}:{swarm_port}"
            swarm_model = os.getenv(
                "KESTREL_SWARM_MODEL",
                "Flux/flux1-dev-fp8.safetensors",
            )
            swarm_video_model = os.getenv(
                "KESTREL_SWARM_VIDEO_MODEL",
                "wan2.2_t2v_low_noise_14B_fp8_scaled.safetensors",
            )

            # LM Studio URL (same remote machine as SwarmUI)
            lmstudio_url = os.getenv("LMSTUDIO_BASE_URL", f"http://{swarm_ip}:1234")

            # Output directory
            output_dir = Path.home() / ".kestrel" / "artifacts" / "media"
            output_dir.mkdir(parents=True, exist_ok=True)

            # ── Phase 1: Unload LLM from VRAM ──────────────────────
            try:
                # Use the LM Studio /api/v1/models endpoint to get loaded instances
                models_resp = _httpx.get(f"{lmstudio_url}/api/v1/models", timeout=10)
                models_data = models_resp.json().get("models", [])
                model_ids = [m.get("key", "") for m in models_data if m.get("key")]
                instance_ids = []
                for m in models_data:
                    for inst in m.get("loaded_instances", []):
                        iid = inst.get("id", "")
                        if iid:
                            instance_ids.append(iid)
                # Unload each loaded instance
                for iid in instance_ids:
                    _httpx.post(
                        f"{lmstudio_url}/api/v1/models/unload",
                        json={"instance_id": iid},
                        timeout=15,
                    )
            except Exception as e:
                return f"Error: Failed to unload LLM from VRAM: {e}"

            # ── Phase 2: Verify VRAM cleared ────────────────────────
            vram_clear = False
            for _retry in range(10):
                _time.sleep(4)
                try:
                    check = _httpx.get(f"{lmstudio_url}/api/v1/models", timeout=10)
                    all_models = check.json().get("models", [])
                    # Check if any LLM models still have loaded instances
                    loaded_count = sum(
                        len(m.get("loaded_instances", []))
                        for m in all_models
                        if m.get("type") == "llm"
                    )
                    if loaded_count == 0:
                        vram_clear = True
                        break
                except Exception:
                    pass
            if not vram_clear:
                # Try to reload and abort
                for mid in model_ids:
                    try:
                        _httpx.post(
                            f"{lmstudio_url}/api/v1/models/load",
                            json={"model": mid},
                            timeout=30,
                        )
                    except Exception:
                        pass
                return "Error: VRAM did not clear after unloading LLM. Aborting to prevent OOM crash. LLM reload attempted."

            # Small delay for GPU driver flush
            _time.sleep(3)

            # ── Phase 3: Generate via SwarmUI ───────────────────────
            try:
                sess_resp = _httpx.post(
                    f"{swarm_base}/API/GetNewSession", json={}, timeout=15
                )
                if sess_resp.status_code != 200:
                    raise RuntimeError(f"SwarmUI session failed (status {sess_resp.status_code})")
                session_id = sess_resp.json().get("session_id", "")
                if not session_id:
                    raise RuntimeError("SwarmUI returned no session_id")
            except Exception as e:
                # Reload LLM before returning error
                for mid in model_ids:
                    try:
                        _httpx.post(f"{lmstudio_url}/api/v1/models/load", json={"model": mid}, timeout=30)
                    except Exception:
                        pass
                return f"Error connecting to SwarmUI: {e}"

            is_video = media_type == "video"
            gen_timeout = 1800 if is_video else 300
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
                payload["videomodel"] = swarm_video_model
                payload["videoframes"] = 17
                payload["videosteps"] = 15
                payload["videocfg"] = 3.0
                payload["videofps"] = 24
                payload["videoformat"] = "mp4"

            try:
                gen_resp = _httpx.post(
                    f"{swarm_base}/API/GenerateText2Image",
                    json=payload,
                    timeout=gen_timeout,
                )
            except Exception as e:
                # Reload LLM before returning error
                for mid in model_ids:
                    try:
                        _httpx.post(f"{lmstudio_url}/api/v1/models/load", json={"model": mid}, timeout=30)
                    except Exception:
                        pass
                return f"Error: SwarmUI generation failed: {e}"

            gen_error = None
            if gen_resp.status_code != 200:
                gen_error = f"SwarmUI returned {gen_resp.status_code}: {gen_resp.text[:300]}"
            else:
                data = gen_resp.json()
                if data.get("error"):
                    gen_error = f"SwarmUI API error: {data['error']}"

            # ── Phase 4: Download and save results ──────────────────
            saved_paths = []
            if not gen_error:
                result_images = data.get("images", [])
                if not result_images:
                    gen_error = "SwarmUI returned no images"
                else:
                    for rel_url in result_images:
                        img_url = f"{swarm_base}/{rel_url}"
                        try:
                            img_resp = _httpx.get(img_url, timeout=60)
                            if img_resp.status_code != 200:
                                continue
                            content_bytes = img_resp.content
                            ext = ".png"
                            if content_bytes[:4] == b"\x89PNG":
                                ext = ".png"
                            elif content_bytes[:3] == b"\xff\xd8\xff":
                                ext = ".jpg"
                            elif content_bytes[:4] == b"RIFF":
                                ext = ".webp"
                            elif b"ftyp" in content_bytes[:12]:
                                ext = ".mp4"
                            elif content_bytes[:4] == b"\x1aE\xdf\xa3":
                                ext = ".webm"
                            filename = f"kestrel_{_uuid.uuid4().hex[:8]}_{int(_time.time())}{ext}"
                            file_path = output_dir / filename
                            file_path.write_bytes(content_bytes)
                            saved_paths.append(str(file_path))
                        except Exception:
                            continue

            # ── Phase 5: Evict diffusion/video models from VRAM ────
            # SwarmUI keeps models loaded after generation. We must free
            # them before reloading the LLM to avoid OOM.
            try:
                for _free_round in range(5):
                    free_resp = _httpx.post(
                        f"{swarm_base}/API/FreeBackendMemory",
                        json={"session_id": session_id},
                        timeout=15,
                    )
                    freed = free_resp.json().get("count", 0) if free_resp.status_code == 200 else 0
                    if freed == 0:
                        break
                    _time.sleep(2)  # Brief pause between eviction rounds
                # Final wait for GPU driver to fully release VRAM
                _time.sleep(8)
            except Exception:
                # Best effort — try to wait even if API failed
                _time.sleep(8)

            # ── Phase 6: Reload LLM ─────────────────────────────────
            reload_ok = False
            for mid in model_ids:
                try:
                    _httpx.post(
                        f"{lmstudio_url}/api/v1/models/load",
                        json={"model": mid},
                        timeout=300,
                    )
                except Exception:
                    pass

            # Wait for the model to actually be ready for inference
            # The load API returns 200 when loading starts, but the
            # model may not be ready for chat completions yet.
            # The 27B model takes ~213s to load, so we poll generously.
            for _wait in range(30):
                _time.sleep(5)
                try:
                    test_resp = _httpx.post(
                        f"{lmstudio_url}/v1/chat/completions",
                        json={
                            "model": model_ids[0] if model_ids else "auto",
                            "messages": [{"role": "user", "content": "hi"}],
                            "max_tokens": 1,
                        },
                        timeout=30,
                    )
                    if test_resp.status_code == 200:
                        reload_ok = True
                        break
                except Exception:
                    pass

            if gen_error:
                return f"Error: {gen_error}. LLM {'reloaded' if reload_ok else 'reload FAILED'}."

            if not saved_paths:
                return f"Error: Failed to download generated images. LLM {'reloaded' if reload_ok else 'reload FAILED'}."

            # ── Deliver to Telegram if configured ───────────────────
            tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
            tg_chat = os.getenv("TELEGRAM_CHAT_ID", "")
            tg_sent = False
            if tg_token and tg_chat:
                for fpath in saved_paths:
                    try:
                        fp = Path(fpath)
                        is_vid = fp.suffix.lower() in (".mp4", ".webm", ".mov")
                        api_method = "sendVideo" if is_vid else "sendDocument"
                        file_field = "video" if is_vid else "document"
                        tg_url = f"https://api.telegram.org/bot{tg_token}/{api_method}"
                        with open(fpath, "rb") as fobj:
                            files = {file_field: (fp.name, fobj)}
                            form_data = {
                                "chat_id": tg_chat,
                                "caption": prompt_text[:1024],
                            }
                            tg_resp = _httpx.post(
                                tg_url, data=form_data, files=files, timeout=60
                            )
                            if tg_resp.status_code == 200:
                                tg_sent = True
                    except Exception:
                        pass  # Best effort delivery

            paths_str = "\n".join(saved_paths)
            reload_note = "" if reload_ok else " WARNING: LLM reload failed — chat may be degraded."
            tg_note = " Sent to Telegram." if tg_sent else ""
            return (
                f"Successfully generated {len(saved_paths)} {media_type}(s).{reload_note}{tg_note}\n"
                f"Saved to:\n{paths_str}\n"
                f"You can reference these files in your response."
            )

        elif name == "take_screenshot":
            output_dir = Path.home() / ".kestrel" / "artifacts" / "media"
            timestamp = int(_time.time())
            file_path = output_dir / f"kestrel_screenshot_{timestamp}_{_uuid.uuid4().hex[:8]}.png"
            send_to_telegram = bool(arguments.get("send_to_telegram", False))
            caption = str(arguments.get("caption") or "Kestrel screenshot")

            _capture_screenshot_to_file(file_path)

            result_lines = [
                f"Screenshot captured successfully.",
                f"Saved to: {file_path}",
                f"File size: {file_path.stat().st_size} bytes",
            ]

            if send_to_telegram:
                sent, delivery_note = _send_file_to_telegram(file_path, caption=caption)
                result_lines.append(delivery_note)
                if not sent:
                    result_lines.append("The screenshot is still available locally at the saved path above.")
            else:
                result_lines.append("Telegram delivery was not requested.")

            return "\n".join(result_lines)

        else:
            return f"Error: Unknown tool: {name}"

    except Exception as exc:
        return f"Error executing {name}: {exc}"


async def complete_local_prompt(
    *,
    prompt: str,
    config: dict[str, Any],
    system_prompt: str = "You are Kestrel, a local autonomous agent OS focused on concise, actionable assistance.",
    history: list[dict[str, str]] | None = None,
    tool_categories: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    fake = os.getenv("KESTREL_FAKE_MODEL_RESPONSE")
    if fake:
        return {
            "provider": "fake",
            "model": "fake",
            "content": fake,
        }

    runtime = await detect_local_model_runtime(config)
    # Build messages list: system prompt, then history, then current prompt
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": prompt})

    MAX_TOOL_ROUNDS = 10

    async with _local_model_session(
        config=config,
        runtime=runtime,
        messages=messages,
        enable_thinking=False,
        model_role="primary",
    ) as session:
        provider = session["provider"]
        model = session["model"]
        base_url = str(session.get("base_url") or "").rstrip("/")

        if provider == "ollama":
            payload = await _http_post_json(
                f"{base_url}/api/chat",
                {
                    "model": model,
                    "stream": False,
                    "messages": messages,
                },
            )
            content = ((payload.get("message") or {}).get("content") or "").strip()

        elif provider == "lmstudio":
            tools = get_chat_tools(tool_categories)

            for _round in range(MAX_TOOL_ROUNDS):
                request_payload: dict[str, Any] = {
                    "model": model,
                    "messages": messages,
                    "temperature": 0.2,
                    "max_tokens": 4096,
                }
                if tools:
                    request_payload["tools"] = tools
                    request_payload["tool_choice"] = "auto"

                payload = await _http_post_json(
                    f"{base_url}/v1/chat/completions",
                    request_payload,
                    timeout_seconds=120,
                )
                choices = payload.get("choices") or []
                if not choices:
                    break

                choice = choices[0] or {}
                message = choice.get("message") or {}
                tool_calls = message.get("tool_calls") or []

                if not tool_calls:
                    # No tool calls — LLM produced a final text response.
                    content = (message.get("content") or "").strip()
                    if not content:
                        content = (message.get("reasoning_content") or "").strip()
                    break

                messages.append(message)

                for tc in tool_calls:
                    fn = tc.get("function") or {}
                    tool_name = fn.get("name", "")
                    try:
                        tool_args = json.loads(fn.get("arguments", "{}"))
                    except json.JSONDecodeError:
                        tool_args = {}

                    result = _execute_tool(tool_name, tool_args)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "content": result,
                    })
            else:
                payload = await _http_post_json(
                    f"{base_url}/v1/chat/completions",
                    {
                        "model": model,
                        "messages": messages,
                        "temperature": 0.2,
                        "max_tokens": 4096,
                    },
                    timeout_seconds=120,
                )
                choices = payload.get("choices") or []
                msg = ((choices[0] or {}).get("message") or {}) if choices else {}
                content = (msg.get("content") or "").strip()
                if not content:
                    content = (msg.get("reasoning_content") or "").strip()

        else:  # pragma: no cover - defensive
            raise RuntimeError(f"Unsupported local model provider: {provider}")

    if not content:
        raise RuntimeError(f"{provider} returned an empty completion")
    return {
        "provider": provider,
        "model": model,
        "content": content,
    }

