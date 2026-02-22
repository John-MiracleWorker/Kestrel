import { BaseChannelAdapter, ChannelType, IncomingMessage, OutgoingMessage } from './base';
import { BrainClient } from '../brain/client';
import { Deduplicator } from '../sync/deduplicator';
import { logger } from '../utils/logger';

/**
 * Channel Registry — manages adapter lifecycle and message routing.
 *
 * All channel adapters register through the registry. Incoming messages
 * from any channel are routed to Brain for processing, and responses
 * are sent back through the originating adapter.
 */
export class ChannelRegistry {
    private adapters = new Map<ChannelType, BaseChannelAdapter>();
    private userChannels = new Map<string, Set<ChannelType>>(); // userId → active channels
    private knownConversations = new Map<string, string>(); // channelKey → conversationId

    constructor(private brain: BrainClient, private deduplicator?: Deduplicator) { }

    /**
     * Register and connect an adapter.
     */
    async register(adapter: BaseChannelAdapter): Promise<void> {
        const type = adapter.channelType;

        if (this.adapters.has(type)) {
            logger.warn(`Channel adapter ${type} already registered — replacing`);
            await this.unregister(type);
        }

        // Wire up message routing
        adapter.on('message', (msg) => this.routeMessage(adapter, msg));

        adapter.on('error', (err) => {
            logger.error(`Channel ${type} error`, { error: err.message });
        });

        adapter.on('status', (status) => {
            logger.info(`Channel ${type} status: ${status}`);
        });

        // Connect
        try {
            await adapter.connect();
            this.adapters.set(type, adapter);
            logger.info(`Channel adapter registered: ${type}`);
        } catch (err) {
            logger.error(`Failed to connect channel ${type}`, { error: (err as Error).message });
            throw err;
        }
    }

    /**
     * Unregister and disconnect an adapter.
     */
    async unregister(type: ChannelType): Promise<void> {
        const adapter = this.adapters.get(type);
        if (!adapter) return;

        try {
            await adapter.disconnect();
        } catch (err) {
            logger.error(`Error disconnecting ${type}`, { error: (err as Error).message });
        }

        this.adapters.delete(type);
        logger.info(`Channel adapter unregistered: ${type}`);
    }

    /**
     * Get a registered adapter by type.
     */
    getAdapter(type: ChannelType): BaseChannelAdapter | undefined {
        return this.adapters.get(type);
    }

    /**
     * List all registered channel types.
     */
    getRegisteredChannels(): ChannelType[] {
        return Array.from(this.adapters.keys());
    }

    /**
     * Track which channels a user is active on.
     */
    trackUserChannel(userId: string, channel: ChannelType): void {
        if (!this.userChannels.has(userId)) {
            this.userChannels.set(userId, new Set());
        }
        this.userChannels.get(userId)!.add(channel);
    }

    /**
     * Remove a user from a channel.
     */
    untrackUserChannel(userId: string, channel: ChannelType): void {
        this.userChannels.get(userId)?.delete(channel);
    }

    /**
     * Route an incoming message to Brain for LLM processing,
     * then stream the response back through the originating adapter.
     */
    private async routeMessage(adapter: BaseChannelAdapter, msg: IncomingMessage): Promise<void> {
        const channel = adapter.channelType;
        logger.info('Routing message', {
            channel,
            userId: msg.userId,
            workspaceId: msg.workspaceId,
            hasAttachments: !!msg.attachments?.length,
        });

        // Track user on this channel
        this.trackUserChannel(msg.userId, channel);

        // Deduplicate: skip if this message was already seen within the dedup window
        if (this.deduplicator) {
            const isDup = await this.deduplicator.isDuplicate(msg.userId, msg.content, channel);
            if (isDup) {
                logger.info('Duplicate message suppressed', { channel, userId: msg.userId });
                return;
            }
        }

        // Process attachments through the adapter's handler
        if (msg.attachments?.length) {
            msg.attachments = await Promise.all(
                msg.attachments.map((a) => adapter.handleAttachment(a))
            );
        }

        // For external channels (Telegram, Discord, etc.), the conversation
        // may not exist in Brain's database yet. Pass empty conversationId
        // to let Brain auto-create, then track the returned ID for continuity.
        const conversationKey = `${channel}:${msg.userId}:${msg.conversationId}`;
        const knownConvId = this.knownConversations.get(conversationKey);
        const useConversationId = knownConvId || '';

        // Stream response from Brain
        try {
            const stream = this.brain.streamChat({
                userId: msg.userId,
                workspaceId: msg.workspaceId,
                conversationId: useConversationId,
                messages: [{ role: 0, content: msg.content }], // USER = 0
                provider: '',   // Use workspace default
                model: '',      // Use workspace default
                parameters: {},
            });

            let fullContent = '';

            for await (const chunk of stream) {
                switch (chunk.type) {
                    case 'CONTENT_DELTA':
                        fullContent += chunk.content_delta || '';
                        break;

                    case 'DONE': {
                        // Capture conversation ID from Brain for future messages
                        const returnedConvId = chunk.conversation_id;
                        if (returnedConvId && msg.conversationId) {
                            this.knownConversations.set(conversationKey, returnedConvId);
                        }

                        // Send complete response back through the channel
                        if (fullContent) {
                            const outgoing: OutgoingMessage = {
                                conversationId: returnedConvId || msg.conversationId || '',
                                content: fullContent,
                                options: { markdown: true },
                            };

                            // Let the adapter format the message for its platform
                            const formatted = adapter.formatOutgoing(outgoing);
                            await adapter.send(msg.userId, formatted);
                        }
                        break;
                    }

                    case 'ERROR':
                        logger.error('Brain error during routing', {
                            channel,
                            userId: msg.userId,
                            error: chunk.error_message,
                        });
                        await adapter.send(msg.userId, {
                            conversationId: msg.conversationId || '',
                            content: 'Sorry, something went wrong. Please try again.',
                        });
                        break;
                }
            }
        } catch (err) {
            logger.error('Failed to route message through Brain', {
                channel,
                userId: msg.userId,
                error: (err as Error).message,
            });

            await adapter.send(msg.userId, {
                conversationId: msg.conversationId || '',
                content: 'Sorry, I couldn\'t process your message. Please try again later.',
            });
        }
    }

    /**
     * Send a message to a user on a specific channel.
     */
    async sendToChannel(type: ChannelType, userId: string, message: OutgoingMessage): Promise<void> {
        const adapter = this.adapters.get(type);
        if (!adapter) {
            logger.warn(`Cannot send — channel ${type} not registered`);
            return;
        }

        const formatted = adapter.formatOutgoing(message);
        await adapter.send(userId, formatted);
    }

    /**
     * Broadcast a message to all channels where a user is active.
     */
    async broadcastToUser(userId: string, message: OutgoingMessage, excludeChannel?: ChannelType): Promise<void> {
        const channels = this.userChannels.get(userId);
        if (!channels) return;

        const promises: Promise<void>[] = [];
        for (const channel of channels) {
            if (channel === excludeChannel) continue;
            promises.push(this.sendToChannel(channel, userId, message));
        }

        await Promise.allSettled(promises);
    }

    /**
     * Disconnect all adapters.
     */
    async shutdown(): Promise<void> {
        const promises = Array.from(this.adapters.keys()).map((type) => this.unregister(type));
        await Promise.allSettled(promises);
        this.userChannels.clear();
        logger.info('Channel registry shut down');
    }
}
