import { FastifyRequest, FastifyReply } from 'fastify';
import jwt from 'jsonwebtoken';
import crypto from 'crypto';
import Redis from 'ioredis';
import { logger } from '../utils/logger';

// ── Types ────────────────────────────────────────────────────────────

export interface JWTPayload {
    sub: string;        // user_id (UUID)
    email: string;
    workspaces: Array<{
        id: string;
        role: 'owner' | 'admin' | 'member' | 'guest';
    }>;
    type?: 'access' | 'refresh';
    iat?: number;
    exp?: number;
}

export type Role = 'owner' | 'admin' | 'member' | 'guest';

// Role hierarchy: owner > admin > member > guest
const ROLE_HIERARCHY: Record<Role, number> = {
    owner: 40,
    admin: 30,
    member: 20,
    guest: 10,
};

// ── Configuration ────────────────────────────────────────────────────

const JWT_SECRET = process.env.JWT_SECRET || 'dev-secret-change-me';
const ACCESS_TOKEN_EXPIRY = process.env.JWT_ACCESS_EXPIRY || '15m';
const REFRESH_TOKEN_EXPIRY = process.env.JWT_REFRESH_EXPIRY || '7d';

// ── Shared Redis Connection for API Key Validation ──────────────────
// Reuse a single connection instead of creating one per request.

let _apiKeyRedis: Redis | null = null;

function getApiKeyRedis(): Redis {
    if (!_apiKeyRedis) {
        const redisUrl = process.env.REDIS_URL ||
            `redis://${process.env.REDIS_HOST || 'localhost'}:${process.env.REDIS_PORT || '6379'}`;
        _apiKeyRedis = new Redis(redisUrl, {
            lazyConnect: true,
            maxRetriesPerRequest: 2,
            enableReadyCheck: true,
        });
        _apiKeyRedis.on('error', (err) =>
            logger.error('API key Redis connection error', { error: err.message })
        );
    }
    return _apiKeyRedis;
}

// ── Token Generation ─────────────────────────────────────────────────

/**
 * Generate an access + refresh token pair for a user.
 */
export function generateTokenPair(payload: Omit<JWTPayload, 'type' | 'iat' | 'exp'>): {
    accessToken: string;
    refreshToken: string;
    expiresIn: string;
} {
    const accessPayload: JWTPayload = { ...payload, type: 'access' };
    const refreshPayload: JWTPayload = { ...payload, type: 'refresh' };

    const accessToken = jwt.sign(accessPayload, JWT_SECRET, {
        expiresIn: ACCESS_TOKEN_EXPIRY,
    } as jwt.SignOptions);

    const refreshToken = jwt.sign(refreshPayload, JWT_SECRET, {
        expiresIn: REFRESH_TOKEN_EXPIRY,
    } as jwt.SignOptions);

    return { accessToken, refreshToken, expiresIn: ACCESS_TOKEN_EXPIRY };
}

/**
 * Verify a JWT token string and return the payload, or null on failure.
 */
export function verifyToken(token: string): JWTPayload | null {
    try {
        return jwt.verify(token, JWT_SECRET) as JWTPayload;
    } catch {
        return null;
    }
}

/**
 * Generate a secure random token (for API keys, magic links, etc.).
 */
export function generateSecureToken(length = 48): string {
    return crypto.randomBytes(length).toString('base64url');
}

// ── Middleware Hooks ──────────────────────────────────────────────────

/**
 * Fastify preHandler hook — verifies Bearer JWT access token or ksk_ API key.
 *
 * API keys (ksk_{keyId}_{secret}) are looked up in Redis. JWTs are verified
 * using the configured secret.
 */
export async function requireAuth(req: FastifyRequest, reply: FastifyReply) {
    const authHeader = req.headers.authorization;
    const token = authHeader?.replace('Bearer ', '');

    if (!token) {
        return reply.status(401).send({ error: 'No token provided' });
    }

    // Check if this is a Kestrel API key (ksk_ prefix)
    if (token.startsWith('ksk_')) {
        const validated = await validateApiKey(token);
        if (!validated) {
            logger.warn('Invalid API key');
            return reply.status(401).send({ error: 'Invalid or expired API key' });
        }
        req.user = {
            id: validated.userId,
            email: validated.email,
            workspaces: validated.workspaces as any,
        };
        return;
    }

    const payload = verifyToken(token);
    if (!payload) {
        logger.warn('Invalid JWT token');
        return reply.status(401).send({ error: 'Invalid or expired token' });
    }

    // Reject refresh tokens used as access tokens
    if (payload.type === 'refresh') {
        return reply.status(401).send({ error: 'Cannot use refresh token for API access' });
    }

    req.user = {
        id: payload.sub,
        email: payload.email,
        workspaces: payload.workspaces as any,
    };
}

/**
 * Validate a Kestrel API key (ksk_{keyId}_{secret}) against Redis.
 * Uses the shared Redis connection instead of creating one per request.
 */
async function validateApiKey(fullKey: string): Promise<{ userId: string; email: string; workspaces: any[] } | null> {
    try {
        // ksk_{keyId}_{secret} — extract keyId
        const parts = fullKey.split('_');
        if (parts.length < 3 || parts[0] !== 'ksk') return null;
        const keyId = parts[1];

        const redis = getApiKeyRedis();
        const raw = await redis.get(`apikey:${keyId}`);
        if (!raw) return null;

        const data = JSON.parse(raw);
        return {
            userId: data.userId,
            email: data.email,
            workspaces: [],  // API keys don't carry workspace memberships
        };
    } catch (err) {
        logger.error('API key validation error', { error: (err as Error).message });
        return null;
    }
}

/**
 * Checks that the authenticated user belongs to the requested workspace.
 */
export async function requireWorkspace(req: FastifyRequest, reply: FastifyReply) {
    const workspaceId = (req.params as any).workspaceId || (req.query as any).workspace;

    if (!workspaceId) {
        return reply.status(400).send({ error: 'Workspace ID required' });
    }

    const ObjectUser = req.user;
    if (!ObjectUser) {
        return reply.status(401).send({ error: 'Not authenticated' });
    }

    const membership = ObjectUser.workspaces?.find((w: any) => w.id === workspaceId);
    if (!membership) {
        return reply.status(403).send({ error: 'Not a member of this workspace' });
    }

    req.workspace = { id: workspaceId, role: membership.role as Role };
}

/**
 * Factory: creates a preHandler that requires a minimum role.
 *
 * Usage: { preHandler: [requireAuth, requireWorkspace, requireRole('admin')] }
 */
export function requireRole(minRole: Role) {
    return async function (req: FastifyRequest, reply: FastifyReply) {
        const ObjectWorkspace = req.workspace;
        if (!ObjectWorkspace) {
            return reply.status(403).send({ error: 'Workspace context required' });
        }

        const userLevel = ROLE_HIERARCHY[ObjectWorkspace.role as Role] || 0;
        const requiredLevel = ROLE_HIERARCHY[minRole] || 0;

        if (userLevel < requiredLevel) {
            return reply.status(403).send({
                error: `Requires ${minRole} role or higher`,
            });
        }
    };
}
