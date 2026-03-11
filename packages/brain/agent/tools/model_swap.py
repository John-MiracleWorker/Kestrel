"""
Model Swap Tool — search and switch models across Ollama and cloud providers.

When a user asks to use a different model (e.g. "switch to llama3", "use gemini pro"),
this tool:
  1. Queries Ollama for installed local models
  2. Queries cloud provider catalogs (Google, OpenAI, Anthropic)
  3. Fuzzy-matches the user's request against all available models
  4. Updates the workspace's active provider and model configuration

Works from any interface: Telegram, web chat, or agent tasks.
"""

import logging
import os
from difflib import SequenceMatcher
from typing import Optional

from agent.types import RiskLevel, ToolDefinition

logger = logging.getLogger("brain.agent.tools.model_swap")


# ── Fuzzy matching helpers ─────────────────────────────────────────


def _normalize(name: str) -> str:
    """Normalize a model name for comparison."""
    return name.lower().strip().replace("-", "").replace("_", "").replace(" ", "")


def _similarity(query: str, candidate: str) -> float:
    """
    Score how well a query matches a candidate model name.
    Uses a combination of:
      - Substring containment (strong signal)
      - SequenceMatcher ratio (fuzzy)
      - Prefix matching (user might type partial names)
    Returns 0.0 to 1.0.
    """
    q = _normalize(query)
    c = _normalize(candidate)

    # Exact match
    if q == c:
        return 1.0

    # Full substring match (e.g. "llama3" in "llama3:latest")
    if q in c:
        # Prefer shorter candidates (more specific match)
        return 0.9 + (0.1 * len(q) / max(len(c), 1))

    # Candidate name (without tag) contains query
    base_c = c.split(":")[0]
    if q in base_c:
        return 0.85 + (0.1 * len(q) / max(len(base_c), 1))

    # Query contains candidate base name
    if base_c in q:
        return 0.7

    # Fuzzy ratio
    ratio = SequenceMatcher(None, q, c).ratio()

    # Bonus for prefix match
    prefix_len = 0
    for a, b in zip(q, c):
        if a == b:
            prefix_len += 1
        else:
            break
    prefix_bonus = 0.1 * (prefix_len / max(len(q), 1))

    return min(1.0, ratio + prefix_bonus)


# ── Core search and swap logic ─────────────────────────────────────


async def _list_all_models() -> list[dict]:
    """
    Gather models from all configured providers.
    Returns a list of dicts: {id, name, provider, source, details}.
    """
    all_models = []

    # 1. Ollama models (local)
    try:
        from providers.ollama import OllamaProvider
        ollama = OllamaProvider()
        if ollama.is_ready():
            ollama_models = await ollama.list_models()
            for m in ollama_models:
                all_models.append({
                    "id": m.get("id", m.get("name", "")),
                    "name": m.get("name", m.get("id", "")),
                    "provider": "ollama",
                    "source": "local",
                    "details": f"{m.get('parameter_size', '')} {m.get('quantization', '')}".strip(),
                })
    except Exception as e:
        logger.warning(f"Failed to list Ollama models: {e}")

    # 2. Cloud provider catalogs (static + dynamic)
    from providers.cloud import MODEL_CATALOG, PROVIDER_CONFIGS

    for provider_name, categories in MODEL_CATALOG.items():
        has_key = bool(os.getenv(PROVIDER_CONFIGS.get(provider_name, {}).get("api_key_env", ""), ""))
        for category, models in categories.items():
            for m in models:
                all_models.append({
                    "id": m["id"],
                    "name": m["id"],
                    "provider": provider_name,
                    "source": "cloud" if has_key else "cloud (no API key)",
                    "details": f"{m.get('ctx', '')} — {m.get('desc', '')}",
                })

    # 3. Try dynamic model registry for any additionally discovered models
    try:
        from core.model_registry import model_registry
        for provider_name in ("google", "openai", "anthropic"):
            try:
                discovered = await model_registry.list_models(provider_name)
                existing_ids = {m["id"] for m in all_models if m["provider"] == provider_name}
                for m in discovered:
                    if m["id"] not in existing_ids:
                        all_models.append({
                            "id": m["id"],
                            "name": m.get("name", m["id"]),
                            "provider": provider_name,
                            "source": "cloud (discovered)",
                            "details": f"ctx: {m.get('context_window', 'unknown')}",
                        })
            except Exception:
                pass
    except Exception:
        pass

    return all_models


async def _search_models(query: str) -> list[dict]:
    """
    Search all available models and return ranked matches.
    Each result includes: id, name, provider, source, details, score.
    """
    all_models = await _list_all_models()

    scored = []
    for m in all_models:
        # Score against both id and name
        score_id = _similarity(query, m["id"])
        score_name = _similarity(query, m["name"])
        score = max(score_id, score_name)

        if score > 0.3:  # Minimum relevance threshold
            scored.append({**m, "score": round(score, 3)})

    # Sort by score (highest first), then by provider preference
    provider_order = {"ollama": 0, "google": 1, "openai": 2, "anthropic": 3}
    scored.sort(key=lambda x: (-x["score"], provider_order.get(x["provider"], 99)))

    return scored[:10]  # Top 10 matches


async def _swap_model(model_id: str, provider: str, workspace_id: str) -> dict:
    """
    Update the workspace's default provider and model.
    Returns success status and details.
    """
    try:
        from db import get_pool
        from providers_registry import set_provider_config
        pool = await get_pool()

        # Map "ollama" to "local" for the provider config table
        config_provider = "local" if provider in ("ollama", "local") else provider

        await set_provider_config(
            workspace_id,
            config_provider,
            {
                "model": model_id,
                "is_default": True,
                "temperature": 0.7,
                "max_tokens": 4096,
            },
        )

        logger.info(f"Model swapped: workspace={workspace_id} → {provider}:{model_id}")
        return {
            "success": True,
            "provider": provider,
            "model": model_id,
            "message": f"Switched to {model_id} ({provider})",
        }

    except Exception as e:
        logger.error(f"Model swap failed: {e}")
        return {
            "success": False,
            "error": str(e),
            "message": f"Failed to switch model: {e}",
        }


# ── Tool handler ───────────────────────────────────────────────────


async def model_swap_handler(
    action: str = "search",
    query: str = "",
    model_id: str = "",
    provider: str = "",
    workspace_id: str = "",
    execution_context=None,
    **kwargs,
) -> dict:
    """
    Model swap tool handler.

    Actions:
      - search: Find models matching a query across all providers
      - swap: Switch to a specific model (requires model_id + provider)
      - list: List all available models grouped by provider
    """
    ws_id = workspace_id or getattr(execution_context, "workspace_id", "")

    if action == "list":
        all_models = await _list_all_models()
        # Group by provider
        by_provider = {}
        for m in all_models:
            by_provider.setdefault(m["provider"], []).append(m)

        lines = []
        for prov, models in by_provider.items():
            lines.append(f"\n## {prov.upper()} ({len(models)} models)")
            for m in models[:15]:  # Limit per provider
                lines.append(f"  - {m['id']}  ({m['source']}) {m.get('details', '')}")
            if len(models) > 15:
                lines.append(f"  ... and {len(models) - 15} more")

        return {
            "success": True,
            "total_models": len(all_models),
            "output": "\n".join(lines),
            "models": all_models,
        }

    elif action == "search":
        if not query:
            return {"success": False, "error": "Query is required for search action"}

        results = await _search_models(query)
        if not results:
            return {
                "success": True,
                "matches": [],
                "output": f"No models found matching '{query}'. Use action='list' to see all available models.",
            }

        lines = [f"Found {len(results)} models matching '{query}':\n"]
        for i, r in enumerate(results, 1):
            score_pct = int(r["score"] * 100)
            lines.append(
                f"  {i}. {r['id']} ({r['provider']}, {r['source']}) — {score_pct}% match"
            )
            if r.get("details"):
                lines.append(f"     {r['details']}")

        # Auto-suggest the best match
        best = results[0]
        lines.append(f"\nBest match: {best['id']} ({best['provider']})")
        lines.append(
            f"To switch, call model_swap with action='swap', "
            f"model_id='{best['id']}', provider='{best['provider']}'"
        )

        return {
            "success": True,
            "matches": results,
            "best_match": best,
            "output": "\n".join(lines),
        }

    elif action == "swap":
        if not model_id:
            # If query is provided, search first then swap to best match
            if query:
                results = await _search_models(query)
                if not results:
                    return {
                        "success": False,
                        "error": f"No models found matching '{query}'",
                    }
                best = results[0]
                model_id = best["id"]
                provider = best["provider"]
            else:
                return {
                    "success": False,
                    "error": "model_id or query is required for swap action",
                }

        if not provider:
            # Try to infer provider from model_id
            results = await _search_models(model_id)
            if results:
                provider = results[0]["provider"]
            else:
                return {
                    "success": False,
                    "error": f"Cannot determine provider for model '{model_id}'. Please specify provider.",
                }

        if not ws_id:
            return {
                "success": False,
                "error": "workspace_id is required. This should be set automatically.",
            }

        result = await _swap_model(model_id, provider, ws_id)
        return result

    else:
        return {
            "success": False,
            "error": f"Unknown action: {action}. Use 'search', 'swap', or 'list'.",
        }


# ── Tool Registration ──────────────────────────────────────────────


MODEL_SWAP_TOOL = ToolDefinition(
    name="model_swap",
    description=(
        "Search for and switch between AI models across all configured providers. "
        "Searches Ollama (local) models and cloud provider catalogs (Google, OpenAI, Anthropic). "
        "Use action='search' to find models by name, action='swap' to switch to a model, "
        "or action='list' to see all available models. "
        "When the user asks to 'use', 'switch to', or 'try' a different model, use this tool."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["search", "swap", "list"],
                "description": "Action to perform: 'search' finds models, 'swap' switches to a model, 'list' shows all available models.",
            },
            "query": {
                "type": "string",
                "description": "Model name to search for (e.g. 'llama3', 'gemini pro', 'claude'). Used with search and swap actions.",
            },
            "model_id": {
                "type": "string",
                "description": "Exact model ID to swap to (e.g. 'llama3:latest', 'gemini-3-flash-preview'). Used with swap action.",
            },
            "provider": {
                "type": "string",
                "enum": ["ollama", "google", "openai", "anthropic"],
                "description": "Provider for the model. Auto-detected if not specified.",
            },
        },
        "required": ["action"],
    },
    risk_level=RiskLevel.MEDIUM,
)


def register_model_swap_tools(registry) -> None:
    """Register model swap tools in the agent tool registry."""
    registry.register(
        definition=MODEL_SWAP_TOOL,
        handler=model_swap_handler,
    )
