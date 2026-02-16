"""
Provider configuration per workspace.

Reads workspace-level settings from the DB and provides
a fallback chain: workspace config → env var → defaults.
"""

import os
import logging
from typing import Optional

import asyncpg

logger = logging.getLogger("brain.provider_config")


class ProviderConfig:
    """Manages per-workspace LLM provider configuration."""

    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    async def get_config(self, workspace_id: str) -> dict:
        """
        Get the effective provider config for a workspace.
        Falls back to env vars / defaults when no DB row exists.
        """
        row = await self._pool.fetchrow(
            """SELECT provider, model, api_key_encrypted, temperature,
                      max_tokens, system_prompt, rag_enabled, rag_top_k,
                      rag_min_similarity, settings
               FROM workspace_provider_config
               WHERE workspace_id = $1 AND is_default = TRUE
               LIMIT 1""",
            workspace_id,
        )

        if row:
            return {
                "provider": row["provider"],
                "model": row["model"] or "",
                "api_key": row["api_key_encrypted"] or "",  # TODO: decrypt
                "temperature": float(row["temperature"]),
                "max_tokens": int(row["max_tokens"]),
                "system_prompt": row["system_prompt"] or "",
                "rag_enabled": bool(row["rag_enabled"]),
                "rag_top_k": int(row["rag_top_k"]),
                "rag_min_similarity": float(row["rag_min_similarity"]),
                "settings": dict(row["settings"]) if row["settings"] else {},
            }

        # Fallback to env-var defaults
        return {
            "provider": os.getenv("DEFAULT_PROVIDER", "local"),
            "model": "",
            "api_key": "",
            "temperature": float(os.getenv("DEFAULT_TEMPERATURE", "0.7")),
            "max_tokens": int(os.getenv("DEFAULT_MAX_TOKENS", "2048")),
            "system_prompt": "",
            "rag_enabled": os.getenv("RAG_ENABLED", "true").lower() == "true",
            "rag_top_k": int(os.getenv("RAG_TOP_K", "5")),
            "rag_min_similarity": float(os.getenv("RAG_MIN_SIMILARITY", "0.3")),
            "settings": {},
        }

    async def set_config(
        self,
        workspace_id: str,
        provider: str,
        model: str = "",
        api_key: str = "",
        temperature: float = 0.7,
        max_tokens: int = 2048,
        system_prompt: str = "",
        rag_enabled: bool = True,
        rag_top_k: int = 5,
        rag_min_similarity: float = 0.3,
        is_default: bool = True,
    ) -> dict:
        """Create or update provider config for a workspace."""
        await self._pool.execute(
            """INSERT INTO workspace_provider_config
               (workspace_id, provider, model, api_key_encrypted, temperature,
                max_tokens, system_prompt, rag_enabled, rag_top_k,
                rag_min_similarity, is_default, updated_at)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, NOW())
               ON CONFLICT (workspace_id, provider)
               DO UPDATE SET
                   model = EXCLUDED.model,
                   api_key_encrypted = EXCLUDED.api_key_encrypted,
                   temperature = EXCLUDED.temperature,
                   max_tokens = EXCLUDED.max_tokens,
                   system_prompt = EXCLUDED.system_prompt,
                   rag_enabled = EXCLUDED.rag_enabled,
                   rag_top_k = EXCLUDED.rag_top_k,
                   rag_min_similarity = EXCLUDED.rag_min_similarity,
                   is_default = EXCLUDED.is_default,
                   updated_at = NOW()""",
            workspace_id, provider, model, api_key,  # TODO: encrypt
            temperature, max_tokens, system_prompt,
            rag_enabled, rag_top_k, rag_min_similarity, is_default,
        )

        if is_default:
            # Unset other defaults for this workspace
            await self._pool.execute(
                """UPDATE workspace_provider_config
                   SET is_default = FALSE
                   WHERE workspace_id = $1 AND provider != $2""",
                workspace_id, provider,
            )

        return await self.get_config(workspace_id)

    async def list_configs(self, workspace_id: str) -> list:
        """List all provider configs for a workspace."""
        rows = await self._pool.fetch(
            """SELECT provider, model, temperature, max_tokens,
                      is_default, rag_enabled, updated_at
               FROM workspace_provider_config
               WHERE workspace_id = $1
               ORDER BY is_default DESC, provider""",
            workspace_id,
        )
        return [
            {
                "provider": r["provider"],
                "model": r["model"] or "",
                "temperature": float(r["temperature"]),
                "max_tokens": int(r["max_tokens"]),
                "is_default": bool(r["is_default"]),
                "rag_enabled": bool(r["rag_enabled"]),
                "updatedAt": r["updated_at"].isoformat(),
            }
            for r in rows
        ]

    async def delete_config(self, workspace_id: str, provider: str) -> bool:
        """Delete a provider config for a workspace."""
        result = await self._pool.execute(
            """DELETE FROM workspace_provider_config
               WHERE workspace_id = $1 AND provider = $2""",
            workspace_id, provider,
        )
        return result == "DELETE 1"
