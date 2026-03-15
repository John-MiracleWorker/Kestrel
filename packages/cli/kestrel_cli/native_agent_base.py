from __future__ import annotations

from . import native_tool_registry as _native_tool_registry
from .native_persona import compose_native_system_prompt

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
        skill_pack_manager: Any | None = None,
        workspace_id: str = "",
        workspace_settings: dict[str, Any] | None = None,
        workspace_system_prompt: str = "",
        ambient_state: dict[str, Any] | None = None,
    ) -> None:
        self.paths = paths
        self.config = config
        self.runtime_policy = runtime_policy
        self.vector_store = vector_store
        self.state_store = state_store
        self.workspace_root = (workspace_root or Path.cwd()).resolve()
        self.event_callback = event_callback
        self.skill_pack_manager = skill_pack_manager
        self.workspace_id = str(workspace_id or "").strip()
        self.workspace_settings = dict(workspace_settings or {})
        self.workspace_system_prompt = str(workspace_system_prompt or "").strip()
        self.ambient_state = dict(ambient_state or {})
        self.tool_registry = NativeToolRegistry(
            paths=paths,
            config=config,
            runtime_policy=runtime_policy,
            vector_store=vector_store,
            workspace_root=self.workspace_root,
            skill_pack_manager=skill_pack_manager,
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

    def _ensure_skill_selection(self, state: dict[str, Any]) -> None:
        if state.get("skill_selection") or self.skill_pack_manager is None:
            return
        try:
            selection = self.skill_pack_manager.select_packs(
                str(state.get("goal") or ""),
                history=list(state.get("history") or []),
            )
        except Exception as exc:
            LOGGER.warning("Failed to resolve native skill packs: %s", exc)
            return
        if isinstance(selection, dict):
            state["skill_selection"] = selection

    def _selected_skill_pack_ids(self, state: dict[str, Any]) -> tuple[str, ...]:
        packs = (state.get("skill_selection") or {}).get("packs") or []
        selected: list[str] = []
        for pack in packs:
            if not isinstance(pack, dict):
                continue
            pack_id = str(pack.get("pack_id") or "").strip().lower()
            if pack_id:
                selected.append(pack_id)
        return tuple(selected)

    def _active_tools_for_state(self, state: dict[str, Any]) -> list[NativeToolSpec]:
        return self.tool_registry.list_tools(selected_pack_ids=self._selected_skill_pack_ids(state))

    def _skill_prompt_block(self, state: dict[str, Any]) -> str:
        return str((state.get("skill_selection") or {}).get("prompt_block") or "").strip()

    def _compose_system_prompt(
        self,
        *,
        role: str,
        instructions: str,
        state: dict[str, Any] | None = None,
    ) -> str:
        return compose_native_system_prompt(
            config=self.config,
            role=role,
            role_instructions=instructions,
            ambient_state=self.ambient_state,
            workspace_system_prompt=self.workspace_system_prompt,
            skill_prompt_block=self._skill_prompt_block(state or {}),
        )

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

    def _infer_explicit_skill_search_query(self, goal: str) -> str | None:
        text = str(goal or "").strip()
        if not text:
            return None
        lowered = text.lower()
        mentions_skills = bool(re.search(r"\b(?:skill|skills|skill pack|skill packs|marketplace)\b", lowered))
        asks_to_discover = bool(
            re.search(
                r"\b(?:search|find|look(?:\s+up)?|discover|browse|list|show|recommend|suggest)\b",
                lowered,
            )
        ) or bool(
            re.search(
                r"\b(?:what|which)\s+skills\b|\bare\s+there\s+any\s+skills\b|\bany\s+skills\b",
                lowered,
            )
        )
        if not mentions_skills or not asks_to_discover:
            return None

        query = text
        query = re.sub(r"(?i)^\s*(?:please\s+)?(?:can|could|would)\s+you\s+", "", query).strip()
        query = re.sub(r"(?i)^\s*i\s+(?:need|want|would like)\s+(?:you\s+)?to\s+", "", query).strip()
        skill_match = re.search(r"(?i)\b(?:skill packs?|skills?|marketplace)\b", query)
        if skill_match:
            suffix = query[skill_match.end():].strip(" \t\r\n:,-")
            suffix = re.sub(r"(?i)^(?:that|which)\s+", "", suffix).strip()
            suffix = re.sub(r"(?i)^(?:can|could|would)\s+", "", suffix).strip()
            suffix = re.sub(r"(?i)^help(?:\s+me)?\s+", "", suffix).strip()
            suffix = re.sub(r"(?i)^(?:with|for|to)\s+", "", suffix).strip()
            if suffix:
                query = suffix

        cleaned = re.sub(
            r"(?i)\b(?:search|find|look(?:\s+up)?|discover|browse|list|show|recommend|suggest|skills?|skill packs?|marketplace)\b",
            " ",
            query,
        )
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" \t\r\n.,:;!?")
        return cleaned or query.strip(" \t\r\n.,:;!?") or None

    def _normalize_skill_search_refinement(self, goal: str) -> str:
        query = self._extract_user_goal(goal)
        query = re.sub(r"(?i)^\s*(?:something|anything)\s+else(?:\s+instead)?\s*[:,-]?\s*", "", query).strip()
        query = re.sub(r"(?i)^\s*(?:how|what)\s+about\s+", "", query).strip()
        query = re.sub(r"(?i)^\s*maybe\s+", "", query).strip()
        query = re.sub(
            r"(?i)^\s*(?:search|find|look(?:\s+up)?|browse|show|recommend|suggest)\s+(?:for\s+)?",
            "",
            query,
        ).strip()
        query = re.sub(r"(?i)\s+(?:instead|please)\s*$", "", query).strip()
        return re.sub(r"\s+", " ", query).strip(" \t\r\n.,:;!?")

    def _looks_like_skill_install_reply(self, goal: str) -> bool:
        lowered = self._extract_user_goal(goal).strip().lower()
        if not lowered:
            return False
        if lowered in {"yes", "y", "sure", "ok", "okay", "do it", "go ahead"}:
            return True
        if re.search(r"\b(?:download|install|enable|add|get)\b", lowered):
            return True
        return bool(re.fullmatch(r"(?:that|this|first|second|third|last)\s+(?:one|skill|pack)", lowered))

    def _looks_like_new_task_prompt(self, goal: str) -> bool:
        lowered = self._extract_user_goal(goal).strip().lower()
        if not lowered:
            return False
        return bool(
            re.match(
                r"^(?:create|build|make|write|run|open|save|send|generate|draft|explain|summarize|review|analyze|help)\b",
                lowered,
            )
        )

    def _infer_skill_search_query_from_history(
        self,
        goal: str,
        history: list[dict[str, Any]] | None = None,
    ) -> str | None:
        candidate = self._normalize_skill_search_refinement(goal)
        if not candidate or candidate.lower() in {
            "another",
            "another one",
            "anything else",
            "else",
            "other",
            "something else",
            "thanks",
            "thank you",
        }:
            return None
        if self._looks_like_skill_install_reply(goal) or self._looks_like_new_task_prompt(goal):
            return None

        recent_skill_request = False
        assistant_skill_context = False
        assistant_offered_refinement = False
        for item in reversed(list(history or [])[-6:]):
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").strip().lower()
            content = self._extract_user_goal(str(item.get("content") or ""))
            if not content:
                continue
            lowered = content.lower()
            if role == "assistant":
                if re.search(r"\b(?:skill|skills|skill pack|skill packs|marketplace)\b", lowered):
                    assistant_skill_context = True
                if re.search(r"\b(?:search|find|look(?:\s+up)?|browse|recommend|suggest)\b", lowered) and re.search(
                    r"\b(?:else|another|different|something|anything)\b",
                    lowered,
                ):
                    assistant_skill_context = True
                    assistant_offered_refinement = True
                if re.search(r"\b(?:download|install|enable)\b", lowered) and re.search(r"\bskill\b", lowered):
                    assistant_skill_context = True
                    assistant_offered_refinement = True
                continue
            if role == "user" and self._infer_explicit_skill_search_query(content):
                recent_skill_request = True
                break

        if not recent_skill_request or not assistant_skill_context:
            return None
        if len(candidate.split()) > 8:
            return None
        if not assistant_offered_refinement and len(candidate.split()) < 2:
            return None
        return candidate

    def _infer_skill_search_query(
        self,
        goal: str,
        history: list[dict[str, Any]] | None = None,
    ) -> str | None:
        query = self._infer_explicit_skill_search_query(goal)
        if query:
            return query
        return self._infer_skill_search_query_from_history(goal, history)

    def _build_skill_search_plan(self, goal: str, query: str) -> NativePlan:
        return NativePlan(
            goal=goal,
            summary="Search the available skill packs for a relevant match.",
            reasoning="The user explicitly asked to discover skills, so use the skill catalog before considering custom tool scaffolding.",
            steps=[
                NativePlanStep(
                    id="step_1",
                    description=f"Search the available skill packs for matches to: {query}",
                    success_criteria="Relevant skill packs are identified or the absence of good matches is reported.",
                    preferred_tools=["skill_search"],
                )
            ],
        )

    def _infer_skill_search_arguments(self, state: dict[str, Any], step: dict[str, Any]) -> dict[str, Any] | None:
        if "skill_search" not in step.get("preferred_tools", []):
            return None
        query = self._infer_skill_search_query(
            str(state.get("goal") or ""),
            history=list(state.get("history") or []),
        )
        if not query:
            query = self._infer_skill_search_query(str(step.get("description") or ""))
        if not query:
            return None
        return {"query": query, "include_marketplace": True}

    def _summarize_skill_search_result(self, payload: dict[str, Any] | None) -> str:
        data = payload if isinstance(payload, dict) else {}
        query = str(data.get("query") or "").strip()
        results = [item for item in list(data.get("results") or []) if isinstance(item, dict)]
        if not results:
            if query:
                return f'I searched the available skill packs for "{query}" and did not find a relevant match.'
            return "I searched the available skill packs and did not find a relevant match."

        lines = []
        if query:
            lines.append(f'I searched the available skill packs for "{query}".')
        else:
            lines.append("I searched the available skill packs and found these matches.")
        for item in results[:3]:
            name = str(item.get("name") or item.get("pack_id") or "Unnamed skill").strip()
            description = str(item.get("description") or "").strip()
            lines.append(f"- {name}: {description or 'No description provided.'}")
        remaining = max(0, int(data.get("total") or len(results)) - min(len(results), 3))
        if remaining:
            lines.append(f"- Plus {remaining} more match(es).")
        return "\n".join(lines)

    def _extract_user_goal(self, goal: str) -> str:
        text = str(goal or "").strip()
        match = re.search(r"User goal:\s*(.+)$", text, re.IGNORECASE | re.DOTALL)
        if match:
            extracted = match.group(1).strip()
            if extracted:
                return extracted
        return text

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

    def _goal_needs_reasoning(self, goal: str, history: list[dict[str, Any]] | None = None) -> bool:
        messages = list(history or [])[-4:] + [{"role": "user", "content": str(goal or "")}]
        return _messages_need_reasoning(messages)

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

    async def _plan_goal(self, goal: str, history: list[dict[str, Any]], initial_tool_call: dict[str, Any] | None, state: dict[str, Any] | None = None) -> tuple[dict[str, Any], str, str]:
        state = state or {}
        selected_tools = self._active_tools_for_state(state)
        prompt_block = self._skill_prompt_block(state)
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

        skill_search_query = self._infer_skill_search_query(goal, history)
        if skill_search_query and self.tool_registry.get("skill_search"):
            plan = self._build_skill_search_plan(goal, skill_search_query)
            return {"mode": "plan", "plan": plan.to_dict()}, "heuristic", "skill_search"

        messages = [
            {
                "role": "system",
                "content": self._compose_system_prompt(
                    role="planner",
                    state=state,
                    instructions=(
                        "Return exactly one JSON object.\n"
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
                        "Prefer preferred_tools=[] for summarization, analysis, drafting, or synthesis steps that can be completed directly from prior evidence.\n"
                        "Do not assign run_python just to summarize or transform text unless the user explicitly asked for executable Python.\n"
                        "Only ask for additional user input when the task is truly blocked and no reasonable default exists.\n"
                        "Only use tools from this catalog:\n"
                        + json.dumps([tool.to_prompt_dict() for tool in selected_tools], indent=2)
                    ),
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
        selected_tools = self._active_tools_for_state(state)
        prompt_block = self._skill_prompt_block(state)
        messages = [
            {
                "role": "system",
                "content": self._compose_system_prompt(
                    role="execution controller",
                    state=state,
                    instructions=(
                        "Return exactly one JSON object.\n"
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
                        "If the current step is summarization, analysis, drafting, or synthesis from prior tool outputs, return the actual text result instead of Python source code.\n"
                        "Do not return Python code unless the step explicitly asks to create or execute Python.\n"
                        "Use need_input only when the task is blocked and no reasonable default can be inferred from the goal or prior evidence.\n"
                        "If no tool exists, use capability_gap instead of inventing a tool name.\n"
                        "Only use this tool catalog:\n"
                        + json.dumps([tool.to_prompt_dict() for tool in selected_tools], indent=2)
                    ),
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
            payload = {"ok": True, "final_response": draft_response, "reason": "No tool evidence to verify."}
            state["verifier_result"] = dict(payload)
            return payload, provider, model
        messages = [
            {
                "role": "system",
                "content": self._compose_system_prompt(
                    role="verifier",
                    state=state,
                    instructions=(
                        "Return exactly one JSON object.\n"
                        "Schema:\n"
                        "{\n"
                        '  "ok": true | false,\n'
                        '  "final_response": "string",\n'
                        '  "reason": "string"\n'
                        "}\n"
                        "Reject unsupported claims. Final response must be grounded in tool_evidence."
                    ),
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
        state["verifier_result"] = dict(payload)
        return payload, provider, model

    async def _direct_response(self, goal: str, history: list[dict[str, Any]], *, state: dict[str, Any] | None = None) -> tuple[str, str, str]:
        response = await _generate_local_text_response(
            messages=[
                {
                    "role": "system",
                    "content": self._compose_system_prompt(
                        role="direct responder",
                        state=state,
                        instructions="Answer directly, honestly, and with concrete next steps when useful.",
                    ),
                },
                *history[-6:],
                {"role": "user", "content": goal},
            ],
            config=self.config,
        )
        return response["content"], response["provider"], response["model"]

    def _step_expects_svg_markup(self, state: dict[str, Any], step: dict[str, Any]) -> bool:
        combined = " ".join(
            part
            for part in (
                str(state.get("goal") or ""),
                str((state.get("plan") or {}).get("summary") or ""),
                str(step.get("description") or ""),
                str(step.get("success_criteria") or ""),
            )
            if part
        ).lower()
        return "svg" in combined

    def _build_step_output_messages(
        self,
        state: dict[str, Any],
        step: dict[str, Any],
        *,
        expects_svg_markup: bool,
    ) -> list[dict[str, str]]:
        previous_outputs = state.get("step_outputs", {})
        recent_tool_evidence = state.get("tool_evidence", [])
        user_goal = self._extract_user_goal(state.get("goal", ""))
        previous_sections: list[str] = []
        for step_id, payload in list(previous_outputs.items())[-4:]:
            if not isinstance(payload, dict):
                continue
            content = str(payload.get("content") or "").strip()
            if not content:
                continue
            snippet = content if len(content) <= 600 else f"{content[:600]}\n..."
            previous_sections.append(f"{step_id}:\n{snippet}")

        evidence_sections: list[str] = []
        for item in list(recent_tool_evidence)[-4:]:
            if not isinstance(item, dict):
                continue
            tool_name = str(item.get("tool_name") or "tool").strip() or "tool"
            snippet = ""
            data = item.get("data")
            if isinstance(data, dict):
                for key in ("body", "content", "text"):
                    value = str(data.get(key) or "").strip()
                    if value:
                        snippet = value
                        break
                if not snippet and data:
                    try:
                        snippet = json.dumps(data, indent=2, sort_keys=True)
                    except TypeError:
                        snippet = str(data)
            if not snippet:
                for key in ("message", "stdout", "stderr"):
                    value = str(item.get(key) or "").strip()
                    if value:
                        snippet = value
                        break
            if not snippet:
                continue
            snippet = snippet if len(snippet) <= 800 else f"{snippet[:800]}\n..."
            evidence_sections.append(f"{tool_name}:\n{snippet}")

        system_lines = [
            self._compose_system_prompt(
                role="step writer",
                state=state,
                instructions=(
                    "You are completing one Kestrel task step.\n"
                    "Return only the concrete output for the current step.\n"
                    "Do not add explanations, markdown fences, or JSON."
                ),
            ),
        ]
        if expects_svg_markup:
            system_lines.extend(
                [
                    "Return only valid standalone SVG markup.",
                    "Include width, height, and viewBox attributes.",
                    "Make the SVG recognizable for the requested subject.",
                ]
            )

        user_sections = [
            f"Goal:\n{user_goal}",
            f"Plan summary:\n{(state.get('plan') or {}).get('summary', '')}",
            f"Current step:\n{step.get('description', '')}",
            f"Success criteria:\n{step.get('success_criteria', '')}",
        ]
        if previous_sections:
            user_sections.append("Previous outputs:\n" + "\n\n".join(previous_sections))
        if evidence_sections:
            user_sections.append("Recent tool evidence:\n" + "\n\n".join(evidence_sections))

        return [
            {"role": "system", "content": "\n".join(system_lines)},
            {"role": "user", "content": "\n\n".join(section for section in user_sections if section.strip())},
        ]

    async def _generate_step_output(self, state: dict[str, Any], step: dict[str, Any]) -> tuple[str, str, str]:
        expects_svg_markup = self._step_expects_svg_markup(state, step)
        messages = self._build_step_output_messages(
            state,
            step,
            expects_svg_markup=expects_svg_markup,
        )

        try:
            response = await _generate_local_text_response(
                messages=messages,
                config=self.config,
                temperature=0.2,
                max_tokens=2048 if expects_svg_markup else 4096,
                enable_thinking=True,
                timeout_seconds=90 if expects_svg_markup else 120,
            )
        except Exception:
            response = await _generate_local_text_response(
                messages=messages,
                config=self.config,
                temperature=0.1,
                max_tokens=1536 if expects_svg_markup else 3072,
                enable_thinking=False,
                timeout_seconds=45 if expects_svg_markup else 60,
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
