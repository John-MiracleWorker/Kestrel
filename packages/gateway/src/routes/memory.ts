/**
 * Memory Palace routes — Brain-owned graph data proxies.
 */
import { FastifyInstance } from 'fastify';
import { requireAuth, requireWorkspace } from '../auth/middleware';
import { logger } from '../utils/logger';
import { BrainClient } from '../brain/client';

export async function memoryRoutes(app: FastifyInstance, deps: { brainClient: BrainClient }) {
    const { brainClient } = deps;

    // ── Memory Palace: Graph Data ───────────────────────────────────
    app.get<{ Params: { workspaceId: string } }>(
        '/api/workspaces/:workspaceId/memory/graph',
        { preHandler: [requireAuth, requireWorkspace] },
        async (request, reply) => {
            const { workspaceId } = request.params;
            const user = request.user!;
            try {
                const result = await brainClient.getMemoryGraph(workspaceId, user.id);
                return reply.send({
                    nodes: result.nodes || [],
                    links: result.links || [],
                });
            } catch (error: any) {
                logger.error('Memory graph error', { error: error.message });
                return reply.send({ nodes: [], links: [] });
            }
        },
    );
}
