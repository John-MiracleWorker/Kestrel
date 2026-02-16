import Redis from 'ioredis';
import { ChannelType } from '../channels/base';
import { logger } from '../utils/logger';

// ── Channel Identity ───────────────────────────────────────────────

export interface ChannelIdentity {
    userId: string;        // Kestrel user ID
    channel: ChannelType;
    channelUserId: string; // Platform-specific ID (phone, Telegram ID, Discord ID)
    displayName?: string;
    linked: boolean;       // Whether this identity has been explicitly linked by the user
    createdAt: Date;
}

// ── Deduplicator ───────────────────────────────────────────────────

/**
 * Handles user identity linking across channels and
 * message deduplication to prevent duplicate Brain calls.
 */
export class Deduplicator {
    private readonly DEDUP_TTL = 5;           // 5 seconds dedup window
    private readonly IDENTITY_PREFIX = 'kestrel:identity:';
    private readonly DEDUP_PREFIX = 'kestrel:dedup:';

    constructor(private redis: Redis) { }

    // ── Identity Linking ───────────────────────────────────────────

    /**
     * Register a channel identity for a user.
     * Called when a user first messages from a new channel.
     */
    async registerIdentity(identity: ChannelIdentity): Promise<void> {
        const key = `${this.IDENTITY_PREFIX}${identity.channel}:${identity.channelUserId}`;
        await this.redis.hset(key, {
            userId: identity.userId,
            channel: identity.channel,
            channelUserId: identity.channelUserId,
            displayName: identity.displayName || '',
            linked: identity.linked ? '1' : '0',
            createdAt: identity.createdAt.toISOString(),
        });

        // Also index by Kestrel user ID for reverse lookup
        const reverseKey = `${this.IDENTITY_PREFIX}user:${identity.userId}`;
        await this.redis.sadd(reverseKey, `${identity.channel}:${identity.channelUserId}`);

        logger.info('Channel identity registered', {
            userId: identity.userId,
            channel: identity.channel,
            channelUserId: identity.channelUserId,
        });
    }

    /**
     * Look up a Kestrel user ID from a channel-specific user ID.
     * Returns null if no identity is linked.
     */
    async resolveUserId(channel: ChannelType, channelUserId: string): Promise<string | null> {
        const key = `${this.IDENTITY_PREFIX}${channel}:${channelUserId}`;
        const userId = await this.redis.hget(key, 'userId');
        return userId;
    }

    /**
     * Link two channel identities to the same Kestrel user.
     * This merges the identities so messages from both channels
     * route to the same conversation history.
     */
    async linkIdentities(
        primaryUserId: string,
        secondaryChannel: ChannelType,
        secondaryChannelUserId: string,
    ): Promise<void> {
        const key = `${this.IDENTITY_PREFIX}${secondaryChannel}:${secondaryChannelUserId}`;
        const existing = await this.redis.hgetall(key);

        if (!existing.userId) {
            logger.warn('Cannot link — secondary identity not found', {
                channel: secondaryChannel,
                channelUserId: secondaryChannelUserId,
            });
            return;
        }

        const oldUserId = existing.userId;

        // Update the identity to point to the primary user
        await this.redis.hset(key, 'userId', primaryUserId, 'linked', '1');

        // Move the reverse index entry
        const oldReverseKey = `${this.IDENTITY_PREFIX}user:${oldUserId}`;
        const newReverseKey = `${this.IDENTITY_PREFIX}user:${primaryUserId}`;
        await this.redis.smove(
            oldReverseKey,
            newReverseKey,
            `${secondaryChannel}:${secondaryChannelUserId}`,
        );

        logger.info('Identities linked', {
            primaryUserId,
            secondaryChannel,
            secondaryChannelUserId,
            previousUserId: oldUserId,
        });
    }

    /**
     * Get all channel identities for a Kestrel user.
     */
    async getUserIdentities(userId: string): Promise<ChannelIdentity[]> {
        const reverseKey = `${this.IDENTITY_PREFIX}user:${userId}`;
        const members = await this.redis.smembers(reverseKey);

        const identities: ChannelIdentity[] = [];
        for (const member of members) {
            const key = `${this.IDENTITY_PREFIX}${member}`;
            const data = await this.redis.hgetall(key);
            if (data.userId) {
                identities.push({
                    userId: data.userId,
                    channel: data.channel as ChannelType,
                    channelUserId: data.channelUserId,
                    displayName: data.displayName || undefined,
                    linked: data.linked === '1',
                    createdAt: new Date(data.createdAt),
                });
            }
        }

        return identities;
    }

    // ── Message Deduplication ──────────────────────────────────────

    /**
     * Check if a message has already been processed within the dedup window.
     * Returns true if this is a DUPLICATE (should be skipped).
     * Returns false if this is a NEW message (should be processed).
     */
    async isDuplicate(
        userId: string,
        content: string,
        channel: ChannelType,
    ): Promise<boolean> {
        // Hash the message content for the dedup key
        const contentHash = this.hashContent(content);
        const key = `${this.DEDUP_PREFIX}${userId}:${contentHash}`;

        // Try to set with NX (only if not exists) + TTL
        const result = await this.redis.set(key, channel, 'EX', this.DEDUP_TTL, 'NX');

        if (result === 'OK') {
            // First time seeing this message — not a duplicate
            return false;
        }

        // Already exists — this is a duplicate
        const existingChannel = await this.redis.get(key);
        logger.info('Duplicate message detected', {
            userId,
            originalChannel: existingChannel,
            duplicateChannel: channel,
        });
        return true;
    }

    /**
     * Simple content hash for deduplication.
     */
    private hashContent(content: string): string {
        // Use a simple hash — no need for crypto since this is ephemeral
        let hash = 0;
        for (let i = 0; i < content.length; i++) {
            const char = content.charCodeAt(i);
            hash = ((hash << 5) - hash) + char;
            hash |= 0; // Convert to 32-bit integer
        }
        return Math.abs(hash).toString(36);
    }
}
