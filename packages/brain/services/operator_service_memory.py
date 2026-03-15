from __future__ import annotations

import math

import grpc

from core.grpc_setup import brain_pb2
from db import get_pool
from .base import BaseServicerMixin
from .operator_service_helpers import _iso

class OperatorMemoryMixin(BaseServicerMixin):
    async def GetMemoryGraph(self, request, context):
        workspace_id = request.workspace_id
        if not workspace_id:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "workspace_id is required")

        pool = await get_pool()
        graph_nodes = await pool.fetch(
            """
            SELECT id, entity_type, name, description, weight, mention_count,
                   first_seen, last_seen, source_conversation_id
            FROM memory_graph_nodes
            WHERE workspace_id = $1
            ORDER BY weight DESC
            LIMIT 200
            """,
            workspace_id,
        )
        graph_edges = await pool.fetch(
            """
            SELECT e.id, e.source_id, e.target_id, e.relation_type, e.strength,
                   e.context, e.conversation_id
            FROM memory_graph_edges e
            JOIN memory_graph_nodes n ON e.source_id = n.id
            WHERE n.workspace_id = $1
            LIMIT 500
            """,
            workspace_id,
        )
        conversations = await pool.fetch(
            """
            SELECT id, title, created_at
            FROM conversations
            WHERE workspace_id = $1
            ORDER BY created_at DESC
            LIMIT 30
            """,
            workspace_id,
        )

        node_ids: set[str] = set()
        nodes: list[brain_pb2.MemoryGraphNode] = []
        links: list[brain_pb2.MemoryGraphLink] = []
        center_x = 400.0
        center_y = 300.0

        conv_count = max(len(conversations), 1)
        for index, conv in enumerate(conversations):
            angle = (2 * math.pi * index) / conv_count
            radius = 120.0
            node_id = str(conv["id"])
            node_ids.add(node_id)
            nodes.append(
                brain_pb2.MemoryGraphNode(
                    id=node_id,
                    label=str(conv["title"] or "Untitled")[:30],
                    entity_type="conversation",
                    description=f"Conversation from {_iso(conv['created_at'])[:10]}",
                    weight=2,
                    mentions=1,
                    last_seen=_iso(conv["created_at"]),
                    x=center_x + math.cos(angle) * radius,
                    y=center_y + math.sin(angle) * radius,
                )
            )

        entity_count = max(len(graph_nodes), 1)
        for index, row in enumerate(graph_nodes):
            node_id = str(row["id"])
            if node_id in node_ids:
                continue
            node_ids.add(node_id)
            angle = (2 * math.pi * index) / entity_count
            radius = 250.0
            nodes.append(
                brain_pb2.MemoryGraphNode(
                    id=node_id,
                    label=str(row["name"] or "Unknown")[:30],
                    entity_type=str(row["entity_type"] or "concept"),
                    description=str(row["description"] or "")[:200],
                    weight=max(1, round(float(row["weight"] or 1) * 2)),
                    mentions=int(row["mention_count"] or 1),
                    last_seen=_iso(row["last_seen"]),
                    x=center_x + math.cos(angle) * radius,
                    y=center_y + math.sin(angle) * radius,
                )
            )
            source_conversation_id = row["source_conversation_id"]
            if source_conversation_id and str(source_conversation_id) in node_ids:
                links.append(
                    brain_pb2.MemoryGraphLink(
                        source=str(source_conversation_id),
                        target=node_id,
                        relation="mentioned_in",
                    )
                )

        for edge in graph_edges:
            source_id = str(edge["source_id"])
            target_id = str(edge["target_id"])
            if source_id in node_ids and target_id in node_ids:
                links.append(
                    brain_pb2.MemoryGraphLink(
                        source=source_id,
                        target=target_id,
                        relation=str(edge["relation_type"] or "related_to"),
                    )
                )

        for index in range(len(conversations) - 1):
            links.append(
                brain_pb2.MemoryGraphLink(
                    source=str(conversations[index]["id"]),
                    target=str(conversations[index + 1]["id"]),
                    relation="followed_by",
                )
            )

        return brain_pb2.GetMemoryGraphResponse(nodes=nodes, links=links)
