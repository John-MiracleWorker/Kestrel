/**
 * Memory Palace routes — graph data from conversations and evidence chain.
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
                const convResult = await pool.query(
                    `SELECT id, title, created_at FROM conversations WHERE workspace_id = $1 ORDER BY created_at DESC LIMIT 30`,
                    [workspaceId]
                );

                const evidenceResult = await pool.query(
                    `SELECT id, task_id, description, decision_type, confidence FROM evidence_chain
                     WHERE task_id IN (SELECT id FROM conversations WHERE workspace_id = $1)
                     ORDER BY created_at DESC LIMIT 50`,
                    [workspaceId]
                ).catch(() => ({ rows: [] }));

                const nodes: any[] = [];
                const links: any[] = [];
                const cx = 400, cy = 300;

                convResult.rows.forEach((conv: any, i: number) => {
                    const angle = (2 * Math.PI * i) / Math.max(convResult.rows.length, 1);
                    const radius = 100 + Math.random() * 120;
                    nodes.push({
                        id: conv.id,
                        label: (conv.title || 'Untitled').slice(0, 25),
                        entity_type: 'concept',
                        weight: 3, mentions: 1,
                        x: cx + Math.cos(angle) * radius,
                        y: cy + Math.sin(angle) * radius,
                        vx: 0, vy: 0,
                    });
                });

                evidenceResult.rows.forEach((ev: any, i: number) => {
                    const angle = (2 * Math.PI * i) / Math.max(evidenceResult.rows.length, 1);
                    const radius = 180 + Math.random() * 60;
                    const typeMap: Record<string, string> = {
                        tool_selection: 'decision', approach_selection: 'decision',
                        error_recovery: 'error', planning: 'concept',
                    };
                    nodes.push({
                        id: ev.id,
                        label: (ev.description || 'Decision').slice(0, 25),
                        entity_type: typeMap[ev.decision_type] || 'concept',
                        weight: Math.round((ev.confidence || 0.5) * 5),
                        mentions: 1,
                        x: cx + Math.cos(angle) * radius,
                        y: cy + Math.sin(angle) * radius,
                        vx: 0, vy: 0,
                    });

                    if (ev.task_id && convResult.rows.find((c: any) => c.id === ev.task_id)) {
                        links.push({ source: ev.task_id, target: ev.id, relation: ev.decision_type || 'related_to' });
                    }
                });

                for (let i = 0; i < convResult.rows.length - 1; i++) {
                    links.push({ source: convResult.rows[i].id, target: convResult.rows[i + 1].id, relation: 'followed_by' });
                }

                return reply.send({ nodes, links });
            } catch (error: any) {
                logger.error('Memory graph error', { error: error.message });
                return reply.send({ nodes: [], links: [] });
            }
        },
    );
}
