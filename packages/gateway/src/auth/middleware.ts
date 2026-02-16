import { FastifyRequest, FastifyReply } from 'fastify';
import jwt from 'jsonwebtoken';
import crypto from 'crypto';
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
 * Fastify preHandler hook — verifies Bearer JWT access token.
 */
export async function requireAuth(req: FastifyRequest, reply: FastifyReply) {
    const authHeader = req.headers.authorization;
    const token = authHeader?.replace('Bearer ', '');

    if (!token) {
        return reply.status(401).send({ error: 'No token provided' });
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

    (req as any).user = {
        id: payload.sub,
        email: payload.email,
        workspaces: payload.workspaces,
    };
}

/**
 * Checks that the authenticated user belongs to the requested workspace.
 */
export async function requireWorkspace(req: FastifyRequest, reply: FastifyReply) {
    const workspaceId = (req.params as any).workspaceId || (req.query as any).workspace;

    if (!workspaceId) {
        return reply.status(400).send({ error: 'Workspace ID required' });
    }

    const user = (req as any).user;
    if (!user) {
        return reply.status(401).send({ error: 'Not authenticated' });
    }

    const membership = user.workspaces?.find((w: any) => w.id === workspaceId);
    if (!membership) {
        return reply.status(403).send({ error: 'Not a member of this workspace' });
    }

    (req as any).workspace = { id: workspaceId, role: membership.role };
}

/**
 * Factory: creates a preHandler that requires a minimum role.
 *
 * Usage: { preHandler: [requireAuth, requireWorkspace, requireRole('admin')] }
 */
export function requireRole(minRole: Role) {
    return async function (req: FastifyRequest, reply: FastifyReply) {
        const workspace = (req as any).workspace;
        if (!workspace) {
            return reply.status(403).send({ error: 'Workspace context required' });
        }

        const userLevel = ROLE_HIERARCHY[workspace.role as Role] || 0;
        const requiredLevel = ROLE_HIERARCHY[minRole] || 0;

        if (userLevel < requiredLevel) {
            return reply.status(403).send({
                error: `Requires ${minRole} role or higher`,
            });
        }
    };
}
