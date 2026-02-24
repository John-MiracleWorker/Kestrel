import { FastifyInstance, FastifyRequest, FastifyReply } from 'fastify';
import { z } from 'zod';
import { ZodTypeProvider } from 'fastify-type-provider-zod';
import { requireAuth, requireWorkspace } from '../auth/middleware';
import { BrainClient } from '../brain/client';
import { logger } from '../utils/logger';
import { getPool } from '../db/pool';

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
    const typedApp = app.withTypeProvider<ZodTypeProvider>();

    const workspaceParamsSchema = z.object({
        workspaceId: z.string()
    });
    type WorkspaceParams = z.infer<typeof workspaceParamsSchema>;

    // ══════════════════════════════════════════════════════════════════
    // CRON JOBS
    // ══════════════════════════════════════════════════════════════════

    const parseCronJobSchema = z.object({
        prompt: z.string().min(1, 'prompt is required'),
    });
    type ParseCronJobBody = z.infer<typeof parseCronJobSchema>;

    // Parse Natural Language to Cron Job
    typedApp.post('/api/workspaces/:workspaceId/automation/cron/parse', {
        preHandler: [requireAuth, requireWorkspace],
        schema: { params: workspaceParamsSchema, body: parseCronJobSchema }
    }, async (req, reply) => {
        const { workspaceId } = req.params as WorkspaceParams;
        const { prompt } = req.body as ParseCronJobBody;

        try {
            const result = await brainClient.parseCronJob(workspaceId, prompt);
            return reply.status(200).send(result);
        } catch (err: any) {
            logger.error('Parse cron job failed', { error: err.message });
            return reply.status(500).send({ error: err.message });
        }
    });

    const createCronJobSchema = z.object({
        name: z.string().min(1, 'name is required'),
        description: z.string().optional(),
        cronExpression: z.string().min(1, 'cronExpression is required'),
        goal: z.string().min(1, 'goal is required'),
        maxRuns: z.number().int().optional()
    });
    type CreateCronJobBody = z.infer<typeof createCronJobSchema>;

    // Create a cron job
    typedApp.post('/api/workspaces/:workspaceId/automation/cron', {
        preHandler: [requireAuth, requireWorkspace],
        schema: { params: workspaceParamsSchema, body: createCronJobSchema }
    }, async (req, reply) => {
        const user = req.user!;
        const { workspaceId } = req.params as WorkspaceParams;
        const { name, description, cronExpression, goal, maxRuns } = req.body as CreateCronJobBody;

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
    typedApp.get('/api/workspaces/:workspaceId/automation/cron', {
        preHandler: [requireAuth, requireWorkspace],
        schema: { params: workspaceParamsSchema }
    }, async (req) => {
        const { workspaceId } = req.params as WorkspaceParams;

        try {
            const result = await brainClient.listCronJobs(workspaceId);
            return { jobs: result.jobs || [] };
        } catch (err: any) {
            logger.error('List cron jobs failed', { error: err.message });
            return { jobs: [] };
        }
    });

    const cronJobParamsSchema = z.object({
        jobId: z.string(),
        workspaceId: z.string()
    });
    type CronJobParams = z.infer<typeof cronJobParamsSchema>;

    // Delete a cron job
    typedApp.delete('/api/workspaces/:workspaceId/automation/cron/:jobId', {
        preHandler: [requireAuth, requireWorkspace],
        schema: { params: cronJobParamsSchema }
    }, async (req, reply) => {
        const { jobId } = req.params as CronJobParams;

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

    const createWebhookSchema = z.object({
        name: z.string().min(1, 'name is required'),
        description: z.string().optional(),
        goalTemplate: z.string().min(1, 'goalTemplate is required'),
        secret: z.string().optional()
    });
    type CreateWebhookBody = z.infer<typeof createWebhookSchema>;

    // Create a webhook endpoint
    typedApp.post('/api/workspaces/:workspaceId/automation/webhooks', {
        preHandler: [requireAuth, requireWorkspace],
        schema: { params: workspaceParamsSchema, body: createWebhookSchema }
    }, async (req, reply) => {
        const user = req.user!;
        const { workspaceId } = req.params as WorkspaceParams;
        const { name, description, goalTemplate, secret } = req.body as CreateWebhookBody;

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
    typedApp.get('/api/workspaces/:workspaceId/automation/webhooks', {
        preHandler: [requireAuth, requireWorkspace],
        schema: { params: workspaceParamsSchema }
    }, async (req) => {
        const { workspaceId } = req.params as WorkspaceParams;

        try {
            const result = await brainClient.listWebhooks(workspaceId);
            return { webhooks: result.webhooks || [] };
        } catch (err: any) {
            logger.error('List webhooks failed', { error: err.message });
            return { webhooks: [] };
        }
    });

    const webhookParamsSchema = z.object({
        webhookId: z.string(),
        workspaceId: z.string()
    });
    type WebhookParams = z.infer<typeof webhookParamsSchema>;

    // Delete a webhook
    typedApp.delete('/api/workspaces/:workspaceId/automation/webhooks/:webhookId', {
        preHandler: [requireAuth, requireWorkspace],
        schema: { params: webhookParamsSchema }
    }, async (req, reply) => {
        const { webhookId } = req.params as WebhookParams;

        try {
            await brainClient.deleteWebhook(webhookId);
            return { success: true };
        } catch (err: any) {
            logger.error('Delete webhook failed', { error: err.message });
            return reply.status(500).send({ error: err.message });
        }
    });

    const triggerWebhookParamsSchema = z.object({
        webhookId: z.string()
    });
    type TriggerWebhookParams = z.infer<typeof triggerWebhookParamsSchema>;

    // ── PUBLIC: Trigger a webhook (no auth required) ────────────────
    typedApp.post('/api/webhooks/:webhookId/trigger', {
        schema: { params: triggerWebhookParamsSchema }
    }, async (req, reply) => {
        const { webhookId } = req.params as TriggerWebhookParams;
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

    const listWorkflowsQuerySchema = z.object({
        category: z.string().optional()
    });
    type ListWorkflowsQuery = z.infer<typeof listWorkflowsQuerySchema>;

    // List available workflow templates
    typedApp.get('/api/workflows', {
        preHandler: [requireAuth],
        schema: { querystring: listWorkflowsQuerySchema }
    }, async (req) => {
        const { category } = req.query as ListWorkflowsQuery;

        try {
            const result = await brainClient.listWorkflows(category);
            return { workflows: result.workflows || [] };
        } catch (err: any) {
            logger.error('List workflows failed', { error: err.message });
            return { workflows: [] };
        }
    });

    const workflowParamsSchema = z.object({
        workflowId: z.string()
    });
    type WorkflowParams = z.infer<typeof workflowParamsSchema>;

    // Get a specific workflow template
    typedApp.get('/api/workflows/:workflowId', {
        preHandler: [requireAuth],
        schema: { params: workflowParamsSchema }
    }, async (req, reply) => {
        const { workflowId } = req.params as WorkflowParams;

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

    const launchWorkflowParamsSchema = z.object({
        workspaceId: z.string(),
        workflowId: z.string()
    });
    type LaunchWorkflowParams = z.infer<typeof launchWorkflowParamsSchema>;

    const launchWorkflowBodySchema = z.object({
        variables: z.record(z.string(), z.any()).optional(),
        conversationId: z.string().optional()
    });
    type LaunchWorkflowBody = z.infer<typeof launchWorkflowBodySchema>;

    // Launch a workflow
    typedApp.post('/api/workspaces/:workspaceId/workflows/:workflowId/launch', {
        preHandler: [requireAuth, requireWorkspace],
        schema: { params: launchWorkflowParamsSchema, body: launchWorkflowBodySchema }
    }, async (req, reply) => {
        const user = req.user!;
        const { workspaceId, workflowId } = req.params as LaunchWorkflowParams;
        const { variables, conversationId } = req.body as LaunchWorkflowBody;

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
            if (!reply.raw.writableEnded) {
                reply.raw.end();
            }
        }
    });

    // ══════════════════════════════════════════════════════════════════
    // CAPABILITIES
    // ══════════════════════════════════════════════════════════════════

    /**
     * Helper: slugify capability name into a stable id.
     *   "Memory Graph" → "memory-graph"
     */
    const toCapId = (name: string): string =>
        name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');

    // Get all agent capability statuses (merged with workspace prefs)
    typedApp.get('/api/workspaces/:workspaceId/capabilities', {
        preHandler: [requireAuth, requireWorkspace],
        schema: { params: workspaceParamsSchema }
    }, async (req) => {
        const { workspaceId } = req.params as WorkspaceParams;

        try {
            // 1. Get capabilities from brain runtime
            const result = await brainClient.getCapabilities(workspaceId);
            const brainCaps: any[] = result.capabilities || [];

            // 2. Get workspace preferences from DB
            const { rows: prefs } = await getPool().query(
                'SELECT capability_id, installed, enabled FROM workspace_capabilities WHERE workspace_id = $1',
                [workspaceId]
            );
            const prefMap = new Map<string, { installed: boolean; enabled: boolean }>();
            for (const row of prefs) {
                prefMap.set(row.capability_id, { installed: row.installed, enabled: row.enabled });
            }

            // 3. Merge
            const capabilities = brainCaps.map((cap: any) => {
                const id = toCapId(cap.name);
                const pref = prefMap.get(id);
                return {
                    id,
                    name: cap.name,
                    description: cap.description || '',
                    status: cap.status || 'disabled',
                    category: cap.category || '',
                    icon: cap.icon || '⚡',
                    stats: cap.stats || {},
                    installed: pref ? pref.installed : (cap.status === 'active'),
                    enabled: pref ? pref.enabled : (cap.status === 'active'),
                };
            });

            return { capabilities };
        } catch (err: any) {
            logger.error('Get capabilities failed', { error: err.message });
            return { capabilities: [] };
        }
    });

    const capabilityParamsSchema = z.object({
        workspaceId: z.string(),
        capId: z.string(),
    });
    type CapabilityParams = z.infer<typeof capabilityParamsSchema>;

    // Install a capability for this workspace
    typedApp.post('/api/workspaces/:workspaceId/capabilities/:capId/install', {
        preHandler: [requireAuth, requireWorkspace],
        schema: { params: capabilityParamsSchema }
    }, async (req, reply) => {
        const { workspaceId, capId } = req.params as CapabilityParams;

        try {
            await getPool().query(
                `INSERT INTO workspace_capabilities (workspace_id, capability_id, installed, enabled, installed_at, updated_at)
                 VALUES ($1, $2, TRUE, TRUE, NOW(), NOW())
                 ON CONFLICT (workspace_id, capability_id) DO UPDATE SET
                     installed = TRUE, enabled = TRUE, installed_at = COALESCE(workspace_capabilities.installed_at, NOW()), updated_at = NOW()`,
                [workspaceId, capId]
            );
            return { success: true, capId, installed: true, enabled: true };
        } catch (err: any) {
            logger.error('Install capability failed', { error: err.message, capId });
            return reply.status(500).send({ error: err.message });
        }
    });

    // Toggle (enable/disable) an installed capability
    typedApp.patch('/api/workspaces/:workspaceId/capabilities/:capId', {
        preHandler: [requireAuth, requireWorkspace],
        schema: {
            params: capabilityParamsSchema,
            body: z.object({ enabled: z.boolean() }),
        }
    }, async (req, reply) => {
        const { workspaceId, capId } = req.params as CapabilityParams;
        const { enabled } = req.body as { enabled: boolean };

        try {
            const result = await getPool().query(
                `UPDATE workspace_capabilities SET enabled = $3, updated_at = NOW()
                 WHERE workspace_id = $1 AND capability_id = $2`,
                [workspaceId, capId, enabled]
            );
            if (result.rowCount === 0) {
                return reply.status(404).send({ error: 'Capability not installed' });
            }
            return { success: true, capId, enabled };
        } catch (err: any) {
            logger.error('Toggle capability failed', { error: err.message, capId });
            return reply.status(500).send({ error: err.message });
        }
    });
}
