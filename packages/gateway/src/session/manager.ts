import Redis from 'ioredis';
import { logger } from '../utils/logger';

const SESSION_TTL = parseInt(process.env.REDIS_SESSION_TTL || '604800'); // 7 days

export interface SessionData {
    userId: string;
    email: string;
    channel: string;
    connectedAt: string;
    workspaceId?: string;
    metadata?: Record<string, any>;
}

/**
 * Redis-backed session manager.
 * Stores per-connection session state with configurable TTL.
 */
export class SessionManager {
    private prefix = 'session:';

    constructor(private redis: Redis) { }

    async create(sessionId: string, data: SessionData): Promise<void> {
        const key = this.prefix + sessionId;
        await this.redis.setex(key, SESSION_TTL, JSON.stringify(data));
        // Track user's active sessions
        await this.redis.sadd(`user_sessions:${data.userId}`, sessionId);
        logger.debug('Session created', { sessionId, userId: data.userId });
    }

    async get(sessionId: string): Promise<SessionData | null> {
        const raw = await this.redis.get(this.prefix + sessionId);
        if (!raw) return null;
        return JSON.parse(raw);
    }

    async update(sessionId: string, updates: Partial<SessionData>): Promise<void> {
        const existing = await this.get(sessionId);
        if (!existing) return;
        const merged = { ...existing, ...updates };
        await this.redis.setex(this.prefix + sessionId, SESSION_TTL, JSON.stringify(merged));
    }

    async destroy(sessionId: string): Promise<void> {
        const data = await this.get(sessionId);
        if (data) {
            await this.redis.srem(`user_sessions:${data.userId}`, sessionId);
        }
        await this.redis.del(this.prefix + sessionId);
        logger.debug('Session destroyed', { sessionId });
    }

    async getUserSessions(userId: string): Promise<string[]> {
        return this.redis.smembers(`user_sessions:${userId}`);
    }

    async destroyAllForUser(userId: string): Promise<void> {
        const sessions = await this.getUserSessions(userId);
        const pipeline = this.redis.pipeline();
        for (const sid of sessions) {
            pipeline.del(this.prefix + sid);
        }
        pipeline.del(`user_sessions:${userId}`);
        await pipeline.exec();
        logger.info('All sessions destroyed for user', { userId, count: sessions.length });
    }
}
