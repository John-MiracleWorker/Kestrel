import crypto from 'crypto';
import { FastifyInstance } from 'fastify';
import { z } from 'zod';
import { ZodTypeProvider } from 'fastify-type-provider-zod';
import { requireAuth, requireWorkspace, requireRole } from '../auth/middleware';
import { getPool } from '../db/pool';

const WEBHOOK_EVENTS = [
    'task.started',
    'task.completed',
    'task.failed',
    'message.created',
] as const;

type WebhookEventType = typeof WEBHOOK_EVENTS[number];

interface WebhookConfig {
    enabled: boolean;
    endpointUrl: string;
    secret: string;
    selectedEvents: WebhookEventType[];
    maxRetries: number;
    timeoutMs: number;
}

const DEFAULT_CONFIG: WebhookConfig = {
    enabled: false,
    endpointUrl: '',
    secret: '',
    selectedEvents: ['task.completed'],
    maxRetries: 3,
    timeoutMs: 5000,
};

function signPayload(body: string, secret: string) {
    const digest = crypto.createHmac('sha256', secret).update(body).digest('hex');
    return `sha256=${digest}`;
}

async function deliverWebhook(config: WebhookConfig, eventType: WebhookEventType, payload: Record<string, unknown>) {
    const body = JSON.stringify({
        id: crypto.randomUUID(),
        type: eventType,
        timestamp: new Date().toISOString(),
        payload,
    });

    const attempts = Math.max(1, config.maxRetries || 3);
    let lastError = '';

    for (let attempt = 1; attempt <= attempts; attempt += 1) {
        try {
            const controller = new AbortController();
            const timeout = setTimeout(() => controller.abort(), config.timeoutMs || 5000);
            const res = await fetch(config.endpointUrl, {
                method: 'POST',
                headers: {
                    'content-type': 'application/json',
                    'x-kestrel-signature': signPayload(body, config.secret),
                    'x-kestrel-event': eventType,
                    'x-kestrel-attempt': String(attempt),
                },
                body,
                signal: controller.signal,
            });
            clearTimeout(timeout);

            if (res.ok) {
                return { success: true, attempt, statusCode: res.status };
            }
            lastError = `HTTP ${res.status}`;
        } catch (err: any) {
            lastError = err.message || 'Network error';
        }

        if (attempt < attempts) {
            const backoffMs = 1000 * 2 ** (attempt - 1);
            await new Promise(resolve => setTimeout(resolve, backoffMs));
        }
    }

    return { success: false, attempt: attempts, error: lastError };
}

export default async function workspaceWebhookRoutes(app: FastifyInstance) {
    const typedApp = app.withTypeProvider<ZodTypeProvider>();

    const workspaceParams = z.object({ workspaceId: z.string() });
    const inboundParams = z.object({ workspaceId: z.string(), event: z.string().optional() });

    const webhookConfigSchema = z.object({
        enabled: z.boolean(),
        endpointUrl: z.string().url().or(z.literal('')),
        secret: z.string().min(8),
        selectedEvents: z.array(z.enum(WEBHOOK_EVENTS)).default(['task.completed']),
        maxRetries: z.number().int().min(1).max(10).default(3),
        timeoutMs: z.number().int().min(1000).max(15000).default(5000),
    });

    typedApp.get('/api/workspaces/:workspaceId/webhooks/config', {
        preHandler: [requireAuth, requireWorkspace],
        schema: { params: workspaceParams },
    }, async (req) => {
        const { workspaceId } = req.params as z.infer<typeof workspaceParams>;
        const pool = getPool();
        const result = await pool.query('SELECT settings FROM workspaces WHERE id = $1', [workspaceId]);
        const settings = (result.rows[0]?.settings || {}) as Record<string, any>;
        return {
            webhook: {
                ...DEFAULT_CONFIG,
                ...(settings.webhooks || {}),
            },
            supportedEvents: WEBHOOK_EVENTS,
        };
    });

    typedApp.put('/api/workspaces/:workspaceId/webhooks/config', {
        preHandler: [requireAuth, requireWorkspace, requireRole('admin')],
        schema: { params: workspaceParams, body: webhookConfigSchema },
    }, async (req) => {
        const { workspaceId } = req.params as z.infer<typeof workspaceParams>;
        const payload = req.body as WebhookConfig;
        const pool = getPool();

        const result = await pool.query('SELECT settings FROM workspaces WHERE id = $1', [workspaceId]);
        const settings = (result.rows[0]?.settings || {}) as Record<string, any>;
        const nextSettings = {
            ...settings,
            webhooks: payload,
        };

        await pool.query('UPDATE workspaces SET settings = $2, updated_at = NOW() WHERE id = $1', [workspaceId, nextSettings]);
        return { success: true, webhook: payload };
    });

    typedApp.post('/api/workspaces/:workspaceId/webhooks/test', {
        preHandler: [requireAuth, requireWorkspace, requireRole('admin')],
        schema: { params: workspaceParams },
    }, async (req, reply) => {
        const { workspaceId } = req.params as z.infer<typeof workspaceParams>;
        const pool = getPool();
        const result = await pool.query('SELECT settings FROM workspaces WHERE id = $1', [workspaceId]);
        const settings = (result.rows[0]?.settings || {}) as Record<string, any>;
        const config = { ...DEFAULT_CONFIG, ...(settings.webhooks || {}) } as WebhookConfig;

        if (!config.endpointUrl || !config.secret) {
            return reply.status(400).send({ error: 'Configure endpoint URL and secret first.' });
        }

        const delivery = await deliverWebhook(config, 'task.completed', {
            workspaceId,
            sample: true,
            taskId: 'sample-task',
            summary: 'Sample webhook delivery from Settings > Integrations',
        });

        return { success: delivery.success, delivery };
    });

    typedApp.post('/api/workspaces/:workspaceId/webhooks/inbound', {
        preHandler: [requireAuth, requireWorkspace],
        schema: { params: inboundParams },
    }, async (req, reply) => {
        const { workspaceId, event } = req.params as z.infer<typeof inboundParams>;
        const pool = getPool();
        const result = await pool.query('SELECT settings FROM workspaces WHERE id = $1', [workspaceId]);
        const settings = (result.rows[0]?.settings || {}) as Record<string, any>;
        const config = { ...DEFAULT_CONFIG, ...(settings.webhooks || {}) } as WebhookConfig;

        if (!config.secret) {
            return reply.status(400).send({ error: 'Webhook secret is not configured' });
        }

        const rawBody = JSON.stringify(req.body || {});
        const providedSig = (req.headers['x-kestrel-signature'] as string) || '';
        const expectedSig = signPayload(rawBody, config.secret);
        const provided = Buffer.from(providedSig);
        const expected = Buffer.from(expectedSig);
        if (!providedSig || provided.length !== expected.length || !crypto.timingSafeEqual(provided, expected)) {
            return reply.status(401).send({ error: 'Invalid signature' });
        }

        return { success: true, message: 'Inbound webhook accepted', event: event || 'unknown' };
    });
}
