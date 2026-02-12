"""
Libre Bird — Local LLM Engine powered by llama-cpp-python.
Supports GPT-OSS 20B and Qwen 3 14B GGUF models.
Zero cloud. All inference runs on your Mac.
"""

import json
import logging
import os
import asyncio
from typing import AsyncIterator, Optional
from llama_cpp import Llama
from tools import TOOL_DEFINITIONS, execute_tool

logger = logging.getLogger("libre_bird.engine")


# Default model search paths
MODELS_DIR = os.path.join(os.path.dirname(__file__), "models")

# System prompt for context-aware assistance
SYSTEM_PROMPT = """You are Libre Bird, a helpful, context-aware personal AI assistant running locally on the user's Mac. You are privacy-first — no data leaves this device.

WHAT YOU CAN SEE:
- The name and window title of the user's currently active app (provided below as "Current Screen Context" when available)
- Historical logs of past app names and window titles (provided below as "Historical Memory" when available)
- You can use the read_screen tool to capture and OCR all visible text on the user's screen when they ask about what's on their screen
- You can NOT see the actual screen pixels, images, or visual content — only text (metadata or OCR-extracted)

CRITICAL HONESTY RULES:
- NEVER fabricate times, activity details, or information not present in the provided context data
- If the Historical Memory section is empty or missing, say "I don't have activity data for that time period"
- If you're unsure, say so. Do NOT guess or make up plausible-sounding answers
- When reporting past activity, cite ONLY the timestamps and app names shown in the data
- If a tool returns raw file paths or system data, explain what they are honestly — don't present internal system files as user workspaces

Key behaviors:
- Reference the user's current context naturally when relevant
- Help with writing, coding, research, and organization
- Keep responses focused and practical
- If context isn't relevant to the question, don't force it
- Keep your internal reasoning brief and focused. For simple questions, think in 1-2 sentences. Save extended reasoning for complex problems."""


class LLMEngine:
    def __init__(self):
        self._model: Optional[Llama] = None
        self._model_path: Optional[str] = None
        self._loading = False

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    @property
    def model_path(self) -> Optional[str]:
        return self._model_path

    def get_available_models(self) -> list[dict]:
        """Scan models directory for GGUF files."""
        models = []
        if not os.path.exists(MODELS_DIR):
            os.makedirs(MODELS_DIR, exist_ok=True)
            return models

        for f in os.listdir(MODELS_DIR):
            if f.endswith(".gguf"):
                path = os.path.realpath(os.path.join(MODELS_DIR, f))
                size_gb = os.path.getsize(path) / (1024 ** 3)
                models.append({
                    "name": f,
                    "path": path,
                    "size_gb": round(size_gb, 2),
                })
        return sorted(models, key=lambda m: m["name"])

    async def load_model(self, model_path: str, n_ctx: int = 8192,
                         n_gpu_layers: int = -1):
        """Load a GGUF model. n_gpu_layers=-1 offloads all layers to Metal GPU."""
        if self._loading:
            raise RuntimeError("Model is already loading")

        self._loading = True
        try:
            # Run in thread to avoid blocking the event loop
            loop = asyncio.get_event_loop()
            self._model = await loop.run_in_executor(
                None,
                lambda: Llama(
                    model_path=model_path,
                    n_ctx=n_ctx,
                    n_gpu_layers=n_gpu_layers,
                    n_threads=os.cpu_count(),
                    verbose=False,
                    flash_attn=True,
                )
            )
            self._model_path = model_path
        finally:
            self._loading = False

    def unload_model(self):
        """Free the model from memory."""
        if self._model:
            del self._model
            self._model = None
            self._model_path = None

    @staticmethod
    def _clean_response(text: str) -> str:
        """Clean model-specific formatting from the response.
        
        Handles:
        - Qwen3 <think>...</think> reasoning blocks
        - GPT-OSS <|channel|>analysis...<|channel|>final<|message|>RESPONSE
        - Stray control tokens
        """
        if not text:
            return text
        
        import re
        
        # 1. Strip Qwen3 thinking blocks: <think>...</think>
        cleaned = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
        
        # 2. Strip tool call/response blocks
        cleaned = re.sub(r'<tool_call>.*?</tool_call>', '', cleaned, flags=re.DOTALL)
        cleaned = re.sub(r'<tool_response>.*?</tool_response>', '', cleaned, flags=re.DOTALL)
        
        # 3. GPT-OSS: extract final channel message
        final_match = re.search(
            r'<\|channel\|>final<\|message\|>(.*?)(?:<\|end\|>|$)',
            cleaned, re.DOTALL
        )
        if final_match:
            cleaned = final_match.group(1)
        
        # 4. Strip any remaining control tokens
        cleaned = re.sub(r'<\|(?:channel|message|start|end|im_start|im_end)\|>[a-z]*', '', cleaned)
        
        return cleaned.strip()

    def _build_messages(self, user_message: str, history: list[dict] = None,
                        context: dict = None, memory: str = None) -> list[dict]:
        """Build the message list with system prompt, context, memory, and history."""
        messages = []

        # System prompt with optional context
        sys_content = SYSTEM_PROMPT
        if context:
            ctx_parts = []
            if context.get("app_name"):
                ctx_parts.append(f"App: {context['app_name']}")
            if context.get("window_title"):
                ctx_parts.append(f"Window: {context['window_title']}")
            if context.get("focused_text"):
                text = context["focused_text"][:2000]  # Limit context size
                ctx_parts.append(f"Screen text: {text}")
            if ctx_parts:
                sys_content += "\n\n--- Current Screen Context ---\n" + "\n".join(ctx_parts)

        # Add historical memory from RAG retrieval
        if memory:
            sys_content += "\n\n--- Historical Memory (past screen activity) ---\n"
            sys_content += memory
            sys_content += "\n\nUse this history to answer the user's question about their past activity. Be specific about times and apps."

        messages.append({"role": "system", "content": sys_content})

        # Add conversation history
        if history:
            for msg in history[-20:]:  # Keep last 20 messages for context
                messages.append({
                    "role": msg["role"],
                    "content": msg["content"],
                })

        # Add current user message
        messages.append({"role": "user", "content": user_message})

        return messages

    async def chat(self, user_message: str, history: list[dict] = None,
                   context: dict = None, memory: str = None,
                   temperature: float = 0.7,
                   max_tokens: int = 2048) -> str:
        """Generate a complete response (non-streaming)."""
        if not self._model:
            raise RuntimeError("No model loaded. Please load a model first.")

        messages = self._build_messages(user_message, history, context, memory)

        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: self._model.create_chat_completion(
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        )

        return self._clean_response(response["choices"][0]["message"]["content"])

    async def chat_stream(self, user_message: str, history: list[dict] = None,
                          context: dict = None, memory: str = None,
                          temperature: float = 0.7,
                          max_tokens: int = 2048) -> AsyncIterator[tuple[str, str]]:
        """Generate a streaming response with tool-calling support.

        Yields tagged tuples: ("tool", name), ("raw", text).
        Always streams. Uses asyncio.Queue to bridge between the synchronous
        llama-cpp stream iterator and the async event loop.
        """
        import re
        import threading

        if not self._model:
            raise RuntimeError("No model loaded. Please load a model first.")

        messages = self._build_messages(user_message, history, context, memory)
        loop = asyncio.get_event_loop()

        _SENTINEL = object()  # marks end of stream

        MAX_TOOL_ROUNDS = 3
        for round_num in range(MAX_TOOL_ROUNDS + 1):
            queue: asyncio.Queue = asyncio.Queue()

            def _stream_worker():
                """Run synchronous llama-cpp streaming in a thread,
                pushing tokens into the async queue."""
                try:
                    stream = self._model.create_chat_completion(
                        messages=messages,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        stream=True,
                        tools=TOOL_DEFINITIONS if round_num < MAX_TOOL_ROUNDS else None,
                    )
                    for chunk in stream:
                        delta = chunk["choices"][0].get("delta", {})
                        token = delta.get("content", "")
                        if token:
                            loop.call_soon_threadsafe(queue.put_nowait, token)
                except Exception as e:
                    logger.error(f"Stream worker error: {e}")
                finally:
                    loop.call_soon_threadsafe(queue.put_nowait, _SENTINEL)

            # Start streaming in background thread
            thread = threading.Thread(target=_stream_worker, daemon=True)
            thread.start()

            accumulated = ""

            # Yield tokens as they arrive from the queue
            while True:
                item = await queue.get()
                if item is _SENTINEL:
                    break
                accumulated += item
                yield ("raw", item)

            thread.join(timeout=5)

            # Check if the accumulated content contains tool calls
            xml_tool_calls = re.findall(
                r'<tool_call>\s*(\{.*?\})\s*</tool_call>', accumulated, re.DOTALL
            )

            if xml_tool_calls and round_num < MAX_TOOL_ROUNDS:
                # Tool call detected — execute and loop
                messages.append({"role": "assistant", "content": accumulated})
                tool_results = []
                for tc_json in xml_tool_calls:
                    try:
                        tc_data = json.loads(tc_json)
                        fn_name = tc_data.get("name", "")
                        fn_args = tc_data.get("arguments", {})
                        logger.info(f"Tool call XML (round {round_num + 1}): {fn_name}({fn_args})")
                        yield ("tool", fn_name)
                        result = execute_tool(fn_name, fn_args)
                        tool_results.append(f"<tool_response>\n{result}\n</tool_response>")
                    except json.JSONDecodeError:
                        logger.warning(f"Failed to parse tool call: {tc_json}")
                        continue

                if tool_results:
                    messages.append({
                        "role": "user",
                        "content": "\n".join(tool_results),
                    })
                continue

            # No tool calls — tokens already yielded, we're done
            return

    async def generate_title(self, first_message: str) -> str:
        """Generate a short conversation title from the first message."""
        if not self._model:
            return first_message[:50]

        prompt_messages = [
            {"role": "system", "content": "Generate a very short title (3-6 words) for this conversation. Reply with ONLY the title, nothing else. /no_think"},
            {"role": "user", "content": first_message},
        ]

        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: self._model.create_chat_completion(
                messages=prompt_messages,
                temperature=0.3,
                max_tokens=50,
            )
        )

        raw = response["choices"][0]["message"]["content"]
        title = self._clean_response(raw).strip().strip('"\'')
        return title[:60]

    async def generate_journal(self, context_entries: list[dict]) -> dict:
        """Generate a daily journal summary from context snapshots."""
        if not self._model:
            raise RuntimeError("No model loaded")

        # Build a timeline of activities
        timeline = []
        for entry in context_entries:
            timeline.append(
                f"[{entry.get('timestamp', '?')}] "
                f"{entry.get('app_name', 'Unknown')} — {entry.get('window_title', '')}"
            )

        timeline_text = "\n".join(timeline[-100:])  # Limit to last 100 entries

        prompt = f"""Based on the following timeline of the user's screen activity today, generate:
1. A brief, natural-language summary of what they worked on (2-3 paragraphs)
2. A JSON array of distinct activities/projects they worked on
3. A JSON array of any actionable tasks or follow-ups you noticed

Respond in this exact JSON format:
{{
  "summary": "Your summary here...",
  "activities": ["Activity 1", "Activity 2"],
  "tasks": ["Task 1", "Task 2"]
}}

Timeline:
{timeline_text}"""

        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: self._model.create_chat_completion(
                messages=[
                    {"role": "system", "content": "You are a helpful assistant that generates daily activity journals. Always respond with valid JSON."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.5,
                max_tokens=1500,
            )
        )

        import json
        content = response["choices"][0]["message"]["content"]
        try:
            # Try to extract JSON from the response
            start = content.find("{")
            end = content.rfind("}") + 1
            if start >= 0 and end > start:
                return json.loads(content[start:end])
        except json.JSONDecodeError:
            pass

        return {
            "summary": content,
            "activities": [],
            "tasks": [],
        }


# Singleton instance
engine = LLMEngine()
