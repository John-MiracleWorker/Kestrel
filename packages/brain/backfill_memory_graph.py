from typing import Optional
"""
Backfill memory graph — scan existing conversations and extract entities.

Run inside the brain container:
    python backfill_memory_graph.py [--workspace WORKSPACE_ID] [--limit N]

This reads conversation messages, sends them through the LLM entity extractor,
and stores structured entities + relations into memory_graph_nodes/edges.
"""

import asyncio
import argparse
import logging
import os
import uuid as uuid_mod
import json

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill")


async def main(workspace_id: Optional[str], limit: int, model_override: Optional[str] = None):
    import asyncpg
    from agent.memory_graph import MemoryGraph, extract_entities_llm
    from providers_registry import get_provider
    from encryption import decrypt

    # Build DB URL from individual env vars
    pg_host = os.getenv("POSTGRES_HOST", "postgres")
    pg_port = os.getenv("POSTGRES_PORT", "5432")
    pg_user = os.getenv("POSTGRES_USER", "kestrel")
    pg_pass = os.getenv("POSTGRES_PASSWORD", "changeme")
    pg_db = os.getenv("POSTGRES_DB", "kestrel")
    db_url = f"postgresql://{pg_user}:{pg_pass}@{pg_host}:{pg_port}/{pg_db}"
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=3)

    memory_graph = MemoryGraph(pool=pool)

    # Get workspaces to process
    if workspace_id:
        workspaces = [uuid_mod.UUID(workspace_id)]
    else:
        rows = await pool.fetch("SELECT id FROM workspaces LIMIT 20")
        workspaces = [r["id"] for r in rows]

    logger.info(f"Processing {len(workspaces)} workspace(s)")

    for ws_uuid in workspaces:
        ws_id = str(ws_uuid)

        # Resolve provider config from DB directly
        try:
            row = await pool.fetchrow(
                """SELECT provider, model, api_key_encrypted
                   FROM workspace_provider_config
                   WHERE workspace_id = $1 AND is_default = TRUE
                   LIMIT 1""",
                ws_uuid,
            )

            if not row:
                logger.info(f"Workspace {ws_id}: no provider config, skipping")
                continue

            provider_name = row["provider"]
            model = model_override or row["model"] or ""
            api_key = decrypt(row["api_key_encrypted"]) if row["api_key_encrypted"] else ""

            if not api_key:
                logger.info(f"Workspace {ws_id}: no API key, skipping")
                continue

            provider = get_provider(provider_name)
            logger.info(f"Workspace {ws_id}: using {provider_name}/{model}")

        except Exception as e:
            logger.warning(f"Failed to resolve provider for workspace {ws_id}: {e}")
            continue

        # Fetch conversations with messages
        conversations = await pool.fetch(
            """
            SELECT c.id, c.title
            FROM conversations c
            WHERE c.workspace_id = $1
            ORDER BY c.created_at DESC
            LIMIT $2
            """,
            ws_uuid, limit,
        )

        logger.info(f"Workspace {ws_id}: {len(conversations)} conversations to scan")
        total_entities = 0
        total_relations = 0

        for conv in conversations:
            conv_id = str(conv["id"])
            conv_title = (conv["title"] or "Untitled")[:50]

            # Check if we already have entities from this conversation
            existing = await pool.fetchval(
                "SELECT COUNT(*) FROM memory_graph_nodes WHERE source_conversation_id = $1",
                conv["id"],
            )
            if existing > 0:
                logger.info(f"  Skipping '{conv_title}' — already has {existing} entities")
                continue

            # Fetch messages
            messages = await pool.fetch(
                """
                SELECT role, content FROM messages
                WHERE conversation_id = $1
                ORDER BY created_at ASC
                LIMIT 20
                """,
                conv["id"],
            )

            if len(messages) < 2:
                continue

            # Build a condensed conversation for extraction
            user_parts = []
            assistant_parts = []
            for m in messages:
                content = m["content"] or ""
                if m["role"] == "user":
                    user_parts.append(content[:300])
                elif m["role"] == "assistant":
                    assistant_parts.append(content[:300])

            user_text = "\n".join(user_parts[:5])
            assistant_text = "\n".join(assistant_parts[:5])

            if not user_text:
                continue

            try:
                entities, relations = await extract_entities_llm(
                    provider=provider,
                    model=model,
                    api_key=api_key,
                    user_message=user_text,
                    assistant_response=assistant_text,
                )

                if entities:
                    await memory_graph.extract_and_store(
                        conversation_id=conv_id,
                        workspace_id=ws_id,
                        entities=entities,
                        relations=relations,
                    )
                    total_entities += len(entities)
                    total_relations += len(relations)
                    logger.info(f"  '{conv_title}': {len(entities)} entities, {len(relations)} relations")
                else:
                    logger.info(f"  '{conv_title}': no entities extracted")

                # Small delay to avoid rate limiting
                await asyncio.sleep(1.0)

            except Exception as e:
                logger.warning(f"  '{conv_title}': extraction failed — {e}")
                import traceback
                traceback.print_exc()
                continue

        logger.info(f"Workspace {ws_id}: total {total_entities} entities, {total_relations} relations stored")

    await pool.close()
    logger.info("Backfill complete!")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill memory graph from conversations")
    parser.add_argument("--workspace", type=str, help="Specific workspace ID to process")
    parser.add_argument("--limit", type=int, default=50, help="Max conversations per workspace")
    parser.add_argument("--model", type=str, default="gemini-2.0-flash", help="Model to use for extraction (default: gemini-2.0-flash)")
    args = parser.parse_args()

    asyncio.run(main(args.workspace, args.limit, args.model))
