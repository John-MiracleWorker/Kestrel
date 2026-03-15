from __future__ import annotations

from agent.core.executor_shared import *
from agent.core.executor_core import TaskExecutorCoreMixin
from agent.core.executor_tools import TaskExecutorToolsMixin


class TaskExecutor(TaskExecutorToolsMixin, TaskExecutorCoreMixin):
    async def _reason_and_act(
        self,
        task: AgentTask,
        step: Any,
    ) -> AsyncIterator[TaskEvent]:
        # ── Deerflow 2.0 Aggressive Context Summarization ────────────────
        if task.iterations > 10 and hasattr(self._tools, "_loop") and hasattr(self._tools._loop, "_reflection"):
            # If we're deep into a complex task, summarize the observation trace
            # to prevent blowing out the context window.
            raw_obs = "\n".join(
                f"[{tc.get('tool', '?')}] → {tc.get('result', '?')}"
                for tc in step.tool_calls
            ) or "(none yet)"
            
            try:
                summary = await self._tools._loop._reflection.reflect(
                    goal=task.goal,
                    observations=raw_obs,
                    plan=json.dumps(task.plan.to_dict()) if task.plan else "",
                )
                observations = f"Aggressive Summary (Iter {task.iterations}):\n{summary}"
            except Exception as e:
                logger.warning(f"Failed to aggressively summarize context: {e}")
                observations = raw_obs
        else:
            observations = "\n".join(
                f"[{tc.get('tool', '?')}] → {tc.get('result', '?')}"
                for tc in step.tool_calls
            ) or "(none yet)"

        done, total = task.plan.progress

        # Build diagnostic context from tracked attempts
        tracker = self._get_tracker(step.id)
        diagnostic_context = tracker.build_diagnostic_prompt()

        if task.messages:
            messages = list(task.messages)
            
            # Inject the agent execution rules and context so the LLM
            # knows it's executing a specific step in a plan.
            # Use compact prompt for local models to reduce input tokens
            _is_local_strategy = str(getattr(self._model_router, '_strategy', '')) == 'local_first'
            _prompt_template = AGENT_SYSTEM_PROMPT_LOCAL if _is_local_strategy else AGENT_SYSTEM_PROMPT
            system_prompt = _prompt_template.format(
                goal=task.goal,
                step_description=step.description,
                step_index=step.index + 1,
                total_steps=total,
                iteration=task.iterations,
                max_iterations=task.config.max_iterations,
                observations=observations,
                diagnostic_context=("\n" + diagnostic_context + "\n") if diagnostic_context else "",
            )
            # Inject persona context if available
            if self._persona_context:
                system_prompt += f"\n\n{self._persona_context}"

            messages.append({
                "role": "user",
                "content": f"[System Instructions]\n{system_prompt}",
            })
            
            # Group tool calls by turn_id so parallel tool calls from
            # the same LLM response stay in a single assistant message.
            # This is required for Gemini's thought_signature validation.
            recent_tcs = step.tool_calls[-10:]
            grouped_turns = {}
            for tc in recent_tcs:
                tid = tc.get("turn_id", tc.get("id", str(uuid.uuid4())))
                if tid not in grouped_turns:
                    grouped_turns[tid] = []
                grouped_turns[tid].append(tc)

            for tid, calls in grouped_turns.items():
                tcs = []
                for tc in calls:
                    tcs.append({
                        "id": tc.get("id", "call_1"),
                        "type": "function",
                        "function": {
                            "name": tc.get("tool", ""),
                            "arguments": json.dumps(tc.get("args", {})),
                        },
                        **({"_gemini_raw_part": tc["_gemini_raw_part"]} if "_gemini_raw_part" in tc else {}),
                    })

                messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": tcs,
                })

                for tc in calls:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id", "call_1"),
                        "content": tc.get("result", ""),
                    })
        else:
            _is_local_strategy = str(getattr(self._model_router, '_strategy', '')) == 'local_first'
            _prompt_template = AGENT_SYSTEM_PROMPT_LOCAL if _is_local_strategy else AGENT_SYSTEM_PROMPT
            system_prompt = _prompt_template.format(
                goal=task.goal,
                step_description=step.description,
                step_index=step.index + 1,
                total_steps=total,
                iteration=task.iterations,
                max_iterations=task.config.max_iterations,
                observations=observations,
                diagnostic_context=("\n" + diagnostic_context + "\n") if diagnostic_context else "",
            )
            # Inject persona context if available
            if self._persona_context:
                system_prompt += f"\n\n{self._persona_context}"

            messages = [{"role": "system", "content": system_prompt}]

            if not step.tool_calls:
                messages.append({
                    "role": "user",
                    "content": f"Execute this step: {step.description}",
                })
            else:
                # Show up to 10 recent tool calls; if older calls were
                # trimmed, prepend a compact summary so the LLM knows
                # what it already tried (prevents redundant retries).
                recent_window = 10
                skipped = step.tool_calls[:-recent_window] if len(step.tool_calls) > recent_window else []
                recent = step.tool_calls[-recent_window:]

                continue_msg = f"Continue executing: {step.description}"
                if skipped:
                    summary_lines = []
                    for tc in skipped:
                        status = "✓" if tc.get("success") else "✗"
                        summary_lines.append(
                            f"  {status} {tc.get('tool', '?')}({json.dumps(tc.get('args', {}))[:60]})"
                        )
                    continue_msg += (
                        f"\n\nEarlier attempts ({len(skipped)} calls, summarized):\n"
                        + "\n".join(summary_lines)
                    )

                messages.append({
                    "role": "user",
                    "content": continue_msg,
                })
                grouped_turns = {}
                for tc in recent:
                    tid = tc.get("turn_id", tc.get("id", str(uuid.uuid4())))
                    if tid not in grouped_turns:
                        grouped_turns[tid] = []
                    grouped_turns[tid].append(tc)

                for tid, calls in grouped_turns.items():
                    tcs = []
                    for tc in calls:
                        tcs.append({
                            "id": tc.get("id", "call_1"),
                            "type": "function",
                            "function": {
                                "name": tc.get("tool", ""),
                                "arguments": json.dumps(tc.get("args", {})),
                            },
                            **({"_gemini_raw_part": tc["_gemini_raw_part"]} if "_gemini_raw_part" in tc else {}),
                        })

                    messages.append({
                        "role": "assistant",
                        "content": None,
                        "tool_calls": tcs,
                    })

                    for tc in calls:
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.get("id", "call_1"),
                            "content": tc.get("result", ""),
                        })

        # ── Conversational shortcut ────────────────────────────────
        # For simple chat messages (greetings, small talk, basic Q&A),
        # skip tool-calling entirely and stream a direct text response.
        # This prevents the LLM from proactively calling tools like
        # moltbook when the user just says "hey kestrel!".
        #
        # Criteria for the shortcut:
        #   1. Chat mode (task.messages is set — came from StreamChat)
        #   2. No prior tool calls on this step (first iteration)
        #   3. Step was pre-classified as simple ("Respond to the user")
        _is_simple_chat = (
            task.messages
            and not step.tool_calls
            and (
                step.description.startswith("Respond to the user")
                or step.description.startswith("Ask the user")
            )
        )

        if _is_simple_chat:
            # Stream directly — no tools offered to the LLM
            logger.info("Simple chat shortcut: streaming without tools")

            route = self._model_router.select(
                step_description=step.description,
                expected_tools=[],
            )
            routed_model = route.model if route.model else self._model
            routed_temp = route.temperature
            routed_max_tokens = route.max_tokens
            if self._provider_resolver and route.provider:
                try:
                    active_provider = self._provider_resolver(route.provider)
                except Exception:
                    active_provider = self._provider
            else:
                active_provider = self._provider

            # If the selected provider isn't ready (e.g. ollama from Docker),
            # fall back to a cloud provider — but NOT if user explicitly set a local model
            if (
                hasattr(active_provider, 'is_ready')
                and not active_provider.is_ready()
                and not self._has_explicit_model
            ):
                logger.info(f"Provider {route.provider} not ready, trying cloud fallback")
                if self._provider_resolver:
                    for cloud_name in ("google", "openai", "anthropic"):
                        try:
                            cloud_p = self._provider_resolver(cloud_name)
                            actual_provider_name = getattr(cloud_p, "provider", "")
                            if actual_provider_name == cloud_name and cloud_p.is_ready():
                                active_provider = cloud_p
                                routed_model = ""  # Use provider's default
                                logger.info(f"Fell back to {cloud_name}")
                                break
                        except Exception:
                            continue

            # Apply context compaction + cloud escalation (same as full path)
            try:
                from agent.context_compactor import compact_context, needs_escalation

                messages, was_compacted = await compact_context(
                    messages=messages,
                    provider_name=route.provider,
                    provider=active_provider,
                    model=routed_model,
                )

                if was_compacted and needs_escalation(messages, route.provider, model=routed_model) and not self._has_explicit_model:
                    if self._provider_resolver:
                        for cloud_name in ("google", "openai", "anthropic"):
                            try:
                                cloud_p = self._provider_resolver(cloud_name)
                                actual_provider_name = getattr(cloud_p, "provider", "")
                                if actual_provider_name == cloud_name and cloud_p.is_ready():
                                    logger.info(
                                        f"Context overflow: escalating {route.provider} → {cloud_name}"
                                    )
                                    active_provider = cloud_p
                                    routed_model = ""
                                    break
                            except Exception:
                                continue
            except ImportError:
                pass
            except Exception as e:
                logger.warning(f"Context compaction failed (non-fatal): {e}")

            if self._event_callback:
                try:
                    await self._event_callback("routing_info", {
                        "provider": route.provider,
                        "model": routed_model,
                        "was_escalated": False,
                        "complexity": 0,
                    })
                except Exception:
                    pass

            # Stream tokens directly so the user sees real-time output.
            # Parse <think>...</think> tags into THINKING events.
            streamed_text = []
            think_buffer = []
            in_think = False

            async def _do_stream(provider_to_use, model_to_use):
                nonlocal in_think
                async for token in provider_to_use.stream(
                    messages=messages,
                    model=model_to_use,
                    temperature=routed_temp,
                    max_tokens=routed_max_tokens,
                ):
                    # Detect <think> tags
                    combined = "".join(think_buffer) + token if think_buffer else token

                    if not in_think and "<think>" in combined:
                        in_think = True
                        # Split: before <think> goes as content, after starts thinking
                        before = combined.split("<think>", 1)[0]
                        after = combined.split("<think>", 1)[1]
                        if before.strip():
                            streamed_text.append(before)
                            yield TaskEvent(
                                type=TaskEventType.STEP_COMPLETE,
                                task_id=task.id,
                                step_id=step.id,
                                content=before,
                            )
                        think_buffer.clear()
                        think_buffer.append(after)
                        continue

                    if in_think:
                        think_buffer.append(token)
                        joined = "".join(think_buffer)
                        if "</think>" in joined:
                            # End of thinking block
                            think_content = joined.split("</think>", 1)[0]
                            remainder = joined.split("</think>", 1)[1]
                            in_think = False
                            think_buffer.clear()
                            # Emit thinking event
                            if think_content.strip():
                                yield TaskEvent(
                                    type=TaskEventType.THINKING,
                                    task_id=task.id,
                                    step_id=step.id,
                                    content=think_content.strip(),
                                )
                            # Emit any text after </think>
                            if remainder.strip():
                                streamed_text.append(remainder)
                                yield TaskEvent(
                                    type=TaskEventType.STEP_COMPLETE,
                                    task_id=task.id,
                                    step_id=step.id,
                                    content=remainder,
                                )
                        continue

                    # Regular token — stream it
                    streamed_text.append(token)
                    yield TaskEvent(
                        type=TaskEventType.STEP_COMPLETE,
                        task_id=task.id,
                        step_id=step.id,
                        content=token,
                    )

                # Flush any remaining think buffer as thinking
                if think_buffer:
                    remaining = "".join(think_buffer)
                    if remaining.strip():
                        if in_think:
                            yield TaskEvent(
                                type=TaskEventType.THINKING,
                                task_id=task.id,
                                step_id=step.id,
                                content=remaining.strip(),
                            )
                        else:
                            streamed_text.append(remaining)
                            yield TaskEvent(
                                type=TaskEventType.STEP_COMPLETE,
                                task_id=task.id,
                                step_id=step.id,
                                content=remaining,
                            )
                    think_buffer.clear()

            try:
                async for event in _do_stream(active_provider, routed_model):
                    yield event
            except Exception as chat_err:
                # ── Cloud failover for simple chat path ──────────────
                from providers.ollama import OllamaUnavailableError
                from providers.lmstudio import LMStudioUnavailableError
                is_local_failure = (
                    isinstance(chat_err, (OllamaUnavailableError, LMStudioUnavailableError))
                    or 'timeout' in str(chat_err).lower()
                    or 'connect' in str(chat_err).lower()
                )
                if is_local_failure and self._provider_resolver:
                    logger.warning(
                        f"Simple chat: {route.provider} failed ({chat_err}), "
                        f"attempting cloud failover..."
                    )
                    from providers_registry import get_cloud_fallback
                    fallback = get_cloud_fallback()
                    if fallback:
                        cloud_name, cloud_p = fallback
                        logger.info(f"Simple chat: falling back to {cloud_name}")
                        async for event in _do_stream(cloud_p, ""):
                            yield event
                    else:
                        raise
                else:
                    raise

            text = "".join(streamed_text)
            logger.info(
                f"Simple chat result: {len(text)} chars, "
                f"preview={text[:100]!r}"
            )
            step.status = StepStatus.COMPLETE
            step.result = _strip_think(text)
            step.completed_at = datetime.now(timezone.utc)
            await self._persistence.update_task(task)
            return

        # ── Full tool-calling path (complex messages / agentic tasks) ──

        # Select relevant tools instead of sending all 38 schemas.
        # This keeps the context small for local models and improves accuracy.
        step_expected = getattr(step, 'expected_tools', None)

        route = self._model_router.select(
            step_description=step.description,
            expected_tools=step_expected,
        )

        routed_model = route.model if route.model else self._model
        routed_temp = route.temperature
        routed_max_tokens = route.max_tokens

        if self._provider_resolver and route.provider:
            try:
                active_provider = self._provider_resolver(route.provider)
            except Exception:
                active_provider = self._provider
        else:
            active_provider = self._provider

        try:
            from agent.tool_selector import ToolSelector
            selector = ToolSelector(self._tools)
            is_local = route.provider in ("ollama", "local", "lmstudio")
            runtime_mode = os.getenv("KESTREL_RUNTIME_MODE", "docker")
            intent_tags: list[str] = []
            approval_state = (
                task.pending_approval.status.value
                if task.pending_approval and getattr(task.pending_approval, "status", None)
                else "pending"
            )

            if is_local:
                # Local models: use instant local ranking.
                selected_tools = selector.select(
                    step_description=step.description,
                    expected_tools=step_expected,
                    provider=route.provider,
                    runtime_mode=runtime_mode,
                    intent_tags=intent_tags,
                    approval_state=approval_state,
                )
            else:
                # Cloud models use the same local ranking to avoid a second model call.
                try:
                    selected_tools = await selector.select_with_llm(
                        step_description=step.description,
                        provider=active_provider,
                        model=routed_model,
                        api_key=self._api_key,
                        expected_tools=step_expected,
                        runtime_mode=runtime_mode,
                        intent_tags=intent_tags,
                        approval_state=approval_state,
                    )
                except Exception as e:
                    logger.warning(f"LLM tool selection failed: {e}, using keyword fallback")
                    selected_tools = selector.select(
                        step_description=step.description,
                        expected_tools=step_expected,
                        provider=route.provider,
                        runtime_mode=runtime_mode,
                        intent_tags=intent_tags,
                        approval_state=approval_state,
                    )

            tool_schemas = [t.to_openai_schema() for t in selected_tools]
        except Exception as e:
            logger.warning(f"ToolSelector failed, using all tools: {e}")
            tool_schemas = [t.to_openai_schema() for t in self._tools.list_tools()]

        try:
            from agent.context_compactor import compact_context, needs_escalation

            messages, was_compacted = await compact_context(
                messages=messages,
                provider_name=route.provider,
                provider=active_provider,
                model=routed_model,
            )

            if was_compacted and needs_escalation(messages, route.provider, model=routed_model) and not self._has_explicit_model:
                if self._provider_resolver:
                    for cloud_name in ("google", "openai", "anthropic"):
                        try:
                            cloud_p = self._provider_resolver(cloud_name)
                            # Provider resolver may silently fall back to ollama if cloud is not ready.
                            # Ensure we actually got the cloud provider we asked for before blanking the model.
                            actual_provider_name = getattr(cloud_p, "provider", "")
                            if actual_provider_name == cloud_name and cloud_p.is_ready():
                                logger.info(
                                    f"Context overflow: escalating {route.provider} → {cloud_name}"
                                )
                                active_provider = cloud_p
                                routed_model = ""
                                break
                        except Exception:
                            continue
        except ImportError:
            pass
        except Exception as e:
            logger.warning(f"Context compaction failed (non-fatal): {e}")

        if self._event_callback:
            try:
                await self._event_callback("routing_info", {
                    "provider": route.provider,
                    "model": routed_model or route.model,
                    "was_escalated": getattr(route, '_escalated', False),
                    "complexity": getattr(route, '_complexity', 0),
                })
            except Exception:
                pass

        logger.debug(
            f"Dispatching to {route.provider}:{routed_model} "
            f"(step={step.description[:50]}...)"
        )

        try:
            # Notify UI that we are blocked on tool generation
            # Extremely important for local models like Qwen 3.5 that "think"
            # for 90 seconds without streaming during tool calls.
            yield TaskEvent(
                type=TaskEventType.THINKING,
                task_id=task.id,
                step_id=step.id,
                content="Analyzing prompt to select optimal tools...",
            )
        except Exception:
            pass

        try:
            response = await active_provider.generate_with_tools(
                messages=messages,
                model=routed_model,
                tools=tool_schemas,
                temperature=routed_temp,
                max_tokens=routed_max_tokens,
                api_key=self._api_key,
            )

            # Check for error content from the provider (not an exception)
            content = response.get("content", "")
            if content.startswith("[Error:") and not response.get("tool_calls"):
                raise RuntimeError(content)

        except Exception as llm_err:
            # ── Graceful cloud failover ──────────────────────────────
            # If the local provider failed, swap to a cloud provider and
            # retry with the same context.
            #
            # ALWAYS failover when a local provider is genuinely unavailable:
            #   - OllamaUnavailableError / LMStudioUnavailableError
            #   - 429 rate limit
            # These indicate the provider cannot serve ANY request, so
            # respecting _has_explicit_model would just cause task failure.
            from providers.ollama import OllamaUnavailableError
            from providers.lmstudio import LMStudioUnavailableError
            is_local_down = isinstance(llm_err, (OllamaUnavailableError, LMStudioUnavailableError))
            is_rate_limited = '429' in str(llm_err)
            is_timeout = 'timeout' in str(llm_err).lower()
            is_connection_error = 'connect' in str(llm_err).lower()
            should_always_failover = is_local_down or is_rate_limited or is_timeout or is_connection_error

            if route.provider in ("ollama", "lmstudio", "local") and self._provider_resolver and (
                not self._has_explicit_model or should_always_failover
            ):
                failover_reason = (
                    "Local provider unavailable" if is_local_down else
                    "429 rate limit" if is_rate_limited else
                    "timeout" if is_timeout else
                    "connection error" if is_connection_error else
                    "error"
                )
                logger.warning(
                    f"Provider {route.provider} failed: {type(llm_err).__name__}: {llm_err}. "
                    f"Attempting cloud failover ({failover_reason})..."
                )
                # Build failover list: workspace's configured provider first
                configured = getattr(self._provider, 'provider', '')
                cloud_order = [configured] if configured in ("google", "openai", "anthropic") else []
                for c in ("google", "openai", "anthropic"):
                    if c not in cloud_order:
                        cloud_order.append(c)

                for cloud_name in cloud_order:
                    try:
                        # Use get_provider directly — NOT self._provider_resolver
                        # which is resolve_provider() and would fall back to ollama.
                        from providers_registry import get_provider
                        cloud_p = get_provider(cloud_name)
                        if not cloud_p.is_ready():
                            continue

                        # Re-select tools with cloud limits (can handle more)
                        try:
                            from agent.tool_selector import ToolSelector
                            selector = ToolSelector(self._tools)
                            cloud_tools = selector.select(
                                step_description=step.description,
                                expected_tools=step_expected,
                                provider=cloud_name,
                                runtime_mode=runtime_mode,
                                intent_tags=intent_tags,
                                approval_state=approval_state,
                            )
                            cloud_schemas = [t.to_openai_schema() for t in cloud_tools]
                        except Exception:
                            cloud_schemas = tool_schemas

                        logger.info(
                            f"Cloud failover: {route.provider} → {cloud_name} "
                            f"({len(cloud_schemas)} tools)"
                        )

                        if self._event_callback:
                            try:
                                await self._event_callback("routing_info", {
                                    "provider": cloud_name,
                                    "model": "",
                                    "was_escalated": True,
                                    "complexity": 0,
                                })
                            except Exception:
                                pass

                        try:
                            yield TaskEvent(
                                type=TaskEventType.THINKING,
                                task_id=task.id,
                                step_id=step.id,
                                content=f"Analyzing prompt with {cloud_name} fallback...",
                            )
                        except Exception:
                            pass

                        response = await cloud_p.generate_with_tools(
                            messages=messages,
                            model="",  # Use provider's default
                            tools=cloud_schemas,
                            temperature=routed_temp,
                            max_tokens=max(routed_max_tokens, 8192),
                            api_key=self._api_key,
                        )
                        # Update active_provider for subsequent logic
                        active_provider = cloud_p
                        break
                    except Exception as cloud_err:
                        import traceback
                        logger.warning(
                            f"Cloud failover to {cloud_name} failed: "
                            f"{type(cloud_err).__name__}: {cloud_err!r}\n"
                            f"{traceback.format_exc()}"
                        )
                        continue
                else:
                    # All cloud providers failed too
                    logger.error(f"All providers failed for step: {llm_err}")
                    step.status = StepStatus.FAILED
                    step.error = f"All providers failed: {str(llm_err)[:300]}"
                    await self._persistence.update_task(task)
                    yield TaskEvent(
                        type=TaskEventType.TASK_FAILED,
                        task_id=task.id,
                        step_id=step.id,
                        content=step.error,
                        progress=self._progress_callback(task),
                    )
                    return
            else:
                # Non-local provider failed — just fail the step
                logger.error(f"LLM API error during step execution: {llm_err}", exc_info=True)
                step.status = StepStatus.FAILED
                step.error = f"LLM API error: {str(llm_err)[:300]}"
                await self._persistence.update_task(task)
                yield TaskEvent(
                    type=TaskEventType.TASK_FAILED,
                    task_id=task.id,
                    step_id=step.id,
                    content=step.error,
                    progress=self._progress_callback(task),
                )
                return

        if response.get("usage"):
            usage = response["usage"]
            self._metrics.record_llm_call(
                model=self._model,
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                cached_tokens=usage.get("cached_tokens", 0),
            )
            if self._event_callback:
                await self._event_callback("metrics_update", self._metrics.metrics.to_compact_dict())

        if response.get("tool_calls"):
            tool_calls = response["tool_calls"]
            logger.info(
                f"LLM returned {len(tool_calls)} tool call(s): "
                f"{[tc.get('function', {}).get('name', '?') for tc in tool_calls]}"
            )

            # Reset text-only streak — LLM is actively using tools
            self._text_only_streak[step.id] = 0

            # Capture LLM's text content alongside tool calls
            # (e.g. "I'll check the MCP server..." before calling tools).
            # This ensures step.result has meaningful content even if
            # task_complete is never explicitly called.
            if response.get("content") and not step.result:
                step.result = _strip_think(response["content"])

            if len(tool_calls) > 1:
                logger.info(f"LLM returned {len(tool_calls)} tool calls — dispatching in parallel")

                # Assign a unified turn_id to all tool calls from this batch
                turn_id = str(uuid.uuid4())
                for tc in tool_calls:
                    tc["turn_id"] = turn_id

                async for event in self._execute_tools_parallel(tool_calls, task, step):
                    yield event
            else:
                tc = tool_calls[0]
                tc["turn_id"] = str(uuid.uuid4())
                async for event in self._execute_single_tool(tc, task, step):
                    yield event

        elif response.get("content"):
            text = response["content"]

            logger.info(
                f"LLM returned text-only (no tool calls): "
                f"streak={self._text_only_streak.get(step.id, 0)+1}, "
                f"total={self._text_only_total.get(step.id, 0)+1}, "
                f"preview={text[:80]!r}"
            )

            yield TaskEvent(
                type=TaskEventType.THINKING,
                task_id=task.id,
                step_id=step.id,
                content=text,
                progress=self._progress_callback(task),
            )

            await self._handle_text_only_response(task, step, text, total)

        else:
            # LLM returned no tool calls AND no content — this is an API
            # error or empty response.  Mark the step as failed so the
            # agent loop can retry or surface the error instead of silently
            # falling through to "Task completed successfully."
            logger.warning(
                f"LLM returned empty response for step '{step.description[:80]}' "
                f"(provider={getattr(active_provider, 'provider', '?')}, model={routed_model})"
            )
            step.status = StepStatus.FAILED
            step.error = (
                "LLM returned an empty response (no content and no tool calls). "
                "This usually means the API rejected the request or the model is unavailable."
            )
            await self._persistence.update_task(task)
