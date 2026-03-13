import Fastify, { FastifyInstance } from 'fastify';
import { WebSocketServer } from 'ws';
import dotenv from 'dotenv';
import { logger, generateCorrelationId, correlatedLogger } from './utils/logger';
import { setupMetrics } from './utils/metrics';
import { requireAuth } from './auth/middleware';
import { createRedisClient, isLocalRedisBackend, RedisLike } from './redis/compat';
import { SessionManager } from './session/manager';
import { BrainClient } from './brain/client';
import { ChannelRegistry } from './channels/registry';
import { Deduplicator } from './sync/deduplicator';
import { WebChannelAdapter } from './channels/web';
import { TelegramAdapter } from './channels/telegram';
import { WhatsAppAdapter } from './channels/whatsapp';
import { DiscordAdapter } from './channels/discord';
import {
    createChannelStores,
    type ChannelConfigStore,
    type ChannelSessionStore,
    type TelegramChannelConfigRecord,
} from './channels/store';
import { serializerCompiler, validatorCompiler } from 'fastify-type-provider-zod';
import authRoutes from './routes/auth';
import workspaceRoutes from './routes/workspaces';
import apiKeyRoutes from './routes/api-keys';
import oauthRoutes from './auth/strategies/oauth';
import magicLinkRoutes from './auth/strategies/magic-link';
import providerRoutes from './routes/providers';
import ollamaRoutes from './routes/ollama';
import lmstudioRoutes from './routes/lmstudio';
import telegramWebhookRoutes from './routes/webhooks/telegram';
import whatsappWebhookRoutes from './routes/webhooks/whatsapp';
import taskRoutes from './routes/tasks';
import integrationRoutes from './routes/integrations';
import automationRoutes from './routes/automation';
import uploadRoutes from './routes/upload';
import { featureRoutes } from './routes/features';
import visionRoutes from './routes/vision';
import { memoryRoutes } from './routes/memory';
import operationsRoutes from './routes/operations';
import { docsRoutes } from './routes/docs';
import { prRoutes } from './routes/pr';
import workspaceWebhookRoutes from './routes/workspace-webhooks';

dotenv.config();

// ── Type Extensions ──────────────────────────────────────────────────
declare module 'fastify' {
    interface FastifyRequest {
        correlationId: string;
    }
}

// ── Configuration ────────────────────────────────────────────────────
const config = {
    port: parseInt(process.env.GATEWAY_PORT || '8741'),
    host: process.env.GATEWAY_HOST || '0.0.0.0',
    wsPath: process.env.GATEWAY_WS_PATH || '/ws',
    jwtSecret: process.env.JWT_SECRET || 'dev-secret-change-me',
    redisUrl:
        process.env.REDIS_URL ||
        `redis://${process.env.REDIS_HOST || 'localhost'}:${process.env.REDIS_PORT || 6379}`,
    brainGrpcUrl: `${process.env.BRAIN_GRPC_HOST || 'localhost'}:${process.env.BRAIN_GRPC_PORT || '50051'}`,
    corsOrigin: process.env.CORS_ORIGIN || 'http://localhost:5173',
};

const app: FastifyInstance = Fastify({
    logger: false, // We use Winston
});

// Setup Zod Validation Compilers
app.setValidatorCompiler(validatorCompiler);
app.setSerializerCompiler(serializerCompiler);

// ── Correlation ID Middleware ────────────────────────────────────────
// Generates or propagates a correlation ID for every request so that
// logs across Gateway and Brain can be joined for a single user action.
app.addHook('onRequest', async (req, reply) => {
    const correlationId = (req.headers['x-correlation-id'] as string) || generateCorrelationId();
    req.correlationId = correlationId;
    reply.header('x-correlation-id', correlationId);
});

// Security Headers
app.register(import('@fastify/helmet'), {
    contentSecurityPolicy: process.env.NODE_ENV === 'production' ? undefined : false,
});

// CORS
app.register(import('@fastify/cors'), {
    origin: config.corsOrigin,
    credentials: true,
});

// Multipart support for file uploads
app.register(import('@fastify/multipart'), {
    limits: {
        fileSize: 10 * 1024 * 1024, // 10 MB
        files: 5, // Max 5 files per request
    },
});

// Global Error Handler
app.setErrorHandler((error, request, reply) => {
    if (error instanceof SyntaxError && error.statusCode === 400 && 'body' in error) {
        logger.warn('JSON Parse Error', { error: error.message, path: request.url });
        return reply.status(400).send({ error: 'Invalid JSON payload format' });
    }

    // Default fallback
    if (error.statusCode && error.statusCode >= 500) {
        logger.error('Server Error', {
            error: error.message,
            stack: error.stack,
            path: request.url,
        });
    }
    return reply
        .status(error.statusCode || 500)
        .send({ error: error.message || 'Internal Server Error' });
});

// ── Services (initialized in start()) ────────────────────────────────
let redis: RedisLike;
let sessionManager: SessionManager;
let brainClient: BrainClient;
let channelRegistry: ChannelRegistry;
let channelConfigStore: ChannelConfigStore;
let channelSessionStore: ChannelSessionStore;

// ── Health Check ─────────────────────────────────────────────────────
app.get('/health', async () => ({
    status: 'ok',
    version: '0.2.0',
    services: {
        gateway: 'running',
        redis: redis?.status === 'ready' ? 'connected' : 'disconnected',
        brain: brainClient?.isConnected() ? 'connected' : 'disconnected',
    },
}));

// ── Mobile Routes (kept inline — simple stubs) ──────────────────────
app.post('/api/mobile/register-push', { preHandler: [requireAuth] }, async (req) => {
    const user = (req as any).user;
    const { deviceToken, platform } = req.body as any;
    return brainClient.registerPushToken(user.id, deviceToken, platform);
});

app.get('/api/mobile/sync', { preHandler: [requireAuth] }, async (req) => {
    const user = (req as any).user;
    const { since } = req.query as any;
    return brainClient.getUpdates(user.id, since);
});

// ── Startup ──────────────────────────────────────────────────────────
async function start() {
    try {
        if (
            process.env.NODE_ENV === 'production' &&
            (!process.env.JWT_SECRET || process.env.JWT_SECRET === 'dev-secret-change-me')
        ) {
            throw new Error(
                'FATAL ERROR: JWT_SECRET is not securely set in production environment!',
            );
        }

        let telegramAdapter: TelegramAdapter | undefined;
        let whatsappAdapter: WhatsAppAdapter | undefined;
        const localRedisBackend = isLocalRedisBackend();

        // 1. Connect to Redis
        redis = createRedisClient(config.redisUrl);
        redis.on('error', (err) => logger.error('Redis error', { error: err.message }));
        redis.on('connect', () => logger.info('Redis connected'));

        // Register Rate Limiting
        if (localRedisBackend) {
            logger.warn(
                'Gateway is using the local Redis compatibility backend; rate limiting is process-local.',
            );
            await app.register(import('@fastify/rate-limit'), {
                global: false,
            });
        } else {
            await app.register(import('@fastify/rate-limit'), {
                global: false,
                redis: redis as any,
            });
        }

        sessionManager = new SessionManager(redis);
        ({ channelConfigStore, channelSessionStore } = createChannelStores(
            redis,
            localRedisBackend,
        ));

        // 2. Connect to Brain gRPC
        brainClient = new BrainClient(config.brainGrpcUrl);
        await brainClient.connect();
        logger.info(
            brainClient.isLocalMode()
                ? 'Gateway attached to local daemon-backed Brain client'
                : `Brain gRPC connected at ${config.brainGrpcUrl}`,
        );

        const createTelegramAdapter = (settings: TelegramChannelConfigRecord): TelegramAdapter => {
            const adapter = new TelegramAdapter(
                {
                    botToken: settings.token,
                    mode: settings.mode,
                    webhookUrl: settings.webhookUrl,
                    defaultWorkspaceId:
                        settings.workspaceId || process.env.DEFAULT_WORKSPACE_ID || 'default',
                },
                {
                    sessionStore: channelSessionStore,
                },
            );
            adapter.setApprovalHandler(async (approvalId, userId, approved) =>
                brainClient.approveAction(approvalId, userId, approved),
            );
            adapter.setPendingApprovalsLookupHandler((userId, workspaceId) =>
                brainClient.listPendingApprovals(userId, workspaceId),
            );
            return adapter;
        };

        // 3. Instantiate adapters required by route handlers
        if (process.env.TELEGRAM_BOT_TOKEN) {
            telegramAdapter = createTelegramAdapter({
                token: process.env.TELEGRAM_BOT_TOKEN,
                mode: (process.env.TELEGRAM_MODE as 'webhook' | 'polling') || 'polling',
                webhookUrl: process.env.TELEGRAM_WEBHOOK_URL,
                workspaceId: process.env.DEFAULT_WORKSPACE_ID || 'default',
                updatedAt: new Date().toISOString(),
            });
        }

        if (process.env.TWILIO_ACCOUNT_SID && process.env.TWILIO_AUTH_TOKEN) {
            whatsappAdapter = new WhatsAppAdapter({
                accountSid: process.env.TWILIO_ACCOUNT_SID,
                authToken: process.env.TWILIO_AUTH_TOKEN,
                fromNumber: process.env.TWILIO_WHATSAPP_FROM || 'whatsapp:+14155238886',
                defaultWorkspaceId: process.env.DEFAULT_WORKSPACE_ID || 'default',
            });
        }

        // 4. Register route plugins
        const deps = { brainClient, redis };
        await authRoutes(app, deps);
        await workspaceRoutes(app, deps);
        await apiKeyRoutes(app, { redis });

        if (process.env.ENABLE_OAUTH === 'true') {
            await oauthRoutes(app, deps);
        }

        if (process.env.ENABLE_MAGIC_LINK === 'true') {
            await magicLinkRoutes(app, deps);
        }

        await providerRoutes(app, deps);
        await ollamaRoutes(app);
        await lmstudioRoutes(app);
        await taskRoutes(app, { brainClient });
        await automationRoutes(app, { brainClient });
        await uploadRoutes(app);
        await featureRoutes(app, { brainClient });
        await visionRoutes(app, { brainClient });
        await memoryRoutes(app, { brainClient });
        await operationsRoutes(app, { brainClient });
        await docsRoutes(app);
        await prRoutes(app);
        await workspaceWebhookRoutes(app);

        if (telegramAdapter) {
            await telegramWebhookRoutes(app, { telegramAdapter });
        }

        if (whatsappAdapter) {
            await whatsappWebhookRoutes(app, { whatsappAdapter });
        }

        // 5. Set up metrics
        setupMetrics(app);

        // 6. Create channel registry + register integration routes (BEFORE listen)
        const deduplicator = new Deduplicator(redis);
        channelRegistry = new ChannelRegistry(brainClient, deduplicator, sessionManager);

        await integrationRoutes(app, {
            channelRegistry,
            defaultWorkspaceId: process.env.DEFAULT_WORKSPACE_ID || 'default',
            channelConfigStore,
            createTelegramAdapter,
        });

        // 7. Start Fastify HTTP server
        await app.listen({ port: config.port, host: config.host });
        logger.info(`Gateway listening on ${config.host}:${config.port}`);

        // 8. Set up WebSocket adapter (requires server to be listening)
        const server = app.server;
        const wss = new WebSocketServer({ server, path: config.wsPath });

        const webAdapter = new WebChannelAdapter(
            wss,
            sessionManager,
            brainClient,
            config.jwtSecret,
        );
        await channelRegistry.register(webAdapter);
        logger.info(`WebSocket server on ${config.wsPath}`);

        // Set up Redis Pub/Sub for notifications
        const redisSub = createRedisClient(config.redisUrl);
        if (localRedisBackend) {
            logger.warn(
                'Gateway notification pubsub is process-local under the local Redis backend.',
            );
        }
        redisSub.subscribe('notifications', (err, count) => {
            if (err)
                logger.error('Failed to subscribe to notifications channel', {
                    error: err.message,
                });
            else logger.info(`Subscribed to notifications channel (${count})`);
        });

        redisSub.on('message', (channel, message) => {
            if (channel === 'notifications') {
                try {
                    const data = JSON.parse(message);
                    if (data.userId && data.notification) {
                        webAdapter.sendNotification(data.userId, data.notification);
                    }
                } catch (e) {
                    logger.error('Failed to parse incoming notification', {
                        error: (e as Error).message,
                    });
                }
            }
        });

        // Telegram (from env var — settings UI uses /integrations/telegram route instead)
        if (telegramAdapter) {
            await channelRegistry.register(telegramAdapter);
            logger.info('Telegram adapter enabled (env var)');
        } else {
            // No env-var token — try to restore token persisted via the settings UI
            const savedConfig = await channelConfigStore.getTelegramConfig();
            if (savedConfig) {
                try {
                    const restoredAdapter = createTelegramAdapter(savedConfig);
                    await channelRegistry.register(restoredAdapter);
                    logger.info('Telegram adapter restored from persisted config');
                } catch (err: any) {
                    logger.warn('Failed to restore Telegram adapter from persisted config', {
                        error: err.message,
                    });
                }
            }
        }

        // WhatsApp (Twilio)
        if (whatsappAdapter) {
            await channelRegistry.register(whatsappAdapter);
            logger.info('WhatsApp adapter enabled');
        }

        // Discord
        if (process.env.DISCORD_BOT_TOKEN && process.env.DISCORD_CLIENT_ID) {
            const dcAdapter = new DiscordAdapter({
                botToken: process.env.DISCORD_BOT_TOKEN,
                clientId: process.env.DISCORD_CLIENT_ID,
                guildId: process.env.DISCORD_GUILD_ID,
                defaultWorkspaceId: process.env.DEFAULT_WORKSPACE_ID || 'default',
            });
            await channelRegistry.register(dcAdapter);
            logger.info('Discord adapter enabled');
        }

        // Graceful shutdown
        const shutdown = async () => {
            logger.info('Shutting down Gateway...');
            await channelRegistry.shutdown();
            await redisSub.quit();
            await redis.quit();
            brainClient.close();
            await app.close();
            process.exit(0);
        };

        process.on('SIGINT', shutdown);
        process.on('SIGTERM', shutdown);
    } catch (err) {
        logger.error('Gateway startup failed', { error: err });
        process.exit(1);
    }
}

start();
