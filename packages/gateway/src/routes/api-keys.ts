import { FastifyInstance, FastifyRequest, FastifyReply } from 'fastify';
import { z } from 'zod';
import { ZodTypeProvider } from 'fastify-type-provider-zod';
import {
    requireAuth,
    requireRole,
    requireWorkspace,
    generateSecureToken,
} from '../auth/middleware';
import { logger } from '../utils/logger';
import Redis from 'ioredis';

/**
 * API key route plugin — manage programmatic access keys.
 */
export default async function apiKeyRoutes(app: FastifyInstance, deps: { redis: Redis }) {
    const { redis } = deps;
    const typedApp = app.withTypeProvider<ZodTypeProvider>();

    // Prefix for API key storage in Redis
    const KEY_PREFIX = 'apikey:';
    const WORKSPACE_KEYS_PREFIX = 'workspace_apikeys:';

    const workspaceParamsSchema = z.object({
        workspaceId: z.string(),
    });
    type WorkspaceParams = z.infer<typeof workspaceParamsSchema>;

    const createKeySchema = z.object({
        name: z.string().min(1, 'API key name required'),
        expiresInDays: z.number().int().positive().optional(),
        role: z.enum(['admin', 'member', 'guest']).optional(),
    });
    type CreateKeyBody = z.infer<typeof createKeySchema>;

    // ── POST /api/workspaces/:workspaceId/api-keys ──────────────────
    typedApp.post(
        '/api/workspaces/:workspaceId/api-keys',
        {
            preHandler: [requireAuth, requireWorkspace, requireRole('admin')],
            schema: { params: workspaceParamsSchema, body: createKeySchema },
        },
        async (req, reply) => {
            const user = req.user!;
            const { workspaceId } = req.params as WorkspaceParams;
            const { name, expiresInDays, role } = req.body as CreateKeyBody;
            const scopedRole = role || 'member';

            const keyId = generateSecureToken(8); // short ID for display
            const secret = generateSecureToken(48); // the actual secret
            const fullKey = `ksk_${keyId}_${secret}`; // kestrel secret key

            const ttl = expiresInDays ? expiresInDays * 86400 : 365 * 86400; // default 1 year
            const expiresAt = new Date(Date.now() + ttl * 1000).toISOString();

            const keyData = JSON.stringify({
                id: keyId,
                name: name.trim(),
                workspaceId,
                role: scopedRole,
                userId: user.id,
                email: user.email,
                actorUserId: user.id,
                actorEmail: user.email,
                createdAt: new Date().toISOString(),
                expiresAt,
            });

            // Store: hash(key) → metadata
            await redis.set(`${KEY_PREFIX}${keyId}`, keyData, 'EX', ttl);

            // Track workspace-scoped keys
            await redis.sadd(`${WORKSPACE_KEYS_PREFIX}${workspaceId}`, keyId);

            logger.info('API key created', {
                userId: user.id,
                workspaceId,
                keyId,
                name: name.trim(),
                role: scopedRole,
            });

            return {
                id: keyId,
                name: name.trim(),
                role: scopedRole,
                workspaceId,
                key: fullKey, // Only shown once!
                expiresAt,
            };
        },
    );

    // ── GET /api/workspaces/:workspaceId/api-keys ───────────────────
    typedApp.get(
        '/api/workspaces/:workspaceId/api-keys',
        {
            preHandler: [requireAuth, requireWorkspace, requireRole('admin')],
            schema: { params: workspaceParamsSchema },
        },
        async (req) => {
            const { workspaceId } = req.params as WorkspaceParams;
            const keyIds = await redis.smembers(`${WORKSPACE_KEYS_PREFIX}${workspaceId}`);

            const keys = [];
            for (const keyId of keyIds) {
                const raw = await redis.get(`${KEY_PREFIX}${keyId}`);
                if (raw) {
                    const data = JSON.parse(raw);
                    if (data.workspaceId === workspaceId) {
                        keys.push({
                            id: data.id,
                            name: data.name,
                            role: data.role || 'member',
                            workspaceId: data.workspaceId,
                            createdAt: data.createdAt,
                            expiresAt: data.expiresAt,
                        });
                    }
                } else {
                    // Key expired, remove from set
                    await redis.srem(`${WORKSPACE_KEYS_PREFIX}${workspaceId}`, keyId);
                }
            }

            return { keys };
        },
    );

    const deleteKeyParamsSchema = z.object({
        id: z.string(),
    });
    type DeleteKeyParams = z.infer<typeof deleteKeyParamsSchema>;

    // ── DELETE /api/workspaces/:workspaceId/api-keys/:id ────────────
    typedApp.delete(
        '/api/workspaces/:workspaceId/api-keys/:id',
        {
            preHandler: [requireAuth, requireWorkspace, requireRole('admin')],
            schema: {
                params: workspaceParamsSchema.extend(deleteKeyParamsSchema.shape),
            },
        },
        async (req, reply) => {
            const user = req.user!;
            const { workspaceId, id } = req.params as WorkspaceParams & DeleteKeyParams;

            const raw = await redis.get(`${KEY_PREFIX}${id}`);
            if (!raw) {
                return reply.status(404).send({ error: 'API key not found' });
            }

            const data = JSON.parse(raw);
            if (data.workspaceId !== workspaceId) {
                return reply.status(404).send({ error: 'API key not found in this workspace' });
            }

            await redis.del(`${KEY_PREFIX}${id}`);
            await redis.srem(`${WORKSPACE_KEYS_PREFIX}${workspaceId}`, id);

            logger.info('API key revoked', { userId: user.id, workspaceId, keyId: id });

            return { success: true };
        },
    );
}
