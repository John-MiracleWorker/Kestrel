import { FastifyInstance, FastifyRequest, FastifyReply } from 'fastify';
import { z } from 'zod';
import { ZodTypeProvider } from 'fastify-type-provider-zod';
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
    const typedApp = app.withTypeProvider<ZodTypeProvider>();

    const registerSchema = z.object({
        email: z.string().email(),
        password: z.string().min(8, 'Password must be at least 8 characters'),
        displayName: z.string().optional(),
    });
    type RegisterBody = z.infer<typeof registerSchema>;

    // ── POST /api/auth/register ──────────────────────────────────────
    typedApp.post('/api/auth/register', {
        schema: { body: registerSchema },
        config: {
            rateLimit: {
                max: 5,
                timeWindow: '1 minute'
            }
        }
    }, async (req, reply) => {
        const { email, password, displayName } = req.body as RegisterBody;

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

    const loginSchema = z.object({
        email: z.string().email(),
        password: z.string(),
    });
    type LoginBody = z.infer<typeof loginSchema>;

    // ── POST /api/auth/login ─────────────────────────────────────────
    typedApp.post('/api/auth/login', {
        schema: { body: loginSchema },
        config: {
            rateLimit: {
                max: 10,
                timeWindow: '1 minute'
            }
        }
    }, async (req, reply) => {
        const { email, password } = req.body as LoginBody;

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

    const refreshSchema = z.object({
        refreshToken: z.string()
    });
    type RefreshBody = z.infer<typeof refreshSchema>;

    // ── POST /api/auth/refresh ───────────────────────────────────────
    typedApp.post('/api/auth/refresh', {
        schema: { body: refreshSchema },
        config: {
            rateLimit: {
                max: 20,
                timeWindow: '1 minute'
            }
        }
    }, async (req, reply) => {
        const { refreshToken } = req.body as RefreshBody;

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

    const logoutSchema = z.object({
        refreshToken: z.string().optional(),
    });
    type LogoutBody = z.infer<typeof logoutSchema>;

    // ── POST /api/auth/logout ────────────────────────────────────────
    typedApp.post('/api/auth/logout', {
        preHandler: [requireAuth],
        schema: { body: logoutSchema }
    }, async (req, reply) => {
        const user = (req as any).user;
        const { refreshToken } = req.body as LogoutBody;

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
