import fs from 'fs';
import os from 'os';
import path from 'path';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { TelegramAdapter } from '../src/channels/telegram';
import { createChannelStores } from '../src/channels/store';
import { LocalRedis } from '../src/redis/compat';

const tempRoots: string[] = [];

function makeTempRoot(): string {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'kestrel-channel-store-'));
    tempRoots.push(root);
    return root;
}

afterEach(() => {
    for (const root of tempRoots.splice(0)) {
        fs.rmSync(root, { recursive: true, force: true });
    }
    delete process.env.KESTREL_HOME;
});

describe('Gateway channel stores', () => {
    it('persists Telegram config and session state in local mode', async () => {
        const root = makeTempRoot();
        process.env.KESTREL_HOME = root;
        const redis = new LocalRedis(path.join(root, 'redis.json'));
        const { channelConfigStore, channelSessionStore } = createChannelStores(redis, true);

        await channelConfigStore.setTelegramConfig({
            token: 'bot-token',
            workspaceId: 'ws-1',
            mode: 'polling',
            updatedAt: '2026-03-13T00:00:00Z',
        });
        await channelSessionStore.setTelegramState({
            mappings: [{ userId: 'user-1', chatId: 12345, threadId: 77 }],
            pollingOffset: 88,
            updatedAt: '2026-03-13T00:00:00Z',
        });

        expect(await channelConfigStore.getTelegramConfig()).toEqual({
            token: 'bot-token',
            workspaceId: 'ws-1',
            mode: 'polling',
            updatedAt: '2026-03-13T00:00:00Z',
        });
        expect(await channelSessionStore.getTelegramState()).toEqual({
            mappings: [{ userId: 'user-1', chatId: 12345, threadId: 77 }],
            pollingOffset: 88,
            updatedAt: '2026-03-13T00:00:00Z',
        });

        await redis.quit();
    });

    it('restores Telegram pairings across adapter restarts', async () => {
        const root = makeTempRoot();
        process.env.KESTREL_HOME = root;
        const redis = new LocalRedis(path.join(root, 'redis.json'));
        const { channelSessionStore } = createChannelStores(redis, true);

        const createAdapter = () => {
            const adapter = new TelegramAdapter(
                {
                    botToken: 'bot-token',
                    mode: 'webhook',
                    webhookUrl: 'https://example.test/telegram',
                    defaultWorkspaceId: 'ws-1',
                },
                {
                    sessionStore: channelSessionStore,
                },
            );
            (adapter as any).api = vi.fn(async (method: string) => {
                if (method === 'getMe') {
                    return { id: 99, username: 'kestrel_test_bot' };
                }
                return {};
            });
            return adapter;
        };

        const adapter = createAdapter();
        await adapter.connect();
        const userId = adapter.resolveUserId({ id: 42, first_name: 'Test' }, 1001);
        adapter.rememberThread(userId, 501);
        await (adapter as any).persistSessionState();
        await adapter.disconnect();

        const restored = createAdapter();
        await restored.connect();

        expect(restored.chatIdMap.get(userId)).toBe(1001);
        expect(restored.userIdMap.get(1001)).toBe(userId);
        expect(restored.userThreadMap.get(userId)).toBe(501);

        await restored.disconnect();
        await redis.quit();
    });
});
