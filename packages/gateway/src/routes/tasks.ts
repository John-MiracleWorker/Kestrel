import { FastifyInstance, FastifyRequest, FastifyReply } from 'fastify';
import { z } from 'zod';
import { ZodTypeProvider } from 'fastify-type-provider-zod';
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
    const typedApp = app.withTypeProvider<ZodTypeProvider>();

    const workspaceParamsSchema = z.object({
        workspaceId: z.string()
    });
    type WorkspaceParams = z.infer<typeof workspaceParamsSchema>;

    const startTaskBodySchema = z.object({
        goal: z.string().min(3, 'Goal must be at least 3 characters'),
        conversationId: z.string().optional(),
        guardrails: z.record(z.string(), z.any()).optional()
    });
    type StartTaskBody = z.infer<typeof startTaskBodySchema>;

    // ── POST /api/workspaces/:workspaceId/tasks ─────────────────
    // Start a new autonomous agent task and stream events via SSE.
    typedApp.post('/api/workspaces/:workspaceId/tasks', {
        preHandler: [requireAuth, requireWorkspace],
        schema: { params: workspaceParamsSchema, body: startTaskBodySchema }
    }, async (req, reply) => {
        const user = req.user!;
        const { workspaceId } = req.params as WorkspaceParams;
        const { goal, conversationId, guardrails } = req.body as StartTaskBody;


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

    const listTasksQuerySchema = z.object({
        status: z.string().optional()
    });
    type ListTasksQuery = z.infer<typeof listTasksQuerySchema>;

    // ── GET /api/workspaces/:workspaceId/tasks ──────────────────
    // List agent tasks for the current user in a workspace.
    typedApp.get('/api/workspaces/:workspaceId/tasks', {
        preHandler: [requireAuth, requireWorkspace],
        schema: { params: workspaceParamsSchema, querystring: listTasksQuerySchema }
    }, async (req) => {
        const user = req.user!;
        const { workspaceId } = req.params as WorkspaceParams;
        const { status } = req.query as ListTasksQuery;

        const result = await brainClient.listTasks(user.id, workspaceId, status);
        return { tasks: result.tasks || [] };
    });

    const taskParamsSchema = z.object({
        taskId: z.string()
    });
    type TaskParams = z.infer<typeof taskParamsSchema>;

    const approveActionBodySchema = z.object({
        approvalId: z.string(),
        approved: z.boolean()
    });
    type ApproveActionBody = z.infer<typeof approveActionBodySchema>;

    // ── POST /api/tasks/:taskId/approve ─────────────────────────
    // Approve or deny a pending agent action.
    typedApp.post('/api/tasks/:taskId/approve', {
        preHandler: [requireAuth],
        schema: { params: taskParamsSchema, body: approveActionBodySchema }
    }, async (req, reply) => {
        const user = req.user!;
        const { taskId } = req.params as TaskParams;
        const { approvalId, approved } = req.body as ApproveActionBody;

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
    typedApp.post('/api/tasks/:taskId/cancel', {
        preHandler: [requireAuth],
        schema: { params: taskParamsSchema }
    }, async (req, reply) => {
        const user = req.user!;
        const { taskId } = req.params as TaskParams;

        try {
            const result = await brainClient.cancelTask(taskId, user.id);
            return result;
        } catch (err: any) {
            logger.error('Cancel task failed', { error: err.message, taskId });
            return reply.status(500).send({ error: err.message });
        }
    });
}
