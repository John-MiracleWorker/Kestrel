import Fastify, { FastifyInstance } from 'fastify';
import { WebSocketServer } from 'ws';
import Redis from 'ioredis';
import dotenv from 'dotenv';
import { logger } from './utils/logger';
import { setupMetrics } from './utils/metrics';
import { requireAuth } from './auth/middleware';
import { SessionManager } from './session/manager';
import { BrainClient } from './brain/client';
import { WebChannelAdapter } from './channels/web';
import authRoutes from './routes/auth';
import workspaceRoutes from './routes/workspaces';
import apiKeyRoutes from './routes/api-keys';
import oauthRoutes from './auth/strategies/oauth';
import magicLinkRoutes from './auth/strategies/magic-link';
import providerRoutes from './routes/providers';

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

// ── Fastify App ──────────────────────────────────────────────────────
const app: FastifyInstance = Fastify({
    logger: false, // We use Winston
});

// CORS
app.register(import('@fastify/cors'), {
    origin: config.corsOrigin,
    credentials: true,
});

// ── Services (initialized in start()) ────────────────────────────────
let redis: Redis;
let sessionManager: SessionManager;
let brainClient: BrainClient;
let webChannel: WebChannelAdapter;

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
        // 1. Connect to Redis
        redis = new Redis(config.redisUrl);
        redis.on('error', (err) => logger.error('Redis error', { error: err.message }));
        redis.on('connect', () => logger.info('Redis connected'));

        sessionManager = new SessionManager(redis);

        // 2. Connect to Brain gRPC
        brainClient = new BrainClient(config.brainGrpcUrl);
        await brainClient.connect();
        logger.info(`Brain gRPC connected at ${config.brainGrpcUrl}`);

        // 3. Register route plugins
        const deps = { brainClient, redis };
        await authRoutes(app, deps);
        await workspaceRoutes(app, deps);
        await apiKeyRoutes(app, { redis });
        await oauthRoutes(app, deps);
        await magicLinkRoutes(app, deps);
        await providerRoutes(app, deps);

        // 4. Set up metrics
        setupMetrics(app);

        // 5. Start Fastify HTTP server
        await app.listen({ port: config.port, host: config.host });
        logger.info(`Gateway listening on ${config.host}:${config.port}`);

        // 6. Start WebSocket server (shares same HTTP server)
        const server = app.server;
        const wss = new WebSocketServer({ server, path: config.wsPath });

        webChannel = new WebChannelAdapter(wss, sessionManager, brainClient, config.jwtSecret);
        await webChannel.connect();
        logger.info(`WebSocket server on ${config.wsPath}`);

        // Graceful shutdown
        const shutdown = async () => {
            logger.info('Shutting down Gateway...');
            await webChannel.disconnect();
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
