import { FastifyInstance, FastifyRequest, FastifyReply } from 'fastify';
import { requireAuth, requireWorkspace } from '../auth/middleware';
import { BrainClient } from '../brain/client';
import { logger } from '../utils/logger';

interface AutomationDeps {
    brainClient: BrainClient;
}

/**
 * Automation routes — Cron jobs, Webhooks, and Workflows.
 *
 * Cron:
 *   POST   /api/workspaces/:workspaceId/automation/cron
 *   GET    /api/workspaces/:workspaceId/automation/cron
 *   DELETE /api/workspaces/:workspaceId/automation/cron/:jobId
 *
 * Webhooks:
 *   POST   /api/workspaces/:workspaceId/automation/webhooks
 *   GET    /api/workspaces/:workspaceId/automation/webhooks
 *   DELETE /api/workspaces/:workspaceId/automation/webhooks/:webhookId
 *   POST   /api/webhooks/:webhookId/trigger   (public — no auth)
 *
 * Workflows:
 *   GET    /api/workflows
 *   GET    /api/workflows/:workflowId
 *   POST   /api/workspaces/:workspaceId/workflows/:workflowId/launch
 */
export default async function automationRoutes(app: FastifyInstance, deps: AutomationDeps) {
    const { brainClient } = deps;

    // ══════════════════════════════════════════════════════════════════
    // CRON JOBS
    // ══════════════════════════════════════════════════════════════════

    // Create a cron job
    app.post('/api/workspaces/:workspaceId/automation/cron', {
        preHandler: [requireAuth, requireWorkspace],
    }, async (req: FastifyRequest, reply: FastifyReply) => {
        const user = (req as any).user;
        const { workspaceId } = req.params as any;
        const { name, description, cronExpression, goal, maxRuns } = req.body as any;

        if (!name || !cronExpression || !goal) {
            return reply.status(400).send({
                error: 'name, cronExpression, and goal are required',
            });
        }

        try {
            const result = await brainClient.createCronJob({
                workspaceId,
                userId: user.id,
                name,
                description: description || '',
                cronExpression,
                goal,
                maxRuns,
            });
            return reply.status(201).send(result);
        } catch (err: any) {
            logger.error('Create cron job failed', { error: err.message });
            return reply.status(500).send({ error: err.message });
        }
    });

    // List cron jobs
    app.get('/api/workspaces/:workspaceId/automation/cron', {
        preHandler: [requireAuth, requireWorkspace],
    }, async (req: FastifyRequest) => {
        const { workspaceId } = req.params as any;

        try {
            const result = await brainClient.listCronJobs(workspaceId);
            return { jobs: result.jobs || [] };
        } catch (err: any) {
            logger.error('List cron jobs failed', { error: err.message });
            return { jobs: [] };
        }
    });

    // Delete a cron job
    app.delete('/api/workspaces/:workspaceId/automation/cron/:jobId', {
        preHandler: [requireAuth, requireWorkspace],
    }, async (req: FastifyRequest, reply: FastifyReply) => {
        const { jobId } = req.params as any;

        try {
            await brainClient.deleteCronJob(jobId);
            return { success: true };
        } catch (err: any) {
            logger.error('Delete cron job failed', { error: err.message });
            return reply.status(500).send({ error: err.message });
        }
    });

    // ══════════════════════════════════════════════════════════════════
    // WEBHOOKS
    // ══════════════════════════════════════════════════════════════════

    // Create a webhook endpoint
    app.post('/api/workspaces/:workspaceId/automation/webhooks', {
        preHandler: [requireAuth, requireWorkspace],
    }, async (req: FastifyRequest, reply: FastifyReply) => {
        const user = (req as any).user;
        const { workspaceId } = req.params as any;
        const { name, description, goalTemplate, secret } = req.body as any;

        if (!name || !goalTemplate) {
            return reply.status(400).send({
                error: 'name and goalTemplate are required',
            });
        }

        try {
            const result = await brainClient.createWebhook({
                workspaceId,
                userId: user.id,
                name,
                description: description || '',
                goalTemplate,
                secret: secret || '',
            });
            return reply.status(201).send(result);
        } catch (err: any) {
            logger.error('Create webhook failed', { error: err.message });
            return reply.status(500).send({ error: err.message });
        }
    });

    // List webhooks
    app.get('/api/workspaces/:workspaceId/automation/webhooks', {
        preHandler: [requireAuth, requireWorkspace],
    }, async (req: FastifyRequest) => {
        const { workspaceId } = req.params as any;

        try {
            const result = await brainClient.listWebhooks(workspaceId);
            return { webhooks: result.webhooks || [] };
        } catch (err: any) {
            logger.error('List webhooks failed', { error: err.message });
            return { webhooks: [] };
        }
    });

    // Delete a webhook
    app.delete('/api/workspaces/:workspaceId/automation/webhooks/:webhookId', {
        preHandler: [requireAuth, requireWorkspace],
    }, async (req: FastifyRequest, reply: FastifyReply) => {
        const { webhookId } = req.params as any;

        try {
            await brainClient.deleteWebhook(webhookId);
            return { success: true };
        } catch (err: any) {
            logger.error('Delete webhook failed', { error: err.message });
            return reply.status(500).send({ error: err.message });
        }
    });

    // ── PUBLIC: Trigger a webhook (no auth required) ────────────────
    app.post('/api/webhooks/:webhookId/trigger', {
        // No auth — this is the public endpoint external services call
    }, async (req: FastifyRequest, reply: FastifyReply) => {
        const { webhookId } = req.params as any;
        const payload = JSON.stringify(req.body || {});
        const headers = req.headers as Record<string, string>;
        const sourceIp = req.ip || '';

        try {
            const result = await brainClient.triggerWebhook({
                webhookId,
                payload,
                headers: {
                    'content-type': headers['content-type'] || '',
                    'x-signature-256': headers['x-signature-256'] || headers['x-hub-signature-256'] || '',
                    'user-agent': headers['user-agent'] || '',
                },
                sourceIp,
            });

            const status = result.status || 200;
            return reply.status(status).send({
                success: result.success,
                message: result.message || (result.success ? 'OK' : 'Failed'),
            });
        } catch (err: any) {
            logger.error('Webhook trigger failed', { error: err.message, webhookId });
            return reply.status(500).send({ error: 'Internal server error' });
        }
    });

    // ══════════════════════════════════════════════════════════════════
    // WORKFLOWS
    // ══════════════════════════════════════════════════════════════════

    // List available workflow templates
    app.get('/api/workflows', {
        preHandler: [requireAuth],
    }, async (req: FastifyRequest) => {
        const { category } = req.query as any;

        try {
            const result = await brainClient.listWorkflows(category);
            return { workflows: result.workflows || [] };
        } catch (err: any) {
            logger.error('List workflows failed', { error: err.message });
            return { workflows: [] };
        }
    });

    // Get a specific workflow template
    app.get('/api/workflows/:workflowId', {
        preHandler: [requireAuth],
    }, async (req: FastifyRequest, reply: FastifyReply) => {
        const { workflowId } = req.params as any;

        try {
            const result = await brainClient.getWorkflow(workflowId);
            if (!result.workflow) {
                return reply.status(404).send({ error: 'Workflow not found' });
            }
            return result.workflow;
        } catch (err: any) {
            return reply.status(500).send({ error: err.message });
        }
    });

    // Launch a workflow
    app.post('/api/workspaces/:workspaceId/workflows/:workflowId/launch', {
        preHandler: [requireAuth, requireWorkspace],
    }, async (req: FastifyRequest, reply: FastifyReply) => {
        const user = (req as any).user;
        const { workspaceId, workflowId } = req.params as any;
        const { variables, conversationId } = req.body as any;

        try {
            // First, start a task via the workflow
            const stream = brainClient.launchWorkflow({
                workflowId,
                userId: user.id,
                workspaceId,
                variables: variables || {},
                conversationId,
            });

            // Stream the task events via SSE
            reply.raw.writeHead(200, {
                'Content-Type': 'text/event-stream',
                'Cache-Control': 'no-cache',
                'Connection': 'keep-alive',
                'X-Accel-Buffering': 'no',
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
                    metrics: event.metrics,
                });

                reply.raw.write(`event: task\ndata: ${sseData}\n\n`);
            }

            reply.raw.write('event: done\ndata: {}\n\n');
        } catch (err: any) {
            logger.error('Workflow launch failed', { error: err.message, workflowId });
            if (!reply.raw.headersSent) {
                return reply.status(500).send({ error: err.message });
            }
            reply.raw.write(`event: error\ndata: ${JSON.stringify({ error: err.message })}\n\n`);
        } finally {
            if (reply.raw.headersSent) {
                reply.raw.end();
            }
        }
    });
}
