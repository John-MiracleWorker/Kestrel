import { describe, it, expect, vi, beforeEach } from 'vitest';
import jwt from 'jsonwebtoken';
import { requireAuth, requireWorkspace, verifyToken, JWTPayload } from '../src/auth/middleware';

// ── Helpers ─────────────────────────────────────────────────────────
const SECRET = 'dev-secret-change-me';

function makeToken(payload: Partial<JWTPayload>, expiresIn = '1h'): string {
    return jwt.sign(
        { sub: 'user-1', email: 'test@example.com', workspaces: [], ...payload },
        SECRET,
        { expiresIn } as jwt.SignOptions,
    );
}

function mockRequest(headers: Record<string, string> = {}, params: any = {}, query: any = {}): any {
    return { headers, params, query };
}

function mockReply(): any {
    const reply: any = {
        statusCode: 200,
        status(code: number) { reply.statusCode = code; return reply; },
        send(body: any) { reply.body = body; return reply; },
    };
    return reply;
}

// ── Tests ───────────────────────────────────────────────────────────

describe('requireAuth', () => {
    it('rejects when no Authorization header is present', async () => {
        const req = mockRequest();
        const reply = mockReply();
        await requireAuth(req, reply);
        expect(reply.statusCode).toBe(401);
        expect(reply.body.error).toBe('No token provided');
    });

    it('rejects an expired token', async () => {
        const token = makeToken({}, '-1s');  // Already expired
        const req = mockRequest({ authorization: `Bearer ${token}` });
        const reply = mockReply();
        await requireAuth(req, reply);
        expect(reply.statusCode).toBe(401);
        expect(reply.body.error).toBe('Invalid or expired token');
    });

    it('sets req.user on valid token', async () => {
        const token = makeToken({
            sub: 'user-42',
            email: 'alice@example.com',
            workspaces: [{ id: 'ws-1', role: 'owner' }],
        });
        const req = mockRequest({ authorization: `Bearer ${token}` });
        const reply = mockReply();
        await requireAuth(req, reply);

        expect(req.user).toBeDefined();
        expect(req.user.id).toBe('user-42');
        expect(req.user.email).toBe('alice@example.com');
        expect(req.user.workspaces).toHaveLength(1);
    });
});

describe('requireWorkspace', () => {
    it('rejects when workspaceId is missing', async () => {
        const req = mockRequest({}, {}, {});
        req.user = { id: 'user-1', workspaces: [] };
        const reply = mockReply();
        await requireWorkspace(req, reply);
        expect(reply.statusCode).toBe(400);
    });

    it('rejects when user is not a member', async () => {
        const req = mockRequest({}, { workspaceId: 'ws-99' }, {});
        req.user = { id: 'user-1', workspaces: [{ id: 'ws-1', role: 'member' }] };
        const reply = mockReply();
        await requireWorkspace(req, reply);
        expect(reply.statusCode).toBe(403);
    });

    it('sets req.workspace on valid membership', async () => {
        const req = mockRequest({}, { workspaceId: 'ws-1' }, {});
        req.user = { id: 'user-1', workspaces: [{ id: 'ws-1', role: 'admin' }] };
        const reply = mockReply();
        await requireWorkspace(req, reply);
        expect(req.workspace).toEqual({ id: 'ws-1', role: 'admin' });
    });
});

describe('verifyToken', () => {
    it('returns payload for a valid token', () => {
        const token = makeToken({ sub: 'user-7', email: 'bob@example.com' });
        const result = verifyToken(token);
        expect(result).not.toBeNull();
        expect(result!.sub).toBe('user-7');
    });

    it('returns null for a tampered token', () => {
        const token = makeToken({}) + 'TAMPERED';
        expect(verifyToken(token)).toBeNull();
    });

    it('returns null for a garbage string', () => {
        expect(verifyToken('not-a-jwt')).toBeNull();
    });
});
