from __future__ import annotations

from . import native_tool_registry as _native_tool_registry

globals().update({name: value for name, value in vars(_native_tool_registry).items() if not name.startswith("__")})

class NativeAgentRunnerBase:
    def __init__(
        self,
        *,
        paths: KestrelPaths,
        config: dict[str, Any],
        runtime_policy: RuntimePolicy,
        vector_store: VectorMemoryStore | None = None,
        state_store: SQLiteStateStore | None = None,
        event_callback: Callable[[str, str, dict[str, Any]], None] | None = None,
        workspace_root: Path | None = None,
    ) -> None:
        self.paths = paths
        self.config = config
        self.runtime_policy = runtime_policy
        self.vector_store = vector_store
        self.state_store = state_store
        self.workspace_root = (workspace_root or Path.cwd()).resolve()
        self.event_callback = event_callback
        self.tool_registry = NativeToolRegistry(
            paths=paths,
            config=config,
            runtime_policy=runtime_policy,
            vector_store=vector_store,
            workspace_root=self.workspace_root,
        )
        self.max_plan_steps = int(self.config.get("agent", {}).get("max_plan_steps", 8))
        self.max_step_iterations = int(self.config.get("agent", {}).get("max_step_iterations", 6))
        self.max_total_tool_calls = int(self.config.get("agent", {}).get("max_total_tool_calls", 24))
        self.max_execution_seconds = int(self.config.get("agent", {}).get("max_execution_seconds", 180))

    def _emit(self, event_type: str, content: str, **payload: Any) -> None:
        if self.event_callback is None:
            return
        self.event_callback(event_type, content, payload)

    def _persist_state(self, task_id: str, state: dict[str, Any], *, status: str | None = None, result: dict[str, Any] | None = None, error: str | None = None) -> None:
        if not self.state_store or not task_id:
            return
        metadata = {
            "agent_state": state,
            "plan": state.get("plan"),
            "tool_evidence": state.get("tool_evidence", []),
            "artifacts": state.get("artifacts", []),
            "step_outputs": state.get("step_outputs", {}),
        }
        self.state_store.update_task(task_id, status=status, metadata=metadata, result=result, error=error)

    def _normalize_execution_action(self, action: dict[str, Any], step: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(action or {})
        action_type = str(normalized.get("action") or "").strip().lower()
        if not action_type:
            if normalized.get("tool_name"):
                action_type = "tool_call"
            elif normalized.get("question"):
                action_type = "need_input"
            elif any(normalized.get(key) for key in ("result", "content", "output")):
                action_type = "store_result"
            elif normalized.get("strategy"):
                action_type = "capability_gap"
            elif normalized.get("summary"):
                action_type = "finish"
        normalized["action"] = action_type
        if action_type == "store_result" and not normalized.get("result"):
            normalized["result"] = (
                normalized.get("content")
                or normalized.get("output")
                or normalized.get("summary")
                or step.get("description")
                or ""
            )
        return normalized

    def _looks_like_svg_render_goal(self, goal: str) -> bool:
        lowered = str(goal or "").strip().lower()
        return "svg" in lowered and bool(
            re.search(r"\b(?:png|jpg|jpeg|webp|render|export|convert)\b", lowered)
        )

    def _prefers_telegram_delivery(self, goal: str) -> bool:
        lowered = str(goal or "").strip().lower()
        return any(
            phrase in lowered
            for phrase in (
                "send it to me",
                "send me",
                "share it with me",
                "telegram",
                "message me",
                "dm me",
            )
        )

    def _build_svg_render_plan(self, goal: str) -> NativePlan:
        return NativePlan(
            goal=goal,
            summary="Generate SVG markup and render it to a PNG artifact.",
            reasoning="The request explicitly asks for SVG creation plus image rendering, so use a deterministic local SVG render flow instead of a plain chat reply.",
            steps=[
                NativePlanStep(
                    id="step_1",
                    description="Generate the requested SVG markup.",
                    success_criteria="Valid SVG markup exists for the requested subject.",
                    preferred_tools=[],
                ),
                NativePlanStep(
                    id="step_2",
                    description="Render the SVG markup to a PNG artifact with render_svg_asset.",
                    success_criteria="Both the SVG and PNG artifacts are saved locally and ready for delivery.",
                    preferred_tools=["render_svg_asset"],
                ),
            ],
        )

    def _infer_svg_render_arguments(self, state: dict[str, Any], step: dict[str, Any]) -> dict[str, Any] | None:
        svg_content = self._latest_step_output(state, step)
        if not svg_content or "<svg" not in svg_content.lower():
            return None
        goal = str(state.get("goal") or "")
        base_name = "hand" if "hand" in goal.lower() else "generated-svg"
        return {
            "svg_content": svg_content,
            "prompt": goal,
            "base_name": base_name,
            "send_to_telegram": False,
        }

    def _fallback_plan_for_goal(self, goal: str) -> NativePlan | None:
        lowered = goal.lower()
        available_tools = {tool.name for tool in self.tool_registry.list_tools()}

        if self._looks_like_svg_render_goal(goal) and "render_svg_asset" in available_tools:
            return self._build_svg_render_plan(goal)

        if any(token in lowered for token in ("save", "write", "desktop", ".svg", ".txt", ".md", ".json", ".html", ".xml")) and "write_file" in available_tools:
            return NativePlan(
                goal=goal,
                summary="Use write_file to complete the requested file task.",
                reasoning="The planner returned no actionable steps, so fall back to generating the content first and then writing it with an approval-backed file tool.",
                steps=[
                    NativePlanStep(
                        id="step_1",
                        description="Generate the requested content so it can be saved.",
                        success_criteria="Reusable content exists for the save step.",
                        preferred_tools=[],
                    ),
                    NativePlanStep(
                        id="step_2",
                        description="Write the generated content to the requested path with write_file.",
                        success_criteria="The requested file is written to the target path.",
                        preferred_tools=["write_file"],
                    )
                ],
            )

        if any(token in lowered for token in ("run ", "execute", "command", "shell")) and "run_command" in available_tools:
            return NativePlan(
                goal=goal,
                summary="Use run_command to complete the requested system task.",
                reasoning="The planner returned no actionable steps, so fall back to a concrete command-execution step.",
                steps=[
                    NativePlanStep(
                        id="step_1",
                        description="Use run_command to complete the request.",
                        success_criteria="The requested command or system action is completed.",
                        preferred_tools=["run_command"],
                    )
                ],
            )

        if any(token in lowered for token in ("find", "search", "todo", "grep")) and "search_files" in available_tools:
            return NativePlan(
                goal=goal,
                summary="Use search_files to inspect the workspace.",
                reasoning="The planner returned no actionable steps, so fall back to a concrete search step.",
                steps=[
                    NativePlanStep(
                        id="step_1",
                        description="Search the workspace for the requested information.",
                        success_criteria="Relevant matches are collected from the workspace.",
                        preferred_tools=["search_files"],
                    )
                ],
            )

        return None

    async def _plan_goal(self, goal: str, history: list[dict[str, Any]], initial_tool_call: dict[str, Any] | None) -> tuple[dict[str, Any], str, str]:
        if initial_tool_call:
            plan = NativePlan(
                goal=goal,
                summary="Deterministic native fast path",
                reasoning="A deterministic native tool matched the request before the planner ran.",
                steps=[
                    NativePlanStep(
                        id="step_1",
                        description=f"Use {initial_tool_call['tool_name']} to satisfy the request.",
                        success_criteria="Return the actual tool result to the user.",
                        preferred_tools=[initial_tool_call["tool_name"]],
                    )
                ],
            )
            return {"mode": "plan", "plan": plan.to_dict()}, "native-fast-path", initial_tool_call["tool_name"]

        if self._looks_like_svg_render_goal(goal) and self.tool_registry.get("render_svg_asset"):
            plan = self._build_svg_render_plan(goal)
            return {"mode": "plan", "plan": plan.to_dict()}, "heuristic", "render_svg_asset"

        lowered = goal.strip().lower()
        if lowered.startswith("shell:"):
            command = goal.split(":", 1)[1].strip()
            plan = NativePlan(
                goal=goal,
                summary="Execute the requested shell command",
                reasoning="The task explicitly requested direct shell execution.",
                steps=[
                    NativePlanStep(
                        id="step_1",
                        description=f"Run the exact shell command: {command}",
                        success_criteria="Command output is captured and returned.",
                        preferred_tools=["run_command"],
                    )
                ],
            )
            return {"mode": "plan", "plan": plan.to_dict()}, "heuristic", "run_command"

        messages = [
            {
                "role": "system",
                "content": (
                    "You are Kestrel's native planner. Return exactly one JSON object.\n"
                    "Decide whether to answer directly or produce a step plan.\n"
                    "JSON schema:\n"
                    "{\n"
                    '  "mode": "direct_response" | "plan",\n'
                    '  "response": "string when mode is direct_response",\n'
                    '  "summary": "string when mode is plan",\n'
                    '  "reasoning": "string",\n'
                    '  "steps": [\n'
                    "    {\n"
                    '      "id": "step_1",\n'
                    '      "description": "what the step does",\n'
                    '      "success_criteria": "how to tell it is done",\n'
                    '      "preferred_tools": ["tool_name"]\n'
                    "    }\n"
                    "  ]\n"
                    "}\n"
                    "Only use tools from this catalog:\n"
                    + json.dumps([tool.to_prompt_dict() for tool in self.tool_registry.list_tools()], indent=2)
                ),
            },
            {
                "role": "user",
                "content": json.dumps({"goal": goal, "history": history[-6:]}, indent=2),
            },
        ]
        payload, provider, model = await _request_model_json(
            messages=messages,
            config=self.config,
            repair_label="planner",
        )
        return payload, provider, model

    async def _next_action(self, state: dict[str, Any], step: dict[str, Any]) -> tuple[dict[str, Any], str, str]:
        messages = [
            {
                "role": "system",
                "content": (
                    "You are Kestrel's native execution controller. Return exactly one JSON object.\n"
                    "Allowed actions:\n"
                    "{\n"
                    '  "action": "tool_call" | "finish" | "need_input" | "capability_gap" | "store_result",\n'
                    '  "tool_name": "tool for tool_call",\n'
                    '  "arguments": { },\n'
                    '  "scope": "step" | "task" when action is finish,\n'
                    '  "summary": "result summary",\n'
                    '  "question": "user question" when action is need_input,\n'
                    '  "result": "string content to persist for later steps when action is store_result",\n'
                    '  "strategy": "custom_tool" | "missing_prerequisite" | "reuse_tools" when action is capability_gap,\n'
                    '  "reason": "short reason"\n'
                    "}\n"
                    "Use store_result when you generate reusable text, code, SVG, JSON, or other content that a later step must consume.\n"
                    "If no tool exists, use capability_gap instead of inventing a tool name.\n"
                    "Only use this tool catalog:\n"
                    + json.dumps([tool.to_prompt_dict() for tool in self.tool_registry.list_tools()], indent=2)
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "goal": state["goal"],
                        "plan": state.get("plan"),
                        "current_step": step,
                        "tool_evidence": state.get("tool_evidence", [])[-8:],
                        "step_outputs": state.get("step_outputs", {}),
                        "completed_steps": state.get("completed_steps", []),
                        "tool_calls": state.get("tool_calls", 0),
                    },
                    indent=2,
                ),
            },
        ]
        payload, provider, model = await _request_model_json(
            messages=messages,
            config=self.config,
            repair_label="executor",
        )
        return payload, provider, model

    async def _verify_response(self, state: dict[str, Any], draft_response: str) -> tuple[dict[str, Any], str, str]:
        if not state.get("tool_evidence"):
            provider = str(state.get("provider") or "native-direct")
            model = str(state.get("model") or "native-direct")
            return {"ok": True, "final_response": draft_response, "reason": "No tool evidence to verify."}, provider, model
        messages = [
            {
                "role": "system",
                "content": (
                    "You are Kestrel's native verifier. Return exactly one JSON object.\n"
                    "Schema:\n"
                    "{\n"
                    '  "ok": true | false,\n'
                    '  "final_response": "string",\n'
                    '  "reason": "string"\n'
                    "}\n"
                    "Reject unsupported claims. Final response must be grounded in tool_evidence."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "goal": state["goal"],
                        "draft_response": draft_response,
                        "tool_evidence": state.get("tool_evidence", []),
                        "artifacts": state.get("artifacts", []),
                    },
                    indent=2,
                ),
            },
        ]
        payload, provider, model = await _request_model_json(
            messages=messages,
            config=self.config,
            repair_label="verifier",
        )
        return payload, provider, model

    async def _direct_response(self, goal: str, history: list[dict[str, Any]]) -> tuple[str, str, str]:
        response = await _generate_local_text_response(
            messages=[
                {
                    "role": "system",
                    "content": "You are Kestrel, a concise local autonomous assistant. Answer directly and honestly.",
                },
                *history[-6:],
                {"role": "user", "content": goal},
            ],
            config=self.config,
        )
        return response["content"], response["provider"], response["model"]

    async def _generate_step_output(self, state: dict[str, Any], step: dict[str, Any]) -> tuple[str, str, str]:
        response = await _generate_local_text_response(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are generating the concrete output for a single Kestrel plan step.\n"
                        "Return only the requested content for the step and nothing else.\n"
                        "Do not explain your work. Do not wrap the content in JSON."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "goal": state["goal"],
                            "plan_summary": (state.get("plan") or {}).get("summary", ""),
                            "current_step": step,
                            "previous_outputs": state.get("step_outputs", {}),
                            "tool_evidence": state.get("tool_evidence", [])[-4:],
                        },
                        indent=2,
                    ),
                },
            ],
            config=self.config,
            temperature=0.2,
            max_tokens=4096,
        )
        return _strip_wrappers(response["content"]).strip(), response["provider"], response["model"]

    def _latest_step_output(self, state: dict[str, Any], step: dict[str, Any]) -> str:
        step_outputs = state.get("step_outputs", {})
        current = step_outputs.get(step["id"])
        if isinstance(current, dict) and str(current.get("content") or "").strip():
            return str(current["content"]).strip()
        for output in reversed(list(step_outputs.values())):
            if isinstance(output, dict) and str(output.get("content") or "").strip():
                return str(output["content"]).strip()
        return ""

    def _infer_write_target_path(self, goal: str, step: dict[str, Any], content: str) -> Path | None:
        combined = " ".join(
            part
            for part in (
                goal,
                str(step.get("description") or ""),
                str(step.get("success_criteria") or ""),
            )
            if part
        )

        absolute_match = re.search(r"(?P<path>(?:~|/)[^\s,'\"`]+)", combined)
        if absolute_match:
            return Path(absolute_match.group("path")).expanduser()

        filename = ""
        named_match = re.search(r"\b(?:named|called)\s+([A-Za-z0-9._-]+\.[A-Za-z0-9]+)\b", combined, re.IGNORECASE)
        if named_match:
            filename = named_match.group(1)
        else:
            file_match = re.search(r"\b([A-Za-z0-9._-]+\.(?:svg|txt|md|json|html|xml))\b", combined, re.IGNORECASE)
            if file_match:
                filename = file_match.group(1)

        lowered = combined.lower()
        content_lowered = content.lower()
        if not filename:
            if "<svg" in content_lowered or "svg" in lowered:
                filename = "hand.svg" if "hand" in lowered else "generated.svg"
            elif "json" in lowered:
                filename = "generated.json"
            elif "html" in lowered:
                filename = "generated.html"
            elif "markdown" in lowered or ".md" in lowered:
                filename = "generated.md"
            else:
                filename = "generated.txt"

        if "desktop" in lowered:
            directory = Path.home() / "Desktop"
        elif "documents" in lowered:
            directory = Path.home() / "Documents"
        elif "downloads" in lowered:
            directory = Path.home() / "Downloads"
        else:
            directory = self.workspace_root
        return directory / filename

    def _infer_write_file_arguments(self, state: dict[str, Any], step: dict[str, Any]) -> dict[str, Any] | None:
        content = self._latest_step_output(state, step)
        if not content:
            return None
        target_path = self._infer_write_target_path(state["goal"], step, content)
        if target_path is None:
            return None
        return {"path": str(target_path), "content": content}
