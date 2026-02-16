import Redis from 'ioredis';
import { ChannelType, OutgoingMessage } from '../channels/base';
import { ChannelRegistry } from '../channels/registry';
import { logger } from '../utils/logger';

// ── Notification Preference ────────────────────────────────────────

export type NotifyStrategy = 'same_channel' | 'all_channels' | 'prefer_web';

export interface UserNotificationPrefs {
    strategy: NotifyStrategy;
    enabledChannels: ChannelType[];
    muteUntil?: Date;
}

// ── Message Router ─────────────────────────────────────────────────

/**
 * Routes outgoing messages to the appropriate channel(s) for a user,
 * based on their notification preferences and active connections.
 */
export class MessageRouter {
    // User notification preferences (userId → prefs)
    private prefs = new Map<string, UserNotificationPrefs>();

    constructor(
        private registry: ChannelRegistry,
        private redis: Redis,
    ) { }

    /**
     * Load notification preferences from Redis.
     */
    async loadPrefs(userId: string): Promise<UserNotificationPrefs> {
        const cached = this.prefs.get(userId);
        if (cached) return cached;

        const raw = await this.redis.get(`kestrel:notify:${userId}`);
        const defaults: UserNotificationPrefs = {
            strategy: 'same_channel',
            enabledChannels: ['web', 'telegram', 'whatsapp', 'discord'],
        };

        const prefs = raw ? { ...defaults, ...JSON.parse(raw) } : defaults;
        this.prefs.set(userId, prefs);
        return prefs;
    }

    /**
     * Save notification preferences.
     */
    async savePrefs(userId: string, prefs: UserNotificationPrefs): Promise<void> {
        this.prefs.set(userId, prefs);
        await this.redis.set(
            `kestrel:notify:${userId}`,
            JSON.stringify(prefs),
            'EX', 86400 * 30, // 30-day TTL
        );
    }

    /**
     * Route a message to the appropriate channel(s) for a user.
     *
     * @param userId       - Kestrel user ID
     * @param message      - The message to route
     * @param originChannel - The channel the message originally came from
     */
    async route(
        userId: string,
        message: OutgoingMessage,
        originChannel: ChannelType,
    ): Promise<void> {
        const prefs = await this.loadPrefs(userId);

        // Check mute
        if (prefs.muteUntil && prefs.muteUntil > new Date()) {
            logger.debug('User is muted, skipping notification', { userId });
            return;
        }

        switch (prefs.strategy) {
            case 'same_channel':
                // Reply only on the same channel the message came from
                await this.registry.sendToChannel(originChannel, userId, message);
                break;

            case 'all_channels':
                // Broadcast to all enabled channels (except origin to avoid double)
                for (const ch of prefs.enabledChannels) {
                    if (ch !== originChannel) {
                        await this.registry.sendToChannel(ch, userId, message).catch(() => { });
                    }
                }
                // Always send on origin channel
                await this.registry.sendToChannel(originChannel, userId, message);
                break;

            case 'prefer_web':
                // Send on web if connected, otherwise use origin channel
                const webAdapter = this.registry.getAdapter('web');
                if (webAdapter?.status === 'connected') {
                    await this.registry.sendToChannel('web', userId, message);
                    // Also send on origin if it's not web
                    if (originChannel !== 'web') {
                        await this.registry.sendToChannel(originChannel, userId, message);
                    }
                } else {
                    await this.registry.sendToChannel(originChannel, userId, message);
                }
                break;

            default:
                // Fallback: same channel
                await this.registry.sendToChannel(originChannel, userId, message);
        }
    }

    /**
     * Clear cached prefs (e.g., when user updates them).
     */
    invalidatePrefs(userId: string): void {
        this.prefs.delete(userId);
    }
}
