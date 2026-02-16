import { FastifyRequest, FastifyReply } from 'fastify';
import jwt from 'jsonwebtoken';
import { logger } from '../utils/logger';

export interface JWTPayload {
    sub: string;        // user_id (UUID)
    email: string;
    workspaces: Array<{
        id: string;
        role: 'owner' | 'admin' | 'member' | 'guest';
    }>;
    iat?: number;
    exp?: number;
}

const JWT_SECRET = process.env.JWT_SECRET || 'dev-secret-change-me';

/**
 * Fastify preHandler hook — verifies Bearer JWT token.
 */
export async function requireAuth(req: FastifyRequest, reply: FastifyReply) {
    const authHeader = req.headers.authorization;
    const token = authHeader?.replace('Bearer ', '');

    if (!token) {
        return reply.status(401).send({ error: 'No token provided' });
    }

    try {
        const payload = jwt.verify(token, JWT_SECRET) as JWTPayload;
        (req as any).user = {
            id: payload.sub,
            email: payload.email,
            workspaces: payload.workspaces,
        };
    } catch (err) {
        logger.warn('Invalid JWT token', { error: (err as Error).message });
        return reply.status(401).send({ error: 'Invalid or expired token' });
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
 * Verify a JWT token string and return the payload, or null on failure.
 */
export function verifyToken(token: string): JWTPayload | null {
    try {
        return jwt.verify(token, JWT_SECRET) as JWTPayload;
    } catch {
        return null;
    }
}
