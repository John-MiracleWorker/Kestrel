import { describe, expect, it, vi } from 'vitest';
import { MessageRouter } from '../src/sync/router';

describe('MessageRouter', () => {
    it('defaults to prefer_telegram and routes Telegram first when connected', async () => {
        const sends: string[] = [];
        const registry = {
            sendToChannel: vi.fn(async (channel: string) => {
                sends.push(channel);
            }),
            getAdapter: vi.fn((channel: string) =>
                channel === 'telegram' ? { status: 'connected' } : undefined,
            ),
        } as any;
        const redis = {
            get: vi.fn(async () => null),
            set: vi.fn(async () => undefined),
        } as any;

        const router = new MessageRouter(registry, redis);
        const prefs = await router.loadPrefs('user-1');

        expect(prefs.strategy).toBe('prefer_telegram');

        await router.route(
            'user-1',
            {
                conversationId: 'conv-1',
                content: 'hello',
            },
            'web',
        );

        expect(sends).toEqual(['telegram', 'web']);
    });
});
