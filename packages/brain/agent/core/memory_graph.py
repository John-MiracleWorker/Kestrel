"""
Conversation Memory Graph — a persistent knowledge graph that connects
entities, decisions, and relationships across all conversations.

Unlike flat chat history, this builds a semantic web of knowledge:
  - Entities: people, projects, files, concepts, tools, decisions
  - Relations: mentioned_in, decided_by, depends_on, related_to, etc.
  - Temporal links: when things were discussed, decided, or changed
  - Decay: old, unreferenced nodes lose weight over time

Agents traverse the graph before planning to surface relevant context
they wouldn't find from keyword search alone.

This is Kestrel's "second brain" — it grows smarter with every conversation.
"""

import json
import logging
import math
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger("brain.agent.memory_graph")


def _extract_max_tokens() -> int:
    """Token cap for LLM entity extraction (small structured JSON output)."""
    raw = os.getenv("MEMORY_GRAPH_EXTRACT_MAX_TOKENS", "768")
    try:
        val = int(raw)
    except ValueError:
        val = 768
    return max(128, min(val, 4096))


# ── Node & Edge Types ────────────────────────────────────────────────

class EntityType(str, Enum):
    PERSON = "person"
    PROJECT = "project"
    FILE = "file"
    FUNCTION = "function"
    CONCEPT = "concept"
    DECISION = "decision"
    TOOL = "tool"
    ERROR = "error"
    PREFERENCE = "preference"
    GOAL = "goal"
    OUTCOME = "outcome"


class RelationType(str, Enum):
    MENTIONED_IN = "mentioned_in"
    DECIDED_BY = "decided_by"
    DEPENDS_ON = "depends_on"
    RELATED_TO = "related_to"
    CAUSED_BY = "caused_by"
    RESOLVED_BY = "resolved_by"
    CREATED_BY = "created_by"
    MODIFIED_BY = "modified_by"
    PREFERS = "prefers"
    CONFLICTS_WITH = "conflicts_with"
    SUCCEEDED_BY = "succeeded_by"
    PART_OF = "part_of"


@dataclass
class EntityNode:
    """A node in the memory graph."""
    id: str
    entity_type: EntityType
    name: str
    description: str = ""
    properties: dict[str, Any] = field(default_factory=dict)
    weight: float = 1.0          # Relevance weight (decays over time)
    mention_count: int = 1
    first_seen: str = ""
    last_seen: str = ""
    source_conversation_id: str = ""
    workspace_id: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.entity_type.value,
            "name": self.name,
            "description": self.description[:300],
            "weight": round(self.weight, 3),
            "mention_count": self.mention_count,
            "last_seen": self.last_seen,
        }


@dataclass
class RelationEdge:
    """A directed edge between two nodes."""
    id: str
    source_id: str
    target_id: str
    relation_type: RelationType
    strength: float = 1.0        # How strong this relationship is
    context: str = ""            # Why this relation exists
    conversation_id: str = ""
    created_at: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "source_id": self.source_id,
            "target_id": self.target_id,
            "relation": self.relation_type.value,
            "strength": round(self.strength, 3),
            "context": self.context[:200],
        }


# ── LLM Entity Extraction ───────────────────────────────────────────

_EXTRACTION_PROMPT = """\
Extract structured entities and relationships from this conversation turn.

Entity types: file, person, project, tool, decision, error, concept
Relationship types: depends_on, related_to, caused_by, resolved_by, uses, part_of, decided

Return ONLY a JSON object with this exact structure (no markdown, no explanation):
{{"entities": [{{"type": "...", "name": "...", "description": "..."}}], "relations": [{{"source": "name1", "target": "name2", "relation": "..."}}]}}

Rules:
- Extract 1-{max_entities} entities maximum, only genuinely important ones
- Names should be concise (1-4 words)
- Skip generic words like "The", "System", "Data"
- File entities should be actual filenames (e.g. "server.py")
- Person entities should be actual names or roles
- Decision entities describe choices made (e.g. "Use gRPC over REST")
- Error entities describe bugs or failures
- Only create relations between entities you extracted

USER: {user_message}

ASSISTANT: {assistant_response}"""


def _compute_max_entities(user_message: str, assistant_response: str) -> int:
    """Dynamically compute entity extraction limit based on conversation richness."""
    combined_length = len(user_message) + len(assistant_response)
    if combined_length < 200:
        return 3
    elif combined_length < 800:
        return 5
    elif combined_length < 2000:
        return 8
    else:
        return 12


async def extract_entities_llm(
    provider,
    model: str,
    api_key: str,
    user_message: str,
    assistant_response: str,
) -> tuple[list[dict], list[dict]]:
    """
    Use a lightweight LLM call to extract structured entities and relations
    from a conversation turn.

    Returns (entities, relations) suitable for MemoryGraph.extract_and_store().
    """
    # Truncate to keep the prompt cheap
    user_msg = user_message[:800]
    asst_msg = assistant_response[:1200]

    max_entities = _compute_max_entities(user_msg, asst_msg)
    prompt = _EXTRACTION_PROMPT.format(
        max_entities=max_entities,
        user_message=user_msg,
        assistant_response=asst_msg,
    )

    try:
        chunks = []
        async for token in provider.stream(
            messages=[{"role": "user", "content": prompt}],
            model=model,
            temperature=0.1,
            max_tokens=_extract_max_tokens(),
            api_key=api_key,
        ):
            if isinstance(token, str):
                chunks.append(token)

        raw = "".join(chunks).strip()

        # Debug log first 200 chars of raw response
        logger.debug(f"LLM raw response ({len(raw)} chars): {raw[:200]}")

        if not raw:
            logger.warning("LLM entity extraction returned empty response")
            return [], []

        # Strip markdown fences if the LLM wrapped it
        if "```" in raw:
            # Handle ```json\n...\n``` pattern
            import re as _re
            fence_match = _re.search(r'```(?:json)?\s*\n?(.*?)```', raw, _re.DOTALL)
            if fence_match:
                raw = fence_match.group(1).strip()
            elif raw.startswith("```"):
                raw = raw.split("\n", 1)[-1]
                if raw.endswith("```"):
                    raw = raw[:-3]
                raw = raw.strip()

        # Try to find JSON object in the response
        if not raw.startswith("{"):
            json_start = raw.find("{")
            if json_start >= 0:
                raw = raw[json_start:]

        data = json.loads(raw)
        entities = data.get("entities", [])
        relations = data.get("relations", [])

        # Validate entity types
        valid_types = {"file", "person", "project", "tool", "decision", "error", "concept"}
        entities = [
            e for e in entities
            if isinstance(e, dict) and e.get("type") in valid_types and e.get("name")
        ]

        # Validate relations
        entity_names = {e["name"] for e in entities}
        relations = [
            r for r in relations
            if isinstance(r, dict) and r.get("source") in entity_names and r.get("target") in entity_names
        ]

        logger.info(f"LLM extracted {len(entities)} entities, {len(relations)} relations")
        return entities, relations

    except json.JSONDecodeError as e:
        logger.warning(f"LLM entity extraction JSON parse failed: {e}")
        logger.debug(f"Raw response was: {raw[:300] if raw else '(empty)'}")
        return [], []
    except Exception as e:
        logger.warning(f"LLM entity extraction failed: {e}")
        return [], []


# ── Memory Graph Engine ─────────────────────────────────────────────

class MemoryGraph:
    """
    Persistent knowledge graph that grows across conversations.

    Key capabilities:
      - Extract entities and relations from conversation turns
      - Traverse the graph to find relevant context for new tasks
      - Decay stale nodes to keep the graph focused
      - Query by entity type, relation, or semantic proximity

    Storage: PostgreSQL with JSONB for flexible properties.
    """

    # Decay half-life in days — after this many days, weight halves
    DECAY_HALF_LIFE_DAYS = 30.0

    def __init__(self, pool):
        self._pool = pool

    @staticmethod
    def _to_uuid(val) -> 'uuid.UUID':
        """Convert a string to uuid.UUID if needed (asyncpg requires native UUIDs)."""
        if isinstance(val, uuid.UUID):
            return val
        return uuid.UUID(str(val))

    async def extract_and_store(
        self,
        conversation_id: str,
        workspace_id: str,
        entities: list[dict],
        relations: list[dict],
        vector_store=None,
    ) -> dict[str, int]:
        """
        Store extracted entities and relations from a conversation turn.

        Input format:
          entities: [{"type": "file", "name": "auth.py", "description": "...", "properties": {...}}]
          relations: [{"source": "auth.py", "target": "User", "relation": "depends_on", "context": "..."}]

        When vector_store is provided, also indexes entity descriptions for
        semantic similarity search via hybrid_query().

        Returns count of nodes and edges created/updated.
        """
        now = datetime.now(timezone.utc)
        nodes_upserted = 0
        edges_created = 0
        name_to_id: dict[str, str] = {}

        # asyncpg requires native uuid.UUID objects for uuid columns
        ws_uuid = uuid.UUID(workspace_id) if isinstance(workspace_id, str) else workspace_id
        conv_uuid = uuid.UUID(conversation_id) if isinstance(conversation_id, str) else conversation_id

        async with self._pool.acquire() as conn:
            # ── Upsert entity nodes ──────────────────────────────
            for entity in entities:
                entity_type = entity.get("type", "concept")
                name = entity.get("name", "").strip()
                if not name:
                    continue

                # Check if node already exists (by name + workspace)
                existing = await conn.fetchrow(
                    """
                    SELECT id, mention_count, weight FROM memory_graph_nodes
                    WHERE workspace_id = $1 AND name = $2 AND entity_type = $3
                    """,
                    ws_uuid, name, entity_type,
                )

                if existing:
                    node_id = existing["id"]
                    new_count = existing["mention_count"] + 1
                    # Reinforce weight with diminishing returns based on mention frequency
                    boost = 0.3 / (1 + existing["mention_count"] * 0.1)
                    new_weight = min(existing["weight"] + boost, 5.0)

                    await conn.execute(
                        """
                        UPDATE memory_graph_nodes
                        SET mention_count = $2, weight = $3, last_seen = $4,
                            description = COALESCE(NULLIF($5, ''), description),
                            properties = properties || $6::jsonb
                        WHERE id = $1
                        """,
                        node_id, new_count, new_weight, now,
                        entity.get("description", ""),
                        json.dumps(entity.get("properties", {})),
                    )
                else:
                    node_id = uuid.uuid4()
                    await conn.execute(
                        """
                        INSERT INTO memory_graph_nodes
                            (id, workspace_id, entity_type, name, description,
                             properties, weight, mention_count, first_seen, last_seen,
                             source_conversation_id)
                        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, 1, $8, $8, $9)
                        """,
                        node_id, ws_uuid, entity_type, name,
                        entity.get("description", ""),
                        json.dumps(entity.get("properties", {})),
                        1.0, now, conv_uuid,
                    )

                name_to_id[name] = node_id if isinstance(node_id, uuid.UUID) else uuid.UUID(str(node_id))
                nodes_upserted += 1

            # ── Create relation edges ────────────────────────────
            for rel in relations:
                source_name = rel.get("source", "")
                target_name = rel.get("target", "")
                relation_type = rel.get("relation", "related_to")

                source_id = name_to_id.get(source_name)
                target_id = name_to_id.get(target_name)

                if not source_id or not target_id:
                    # Try to find existing nodes by name
                    if not source_id:
                        row = await conn.fetchrow(
                            "SELECT id FROM memory_graph_nodes WHERE workspace_id = $1 AND name = $2",
                            ws_uuid, source_name,
                        )
                        source_id = row["id"] if row else None
                    if not target_id:
                        row = await conn.fetchrow(
                            "SELECT id FROM memory_graph_nodes WHERE workspace_id = $1 AND name = $2",
                            ws_uuid, target_name,
                        )
                        target_id = row["id"] if row else None

                if not source_id or not target_id:
                    continue

                # Avoid duplicate edges
                dup = await conn.fetchrow(
                    """
                    SELECT id FROM memory_graph_edges
                    WHERE source_id = $1 AND target_id = $2 AND relation_type = $3
                    """,
                    source_id, target_id, relation_type,
                )

                if dup:
                    # Reinforce existing edge with diminishing returns
                    await conn.execute(
                        "UPDATE memory_graph_edges SET strength = LEAST(strength + 0.4 / (1 + strength * 0.2), 5.0) WHERE id = $1",
                        dup["id"],
                    )
                else:
                    edge_id = uuid.uuid4()
                    await conn.execute(
                        """
                        INSERT INTO memory_graph_edges
                            (id, source_id, target_id, relation_type, strength,
                             context, conversation_id, created_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                        """,
                        edge_id, source_id, target_id, relation_type,
                        rel.get("strength", 1.0),
                        rel.get("context", ""),
                        conv_uuid, now,
                    )
                    edges_created += 1

        # ── Index entity descriptions into vector store ──────────
        # This enables hybrid_query() to find nodes via semantic similarity,
        # not just graph traversal.
        if vector_store and entities:
            try:
                docs = []
                for entity in entities:
                    name = entity.get("name", "").strip()
                    desc = entity.get("description", "").strip()
                    if name and desc:
                        docs.append({
                            "content": f"{name}: {desc}",
                            "metadata": {
                                "entity_name": name,
                                "entity_type": entity.get("type", "concept"),
                                "source": "memory_graph",
                                "conversation_id": conversation_id,
                            },
                        })
                if docs:
                    await vector_store.upsert(
                        workspace_id=workspace_id,
                        documents=docs,
                        source_filter="memory_graph",
                    )
                    logger.debug(f"Indexed {len(docs)} memory graph entities into vector store")
            except Exception as e:
                logger.warning(f"Vector indexing of memory graph entities failed (non-fatal): {e}")

        logger.info(f"Memory graph updated: {nodes_upserted} nodes, {edges_created} edges")
        return {"nodes_upserted": nodes_upserted, "edges_created": edges_created}

    async def query_context(
        self,
        workspace_id: str,
        query_entities: list[str],
        max_depth: int = 2,
        max_nodes: int = 30,
    ) -> dict[str, Any]:
        """
        Traverse the graph starting from named entities and return
        relevant context for agent planning.

        Uses breadth-first traversal with weight-based pruning.
        """
        visited: set[str] = set()
        result_nodes: list[dict] = []
        result_edges: list[dict] = []

        ws_uuid = self._to_uuid(workspace_id)

        async with self._pool.acquire() as conn:
            # Find seed nodes by name
            seed_ids = []
            for name in query_entities:
                rows = await conn.fetch(
                    """
                    SELECT id, entity_type, name, description, weight, mention_count, last_seen
                    FROM memory_graph_nodes
                    WHERE workspace_id = $1 AND (name ILIKE $2 OR description ILIKE $2)
                    ORDER BY weight DESC
                    LIMIT 3
                    """,
                    ws_uuid, f"%{name}%",
                )
                for row in rows:
                    if row["id"] not in visited:
                        seed_ids.append(row["id"])
                        visited.add(row["id"])
                        result_nodes.append({
                            "id": row["id"],
                            "type": row["entity_type"],
                            "name": row["name"],
                            "description": (row["description"] or "")[:200],
                            "weight": float(row["weight"]),
                            "mentions": row["mention_count"],
                            "depth": 0,
                        })

            # BFS traversal
            frontier = seed_ids[:]
            for depth in range(1, max_depth + 1):
                if not frontier or len(result_nodes) >= max_nodes:
                    break

                next_frontier = []
                for node_id in frontier:
                    # Get outgoing and incoming edges
                    edges = await conn.fetch(
                        """
                        SELECT e.id, e.source_id, e.target_id, e.relation_type,
                               e.strength, e.context,
                               n.id as neighbor_id, n.entity_type, n.name,
                               n.description, n.weight, n.mention_count, n.last_seen
                        FROM memory_graph_edges e
                        JOIN memory_graph_nodes n ON (
                            CASE WHEN e.source_id = $1 THEN e.target_id
                                 ELSE e.source_id END = n.id
                        )
                        WHERE (e.source_id = $1 OR e.target_id = $1)
                          AND n.workspace_id = $2
                        ORDER BY e.strength * n.weight DESC
                        LIMIT 10
                        """,
                        node_id, ws_uuid,
                    )

                    for edge in edges:
                        neighbor_id = edge["neighbor_id"]
                        if neighbor_id in visited:
                            continue

                        visited.add(neighbor_id)
                        result_nodes.append({
                            "id": neighbor_id,
                            "type": edge["entity_type"],
                            "name": edge["name"],
                            "description": (edge["description"] or "")[:200],
                            "weight": float(edge["weight"]),
                            "mentions": edge["mention_count"],
                            "depth": depth,
                        })
                        result_edges.append({
                            "source": node_id,
                            "target": neighbor_id,
                            "relation": edge["relation_type"],
                            "strength": float(edge["strength"]),
                            "context": (edge["context"] or "")[:100],
                        })
                        next_frontier.append(neighbor_id)

                        if len(result_nodes) >= max_nodes:
                            break

                frontier = next_frontier

        # Sort by combined weight
        result_nodes.sort(key=lambda n: n["weight"] * (1.0 / (1 + n["depth"])), reverse=True)

        return {
            "nodes": result_nodes[:max_nodes],
            "edges": result_edges,
            "seed_entities": query_entities,
            "total_traversed": len(visited),
        }

    async def decay_weights(self, workspace_id: str) -> int:
        """
        Apply time-based decay to node weights.
        Nodes that haven't been mentioned recently lose relevance.
        Should be run periodically (e.g., daily via cron).
        """
        now = datetime.now(timezone.utc)
        updated = 0

        ws_uuid = self._to_uuid(workspace_id)

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, weight, last_seen FROM memory_graph_nodes WHERE workspace_id = $1",
                ws_uuid,
            )

            for row in rows:
                last_seen = row["last_seen"]
                if not last_seen:
                    continue

                days_since = (now - last_seen.replace(tzinfo=timezone.utc)).total_seconds() / 86400
                decay_factor = math.pow(0.5, days_since / self.DECAY_HALF_LIFE_DAYS)
                new_weight = max(row["weight"] * decay_factor, 0.01)

                if abs(new_weight - row["weight"]) > 0.01:
                    await conn.execute(
                        "UPDATE memory_graph_nodes SET weight = $2 WHERE id = $1",
                        row["id"], new_weight,
                    )
                    updated += 1

        logger.info(f"Decayed {updated} nodes in workspace {workspace_id}")
        return updated

    async def expire_stale_edges(self, workspace_id: str) -> int:
        """Remove edges that are stale: both endpoints have decayed below threshold
        OR the edge is older than EDGE_MAX_AGE_DAYS (default 90).

        Should be called alongside decay_weights() in the periodic cron.
        """
        max_age_days = int(os.getenv("EDGE_MAX_AGE_DAYS", "90"))
        ws_uuid = self._to_uuid(workspace_id)

        async with self._pool.acquire() as conn:
            deleted = await conn.fetchval(
                """
                WITH stale AS (
                    SELECT e.id
                    FROM memory_graph_edges e
                    JOIN memory_graph_nodes src ON e.source_id = src.id
                    JOIN memory_graph_nodes tgt ON e.target_id = tgt.id
                    WHERE src.workspace_id = $1
                      AND (
                        (src.weight < 0.1 AND tgt.weight < 0.1)
                        OR (e.created_at < NOW() - ($2::int || ' days')::interval)
                      )
                )
                DELETE FROM memory_graph_edges WHERE id IN (SELECT id FROM stale)
                RETURNING COUNT(*)
                """,
                ws_uuid, max_age_days,
            )

        expired = deleted or 0
        if expired:
            logger.info(f"Expired {expired} stale edges in workspace {workspace_id}")
        return expired

    async def get_stats(self, workspace_id: str) -> dict:
        """Get graph statistics for a workspace."""
        async with self._pool.acquire() as conn:
            ws_uuid = self._to_uuid(workspace_id)
            node_count = await conn.fetchval(
                "SELECT COUNT(*) FROM memory_graph_nodes WHERE workspace_id = $1",
                ws_uuid,
            )
            edge_count = await conn.fetchval(
                "SELECT COUNT(*) FROM memory_graph_edges e JOIN memory_graph_nodes n ON e.source_id = n.id WHERE n.workspace_id = $1",
                ws_uuid,
            )
            top_nodes = await conn.fetch(
                """
                SELECT name, entity_type, weight, mention_count
                FROM memory_graph_nodes
                WHERE workspace_id = $1
                ORDER BY weight DESC LIMIT 10
                """,
                ws_uuid,
            )

        return {
            "total_nodes": node_count,
            "total_edges": edge_count,
            "top_entities": [
                {"name": r["name"], "type": r["entity_type"], "weight": float(r["weight"]), "mentions": r["mention_count"]}
                for r in top_nodes
            ],
        }

    async def query_by_vector_similarity(
        self,
        workspace_id: str,
        query_text: str,
        top_k: int = 10,
        vector_store=None,
    ) -> list[dict]:
        """
        Find memory graph nodes that are semantically similar to a query
        using pgvector similarity search on node descriptions.

        This complements the graph traversal by finding relevant nodes that
        might not be directly connected to known entities. Together, they
        form a hybrid retrieval system: graph structure + semantic similarity.
        """
        if not vector_store:
            return []

        try:
            # Search the vector store for similar memory descriptions
            results = await vector_store.search(
                workspace_id=workspace_id,
                query=query_text,
                top_k=top_k,
                source_filter="memory_graph",
            )

            if not results:
                return []

            # Resolve matching nodes from the graph
            ws_uuid = self._to_uuid(workspace_id)
            matched_nodes = []
            async with self._pool.acquire() as conn:
                for r in results:
                    node_name = r.get("metadata", {}).get("entity_name", "")
                    if not node_name:
                        continue
                    row = await conn.fetchrow(
                        """
                        SELECT id, entity_type, name, description, weight, mention_count
                        FROM memory_graph_nodes
                        WHERE workspace_id = $1 AND name = $2
                        ORDER BY weight DESC
                        LIMIT 1
                        """,
                        ws_uuid, node_name,
                    )
                    if row:
                        matched_nodes.append({
                            "id": row["id"],
                            "type": row["entity_type"],
                            "name": row["name"],
                            "description": (row["description"] or "")[:200],
                            "weight": float(row["weight"]),
                            "mentions": row["mention_count"],
                            "similarity": r.get("score", 0.0),
                            "depth": -1,  # Indicates vector-matched, not graph-traversed
                        })

            return matched_nodes

        except Exception as e:
            logger.warning(f"Vector similarity search in memory graph failed: {e}")
            return []

    async def hybrid_query(
        self,
        workspace_id: str,
        query_entities: list[str],
        query_text: str = "",
        max_depth: int = 2,
        max_nodes: int = 30,
        vector_store=None,
    ) -> dict[str, Any]:
        """
        Hybrid retrieval combining graph traversal with vector similarity.

        1. Graph traversal finds structurally connected context
        2. Vector search finds semantically similar but disconnected context
        3. Results are merged and ranked by a combined score

        This produces higher-quality context than either approach alone.
        """
        # Run graph traversal and vector search concurrently
        graph_task = self.query_context(workspace_id, query_entities, max_depth, max_nodes)

        vector_results = []
        if query_text and vector_store:
            try:
                vector_results = await self.query_by_vector_similarity(
                    workspace_id=workspace_id,
                    query_text=query_text,
                    top_k=max_nodes // 2,
                    vector_store=vector_store,
                )
            except Exception as e:
                logger.warning(f"Vector leg of hybrid query failed: {e}")

        graph_ctx = await graph_task

        # Merge results, deduplicating by node ID
        seen_ids = {n["id"] for n in graph_ctx["nodes"]}
        merged_nodes = list(graph_ctx["nodes"])

        for vn in vector_results:
            if vn["id"] not in seen_ids:
                seen_ids.add(vn["id"])
                merged_nodes.append(vn)

        # Combined ranking: graph weight * (1 / depth+1) + similarity bonus
        for node in merged_nodes:
            depth = node.get("depth", 0)
            similarity = node.get("similarity", 0.0)
            graph_score = node["weight"] * (1.0 / (1 + max(depth, 0)))
            vector_bonus = similarity * 2.0 if similarity > 0 else 0.0
            node["_combined_score"] = graph_score + vector_bonus

        merged_nodes.sort(key=lambda n: n.get("_combined_score", 0), reverse=True)

        return {
            "nodes": merged_nodes[:max_nodes],
            "edges": graph_ctx["edges"],
            "seed_entities": query_entities,
            "total_traversed": graph_ctx["total_traversed"],
            "vector_matches": len(vector_results),
            "hybrid": True,
        }

    async def format_for_prompt(
        self,
        workspace_id: str,
        query_entities: list[str],
        query_text: str = "",
        vector_store=None,
    ) -> str:
        """
        Query the graph and format results as a prompt context block.
        Designed to be injected into the agent's system prompt.

        Uses hybrid retrieval (graph + vector) when a vector_store is provided.
        """
        if query_text and vector_store:
            ctx = await self.hybrid_query(
                workspace_id, query_entities,
                query_text=query_text,
                vector_store=vector_store,
            )
        else:
            ctx = await self.query_context(workspace_id, query_entities)

        if not ctx["nodes"]:
            return ""

        lines = ["## Memory Graph Context", ""]

        for node in ctx["nodes"][:15]:
            depth = node.get("depth", 0)
            if depth == -1:
                marker = "~"   # Vector-matched node
            elif depth == 0:
                marker = "*"   # Seed node
            else:
                marker = "-"   # Graph-traversed node
            desc = f" — {node['description']}" if node.get("description") else ""
            lines.append(f"{marker} **{node['name']}** ({node['type']}){desc}")

        if ctx["edges"]:
            lines.append("")
            lines.append("**Relationships:**")
            for edge in ctx["edges"][:10]:
                src_name = next((n["name"] for n in ctx["nodes"] if n["id"] == edge["source"]), "?")
                tgt_name = next((n["name"] for n in ctx["nodes"] if n["id"] == edge["target"]), "?")
                lines.append(f"  {src_name} -[{edge['relation']}]-> {tgt_name}")

        if ctx.get("vector_matches"):
            lines.append(f"\n_({ctx['vector_matches']} additional nodes found via semantic similarity)_")

        return "\n".join(lines)
