import Fastify, { FastifyInstance, FastifyReply, FastifyRequest } from 'fastify';
import { WebSocketServer } from 'ws';
import Redis from 'ioredis';
import jwt from 'jsonwebtoken';
import dotenv from 'dotenv';
import { logger } from './utils/logger';
import { setupMetrics } from './utils/metrics';
import { requireAuth, JWTPayload } from './auth/middleware';
import { SessionManager } from './session/manager';
import { BrainClient } from './brain/client';
import { WebChannelAdapter } from './channels/web';

dotenv.config();

// ── Configuration ────────────────────────────────────────────────────
const config = {
    port: parseInt(process.env.GATEWAY_PORT || '8741'),
    host: process.env.GATEWAY_HOST || '0.0.0.0',
    wsPath: process.env.GATEWAY_WS_PATH || '/ws',
    jwtSecret: process.env.JWT_SECRET || 'dev-secret-change-me',
    jwtExpiration: process.env.JWT_EXPIRATION || '7d',
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

// ── Services ─────────────────────────────────────────────────────────
let redis: Redis;
let sessionManager: SessionManager;
let brainClient: BrainClient;
let webChannel: WebChannelAdapter;

// ── REST Routes ──────────────────────────────────────────────────────

// Health check
app.get('/health', async () => ({
    status: 'ok',
    version: '0.1.0',
    services: {
        gateway: 'running',
        redis: redis?.status === 'ready' ? 'connected' : 'disconnected',
        brain: brainClient?.isConnected() ? 'connected' : 'disconnected',
    },
}));

// Auth routes
app.post('/api/auth/register', async (req: FastifyRequest, reply: FastifyReply) => {
    const { email, password, displayName } = req.body as any;

    if (!email || !password) {
        return reply.status(400).send({ error: 'Email and password required' });
    }

    try {
        // Create user via Brain service
        const user = await brainClient.createUser(email, password, displayName);

        // Generate JWT
        const payload: JWTPayload = {
            sub: user.id,
            email: user.email,
            workspaces: [],
        };
        const token = jwt.sign(payload, config.jwtSecret, { expiresIn: config.jwtExpiration } as jwt.SignOptions);

        return { token, user: { id: user.id, email: user.email, displayName: user.displayName } };
    } catch (err: any) {
        logger.error('Registration failed', { error: err.message });
        return reply.status(400).send({ error: err.message });
    }
});

app.post('/api/auth/login', async (req: FastifyRequest, reply: FastifyReply) => {
    const { email, password } = req.body as any;

    if (!email || !password) {
        return reply.status(400).send({ error: 'Email and password required' });
    }

    try {
        const user = await brainClient.authenticateUser(email, password);

        const payload: JWTPayload = {
            sub: user.id,
            email: user.email,
            workspaces: user.workspaces || [],
        };
        const token = jwt.sign(payload, config.jwtSecret, { expiresIn: config.jwtExpiration } as jwt.SignOptions);

        return { token, user: { id: user.id, email: user.email, displayName: user.displayName } };
    } catch (err: any) {
        logger.error('Login failed', { error: err.message });
        return reply.status(401).send({ error: 'Invalid credentials' });
    }
});

// Workspace routes (protected)
app.get('/api/workspaces', { preHandler: [requireAuth] }, async (req: FastifyRequest) => {
    const user = (req as any).user;
    return brainClient.listWorkspaces(user.id);
});

app.post('/api/workspaces', { preHandler: [requireAuth] }, async (req: FastifyRequest) => {
    const user = (req as any).user;
    const { name } = req.body as any;
    return brainClient.createWorkspace(user.id, name);
});

// Conversation routes (protected)
app.get('/api/workspaces/:workspaceId/conversations', { preHandler: [requireAuth] }, async (req: FastifyRequest) => {
    const { workspaceId } = req.params as any;
    const user = (req as any).user;
    return brainClient.listConversations(user.id, workspaceId);
});

app.post('/api/workspaces/:workspaceId/conversations', { preHandler: [requireAuth] }, async (req: FastifyRequest) => {
    const { workspaceId } = req.params as any;
    const user = (req as any).user;
    return brainClient.createConversation(user.id, workspaceId);
});

app.get('/api/workspaces/:workspaceId/conversations/:conversationId/messages', { preHandler: [requireAuth] }, async (req: FastifyRequest) => {
    const { workspaceId, conversationId } = req.params as any;
    const user = (req as any).user;
    return brainClient.getMessages(user.id, workspaceId, conversationId);
});

// Push notification registration (mobile)
app.post('/api/mobile/register-push', { preHandler: [requireAuth] }, async (req: FastifyRequest) => {
    const user = (req as any).user;
    const { deviceToken, platform } = req.body as any;
    return brainClient.registerPushToken(user.id, deviceToken, platform);
});

// Sync endpoint (mobile)
app.get('/api/mobile/sync', { preHandler: [requireAuth] }, async (req: FastifyRequest) => {
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

        // 3. Set up metrics
        setupMetrics(app);

        // 4. Start Fastify HTTP server
        await app.listen({ port: config.port, host: config.host });
        logger.info(`Gateway listening on ${config.host}:${config.port}`);

        // 5. Start WebSocket server (shares same HTTP server)
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
