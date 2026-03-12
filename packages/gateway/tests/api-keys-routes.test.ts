import Fastify from 'fastify';
import { serializerCompiler, validatorCompiler } from 'fastify-type-provider-zod';
import jwt from 'jsonwebtoken';
import { afterEach, describe, expect, it } from 'vitest';
import apiKeyRoutes from '../src/routes/api-keys';

const SECRET = 'dev-secret-change-me';

function makeToken() {
    return jwt.sign(
        {
            sub: 'user-1',
            email: 'owner@example.com',
            workspaces: [{ id: 'ws-1', role: 'owner' }],
        },
        SECRET,
        { expiresIn: '1h' } as jwt.SignOptions,
    );
}

class FakeRedis {
    private values = new Map<string, string>();
    private sets = new Map<string, Set<string>>();

    async set(key: string, value: string) {
        this.values.set(key, value);
        return 'OK';
    }

    async get(key: string) {
        return this.values.get(key) ?? null;
    }

    async sadd(key: string, value: string) {
        const set = this.sets.get(key) || new Set<string>();
        set.add(value);
        this.sets.set(key, set);
        return set.size;
    }

    async smembers(key: string) {
        return Array.from(this.sets.get(key) || []);
    }

    async srem(key: string, value: string) {
        this.sets.get(key)?.delete(value);
        return 1;
    }

    async del(key: string) {
        this.values.delete(key);
        return 1;
    }
}

describe('workspace-scoped API key routes', () => {
    afterEach(() => {
        process.env.JWT_SECRET = SECRET;
    });

    it('creates, lists, and revokes workspace-scoped keys', async () => {
        process.env.JWT_SECRET = SECRET;
        const app = Fastify();
        app.setValidatorCompiler(validatorCompiler);
        app.setSerializerCompiler(serializerCompiler);
        const redis = new FakeRedis();
        await apiKeyRoutes(app, { redis: redis as any });

        const authHeader = { authorization: `Bearer ${makeToken()}` };
        const createRes = await app.inject({
            method: 'POST',
            url: '/api/workspaces/ws-1/api-keys',
            headers: authHeader,
            payload: {
                name: 'CI key',
                role: 'member',
            },
        });

        expect(createRes.statusCode).toBe(200);
        const created = createRes.json();
        expect(created.workspaceId).toBe('ws-1');
        expect(created.role).toBe('member');
        expect(created.key).toMatch(/^ksk_/);

        const stored = JSON.parse((await redis.get(`apikey:${created.id}`)) || '{}');
        expect(stored.workspaceId).toBe('ws-1');
        expect(stored.role).toBe('member');
        expect(stored.actorUserId).toBe('user-1');

        const listRes = await app.inject({
            method: 'GET',
            url: '/api/workspaces/ws-1/api-keys',
            headers: authHeader,
        });
        expect(listRes.statusCode).toBe(200);
        expect(listRes.json().keys).toEqual([
            expect.objectContaining({
                id: created.id,
                name: 'CI key',
                role: 'member',
                workspaceId: 'ws-1',
            }),
        ]);

        const deleteRes = await app.inject({
            method: 'DELETE',
            url: `/api/workspaces/ws-1/api-keys/${created.id}`,
            headers: authHeader,
        });
        expect(deleteRes.statusCode).toBe(200);
        expect(await redis.get(`apikey:${created.id}`)).toBeNull();

        await app.close();
    });
});
