import type { TelegramChannelStateRecord } from '../store';

import { logger } from '../../utils/logger';
import { processUpdate } from './handlers';

type TelegramAdapterRuntime = any;

export async function connectTelegramAdapter(adapter: TelegramAdapterRuntime): Promise<void> {
    adapter.setStatus('connecting');

    const me = await adapter.api('getMe');
    adapter._botId = me.id;
    adapter._botUsername = me.username;
    logger.info(`Telegram bot connected: @${me.username} (${me.id})`);
    await restoreSessionState(adapter);

    if (adapter.config.mode === 'webhook' && adapter.config.webhookUrl) {
        await adapter.api('setWebhook', {
            url: `${adapter.config.webhookUrl}`,
            allowed_updates: JSON.stringify(['message', 'callback_query']),
        });
        logger.info(`Telegram webhook set: ${adapter.config.webhookUrl}`);
    } else {
        await adapter.api('deleteWebhook');
        startPolling(adapter);
        logger.info('Telegram polling started');
    }

    adapter.setStatus('connected');
}

export async function disconnectTelegramAdapter(adapter: TelegramAdapterRuntime): Promise<void> {
    adapter.pollingActive = false;
    if (adapter.pollingTimer) {
        clearTimeout(adapter.pollingTimer);
    }
    if (adapter.persistTimer) {
        clearTimeout(adapter.persistTimer);
    }

    for (const [, interval] of adapter.typingIntervals) {
        clearInterval(interval);
    }
    adapter.typingIntervals.clear();

    if (adapter.config.mode === 'webhook') {
        await adapter.api('deleteWebhook').catch(() => {
            // Best effort cleanup only.
        });
    }

    await persistSessionState(adapter);
    adapter.setStatus('disconnected');
    logger.info('Telegram adapter disconnected');
}

export function startTyping(
    adapter: TelegramAdapterRuntime,
    chatId: number,
    threadId?: number,
): void {
    const params: Record<string, any> = { chat_id: chatId, action: 'typing' };
    if (threadId !== undefined) {
        params.message_thread_id = threadId;
    }

    adapter.api('sendChatAction', params).catch(() => {});
    if (!adapter.typingIntervals.has(chatId)) {
        const interval = setInterval(() => {
            adapter.api('sendChatAction', params).catch(() => {});
        }, 4000);
        adapter.typingIntervals.set(chatId, interval);
    }
}

export function stopTyping(adapter: TelegramAdapterRuntime, chatId: number): void {
    const interval = adapter.typingIntervals.get(chatId);
    if (interval) {
        clearInterval(interval);
        adapter.typingIntervals.delete(chatId);
    }
}

export function startPolling(adapter: TelegramAdapterRuntime): void {
    adapter.pollingActive = true;
    void poll(adapter);
}

export async function poll(adapter: TelegramAdapterRuntime): Promise<void> {
    if (!adapter.pollingActive) {
        return;
    }

    try {
        const updates = await adapter.api('getUpdates', {
            offset: adapter.pollingOffset,
            timeout: 30,
            allowed_updates: JSON.stringify(['message', 'callback_query']),
        });

        for (const update of updates) {
            adapter.pollingOffset = update.update_id + 1;
            queuePersistSessionState(adapter);
            await processUpdate(adapter, update);
        }
    } catch (error) {
        logger.error('Telegram polling error', { error: (error as Error).message });
    }

    adapter.pollingTimer = setTimeout(() => void poll(adapter), adapter.pollingActive ? 100 : 5000);
}

export async function callTelegramApi(
    adapter: TelegramAdapterRuntime,
    method: string,
    params?: Record<string, any>,
): Promise<any> {
    const response = await fetch(`${adapter.apiBase}/${method}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: params ? JSON.stringify(params) : undefined,
    });

    const data = (await response.json()) as { ok: boolean; result: any; description?: string };
    if (!data.ok) {
        throw new Error(`Telegram API error: ${data.description || 'Unknown error'} (${method})`);
    }
    return data.result;
}

export function queuePersistSessionState(adapter: TelegramAdapterRuntime): void {
    if (!adapter.sessionStore) {
        return;
    }
    if (adapter.persistTimer) {
        clearTimeout(adapter.persistTimer);
    }
    adapter.persistTimer = setTimeout(() => {
        adapter.persistTimer = undefined;
        void persistSessionState(adapter);
    }, 25);
}

export async function restoreSessionState(adapter: TelegramAdapterRuntime): Promise<void> {
    if (!adapter.sessionStore) {
        return;
    }
    const state = await adapter.sessionStore.getTelegramState();
    if (state) {
        applySessionState(adapter, state);
    }
}

export function applySessionState(
    adapter: TelegramAdapterRuntime,
    state: TelegramChannelStateRecord,
): void {
    adapter.pollingOffset = Number(state.pollingOffset || 0);
    adapter.chatIdMap.clear();
    adapter.userIdMap.clear();
    adapter.userThreadMap.clear();
    for (const mapping of state.mappings || []) {
        adapter.chatIdMap.set(mapping.userId, mapping.chatId);
        adapter.userIdMap.set(mapping.chatId, mapping.userId);
        adapter.userThreadMap.set(mapping.userId, mapping.threadId);
    }
}

export async function persistSessionState(adapter: TelegramAdapterRuntime): Promise<void> {
    if (!adapter.sessionStore) {
        return;
    }
    const mappings = Array.from(adapter.chatIdMap.entries() as Iterable<[string, number]>).map(
        ([userId, chatId]) => ({
            userId,
            chatId,
            threadId: adapter.userThreadMap.get(userId),
        }),
    );
    await adapter.sessionStore.setTelegramState({
        mappings,
        pollingOffset: adapter.pollingOffset,
        updatedAt: new Date().toISOString(),
    });
}
