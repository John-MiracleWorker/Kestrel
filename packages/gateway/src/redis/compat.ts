import { EventEmitter } from 'events';
import fs from 'fs';
import path from 'path';
import Redis from 'ioredis';
import type { RedisOptions } from 'ioredis';

type StoredValue = {
    kind: 'string' | 'set' | 'hash' | 'list';
    value: string | string[] | Record<string, string>;
    expiresAt?: number;
};

type SharedState = {
    filePath: string;
    store: Map<string, StoredValue>;
    clients: Set<LocalRedis>;
};

type SetArgs = Array<string | number>;

const sharedStates = new Map<string, SharedState>();

function kestrelHome(): string {
    return (
        process.env.KESTREL_HOME ||
        path.join(process.env.HOME || process.env.USERPROFILE || '.', '.kestrel')
    );
}

function localStorePath(): string {
    return path.join(kestrelHome(), 'state', 'gateway-local-redis.json');
}

function useLocalRedisBackend(): boolean {
    const explicit = (process.env.GATEWAY_REDIS_BACKEND || '').trim().toLowerCase();
    if (explicit) {
        return explicit === 'local';
    }
    const runtimeMode = (process.env.KESTREL_RUNTIME_MODE || '').trim().toLowerCase();
    return runtimeMode === 'native' || runtimeMode === 'local';
}

function loadSharedState(filePath: string): SharedState {
    const existing = sharedStates.get(filePath);
    if (existing) {
        return existing;
    }

    fs.mkdirSync(path.dirname(filePath), { recursive: true });
    const store = new Map<string, StoredValue>();
    if (fs.existsSync(filePath)) {
        try {
            const raw = JSON.parse(fs.readFileSync(filePath, 'utf-8')) as Record<
                string,
                StoredValue
            >;
            for (const [key, value] of Object.entries(raw || {})) {
                store.set(key, value);
            }
        } catch {
            // Ignore corrupted local cache and start fresh.
        }
    }

    const state: SharedState = {
        filePath,
        store,
        clients: new Set(),
    };
    sharedStates.set(filePath, state);
    return state;
}

function persistSharedState(state: SharedState): void {
    const payload = Object.fromEntries(state.store.entries());
    fs.writeFileSync(state.filePath, JSON.stringify(payload, null, 2), 'utf-8');
}

function purgeExpired(state: SharedState): void {
    const now = Date.now();
    let changed = false;
    for (const [key, value] of state.store.entries()) {
        if (value.expiresAt && value.expiresAt <= now) {
            state.store.delete(key);
            changed = true;
        }
    }
    if (changed) {
        persistSharedState(state);
    }
}

function globToRegex(pattern: string): RegExp {
    const escaped = pattern.replace(/[.+^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '.*');
    return new RegExp(`^${escaped}$`);
}

export interface RedisPipelineLike {
    del(...keys: string[]): RedisPipelineLike;
    exec(): Promise<any[]>;
}

export interface RedisLike {
    status: string;
    on(event: 'error' | 'connect' | 'message', listener: (...args: any[]) => void): this;
    set(key: string, value: string, ...args: SetArgs): Promise<'OK' | null>;
    setex(key: string, ttl: number, value: string): Promise<'OK'>;
    get(key: string): Promise<string | null>;
    del(...keys: string[]): Promise<number>;
    sadd(key: string, ...members: string[]): Promise<number>;
    srem(key: string, ...members: string[]): Promise<number>;
    smembers(key: string): Promise<string[]>;
    smove(source: string, destination: string, member: string): Promise<number>;
    exists(key: string): Promise<number>;
    hset(key: string, ...args: any[]): Promise<number>;
    hget(key: string, field: string): Promise<string | null>;
    hgetall(key: string): Promise<Record<string, string>>;
    keys(pattern: string): Promise<string[]>;
    subscribe(
        channel: string,
        listener?: (err: Error | null, count: number) => void,
    ): Promise<number>;
    publish(channel: string, message: string): Promise<number>;
    pipeline(): RedisPipelineLike;
    quit(): Promise<'OK'>;
}

export class LocalRedis extends EventEmitter implements RedisLike {
    status = 'connecting';
    private readonly state: SharedState;
    private readonly subscriptions = new Set<string>();

    constructor(filePath: string = localStorePath()) {
        super();
        this.state = loadSharedState(filePath);
        this.state.clients.add(this);
        queueMicrotask(() => {
            this.status = 'ready';
            this.emit('connect');
        });
    }

    private persist(): void {
        persistSharedState(this.state);
    }

    private purge(): void {
        purgeExpired(this.state);
    }

    private read(key: string): StoredValue | undefined {
        this.purge();
        return this.state.store.get(key);
    }

    private write(key: string, value: StoredValue): void {
        this.state.store.set(key, value);
        this.persist();
    }

    async set(key: string, value: string, ...args: SetArgs): Promise<'OK' | null> {
        let ttlSeconds: number | undefined;
        let nx = false;

        for (let i = 0; i < args.length; i++) {
            const token = String(args[i]).toUpperCase();
            if (token === 'EX' && i + 1 < args.length) {
                ttlSeconds = Number(args[i + 1]);
                i += 1;
            } else if (token === 'NX') {
                nx = true;
            }
        }

        if (nx && this.read(key)) {
            return null;
        }

        this.write(key, {
            kind: 'string',
            value,
            expiresAt: ttlSeconds ? Date.now() + ttlSeconds * 1000 : undefined,
        });
        return 'OK';
    }

    async setex(key: string, ttl: number, value: string): Promise<'OK'> {
        await this.set(key, value, 'EX', ttl);
        return 'OK';
    }

    async get(key: string): Promise<string | null> {
        const entry = this.read(key);
        return entry?.kind === 'string' ? String(entry.value) : null;
    }

    async del(...keys: string[]): Promise<number> {
        let removed = 0;
        for (const key of keys) {
            if (this.state.store.delete(key)) {
                removed += 1;
            }
        }
        if (removed) {
            this.persist();
        }
        return removed;
    }

    async sadd(key: string, ...members: string[]): Promise<number> {
        const entry = this.read(key);
        const values = new Set<string>(entry?.kind === 'set' ? (entry.value as string[]) : []);
        const before = values.size;
        for (const member of members) {
            values.add(member);
        }
        this.write(key, {
            kind: 'set',
            value: Array.from(values),
            expiresAt: entry?.expiresAt,
        });
        return values.size - before;
    }

    async srem(key: string, ...members: string[]): Promise<number> {
        const entry = this.read(key);
        if (!entry || entry.kind !== 'set') {
            return 0;
        }
        const values = new Set(entry.value as string[]);
        const before = values.size;
        for (const member of members) {
            values.delete(member);
        }
        this.write(key, {
            kind: 'set',
            value: Array.from(values),
            expiresAt: entry.expiresAt,
        });
        return before - values.size;
    }

    async smembers(key: string): Promise<string[]> {
        const entry = this.read(key);
        return entry?.kind === 'set' ? Array.from(entry.value as string[]) : [];
    }

    async smove(source: string, destination: string, member: string): Promise<number> {
        const sourceMembers = new Set(await this.smembers(source));
        if (!sourceMembers.has(member)) {
            return 0;
        }
        sourceMembers.delete(member);
        await this.sadd(destination, member);
        this.write(source, {
            kind: 'set',
            value: Array.from(sourceMembers),
        });
        return 1;
    }

    async exists(key: string): Promise<number> {
        return this.read(key) ? 1 : 0;
    }

    async hset(key: string, ...args: any[]): Promise<number> {
        const entry = this.read(key);
        const record: Record<string, string> =
            entry?.kind === 'hash' ? { ...(entry.value as Record<string, string>) } : {};
        let updates: Record<string, string> = {};

        if (args.length === 1 && typeof args[0] === 'object' && args[0] !== null) {
            updates = Object.fromEntries(
                Object.entries(args[0]).map(([field, value]) => [field, String(value)]),
            );
        } else {
            for (let i = 0; i < args.length; i += 2) {
                updates[String(args[i])] = String(args[i + 1]);
            }
        }

        const before = Object.keys(record).length;
        Object.assign(record, updates);
        this.write(key, {
            kind: 'hash',
            value: record,
            expiresAt: entry?.expiresAt,
        });
        return Math.max(0, Object.keys(record).length - before);
    }

    async hget(key: string, field: string): Promise<string | null> {
        const entry = this.read(key);
        if (!entry || entry.kind !== 'hash') {
            return null;
        }
        return (entry.value as Record<string, string>)[field] ?? null;
    }

    async hgetall(key: string): Promise<Record<string, string>> {
        const entry = this.read(key);
        if (!entry || entry.kind !== 'hash') {
            return {};
        }
        return { ...(entry.value as Record<string, string>) };
    }

    async keys(pattern: string): Promise<string[]> {
        this.purge();
        const regex = globToRegex(pattern);
        return Array.from(this.state.store.keys()).filter((key) => regex.test(key));
    }

    async subscribe(
        channel: string,
        listener?: (err: Error | null, count: number) => void,
    ): Promise<number> {
        this.subscriptions.add(channel);
        listener?.(null, this.subscriptions.size);
        return this.subscriptions.size;
    }

    async publish(channel: string, message: string): Promise<number> {
        let delivered = 0;
        for (const client of this.state.clients) {
            if (client.subscriptions.has(channel)) {
                client.emit('message', channel, message);
                delivered += 1;
            }
        }
        return delivered;
    }

    pipeline(): RedisPipelineLike {
        const operations: Array<() => Promise<any>> = [];
        const pipeline = {
            del: (...keys: string[]) => {
                operations.push(() => this.del(...keys));
                return pipeline;
            },
            exec: async () => {
                const results: any[] = [];
                for (const operation of operations) {
                    results.push(await operation());
                }
                return results;
            },
        };
        return pipeline;
    }

    async quit(): Promise<'OK'> {
        this.status = 'end';
        this.state.clients.delete(this);
        this.removeAllListeners();
        return 'OK';
    }
}

export function createRedisClient(redisUrl: string, options?: RedisOptions): RedisLike {
    if (useLocalRedisBackend()) {
        return new LocalRedis();
    }
    return new Redis(redisUrl, options ?? {}) as unknown as RedisLike;
}

export function isLocalRedisBackend(): boolean {
    return useLocalRedisBackend();
}
