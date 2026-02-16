import { describe, it, expect, vi, beforeEach } from 'vitest';
import { SessionManager, SessionData } from '../src/session/manager';

// ── Mock Redis ──────────────────────────────────────────────────────

function createMockRedis() {
    const store = new Map<string, string>();
    const sets = new Map<string, Set<string>>();

    return {
        store,
        sets,

        async setex(key: string, _ttl: number, value: string) {
            store.set(key, value);
        },

        async get(key: string) {
            return store.get(key) ?? null;
        },

        async del(key: string) {
            store.delete(key);
        },

        async sadd(key: string, member: string) {
            if (!sets.has(key)) sets.set(key, new Set());
            sets.get(key)!.add(member);
        },

        async srem(key: string, member: string) {
            sets.get(key)?.delete(member);
        },

        async smembers(key: string) {
            return Array.from(sets.get(key) ?? []);
        },

        pipeline() {
            const ops: Array<() => void> = [];
            const pipe = {
                del(key: string) {
                    ops.push(() => store.delete(key));
                    return pipe;
                },
                async exec() {
                    ops.forEach(op => op());
                },
            };
            return pipe;
        },
    } as any;
}

// ── Tests ───────────────────────────────────────────────────────────

describe('SessionManager', () => {
    let redis: ReturnType<typeof createMockRedis>;
    let mgr: SessionManager;

    const sampleSession: SessionData = {
        userId: 'user-1',
        email: 'test@example.com',
        channel: 'web',
        connectedAt: new Date().toISOString(),
    };

    beforeEach(() => {
        redis = createMockRedis();
        mgr = new SessionManager(redis);
    });

    it('creates and retrieves a session', async () => {
        await mgr.create('sess-1', sampleSession);
        const result = await mgr.get('sess-1');
        expect(result).not.toBeNull();
        expect(result!.userId).toBe('user-1');
        expect(result!.email).toBe('test@example.com');
    });

    it('returns null for non-existent session', async () => {
        const result = await mgr.get('does-not-exist');
        expect(result).toBeNull();
    });

    it('updates an existing session', async () => {
        await mgr.create('sess-2', sampleSession);
        await mgr.update('sess-2', { workspaceId: 'ws-42' });
        const result = await mgr.get('sess-2');
        expect(result!.workspaceId).toBe('ws-42');
        expect(result!.userId).toBe('user-1'); // Unchanged
    });

    it('destroys a session', async () => {
        await mgr.create('sess-3', sampleSession);
        await mgr.destroy('sess-3');
        const result = await mgr.get('sess-3');
        expect(result).toBeNull();
    });

    it('tracks user sessions', async () => {
        await mgr.create('sess-a', sampleSession);
        await mgr.create('sess-b', sampleSession);
        const sessions = await mgr.getUserSessions('user-1');
        expect(sessions).toContain('sess-a');
        expect(sessions).toContain('sess-b');
    });

    it('destroys all sessions for a user', async () => {
        await mgr.create('sess-x', sampleSession);
        await mgr.create('sess-y', sampleSession);
        await mgr.destroyAllForUser('user-1');
        const result = await mgr.get('sess-x');
        expect(result).toBeNull();
    });
});
