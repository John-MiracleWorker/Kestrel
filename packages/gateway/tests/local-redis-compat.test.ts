import fs from 'fs';
import os from 'os';
import path from 'path';
import { afterEach, describe, expect, it } from 'vitest';
import { LocalRedis } from '../src/redis/compat';
import { SessionManager, type SessionData } from '../src/session/manager';
import { Deduplicator } from '../src/sync/deduplicator';

const tempRoots: string[] = [];

function makeTempFile(name: string): string {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'kestrel-gateway-'));
    tempRoots.push(root);
    return path.join(root, name);
}

afterEach(() => {
    for (const root of tempRoots.splice(0)) {
        fs.rmSync(root, { recursive: true, force: true });
    }
});

describe('LocalRedis compatibility backend', () => {
    it('supports core key, set, hash, list, and pubsub operations', async () => {
        const filePath = makeTempFile('redis.json');
        const redis = new LocalRedis(filePath);
        const subscriber = new LocalRedis(filePath);

        await redis.set('alpha', 'one', 'EX', 60);
        expect(await redis.get('alpha')).toBe('one');
        expect(await redis.exists('alpha')).toBe(1);

        await redis.sadd('members', 'a', 'b');
        await redis.srem('members', 'b');
        expect(await redis.smembers('members')).toEqual(['a']);

        await redis.hset('identity:web:1', {
            userId: 'user-1',
            linked: '0',
        });
        await redis.hset('identity:web:1', 'linked', '1');
        expect(await redis.hget('identity:web:1', 'userId')).toBe('user-1');
        expect(await redis.hgetall('identity:web:1')).toEqual({
            userId: 'user-1',
            linked: '1',
        });

        await redis.sadd('group:old', 'member-1');
        expect(await redis.smove('group:old', 'group:new', 'member-1')).toBe(1);
        expect(await redis.smembers('group:new')).toEqual(['member-1']);

        await redis.set('refresh:user-1:abc', 'token', 'EX', 60);
        expect(await redis.keys('refresh:user-1:*')).toEqual(['refresh:user-1:abc']);

        const messages: string[] = [];
        subscriber.on('message', (_channel, message) => messages.push(message));
        await subscriber.subscribe('notifications');
        await redis.publish('notifications', JSON.stringify({ ok: true }));
        expect(messages).toEqual([JSON.stringify({ ok: true })]);

        await subscriber.quit();
        await redis.quit();
    });

    it('works with session manager and deduplicator', async () => {
        const redis = new LocalRedis(makeTempFile('redis.json'));
        const sessions = new SessionManager(redis);
        const deduplicator = new Deduplicator(redis);

        const session: SessionData = {
            userId: 'user-1',
            email: 'user@example.com',
            channel: 'web',
            connectedAt: new Date().toISOString(),
        };

        await sessions.create('sess-1', session);
        expect((await sessions.get('sess-1'))?.email).toBe('user@example.com');

        await deduplicator.registerIdentity({
            userId: 'user-1',
            channel: 'web',
            channelUserId: '42',
            linked: false,
            createdAt: new Date(),
        });
        expect(await deduplicator.resolveUserId('web', '42')).toBe('user-1');
        expect(await deduplicator.isDuplicate('user-1', 'hello world', 'web')).toBe(false);
        expect(await deduplicator.isDuplicate('user-1', 'hello world', 'telegram')).toBe(true);

        await redis.quit();
    });
});
