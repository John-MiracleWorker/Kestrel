"""
LLM provider registry and workspace configuration.
"""
import json
import logging
from typing import Optional, Union

from providers.local import LocalProvider
from providers.cloud import CloudProvider
from db import get_pool

logger = logging.getLogger("brain.providers")

_providers: dict[str, Union[LocalProvider, CloudProvider]] = {}


def get_provider(name: str):
    if name not in _providers:
        if name == "local":
            _providers[name] = LocalProvider()
        else:
            _providers[name] = CloudProvider(name)
    return _providers[name]


async def list_provider_configs(workspace_id):
    query = """
        SELECT * FROM workspace_provider_config
        WHERE workspace_id = $1
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(query, workspace_id)


async def set_provider_config(workspace_id, provider, config):
    # upsert
    query = """
        INSERT INTO workspace_provider_config (
            workspace_id, provider, model, api_key_encrypted, 
            temperature, max_tokens, system_prompt, rag_enabled, 
            rag_top_k, rag_min_similarity, is_default, settings
        ) VALUES (
            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12
        )
        ON CONFLICT (workspace_id, provider) DO UPDATE SET
            model = EXCLUDED.model,
            api_key_encrypted = COALESCE(EXCLUDED.api_key_encrypted, workspace_provider_config.api_key_encrypted),
            temperature = EXCLUDED.temperature,
            max_tokens = EXCLUDED.max_tokens,
            system_prompt = EXCLUDED.system_prompt,
            rag_enabled = EXCLUDED.rag_enabled,
            rag_top_k = EXCLUDED.rag_top_k,
            rag_min_similarity = EXCLUDED.rag_min_similarity,
            is_default = EXCLUDED.is_default,
            settings = EXCLUDED.settings,
            updated_at = NOW()
        RETURNING *
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            if config.get('is_default', False):
                await conn.execute(
                    "UPDATE workspace_provider_config SET is_default = FALSE WHERE workspace_id = $1",
                    workspace_id
                )

            return await conn.fetchrow(query,
                workspace_id, provider, config.get('model'), config.get('api_key_encrypted'),
                config.get('temperature', 0.7), config.get('max_tokens', 2048),
                config.get('system_prompt'), config.get('rag_enabled', True),
                config.get('rag_top_k', 5), config.get('rag_min_similarity', 0.3),
                config.get('is_default', False), json.dumps(config.get('settings', {}))
            )


async def delete_provider_config(workspace_id, provider):
    query = "DELETE FROM workspace_provider_config WHERE workspace_id = $1 AND provider = $2"
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(query, workspace_id, provider)
