import { FastifyInstance } from 'fastify';
import { z } from 'zod';
import { ZodTypeProvider } from 'fastify-type-provider-zod';
import { requireAuth, requireWorkspace, requireRole } from '../auth/middleware';
import { ChannelRegistry } from '../channels/registry';
import { TelegramAdapter } from '../channels/telegram';
import { logger } from '../utils/logger';

interface IntegrationDeps {
    channelRegistry: ChannelRegistry;
    defaultWorkspaceId?: string;
}

/**
 * Integration management routes — dynamic channel adapter lifecycle.
 * Allows the settings UI to start/stop integrations at runtime.
 */
export default async function integrationRoutes(
    app: FastifyInstance,
    { channelRegistry, defaultWorkspaceId }: IntegrationDeps
) {
    const typedApp = app.withTypeProvider<ZodTypeProvider>();

    const workspaceParamsSchema = z.object({
        workspaceId: z.string(),
    });
    type WorkspaceParams = z.infer<typeof workspaceParamsSchema>;

    // ── GET /api/workspaces/:workspaceId/integrations/status ─────────
    typedApp.get(
        '/api/workspaces/:workspaceId/integrations/status',
        {
            preHandler: [requireAuth, requireWorkspace],
            schema: { params: workspaceParamsSchema },
        },
        async () => {
            const telegramAdapter = channelRegistry.getAdapter('telegram') as TelegramAdapter | undefined;
            return {
                telegram: {
                    connected: telegramAdapter?.status === 'connected',
                    status: telegramAdapter?.status ?? 'disconnected',
                },
                discord: { connected: false, status: 'disconnected' },
                whatsapp: { connected: false, status: 'disconnected' },
            };
        }
    );

    // ── POST /api/workspaces/:workspaceId/integrations/telegram ──────
    // Start or restart the Telegram adapter with a new bot token.
    const telegramBodySchema = z.object({
        token: z.string().min(10, 'Invalid bot token'),
        enabled: z.boolean().default(true),
    });

    typedApp.post(
        '/api/workspaces/:workspaceId/integrations/telegram',
        {
            preHandler: [requireAuth, requireWorkspace, requireRole('admin')],
            schema: { params: workspaceParamsSchema, body: telegramBodySchema },
        },
        async (req, reply) => {
            const { workspaceId } = req.params as WorkspaceParams;
            const { token, enabled } = req.body as z.infer<typeof telegramBodySchema>;

            try {
                // Remove existing adapter if present
                const existing = channelRegistry.getAdapter('telegram');
                if (existing) {
                    await channelRegistry.unregister('telegram');
                    logger.info('Existing Telegram adapter disconnected');
                }

                if (!enabled) {
                    return { success: true, status: 'disconnected' };
                }

                // Create and register new adapter
                const adapter = new TelegramAdapter({
                    botToken: token,
                    mode: 'polling',
                    defaultWorkspaceId: workspaceId || defaultWorkspaceId || 'default',
                });

                await channelRegistry.register(adapter);
                logger.info('Telegram adapter connected via settings');

                return { success: true, status: 'connected' };
            } catch (err: any) {
                logger.error('Telegram integration error', { error: err.message });
                return reply.status(400).send({
                    error: `Failed to connect Telegram: ${err.message}`,
                });
            }
        }
    );

    // ── DELETE /api/workspaces/:workspaceId/integrations/telegram ────
    typedApp.delete(
        '/api/workspaces/:workspaceId/integrations/telegram',
        {
            preHandler: [requireAuth, requireWorkspace, requireRole('admin')],
            schema: { params: workspaceParamsSchema },
        },
        async () => {
            const existing = channelRegistry.getAdapter('telegram');
            if (existing) {
                await channelRegistry.unregister('telegram');
                logger.info('Telegram adapter disconnected via settings');
            }
            return { success: true, status: 'disconnected' };
        }
    );
}
