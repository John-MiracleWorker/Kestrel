"""
Ollama MCP Server — Exposes local Ollama models as MCP tools over stdio.

Tools provided:
  - ollama_generate: Generate text from an Ollama model
  - ollama_chat: Chat with an Ollama model (multi-turn)
  - ollama_list_models: List installed Ollama models

Implements the MCP protocol (JSON-RPC 2.0 over stdio/JSONL).
"""

import asyncio
import json
import os
import sys

import httpx

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://host.docker.internal:11434")

TOOLS = [
    {
        "name": "ollama_generate",
        "description": "Generate text from a local Ollama model. Supports any installed model.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "The prompt to send to the model",
                },
                "model": {
                    "type": "string",
                    "description": "Ollama model name (e.g., 'llama3', 'qwen3:8b')",
                    "default": "qwen3:8b",
                },
                "system": {
                    "type": "string",
                    "description": "Optional system prompt",
                    "default": "",
                },
                "temperature": {
                    "type": "number",
                    "description": "Sampling temperature (0.0-2.0)",
                    "default": 0.7,
                },
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "ollama_chat",
        "description": "Chat with a local Ollama model using multi-turn messages.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "messages": {
                    "type": "array",
                    "description": "Array of {role, content} message objects",
                    "items": {
                        "type": "object",
                        "properties": {
                            "role": {"type": "string", "enum": ["system", "user", "assistant"]},
                            "content": {"type": "string"},
                        },
                        "required": ["role", "content"],
                    },
                },
                "model": {
                    "type": "string",
                    "description": "Ollama model name",
                    "default": "qwen3:8b",
                },
                "temperature": {
                    "type": "number",
                    "description": "Sampling temperature (0.0-2.0)",
                    "default": 0.7,
                },
            },
            "required": ["messages"],
        },
    },
    {
        "name": "ollama_list_models",
        "description": "List all Ollama models installed on the local machine.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
]


async def handle_tool_call(name: str, arguments: dict) -> dict:
    """Execute an Ollama tool and return MCP-formatted content."""
    base = OLLAMA_HOST.rstrip("/")

    try:
        if name == "ollama_generate":
            model = arguments.get("model", "qwen3:8b")
            prompt = arguments.get("prompt", "")
            system_prompt = arguments.get("system", "")
            temperature = arguments.get("temperature", 0.7)

            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})

            async with httpx.AsyncClient(timeout=300) as client:
                resp = await client.post(
                    f"{base}/api/chat",
                    json={
                        "model": model,
                        "messages": messages,
                        "stream": False,
                        "options": {"temperature": temperature},
                    },
                )
                resp.raise_for_status()
                data = resp.json()

            content = data.get("message", {}).get("content", "")
            return {"content": [{"type": "text", "text": content}]}

        elif name == "ollama_chat":
            model = arguments.get("model", "qwen3:8b")
            messages = arguments.get("messages", [])
            temperature = arguments.get("temperature", 0.7)

            async with httpx.AsyncClient(timeout=300) as client:
                resp = await client.post(
                    f"{base}/api/chat",
                    json={
                        "model": model,
                        "messages": messages,
                        "stream": False,
                        "options": {"temperature": temperature},
                    },
                )
                resp.raise_for_status()
                data = resp.json()

            content = data.get("message", {}).get("content", "")
            return {"content": [{"type": "text", "text": content}]}

        elif name == "ollama_list_models":
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{base}/api/tags")
                resp.raise_for_status()
                data = resp.json()

            models = data.get("models", [])
            model_list = []
            for m in models:
                info = {
                    "name": m.get("name", ""),
                    "size": m.get("size", 0),
                    "parameter_size": m.get("details", {}).get("parameter_size", ""),
                    "family": m.get("details", {}).get("family", ""),
                }
                model_list.append(info)

            return {
                "content": [
                    {"type": "text", "text": json.dumps(model_list, indent=2)}
                ]
            }

        else:
            return {
                "content": [{"type": "text", "text": f"Unknown tool: {name}"}],
                "isError": True,
            }

    except httpx.ConnectError:
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Cannot connect to Ollama at {base}. Is Ollama running?",
                }
            ],
            "isError": True,
        }
    except Exception as e:
        return {
            "content": [{"type": "text", "text": f"Error: {e}"}],
            "isError": True,
        }


def handle_request(request: dict) -> dict:
    """Route a JSON-RPC request to the appropriate handler. Returns the result payload."""
    method = request.get("method", "")
    params = request.get("params", {})

    if method == "initialize":
        return {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "ollama-mcp", "version": "0.1.0"},
        }

    elif method == "notifications/initialized":
        # Notification — no response needed
        return None

    elif method == "tools/list":
        return {"tools": TOOLS}

    elif method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        # Run the async handler synchronously within the event loop
        result = asyncio.get_event_loop().run_until_complete(
            handle_tool_call(tool_name, arguments)
        )
        return result

    else:
        return {"error": {"code": -32601, "message": f"Method not found: {method}"}}


def main():
    """Main stdio loop — reads JSONL from stdin, writes JSONL to stdout."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            request = json.loads(line)
        except json.JSONDecodeError as e:
            error_resp = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse error: {e}"},
            }
            print(json.dumps(error_resp), flush=True)
            continue

        result = handle_request(request)

        # Notifications (no "id" in request) get no response
        if "id" not in request:
            continue

        # Don't send response for notification-style results
        if result is None:
            continue

        # Check if the result is an error
        if isinstance(result, dict) and "error" in result and "code" in result.get("error", {}):
            response = {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "error": result["error"],
            }
        else:
            response = {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "result": result,
            }

        print(json.dumps(response), flush=True)


if __name__ == "__main__":
    main()
