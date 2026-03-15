from __future__ import annotations

from . import native_agent_base as _native_agent_base

globals().update({name: value for name, value in vars(_native_agent_base).items() if not name.startswith("__")})

class NativeAgentRunnerRunMixin:
    async def run(
        self,
        *,
        goal: str,
        history: list[dict[str, Any]] | None = None,
        task_id: str = "",
        task_kind: str = "task",
        initial_tool_call: dict[str, Any] | None = None,
        resume_state: dict[str, Any] | None = None,
        approved: bool = False,
    ) -> NativeAgentOutcome:
        history = list(history or [])
        started = time.time()
        state = dict(resume_state or {})
        state.setdefault("goal", goal)
        state.setdefault("history", history)
        state.setdefault("tool_evidence", [])
        state.setdefault("artifacts", [])
        state.setdefault("completed_steps", [])
        state.setdefault("step_outputs", {})
        state.setdefault("tool_calls", 0)
        state.setdefault("pending_action", None)
        state.setdefault("forced_action", None)
        state.setdefault("final_response_draft", "")
        state.setdefault("complete_step_after_tool", {})

        provider = state.get("provider", "")
        model = state.get("model", "")

        if not state.get("plan"):
            planning_payload, provider, model = await self._plan_goal(goal, history, initial_tool_call)
            state["provider"] = provider
            state["model"] = model
            if planning_payload.get("mode") == "direct_response":
                response_text = ""
                if not self._goal_needs_reasoning(goal, history):
                    response_text = str(planning_payload.get("response") or "").strip()
                if not response_text:
                    response_text, provider, model = await self._direct_response(goal, history)
                state["provider"] = provider
                state["model"] = model
                verifier_payload, provider, model = await self._verify_response(state, response_text)
                final_text = str(verifier_payload.get("final_response") or response_text).strip()
                if not verifier_payload.get("ok", True):
                    final_text = response_text
                state["plan"] = None
                self._persist_state(
                    task_id,
                    state,
                    status="completed",
                    result={
                        "message": final_text,
                        "provider": provider,
                        "model": model,
                        "plan": None,
                        "artifacts": [],
                    },
                )
                return NativeAgentOutcome(
                    status="completed",
                    message=final_text,
                    provider=provider,
                    model=model,
                    plan=None,
                    artifacts=[],
                    state=state,
                )

            plan_data = planning_payload.get("plan") if isinstance(planning_payload.get("plan"), dict) else planning_payload
            steps = list(plan_data.get("steps") or [])[: self.max_plan_steps]
            normalized_steps = []
            for index, raw_step in enumerate(steps, start=1):
                normalized_steps.append(
                    NativePlanStep(
                        id=str(raw_step.get("id") or f"step_{index}"),
                        description=str(raw_step.get("description") or f"Complete step {index}"),
                        success_criteria=str(raw_step.get("success_criteria") or ""),
                        preferred_tools=list(raw_step.get("preferred_tools") or []),
                    ).to_dict()
                )
            if not normalized_steps:
                fallback_plan = self._fallback_plan_for_goal(goal)
                if fallback_plan is not None:
                    plan_data = fallback_plan.to_dict()
                    normalized_steps = [step.to_dict() for step in fallback_plan.steps]
            state["plan"] = {
                "goal": goal,
                "summary": str(plan_data.get("summary") or "Execute the task."),
                "reasoning": str(plan_data.get("reasoning") or ""),
                "steps": normalized_steps,
            }
            self._emit(
                "plan_created",
                state["plan"]["summary"],
                plan=state["plan"],
                step_count=len(normalized_steps),
            )
            if initial_tool_call:
                state["forced_action"] = {
                    "action": "tool_call",
                    "tool_name": str(initial_tool_call.get("tool_name") or ""),
                    "arguments": dict(initial_tool_call.get("arguments") or {}),
                    "reason": "Deterministic native fast path.",
                }
            self._persist_state(task_id, state, status="running")

        plan = state.get("plan") or {}
        steps = list(plan.get("steps") or [])
        if not steps:
            response_text, provider, model = await self._direct_response(goal, history)
            self._persist_state(
                task_id,
                state,
                status="completed",
                result={
                    "message": response_text,
                    "provider": provider,
                    "model": model,
                    "plan": plan or None,
                    "artifacts": list(state.get("artifacts", [])),
                },
            )
            return NativeAgentOutcome(
                status="completed",
                message=response_text,
                provider=provider,
                model=model,
                plan=plan or None,
                artifacts=list(state.get("artifacts", [])),
                state=state,
            )

        current_index = int(state.get("current_step_index") or 0)
        while current_index < len(steps):
            if time.time() - started > self.max_execution_seconds:
                self._persist_state(task_id, state, status="failed", error="Native execution timed out.")
                return NativeAgentOutcome(
                    status="failed",
                    message="Native execution timed out.",
                    provider=provider,
                    model=model,
                    plan=plan,
                    artifacts=list(state.get("artifacts", [])),
                    state=state,
                )
            step = steps[current_index]
            step.setdefault("status", "pending")
            step.setdefault("preferred_tools", [])
            if step["status"] == "complete":
                current_index += 1
                continue
            step["status"] = "running"
            self._emit("step_started", step["description"], step_id=step["id"], step=step)
            self._persist_state(task_id, state, status="running")

            if (
                not step.get("preferred_tools")
                and not state.get("forced_action")
                and not state.get("pending_action")
            ):
                generated_output, provider, model = await self._generate_step_output(state, step)
                if generated_output:
                    state["provider"] = provider
                    state["model"] = model
                    state.setdefault("step_outputs", {})[step["id"]] = {
                        "content": generated_output,
                        "summary": step["description"],
                    }
                    step["status"] = "complete"
                    state["completed_steps"].append(step["id"])
                    self._emit("step_complete", step["description"], step_id=step["id"], step=step)
                    self._persist_state(task_id, state, status="running")
                    current_index += 1
                    state["current_step_index"] = current_index
                    self._persist_state(task_id, state, status="running")
                    continue

            for _iteration in range(self.max_step_iterations):
                if state.get("tool_calls", 0) >= self.max_total_tool_calls:
                    break
                action: dict[str, Any]
                approved_for_action = False
                auto_render_args = None
                if (
                    "render_svg_asset" in step.get("preferred_tools", [])
                    and not state.get("forced_action")
                    and not state.get("pending_action")
                ):
                    auto_render_args = self._infer_svg_render_arguments(state, step)
                if auto_render_args:
                    action = {
                        "action": "tool_call",
                        "tool_name": "render_svg_asset",
                        "arguments": auto_render_args,
                        "reason": "Deterministic SVG render from stored step output.",
                    }
                    state.setdefault("complete_step_after_tool", {})[step["id"]] = "render_svg_asset"
                elif isinstance(state.get("forced_action"), dict):
                    action = dict(state["forced_action"])
                    state["forced_action"] = None
                    approved_for_action = False
                elif approved and isinstance(state.get("pending_action"), dict):
                    action = dict(state["pending_action"])
                    state["pending_action"] = None
                    approved_for_action = True
                else:
                    action, provider, model = await self._next_action(state, step)
                    state["provider"] = provider
                    state["model"] = model
                    approved_for_action = False

                action = self._normalize_execution_action(action, step)
                action_type = str(action.get("action") or "").strip().lower()
                if action_type == "tool_call":
                    tool_name = str(action.get("tool_name") or "").strip()
                    tool_args = action.get("arguments") if isinstance(action.get("arguments"), dict) else {}
                    self._emit(
                        "tool_call",
                        tool_name,
                        toolName=tool_name,
                        toolArgs=tool_args,
                        tool_name=tool_name,
                        tool_args=tool_args,
                        step_id=step["id"],
                    )
                    result = self.tool_registry.execute(
                        tool_name,
                        tool_args,
                        task_id=task_id,
                        approved=approved_for_action,
                    )
                    if approved_for_action:
                        approved = False
                    if result.approval_required:
                        state["pending_action"] = {
                            "action": "tool_call",
                            "tool_name": tool_name,
                            "arguments": tool_args,
                            "reason": action.get("reason") or result.message,
                        }
                        resume_payload = {
                            "state": state,
                            "step_id": step["id"],
                            "tool_name": tool_name,
                        }
                        approval_info: dict[str, Any]
                        if self.state_store and task_id:
                            approval = self.state_store.create_approval(
                                task_id=task_id,
                                operation=result.approval_operation or "approval",
                                command=str(result.approval_payload.get("summary") or tool_name),
                                payload=result.approval_payload,
                                resume=resume_payload,
                            )
                            approval_info = {
                                "id": approval["id"],
                                "task_id": task_id,
                                "operation": approval["operation"],
                                "summary": result.approval_payload.get("summary") or result.message,
                            }
                            self._persist_state(task_id, state, status="waiting_approval")
                        else:
                            approval_info = {
                                "id": "",
                                "task_id": task_id,
                                "operation": result.approval_operation or "approval",
                                "summary": result.approval_payload.get("summary") or result.message,
                            }
                        self._emit(
                            "approval_needed",
                            approval_info["summary"],
                            approvalId=approval_info["id"],
                            approval=approval_info,
                            step_id=step["id"],
                        )
                        return NativeAgentOutcome(
                            status="waiting_approval",
                            message=approval_info["summary"],
                            provider=provider,
                            model=model,
                            plan=plan,
                            approval=approval_info,
                            artifacts=list(state.get("artifacts", [])),
                            state=state,
                        )

                    evidence = {
                        "step_id": step["id"],
                        "tool_name": tool_name,
                        "arguments": tool_args,
                        **result.to_dict(),
                    }
                    state["tool_calls"] = int(state.get("tool_calls", 0)) + 1
                    state["tool_evidence"].append(evidence)
                    state["artifacts"].extend(result.artifacts)
                    self._emit(
                        "tool_result",
                        result.to_text(),
                        toolName=tool_name,
                        toolResult=result.to_dict(),
                        tool_name=tool_name,
                        tool_result=result.to_dict(),
                        step_id=step["id"],
                    )
                    self._persist_state(task_id, state, status="running")
                    complete_after_tool = state.get("complete_step_after_tool", {})
                    if (
                        result.success
                        and isinstance(complete_after_tool, dict)
                        and complete_after_tool.get(step["id"]) == tool_name
                    ):
                        step["status"] = "complete"
                        state["completed_steps"].append(step["id"])
                        summary = str(result.message or step["description"]).strip()
                        state["final_response_draft"] = summary
                        complete_after_tool.pop(step["id"], None)
                        self._emit("step_complete", summary, step_id=step["id"], step=step)
                        self._persist_state(task_id, state, status="running")
                        break
                    if result.success and tool_name == "custom_tool_create":
                        step["status"] = "complete"
                        state["completed_steps"].append(step["id"])
                        state["final_response_draft"] = result.message
                        self._emit("step_complete", result.message, step_id=step["id"], step=step)
                        break
                    continue

                if action_type == "need_input":
                    question = str(action.get("question") or "I need more information.").strip()
                    step["status"] = "complete"
                    state["completed_steps"].append(step["id"])
                    state["final_response_draft"] = question
                    self._emit("step_complete", question, step_id=step["id"], step=step)
                    break

                if action_type == "store_result":
                    result_text = str(action.get("result") or "").strip()
                    summary = str(action.get("summary") or step["description"]).strip()
                    if not result_text:
                        continue
                    state.setdefault("step_outputs", {})[step["id"]] = {
                        "content": result_text,
                        "summary": summary,
                    }
                    if step.get("preferred_tools"):
                        self._persist_state(task_id, state, status="running")
                        continue
                    step["status"] = "complete"
                    state["completed_steps"].append(step["id"])
                    self._emit("step_complete", summary, step_id=step["id"], step=step)
                    self._persist_state(task_id, state, status="running")
                    break

                if action_type == "capability_gap":
                    strategy = str(action.get("strategy") or "missing_prerequisite").strip().lower()
                    if strategy == "custom_tool" and bool(self.config.get("agent", {}).get("allow_custom_tool_scaffolding", True)):
                        blueprint = _build_custom_tool_blueprint(goal, action)
                        scaffold_result = self.tool_registry.execute(
                            "custom_tool_create",
                            {
                                "name": blueprint["name"],
                                "description": blueprint["description"],
                                "runtime": blueprint["runtime"],
                                "entrypoint": blueprint["entrypoint"],
                                "input_schema": blueprint["input_schema"],
                                "risk_class": blueprint["risk_class"],
                                "approval_required": blueprint["approval_required"],
                                "setup_notes": blueprint["setup_notes"],
                                "files": blueprint["files"],
                            },
                            task_id=task_id,
                            approved=approved,
                        )
                        state["pending_action"] = {
                            "action": "tool_call",
                            "tool_name": "custom_tool_create",
                            "arguments": {
                                "name": blueprint["name"],
                                "description": blueprint["description"],
                                "runtime": blueprint["runtime"],
                                "entrypoint": blueprint["entrypoint"],
                                "input_schema": blueprint["input_schema"],
                                "risk_class": blueprint["risk_class"],
                                "approval_required": blueprint["approval_required"],
                                "setup_notes": blueprint["setup_notes"],
                                "files": blueprint["files"],
                            },
                            "reason": action.get("reason") or "Scaffold a custom tool to close the capability gap.",
                        }
                        resume_payload = {
                            "state": state,
                            "step_id": step["id"],
                            "tool_name": "custom_tool_create",
                        }
                        approval_info: dict[str, Any]
                        if self.state_store and task_id:
                            approval = self.state_store.create_approval(
                                task_id=task_id,
                                operation="custom_tool_create",
                                command=str(scaffold_result.approval_payload.get("summary") or blueprint["name"]),
                                payload=scaffold_result.approval_payload,
                                resume=resume_payload,
                            )
                            approval_info = {
                                "id": approval["id"],
                                "task_id": task_id,
                                "operation": "custom_tool_create",
                                "summary": scaffold_result.approval_payload.get("summary") or scaffold_result.message,
                            }
                            self._persist_state(task_id, state, status="waiting_approval")
                        else:
                            approval_info = {
                                "id": "",
                                "task_id": task_id,
                                "operation": "custom_tool_create",
                                "summary": scaffold_result.approval_payload.get("summary") or scaffold_result.message,
                            }
                        self._emit("approval_needed", approval_info["summary"], approvalId=approval_info["id"], approval=approval_info, step_id=step["id"])
                        return NativeAgentOutcome(
                            status="waiting_approval",
                            message=approval_info["summary"],
                            provider=provider,
                            model=model,
                            plan=plan,
                            approval=approval_info,
                            artifacts=list(state.get("artifacts", [])),
                            state=state,
                        )

                    missing_message = str(
                        action.get("summary")
                        or action.get("reason")
                        or "A required capability or prerequisite is missing."
                    ).strip()
                    state["final_response_draft"] = missing_message
                    step["status"] = "complete"
                    state["completed_steps"].append(step["id"])
                    self._emit("step_complete", missing_message, step_id=step["id"], step=step)
                    break

                if action_type == "finish":
                    summary = str(action.get("summary") or step["description"]).strip()
                    scope = str(action.get("scope") or "step").strip().lower()
                    step["status"] = "complete"
                    state["completed_steps"].append(step["id"])
                    if scope == "task" or current_index == len(steps) - 1:
                        state["final_response_draft"] = summary
                    self._emit("step_complete", summary, step_id=step["id"], step=step)
                    break

            if step["status"] != "complete":
                if not step.get("preferred_tools"):
                    generated_output, provider, model = await self._generate_step_output(state, step)
                    if generated_output:
                        state["provider"] = provider
                        state["model"] = model
                        state.setdefault("step_outputs", {})[step["id"]] = {
                            "content": generated_output,
                            "summary": step["description"],
                        }
                        step["status"] = "complete"
                        state["completed_steps"].append(step["id"])
                        self._emit("step_complete", step["description"], step_id=step["id"], step=step)
                        self._persist_state(task_id, state, status="running")
                        current_index += 1
                        state["current_step_index"] = current_index
                        self._persist_state(task_id, state, status="running")
                        continue
                if "write_file" in step.get("preferred_tools", []):
                    if not self._latest_step_output(state, step):
                        generated_output, provider, model = await self._generate_step_output(state, step)
                        if generated_output:
                            state["provider"] = provider
                            state["model"] = model
                            state.setdefault("step_outputs", {})[step["id"]] = {
                                "content": generated_output,
                                "summary": step["description"],
                            }
                    write_args = self._infer_write_file_arguments(state, step)
                    if write_args and not state.get("forced_action") and not state.get("pending_action"):
                        state["forced_action"] = {
                            "action": "tool_call",
                            "tool_name": "write_file",
                            "arguments": write_args,
                            "reason": "Fallback write_file execution for a stalled file-generation step.",
                        }
                        state.setdefault("complete_step_after_tool", {})[step["id"]] = "write_file"
                        self._persist_state(task_id, state, status="running")
                        continue
                if "render_svg_asset" in step.get("preferred_tools", []):
                    render_args = self._infer_svg_render_arguments(state, step)
                    if render_args and not state.get("forced_action") and not state.get("pending_action"):
                        state["forced_action"] = {
                            "action": "tool_call",
                            "tool_name": "render_svg_asset",
                            "arguments": render_args,
                            "reason": "Fallback SVG render execution for a stalled SVG conversion step.",
                        }
                        state.setdefault("complete_step_after_tool", {})[step["id"]] = "render_svg_asset"
                        self._persist_state(task_id, state, status="running")
                        continue
                self._persist_state(task_id, state, status="failed", error=f"Step failed or stalled: {step['description']}")
                return NativeAgentOutcome(
                    status="failed",
                    message=f"Step failed or stalled: {step['description']}",
                    provider=provider,
                    model=model,
                    plan=plan,
                    artifacts=list(state.get("artifacts", [])),
                    state=state,
                )

            current_index += 1
            state["current_step_index"] = current_index
            self._persist_state(task_id, state, status="running")

        draft_response = str(state.get("final_response_draft") or "").strip()
        if not draft_response:
            summary_response, provider, model = await self._direct_response(
                f"Summarize the completed work for this goal using the provided evidence only.\n\nGoal:\n{goal}\n\nEvidence:\n{json.dumps(state.get('tool_evidence', []), indent=2)}",
                [],
            )
            draft_response = summary_response

        verifier_payload, provider, model = await self._verify_response(state, draft_response)
        final_response = str(verifier_payload.get("final_response") or draft_response).strip()
        if not verifier_payload.get("ok", True):
            final_response = draft_response

        state["provider"] = provider
        state["model"] = model
        self._persist_state(
            task_id,
            state,
            status="completed",
            result={
                "message": final_response,
                "provider": provider,
                "model": model,
                "plan": plan,
                "artifacts": state.get("artifacts", []),
            },
        )
        return NativeAgentOutcome(
            status="completed",
            message=final_response,
            provider=provider,
            model=model,
            plan=plan,
            artifacts=list(state.get("artifacts", [])),
            state=state,
        )
