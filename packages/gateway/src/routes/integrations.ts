import { FastifyInstance } from 'fastify';
import { z } from 'zod';
import { ZodTypeProvider } from 'fastify-type-provider-zod';
import { requireAuth, requireWorkspace, requireRole } from '../auth/middleware';
import { ChannelRegistry } from '../channels/registry';
import { TelegramAdapter } from '../channels/telegram';
import { type ChannelConfigStore, type TelegramChannelConfigRecord } from '../channels/store';
import { logger } from '../utils/logger';

interface IntegrationDeps {
    channelRegistry: ChannelRegistry;
    defaultWorkspaceId?: string;
    channelConfigStore: ChannelConfigStore;
    createTelegramAdapter: (config: TelegramChannelConfigRecord) => TelegramAdapter;
    nativeTelegramOwner?: boolean;
}

/**
 * Integration management routes — dynamic channel adapter lifecycle.
 * Allows the settings UI to start/stop integrations at runtime.
 */
export default async function integrationRoutes(
    app: FastifyInstance,
    {
        channelRegistry,
        defaultWorkspaceId,
        channelConfigStore,
        createTelegramAdapter,
        nativeTelegramOwner = false,
    }: IntegrationDeps,
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
            const telegramAdapter = channelRegistry.getAdapter('telegram') as
                | TelegramAdapter
                | undefined;
            const discordAdapter = channelRegistry.getAdapter('discord');
            const whatsappAdapter = channelRegistry.getAdapter('whatsapp');
            const savedConfig = await channelConfigStore.getTelegramConfig();
            const tokenConfigured = !!savedConfig?.token || !!telegramAdapter;
            return {
                telegram: {
                    owner: nativeTelegramOwner ? 'native_daemon' : 'gateway',
                    connected: telegramAdapter?.status === 'connected',
                    status: telegramAdapter?.status ?? 'disconnected',
                    botId: telegramAdapter?.botInfo?.id,
                    botUsername: telegramAdapter?.botInfo?.username,
                    tokenConfigured,
                    workspaceId:
                        savedConfig?.workspaceId ?? telegramAdapter?.config.defaultWorkspaceId,
                    mode: savedConfig?.mode ?? telegramAdapter?.config.mode,
                },
                discord: {
                    connected: discordAdapter?.status === 'connected',
                    status: discordAdapter?.status ?? 'disconnected',
                },
                whatsapp: {
                    connected: whatsappAdapter?.status === 'connected',
                    status: whatsappAdapter?.status ?? 'disconnected',
                },
            };
        },
    );

    // ── POST /api/workspaces/:workspaceId/integrations/telegram ──────
    // Start or restart the Telegram adapter with a new bot token.
    const telegramBodySchema = z.object({
        token: z.string().min(10, 'Invalid bot token'),
        enabled: z.boolean().default(true),
        mode: z.enum(['polling', 'webhook']).default('polling'),
        webhookUrl: z.string().url().optional(),
    });

    typedApp.post(
        '/api/workspaces/:workspaceId/integrations/telegram',
        {
            preHandler: [requireAuth, requireWorkspace, requireRole('admin')],
            schema: { params: workspaceParamsSchema, body: telegramBodySchema },
        },
        async (req, reply) => {
            const { workspaceId } = req.params as WorkspaceParams;
            const { token, enabled, mode, webhookUrl } = req.body as z.infer<
                typeof telegramBodySchema
            >;

            try {
                // Remove existing adapter if present
                const existing = channelRegistry.getAdapter('telegram');
                if (existing) {
                    await channelRegistry.unregister('telegram');
                    logger.info('Existing Telegram adapter disconnected');
                }

                if (!enabled) {
                    await channelConfigStore.clearTelegramConfig();
                    return { success: true, status: 'disconnected' };
                }

                if (nativeTelegramOwner) {
                    await channelConfigStore.setTelegramConfig({
                        token,
                        mode,
                        webhookUrl,
                        workspaceId: workspaceId || defaultWorkspaceId || 'default',
                        updatedAt: new Date().toISOString(),
                    });
                    return {
                        success: true,
                        owner: 'native_daemon',
                        status: 'managed_by_native_daemon',
                    };
                }

                // Create and register new adapter
                const adapter = createTelegramAdapter({
                    token,
                    mode,
                    webhookUrl,
                    workspaceId: workspaceId || defaultWorkspaceId || 'default',
                    updatedAt: new Date().toISOString(),
                });

                await channelRegistry.register(adapter);

                await channelConfigStore.setTelegramConfig({
                    token,
                    mode,
                    webhookUrl,
                    workspaceId: workspaceId || defaultWorkspaceId || 'default',
                    updatedAt: new Date().toISOString(),
                });

                logger.info('Telegram adapter connected via settings');

                return {
                    success: true,
                    status: 'connected',
                    botId: adapter.botInfo?.id,
                    botUsername: adapter.botInfo?.username,
                };
            } catch (err: any) {
                logger.error('Telegram integration error', { error: err.message });
                return reply.status(400).send({
                    error: `Failed to connect Telegram: ${err.message}`,
                });
            }
        },
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
            await channelConfigStore.clearTelegramConfig();
            return { success: true, status: 'disconnected' };
        },
    );
}
