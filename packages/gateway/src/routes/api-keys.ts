import { FastifyInstance, FastifyRequest, FastifyReply } from 'fastify';
import { requireAuth, generateSecureToken } from '../auth/middleware';
import { logger } from '../utils/logger';
import Redis from 'ioredis';

/**
 * API key route plugin — manage programmatic access keys.
 */
export default async function apiKeyRoutes(app: FastifyInstance, deps: { redis: Redis }) {
    const { redis } = deps;

    // Prefix for API key storage in Redis
    const KEY_PREFIX = 'apikey:';
    const USER_KEYS_PREFIX = 'user_apikeys:';

    // ── POST /api/api-keys ───────────────────────────────────────────
    app.post('/api/api-keys', { preHandler: [requireAuth] }, async (req: FastifyRequest, reply: FastifyReply) => {
        const user = (req as any).user;
        const { name, expiresInDays } = req.body as any;

        if (!name || name.trim().length < 1) {
            return reply.status(400).send({ error: 'API key name required' });
        }

        const keyId = generateSecureToken(8);    // short ID for display
        const secret = generateSecureToken(48);   // the actual secret
        const fullKey = `ksk_${keyId}_${secret}`;  // kestrel secret key

        const ttl = expiresInDays ? expiresInDays * 86400 : 365 * 86400; // default 1 year
        const expiresAt = new Date(Date.now() + ttl * 1000).toISOString();

        const keyData = JSON.stringify({
            id: keyId,
            name: name.trim(),
            userId: user.id,
            email: user.email,
            createdAt: new Date().toISOString(),
            expiresAt,
        });

        // Store: hash(key) → metadata
        await redis.set(`${KEY_PREFIX}${keyId}`, keyData, 'EX', ttl);

        // Track user's keys
        await redis.sadd(`${USER_KEYS_PREFIX}${user.id}`, keyId);

        logger.info('API key created', { userId: user.id, keyId, name: name.trim() });

        return {
            id: keyId,
            name: name.trim(),
            key: fullKey,  // Only shown once!
            expiresAt,
        };
    });

    // ── GET /api/api-keys ────────────────────────────────────────────
    app.get('/api/api-keys', { preHandler: [requireAuth] }, async (req: FastifyRequest) => {
        const user = (req as any).user;
        const keyIds = await redis.smembers(`${USER_KEYS_PREFIX}${user.id}`);

        const keys = [];
        for (const keyId of keyIds) {
            const raw = await redis.get(`${KEY_PREFIX}${keyId}`);
            if (raw) {
                const data = JSON.parse(raw);
                keys.push({
                    id: data.id,
                    name: data.name,
                    createdAt: data.createdAt,
                    expiresAt: data.expiresAt,
                });
            } else {
                // Key expired, remove from set
                await redis.srem(`${USER_KEYS_PREFIX}${user.id}`, keyId);
            }
        }

        return { keys };
    });

    // ── DELETE /api/api-keys/:id ─────────────────────────────────────
    app.delete('/api/api-keys/:id', { preHandler: [requireAuth] }, async (req: FastifyRequest, reply: FastifyReply) => {
        const user = (req as any).user;
        const { id } = req.params as any;

        const raw = await redis.get(`${KEY_PREFIX}${id}`);
        if (!raw) {
            return reply.status(404).send({ error: 'API key not found' });
        }

        const data = JSON.parse(raw);
        if (data.userId !== user.id) {
            return reply.status(403).send({ error: 'Not your API key' });
        }

        await redis.del(`${KEY_PREFIX}${id}`);
        await redis.srem(`${USER_KEYS_PREFIX}${user.id}`, id);

        logger.info('API key revoked', { userId: user.id, keyId: id });

        return { success: true };
    });
}
