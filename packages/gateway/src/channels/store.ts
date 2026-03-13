import fs from 'fs';
import path from 'path';
import type { RedisLike } from '../redis/compat';
import { getGatewayStateFile } from '../utils/paths';

const TELEGRAM_CONFIG_KEY = 'gateway:channels:telegram:config';
const TELEGRAM_STATE_KEY = 'gateway:channels:telegram:state';

export interface TelegramChannelConfigRecord {
    token: string;
    workspaceId: string;
    mode: 'polling' | 'webhook';
    webhookUrl?: string;
    updatedAt: string;
}

export interface TelegramChannelStateRecord {
    mappings: Array<{
        userId: string;
        chatId: number;
        threadId?: number;
    }>;
    pollingOffset: number;
    updatedAt: string;
}

export interface ChannelConfigStore {
    getTelegramConfig(): Promise<TelegramChannelConfigRecord | null>;
    setTelegramConfig(config: TelegramChannelConfigRecord): Promise<void>;
    clearTelegramConfig(): Promise<void>;
}

export interface ChannelSessionStore {
    getTelegramState(): Promise<TelegramChannelStateRecord | null>;
    setTelegramState(state: TelegramChannelStateRecord): Promise<void>;
}

type LocalStoreDocument = {
    telegram?: {
        config?: TelegramChannelConfigRecord | null;
        state?: TelegramChannelStateRecord | null;
    };
};

function readLocalDocument(filePath: string): LocalStoreDocument {
    if (!fs.existsSync(filePath)) {
        return {};
    }
    try {
        return JSON.parse(fs.readFileSync(filePath, 'utf-8')) as LocalStoreDocument;
    } catch {
        return {};
    }
}

function writeLocalDocument(filePath: string, document: LocalStoreDocument): void {
    fs.mkdirSync(path.dirname(filePath), { recursive: true });
    fs.writeFileSync(filePath, JSON.stringify(document, null, 2), 'utf-8');
}

class FileBackedChannelStore implements ChannelConfigStore, ChannelSessionStore {
    constructor(private readonly filePath: string = getGatewayStateFile('gateway-channels.json')) {}

    async getTelegramConfig(): Promise<TelegramChannelConfigRecord | null> {
        return readLocalDocument(this.filePath).telegram?.config ?? null;
    }

    async setTelegramConfig(config: TelegramChannelConfigRecord): Promise<void> {
        const document = readLocalDocument(this.filePath);
        document.telegram = {
            ...(document.telegram || {}),
            config,
        };
        writeLocalDocument(this.filePath, document);
    }

    async clearTelegramConfig(): Promise<void> {
        const document = readLocalDocument(this.filePath);
        document.telegram = {
            ...(document.telegram || {}),
            config: null,
        };
        writeLocalDocument(this.filePath, document);
    }

    async getTelegramState(): Promise<TelegramChannelStateRecord | null> {
        return readLocalDocument(this.filePath).telegram?.state ?? null;
    }

    async setTelegramState(state: TelegramChannelStateRecord): Promise<void> {
        const document = readLocalDocument(this.filePath);
        document.telegram = {
            ...(document.telegram || {}),
            state,
        };
        writeLocalDocument(this.filePath, document);
    }
}

class RedisBackedChannelStore implements ChannelConfigStore, ChannelSessionStore {
    constructor(private readonly redis: RedisLike) {}

    async getTelegramConfig(): Promise<TelegramChannelConfigRecord | null> {
        const raw = await this.redis.get(TELEGRAM_CONFIG_KEY);
        return raw ? (JSON.parse(raw) as TelegramChannelConfigRecord) : null;
    }

    async setTelegramConfig(config: TelegramChannelConfigRecord): Promise<void> {
        await this.redis.set(TELEGRAM_CONFIG_KEY, JSON.stringify(config));
    }

    async clearTelegramConfig(): Promise<void> {
        await this.redis.del(TELEGRAM_CONFIG_KEY);
    }

    async getTelegramState(): Promise<TelegramChannelStateRecord | null> {
        const raw = await this.redis.get(TELEGRAM_STATE_KEY);
        return raw ? (JSON.parse(raw) as TelegramChannelStateRecord) : null;
    }

    async setTelegramState(state: TelegramChannelStateRecord): Promise<void> {
        await this.redis.set(TELEGRAM_STATE_KEY, JSON.stringify(state));
    }
}

export function createChannelStores(
    redis: RedisLike,
    useLocalStore: boolean,
): {
    channelConfigStore: ChannelConfigStore;
    channelSessionStore: ChannelSessionStore;
} {
    const store = useLocalStore ? new FileBackedChannelStore() : new RedisBackedChannelStore(redis);
    return {
        channelConfigStore: store,
        channelSessionStore: store,
    };
}
