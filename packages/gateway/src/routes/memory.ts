/**
 * Memory Palace routes — graph data from memory_graph_nodes/edges + conversations.
 */
import { FastifyInstance } from 'fastify';
import { requireAuth, requireWorkspace } from '../auth/middleware';
import { logger } from '../utils/logger';
import { getPool } from '../db/pool';

export async function memoryRoutes(app: FastifyInstance) {
    // ── Memory Palace: Graph Data ───────────────────────────────────
    app.get<{ Params: { workspaceId: string } }>(
        '/api/workspaces/:workspaceId/memory/graph',
        { preHandler: [requireAuth, requireWorkspace] },
        async (request, reply) => {
            const { workspaceId } = request.params;
            try {
                const pool = getPool();

                // 1. Fetch real memory graph nodes
                const graphNodes = await pool.query(
                    `SELECT id, entity_type, name, description, weight, mention_count,
                            first_seen, last_seen, source_conversation_id
                     FROM memory_graph_nodes
                     WHERE workspace_id = $1
                     ORDER BY weight DESC
                     LIMIT 200`,
                    [workspaceId]
                );

                // 2. Fetch real memory graph edges
                const graphEdges = await pool.query(
                    `SELECT e.id, e.source_id, e.target_id, e.relation_type, e.strength,
                            e.context, e.conversation_id
                     FROM memory_graph_edges e
                     JOIN memory_graph_nodes n ON e.source_id = n.id
                     WHERE n.workspace_id = $1
                     LIMIT 500`,
                    [workspaceId]
                );

                // 3. Fetch recent conversations to use as anchor nodes
                const convResult = await pool.query(
                    `SELECT id, title, created_at FROM conversations
                     WHERE workspace_id = $1
                     ORDER BY created_at DESC LIMIT 30`,
                    [workspaceId]
                );

                const nodes: any[] = [];
                const links: any[] = [];
                const nodeIds = new Set<string>();

                // Build a layout helper
                const cx = 400, cy = 300;

                // Add conversation nodes as anchors
                convResult.rows.forEach((conv: any, i: number) => {
                    const angle = (2 * Math.PI * i) / Math.max(convResult.rows.length, 1);
                    const radius = 80 + Math.random() * 60;
                    const nodeId = conv.id;
                    nodeIds.add(nodeId);
                    nodes.push({
                        id: nodeId,
                        label: (conv.title || 'Untitled').slice(0, 30),
                        entity_type: 'conversation',
                        description: `Conversation from ${new Date(conv.created_at).toLocaleDateString()}`,
                        weight: 2,
                        mentions: 1,
                        last_seen: conv.created_at,
                        x: cx + Math.cos(angle) * radius,
                        y: cy + Math.sin(angle) * radius,
                        vx: 0, vy: 0,
                    });
                });

                // Add memory graph entity nodes
                graphNodes.rows.forEach((row: any, i: number) => {
                    const nodeId = row.id;
                    if (nodeIds.has(nodeId)) return;
                    nodeIds.add(nodeId);

                    const angle = (2 * Math.PI * i) / Math.max(graphNodes.rows.length, 1);
                    const radius = 160 + Math.random() * 120;
                    nodes.push({
                        id: nodeId,
                        label: (row.name || 'Unknown').slice(0, 30),
                        entity_type: row.entity_type || 'concept',
                        description: (row.description || '').slice(0, 200),
                        weight: Math.round(row.weight * 2) || 1,
                        mentions: row.mention_count || 1,
                        last_seen: row.last_seen,
                        x: cx + Math.cos(angle) * radius,
                        y: cy + Math.sin(angle) * radius,
                        vx: 0, vy: 0,
                    });

                    // Link entity to its source conversation
                    if (row.source_conversation_id && nodeIds.has(row.source_conversation_id)) {
                        links.push({
                            source: row.source_conversation_id,
                            target: nodeId,
                            relation: 'mentioned_in',
                        });
                    }
                });

                // Second pass: link entities to conversations that were added after the entity
                graphNodes.rows.forEach((row: any) => {
                    if (row.source_conversation_id && nodeIds.has(row.source_conversation_id)) {
                        const exists = links.some(
                            l => l.source === row.source_conversation_id && l.target === row.id
                        );
                        if (!exists) {
                            links.push({
                                source: row.source_conversation_id,
                                target: row.id,
                                relation: 'mentioned_in',
                            });
                        }
                    }
                });

                // Add memory graph edges (entity-to-entity relationships)
                graphEdges.rows.forEach((edge: any) => {
                    if (nodeIds.has(edge.source_id) && nodeIds.has(edge.target_id)) {
                        links.push({
                            source: edge.source_id,
                            target: edge.target_id,
                            relation: edge.relation_type || 'related_to',
                        });
                    }
                });

                // Chain conversations chronologically
                for (let i = 0; i < convResult.rows.length - 1; i++) {
                    links.push({
                        source: convResult.rows[i].id,
                        target: convResult.rows[i + 1].id,
                        relation: 'followed_by',
                    });
                }

                return reply.send({ nodes, links });
            } catch (error: any) {
                logger.error('Memory graph error', { error: error.message });
                return reply.send({ nodes: [], links: [] });
            }
        },
    );
}
