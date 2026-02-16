import { FastifyInstance, FastifyRequest, FastifyReply } from 'fastify';
import { generateTokenPair, generateSecureToken } from '../../auth/middleware';
import { BrainClient } from '../../brain/client';
import { logger } from '../../utils/logger';
import Redis from 'ioredis';

// Magic link TTL: 15 minutes
const MAGIC_LINK_TTL = 15 * 60;

interface MagicLinkDeps {
    brainClient: BrainClient;
    redis: Redis;
}

/**
 * Magic link auth route plugin — passwordless login via email.
 */
export default async function magicLinkRoutes(app: FastifyInstance, deps: MagicLinkDeps) {
    const { brainClient, redis } = deps;
    const webBaseUrl = process.env.WEB_BASE_URL || 'http://localhost:5173';

    // ── POST /api/auth/magic-link ────────────────────────────────────
    // Request a magic link email
    app.post('/api/auth/magic-link', async (req: FastifyRequest, reply: FastifyReply) => {
        const { email } = req.body as any;

        if (!email) {
            return reply.status(400).send({ error: 'Email required' });
        }

        const token = generateSecureToken(32);

        // Store token → email mapping in Redis
        await redis.set(
            `magic_link:${token}`,
            JSON.stringify({ email, createdAt: Date.now() }),
            'EX',
            MAGIC_LINK_TTL
        );

        const verifyUrl = `${webBaseUrl}/auth/magic-link?token=${token}`;

        // In production: send email via SendGrid/SES/etc.
        // For now, log the URL (dev mode)
        logger.info('Magic link generated', { email, verifyUrl });

        // Always return success to prevent email enumeration
        return {
            success: true,
            message: 'If an account exists, a login link has been sent.',
            // DEV ONLY — remove in production
            ...(process.env.NODE_ENV !== 'production' && { devVerifyUrl: verifyUrl }),
        };
    });

    // ── GET /api/auth/magic-link/verify ──────────────────────────────
    // Verify a magic link token and issue JWT
    app.get('/api/auth/magic-link/verify', async (req: FastifyRequest, reply: FastifyReply) => {
        const { token } = req.query as any;

        if (!token) {
            return reply.status(400).send({ error: 'Token required' });
        }

        const raw = await redis.get(`magic_link:${token}`);
        if (!raw) {
            return reply.status(401).send({ error: 'Invalid or expired magic link' });
        }

        // Consume the token (one-time use)
        await redis.del(`magic_link:${token}`);

        const { email } = JSON.parse(raw);

        // Find or create user
        let user: any;
        try {
            user = await brainClient.authenticateUser(email, `magic_link`);
        } catch {
            // Create new user with magic link marker
            user = await brainClient.createUser(email, `magic:${generateSecureToken(32)}`, email.split('@')[0]);
        }

        // Generate tokens
        const workspaces = await brainClient.listWorkspaces(user.id);
        const tokens = generateTokenPair({
            sub: user.id,
            email,
            workspaces: (workspaces || []).map((w: any) => ({
                id: w.id,
                role: w.role || 'member',
            })),
        });

        await redis.set(
            `refresh:${user.id}:${tokens.refreshToken.slice(-16)}`,
            tokens.refreshToken,
            'EX',
            7 * 24 * 60 * 60
        );

        return {
            accessToken: tokens.accessToken,
            refreshToken: tokens.refreshToken,
            expiresIn: tokens.expiresIn,
            user: { id: user.id, email },
        };
    });
}
