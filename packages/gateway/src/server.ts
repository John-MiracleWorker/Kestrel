import Fastify, { FastifyInstance } from 'fastify';
import { WebSocketServer } from 'ws';
import Redis from 'ioredis';
import dotenv from 'dotenv';
import { logger } from './utils/logger';
import { setupMetrics } from './utils/metrics';
import { requireAuth } from './auth/middleware';
import { SessionManager } from './session/manager';
import { BrainClient } from './brain/client';
import { ChannelRegistry } from './channels/registry';
import { WebChannelAdapter } from './channels/web';
import { TelegramAdapter } from './channels/telegram';
import { WhatsAppAdapter } from './channels/whatsapp';
import { DiscordAdapter } from './channels/discord';
import { serializerCompiler, validatorCompiler } from 'fastify-type-provider-zod';
import authRoutes from './routes/auth';
import workspaceRoutes from './routes/workspaces';
import apiKeyRoutes from './routes/api-keys';
import oauthRoutes from './auth/strategies/oauth';
import magicLinkRoutes from './auth/strategies/magic-link';
import providerRoutes from './routes/providers';
import telegramWebhookRoutes from './routes/webhooks/telegram';
import whatsappWebhookRoutes from './routes/webhooks/whatsapp';
import taskRoutes from './routes/tasks';
import integrationRoutes from './routes/integrations';
import automationRoutes from './routes/automation';

dotenv.config();

// ── Configuration ────────────────────────────────────────────────────
const config = {
    port: parseInt(process.env.GATEWAY_PORT || '8741'),
    host: process.env.GATEWAY_HOST || '0.0.0.0',
    wsPath: process.env.GATEWAY_WS_PATH || '/ws',
    jwtSecret: process.env.JWT_SECRET || 'dev-secret-change-me',
    redisUrl: process.env.REDIS_URL || `redis://${process.env.REDIS_HOST || 'localhost'}:${process.env.REDIS_PORT || 6379}`,
    brainGrpcUrl: `${process.env.BRAIN_GRPC_HOST || 'localhost'}:${process.env.BRAIN_GRPC_PORT || '50051'}`,
    corsOrigin: process.env.CORS_ORIGIN || 'http://localhost:5173',
};

const app: FastifyInstance = Fastify({
    logger: false, // We use Winston
});

// Setup Zod Validation Compilers
app.setValidatorCompiler(validatorCompiler);
app.setSerializerCompiler(serializerCompiler);

// Security Headers
app.register(import('@fastify/helmet'), {
    contentSecurityPolicy: process.env.NODE_ENV === 'production' ? undefined : false,
});

// CORS
app.register(import('@fastify/cors'), {
    origin: config.corsOrigin,
    credentials: true,
});

// Global Error Handler
app.setErrorHandler((error, request, reply) => {
    if (error instanceof SyntaxError && error.statusCode === 400 && 'body' in error) {
        logger.warn('JSON Parse Error', { error: error.message, path: request.url });
        return reply.status(400).send({ error: 'Invalid JSON payload format' });
    }

    // Default fallback
    if (error.statusCode && error.statusCode >= 500) {
        logger.error('Server Error', { error: error.message, stack: error.stack, path: request.url });
    }
    return reply.status(error.statusCode || 500).send({ error: error.message || 'Internal Server Error' });
});

// ── Services (initialized in start()) ────────────────────────────────
let redis: Redis;
let sessionManager: SessionManager;
let brainClient: BrainClient;
let channelRegistry: ChannelRegistry;

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
        if (process.env.NODE_ENV === 'production' && (!process.env.JWT_SECRET || process.env.JWT_SECRET === 'dev-secret-change-me')) {
            throw new Error('FATAL ERROR: JWT_SECRET is not securely set in production environment!');
        }

        let telegramAdapter: TelegramAdapter | undefined;
        let whatsappAdapter: WhatsAppAdapter | undefined;

        // 1. Connect to Redis
        redis = new Redis(config.redisUrl);
        redis.on('error', (err) => logger.error('Redis error', { error: err.message }));
        redis.on('connect', () => logger.info('Redis connected'));

        // Register Rate Limiting
        await app.register(import('@fastify/rate-limit'), {
            global: false,
            redis: redis,
        });

        sessionManager = new SessionManager(redis);

        // 2. Connect to Brain gRPC
        brainClient = new BrainClient(config.brainGrpcUrl);
        await brainClient.connect();
        logger.info(`Brain gRPC connected at ${config.brainGrpcUrl}`);

        // 3. Instantiate adapters required by route handlers
        if (process.env.TELEGRAM_BOT_TOKEN) {
            telegramAdapter = new TelegramAdapter({
                botToken: process.env.TELEGRAM_BOT_TOKEN,
                mode: (process.env.TELEGRAM_MODE as 'webhook' | 'polling') || 'polling',
                webhookUrl: process.env.TELEGRAM_WEBHOOK_URL,
                defaultWorkspaceId: process.env.DEFAULT_WORKSPACE_ID || 'default',
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
        await taskRoutes(app, { brainClient });
        await automationRoutes(app, { brainClient });

        if (telegramAdapter) {
            await telegramWebhookRoutes(app, { telegramAdapter });
        }

        if (whatsappAdapter) {
            await whatsappWebhookRoutes(app, { whatsappAdapter });
        }

        // 5. Set up metrics
        setupMetrics(app);

        // 6. Create channel registry + register integration routes (BEFORE listen)
        channelRegistry = new ChannelRegistry(brainClient);

        await integrationRoutes(app, {
            channelRegistry,
            defaultWorkspaceId: process.env.DEFAULT_WORKSPACE_ID || 'default',
        });

        // 7. Start Fastify HTTP server
        await app.listen({ port: config.port, host: config.host });
        logger.info(`Gateway listening on ${config.host}:${config.port}`);

        // 8. Set up WebSocket adapter (requires server to be listening)
        const server = app.server;
        const wss = new WebSocketServer({ server, path: config.wsPath });

        const webAdapter = new WebChannelAdapter(wss, sessionManager, brainClient, config.jwtSecret);
        await channelRegistry.register(webAdapter);
        logger.info(`WebSocket server on ${config.wsPath}`);

        // Telegram (from env var — settings UI uses /integrations/telegram route instead)
        if (telegramAdapter) {
            await channelRegistry.register(telegramAdapter);
            logger.info('Telegram adapter enabled (env var)');
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
