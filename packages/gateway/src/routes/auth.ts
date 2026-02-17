import { FastifyInstance, FastifyRequest, FastifyReply } from 'fastify';
import { generateTokenPair, verifyToken, requireAuth } from '../auth/middleware';
import { BrainClient } from '../brain/client';
import { logger } from '../utils/logger';
import Redis from 'ioredis';

// Refresh token TTL in Redis (7 days)
const REFRESH_TOKEN_TTL = 7 * 24 * 60 * 60;

interface AuthDeps {
    brainClient: BrainClient;
    redis: Redis;
}

/**
 * Auth route plugin — register, login, refresh, logout.
 */
export default async function authRoutes(app: FastifyInstance, deps: AuthDeps) {
    const { brainClient, redis } = deps;

    // ── POST /api/auth/register ──────────────────────────────────────
    app.post('/api/auth/register', async (req: FastifyRequest, reply: FastifyReply) => {
        const { email, password, displayName } = req.body as any;

        if (!email || !password) {
            return reply.status(400).send({ error: 'Email and password required' });
        }

        if (password.length < 8) {
            return reply.status(400).send({ error: 'Password must be at least 8 characters' });
        }

        try {
            const user = await brainClient.createUser(email, password, displayName);

            // Create a default workspace for the new user
            let workspaces: Array<{ id: string; role: 'owner' | 'admin' | 'member' | 'guest' }> = [];
            try {
                const ws = await brainClient.createWorkspace(user.id, displayName ? `${displayName}'s Workspace` : 'My Workspace');
                workspaces = [{ id: ws.id, role: 'owner' }];
                logger.info('Default workspace created for new user', { userId: user.id, workspaceId: ws.id });
            } catch (wsErr: any) {
                logger.warn('Failed to create default workspace', { error: wsErr.message });
            }

            const tokens = generateTokenPair({
                sub: user.id,
                email: user.email,
                workspaces,
            });

            // Store refresh token in Redis
            await redis.set(
                `refresh:${user.id}:${tokens.refreshToken.slice(-16)}`,
                tokens.refreshToken,
                'EX',
                REFRESH_TOKEN_TTL
            );

            return {
                accessToken: tokens.accessToken,
                refreshToken: tokens.refreshToken,
                expiresIn: tokens.expiresIn,
                user: {
                    id: user.id,
                    email: user.email,
                    displayName: user.display_name || displayName || '',
                },
            };
        } catch (err: any) {
            logger.error('Registration failed', { error: err.message });
            if (err.message?.includes('already exists')) {
                return reply.status(409).send({ error: 'Email already registered' });
            }
            return reply.status(400).send({ error: err.message });
        }
    });

    // ── POST /api/auth/login ─────────────────────────────────────────
    app.post('/api/auth/login', async (req: FastifyRequest, reply: FastifyReply) => {
        const { email, password } = req.body as any;

        if (!email || !password) {
            return reply.status(400).send({ error: 'Email and password required' });
        }

        try {
            const user = await brainClient.authenticateUser(email, password);
            const workspaces = await brainClient.listWorkspaces(user.id);

            const tokens = generateTokenPair({
                sub: user.id,
                email: user.email,
                workspaces: (workspaces || []).map((w: any) => ({
                    id: w.id,
                    role: w.role || 'member',
                })),
            });

            // Store refresh token in Redis
            await redis.set(
                `refresh:${user.id}:${tokens.refreshToken.slice(-16)}`,
                tokens.refreshToken,
                'EX',
                REFRESH_TOKEN_TTL
            );

            return {
                accessToken: tokens.accessToken,
                refreshToken: tokens.refreshToken,
                expiresIn: tokens.expiresIn,
                user: {
                    id: user.id,
                    email: user.email,
                    displayName: user.display_name || '',
                },
            };
        } catch (err: any) {
            logger.error('Login failed', { error: err.message });
            return reply.status(401).send({ error: 'Invalid credentials' });
        }
    });

    // ── POST /api/auth/refresh ───────────────────────────────────────
    app.post('/api/auth/refresh', async (req: FastifyRequest, reply: FastifyReply) => {
        const { refreshToken } = req.body as any;

        if (!refreshToken) {
            return reply.status(400).send({ error: 'Refresh token required' });
        }

        const payload = verifyToken(refreshToken);
        if (!payload || payload.type !== 'refresh') {
            return reply.status(401).send({ error: 'Invalid refresh token' });
        }

        // Check token exists in Redis (not revoked)
        const key = `refresh:${payload.sub}:${refreshToken.slice(-16)}`;
        const stored = await redis.get(key);
        if (!stored) {
            return reply.status(401).send({ error: 'Refresh token revoked or expired' });
        }

        // Fetch fresh workspace list
        let workspaces: any[] = [];
        try {
            workspaces = await brainClient.listWorkspaces(payload.sub);
        } catch {
            workspaces = payload.workspaces || [];
        }

        // Rotate: delete old, issue new pair
        await redis.del(key);

        const tokens = generateTokenPair({
            sub: payload.sub,
            email: payload.email,
            workspaces: (workspaces || []).map((w: any) => ({
                id: w.id,
                role: w.role || 'member',
            })),
        });

        await redis.set(
            `refresh:${payload.sub}:${tokens.refreshToken.slice(-16)}`,
            tokens.refreshToken,
            'EX',
            REFRESH_TOKEN_TTL
        );

        return {
            accessToken: tokens.accessToken,
            refreshToken: tokens.refreshToken,
            expiresIn: tokens.expiresIn,
        };
    });

    // ── POST /api/auth/logout ────────────────────────────────────────
    app.post('/api/auth/logout', { preHandler: [requireAuth] }, async (req: FastifyRequest) => {
        const user = (req as any).user;
        const { refreshToken } = req.body as any;

        if (refreshToken) {
            // Revoke specific refresh token
            const key = `refresh:${user.id}:${refreshToken.slice(-16)}`;
            await redis.del(key);
        } else {
            // Revoke ALL refresh tokens for this user
            const keys = await redis.keys(`refresh:${user.id}:*`);
            if (keys.length > 0) {
                await redis.del(...keys);
            }
        }

        return { success: true };
    });

    // ── GET /api/auth/me ─────────────────────────────────────────────
    app.get('/api/auth/me', { preHandler: [requireAuth] }, async (req: FastifyRequest) => {
        const user = (req as any).user;
        return {
            id: user.id,
            email: user.email,
            workspaces: user.workspaces,
        };
    });
}
