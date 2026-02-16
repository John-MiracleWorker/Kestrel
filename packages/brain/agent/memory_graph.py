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
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger("brain.agent.memory_graph")


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

    async def extract_and_store(
        self,
        conversation_id: str,
        workspace_id: str,
        entities: list[dict],
        relations: list[dict],
    ) -> dict[str, int]:
        """
        Store extracted entities and relations from a conversation turn.

        Input format:
          entities: [{"type": "file", "name": "auth.py", "description": "...", "properties": {...}}]
          relations: [{"source": "auth.py", "target": "User", "relation": "depends_on", "context": "..."}]

        Returns count of nodes and edges created/updated.
        """
        now = datetime.now(timezone.utc).isoformat()
        nodes_upserted = 0
        edges_created = 0
        name_to_id: dict[str, str] = {}

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
                    workspace_id, name, entity_type,
                )

                if existing:
                    node_id = existing["id"]
                    new_count = existing["mention_count"] + 1
                    # Reinforce weight on re-mention
                    new_weight = min(existing["weight"] + 0.2, 5.0)

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
                    node_id = str(uuid.uuid4())
                    await conn.execute(
                        """
                        INSERT INTO memory_graph_nodes
                            (id, workspace_id, entity_type, name, description,
                             properties, weight, mention_count, first_seen, last_seen,
                             source_conversation_id)
                        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, 1, $8, $8, $9)
                        """,
                        node_id, workspace_id, entity_type, name,
                        entity.get("description", ""),
                        json.dumps(entity.get("properties", {})),
                        1.0, now, conversation_id,
                    )

                name_to_id[name] = node_id
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
                            workspace_id, source_name,
                        )
                        source_id = row["id"] if row else None
                    if not target_id:
                        row = await conn.fetchrow(
                            "SELECT id FROM memory_graph_nodes WHERE workspace_id = $1 AND name = $2",
                            workspace_id, target_name,
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
                    # Reinforce existing edge
                    await conn.execute(
                        "UPDATE memory_graph_edges SET strength = LEAST(strength + 0.3, 5.0) WHERE id = $1",
                        dup["id"],
                    )
                else:
                    edge_id = str(uuid.uuid4())
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
                        conversation_id, now,
                    )
                    edges_created += 1

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
                    workspace_id, f"%{name}%",
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
                        node_id, workspace_id,
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

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, weight, last_seen FROM memory_graph_nodes WHERE workspace_id = $1",
                workspace_id,
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

    async def get_stats(self, workspace_id: str) -> dict:
        """Get graph statistics for a workspace."""
        async with self._pool.acquire() as conn:
            node_count = await conn.fetchval(
                "SELECT COUNT(*) FROM memory_graph_nodes WHERE workspace_id = $1",
                workspace_id,
            )
            edge_count = await conn.fetchval(
                "SELECT COUNT(*) FROM memory_graph_edges e JOIN memory_graph_nodes n ON e.source_id = n.id WHERE n.workspace_id = $1",
                workspace_id,
            )
            top_nodes = await conn.fetch(
                """
                SELECT name, entity_type, weight, mention_count
                FROM memory_graph_nodes
                WHERE workspace_id = $1
                ORDER BY weight DESC LIMIT 10
                """,
                workspace_id,
            )

        return {
            "total_nodes": node_count,
            "total_edges": edge_count,
            "top_entities": [
                {"name": r["name"], "type": r["entity_type"], "weight": float(r["weight"]), "mentions": r["mention_count"]}
                for r in top_nodes
            ],
        }

    async def format_for_prompt(
        self,
        workspace_id: str,
        query_entities: list[str],
    ) -> str:
        """
        Query the graph and format results as a prompt context block.
        Designed to be injected into the agent's system prompt.
        """
        ctx = await self.query_context(workspace_id, query_entities)

        if not ctx["nodes"]:
            return ""

        lines = ["## 🧠 Memory Graph Context", ""]

        for node in ctx["nodes"][:15]:
            marker = "🔵" if node["depth"] == 0 else "⚪"
            desc = f" — {node['description']}" if node["description"] else ""
            lines.append(f"{marker} **{node['name']}** ({node['type']}){desc}")

        if ctx["edges"]:
            lines.append("")
            lines.append("**Relationships:**")
            for edge in ctx["edges"][:10]:
                # Find names
                src_name = next((n["name"] for n in ctx["nodes"] if n["id"] == edge["source"]), "?")
                tgt_name = next((n["name"] for n in ctx["nodes"] if n["id"] == edge["target"]), "?")
                lines.append(f"  • {src_name} →[{edge['relation']}]→ {tgt_name}")

        return "\n".join(lines)
