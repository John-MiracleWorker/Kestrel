import os
from pathlib import Path

source = Path("services/chat_service.py")
content = source.read_text(encoding="utf-8")

# Let's do targeted string replacements
# Replace blocks 1 and 2

start_token_1 = "            # ── 1. Load workspace provider config ───────────────────"
end_token_1 = "            # ── 3. Save user message before streaming ───────────────"

new_block_1 = """            # ── 1 & 2. Load context, history, and RAG ───────────────
            pool = await get_pool()
            r = await get_redis()
            ws_config = await ProviderConfig(pool).get_config(workspace_id)
            provider_name = request.provider or ws_config["provider"]
            model = request.model or ws_config["model"]
            
            # Resolve API Key from Redis if it's a reference
            api_key = ws_config.get("api_key", "")
            if api_key and api_key.startswith("provider_key:"):
                try:
                    real_key = await r.get(api_key)
                    api_key = real_key.decode("utf-8") if real_key else ""
                except Exception:
                    api_key = ""
            
            provider = get_provider(provider_name)
            
            from services.context_builder import build_chat_context
            messages = await build_chat_context(
                request, workspace_id, pool, r, runtime, provider_name, model, ws_config, api_key
            )
            
"""

idx1 = content.find(start_token_1)
idx2 = content.find(end_token_1)
content = content[:idx1] + new_block_1 + content[idx2:]

# Replace event parsing loop
start_token_2 = "                # Tuple from the agent loop background task"
end_token_2 = "            # Ensure background task is cleaned up"

new_block_2 = """                # Tuple from the agent loop background task
                if isinstance(item, tuple):
                    from services.tool_parser import parse_agent_event
                    async for response_chunk in parse_agent_event(
                        item, full_response_parts, tool_results_gathered, 
                        provider, model, api_key, self._make_response
                    ):
                        yield response_chunk

"""

idx3 = content.find(start_token_2)
idx4 = content.find(end_token_2)
content = content[:idx3] + new_block_2 + content[idx4:]

source.write_text(content, encoding="utf-8")
print("Chat service refactored.")
