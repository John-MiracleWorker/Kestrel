import { FastifyInstance, FastifyRequest, FastifyReply } from 'fastify';
import { requireAuth, requireWorkspace } from '../auth/middleware';
import { BrainClient } from '../brain/client';
import { logger } from '../utils/logger';

interface TaskDeps {
    brainClient: BrainClient;
}

/**
 * Agent task management routes.
 *
 * POST   /api/workspaces/:workspaceId/tasks          — Start a task
 * GET    /api/workspaces/:workspaceId/tasks          — List tasks
 * GET    /api/workspaces/:workspaceId/tasks/:taskId/events — SSE stream
 * POST   /api/tasks/:taskId/approve                  — Approve/deny action
 * POST   /api/tasks/:taskId/cancel                   — Cancel task
 */
export default async function taskRoutes(app: FastifyInstance, deps: TaskDeps) {
    const { brainClient } = deps;

    // ── POST /api/workspaces/:workspaceId/tasks ─────────────────
    // Start a new autonomous agent task and stream events via SSE.
    app.post('/api/workspaces/:workspaceId/tasks', {
        preHandler: [requireAuth, requireWorkspace],
    }, async (req: FastifyRequest, reply: FastifyReply) => {
        const user = (req as any).user;
        const { workspaceId } = req.params as any;
        const { goal, conversationId, guardrails } = req.body as any;

        if (!goal || goal.trim().length < 3) {
            return reply.status(400).send({ error: 'Goal must be at least 3 characters' });
        }

        // Set up SSE headers
        reply.raw.writeHead(200, {
            'Content-Type': 'text/event-stream',
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no',
        });

        try {
            const stream = brainClient.startTask({
                userId: user.id,
                workspaceId,
                goal: goal.trim(),
                conversationId,
                guardrails,
            });

            for await (const event of stream) {
                const sseData = JSON.stringify({
                    type: event.type,
                    taskId: event.task_id,
                    stepId: event.step_id,
                    content: event.content,
                    toolName: event.tool_name,
                    toolArgs: event.tool_args,
                    toolResult: event.tool_result,
                    approvalId: event.approval_id,
                    progress: event.progress,
                });

                reply.raw.write(`event: task\ndata: ${sseData}\n\n`);
            }

            reply.raw.write('event: done\ndata: {}\n\n');
        } catch (err: any) {
            logger.error('Task stream failed', { error: err.message });
            reply.raw.write(`event: error\ndata: ${JSON.stringify({ error: err.message })}\n\n`);
        } finally {
            reply.raw.end();
        }
    });

    // ── GET /api/workspaces/:workspaceId/tasks ──────────────────
    // List agent tasks for the current user in a workspace.
    app.get('/api/workspaces/:workspaceId/tasks', {
        preHandler: [requireAuth, requireWorkspace],
    }, async (req: FastifyRequest) => {
        const user = (req as any).user;
        const { workspaceId } = req.params as any;
        const { status } = req.query as any;

        const result = await brainClient.listTasks(user.id, workspaceId, status);
        return { tasks: result.tasks || [] };
    });

    // ── POST /api/tasks/:taskId/approve ─────────────────────────
    // Approve or deny a pending agent action.
    app.post('/api/tasks/:taskId/approve', {
        preHandler: [requireAuth],
    }, async (req: FastifyRequest, reply: FastifyReply) => {
        const user = (req as any).user;
        const { taskId } = req.params as any;
        const { approvalId, approved } = req.body as any;

        if (!approvalId) {
            return reply.status(400).send({ error: 'approvalId required' });
        }
        if (typeof approved !== 'boolean') {
            return reply.status(400).send({ error: 'approved must be a boolean' });
        }

        try {
            const result = await brainClient.approveAction(approvalId, user.id, approved);
            return result;
        } catch (err: any) {
            logger.error('Approve action failed', { error: err.message, taskId });
            return reply.status(500).send({ error: err.message });
        }
    });

    // ── POST /api/tasks/:taskId/cancel ──────────────────────────
    // Cancel a running agent task.
    app.post('/api/tasks/:taskId/cancel', {
        preHandler: [requireAuth],
    }, async (req: FastifyRequest, reply: FastifyReply) => {
        const user = (req as any).user;
        const { taskId } = req.params as any;

        try {
            const result = await brainClient.cancelTask(taskId, user.id);
            return result;
        } catch (err: any) {
            logger.error('Cancel task failed', { error: err.message, taskId });
            return reply.status(500).send({ error: err.message });
        }
    });
}
