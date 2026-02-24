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
     *
     * If the adapter supports streaming (sendStreamStart/Update/End),
     * tokens are forwarded progressively. Otherwise, the response
     * is accumulated and sent as a single final message.
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

        // Use the channel-provided conversation ID (e.g. deterministic UUID from
        // Telegram chat ID) so Brain can load and save history across messages.
        // Fall back to a cached ID from a previous Brain response if available.
        const conversationKey = `${channel}:${msg.userId}:${msg.conversationId}`;
        const knownConvId = this.knownConversations.get(conversationKey);
        const useConversationId = knownConvId || msg.conversationId || '';

        // Build parameters — forward attachments so Brain can process images/files
        const parameters: Record<string, string> = {};
        if (msg.attachments?.length) {
            parameters.attachments = JSON.stringify(msg.attachments);
        }
        // Pass channel type so Brain can tag conversations correctly
        parameters.channel = channel;

        // Stream response from Brain
        try {
            const stream = this.brain.streamChat({
                userId: msg.userId,
                workspaceId: msg.workspaceId,
                conversationId: useConversationId,
                messages: [{ role: 0, content: msg.content }], // USER = 0
                provider: '',   // Use workspace default
                model: '',      // Use workspace default
                parameters,
            });

            // ── Streaming path (Telegram, Discord, etc.) ─────────────
            if (adapter.supportsStreaming) {
                await this.routeWithStreaming(adapter, msg, stream, conversationKey, useConversationId);
            } else {
                // ── Accumulate path (legacy adapters) ────────────────
                await this.routeWithAccumulate(adapter, msg, stream, conversationKey, useConversationId);
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
     * Stream response progressively via adapter's streaming interface.
     * Throttles updates to respect platform rate limits.
     */
    private async routeWithStreaming(
        adapter: BaseChannelAdapter,
        msg: IncomingMessage,
        stream: AsyncIterable<any>,
        conversationKey: string,
        useConversationId: string,
    ): Promise<void> {
        const FLUSH_INTERVAL_MS = 1500; // Telegram: ~1 edit/sec max

        // Start streaming — sends "Thinking..." placeholder
        const handle = await adapter.sendStreamStart!(msg.userId, {
            conversationId: msg.conversationId || '',
        });

        let fullContent = '';
        let lastFlushLen = 0;
        let flushTimer: NodeJS.Timeout | undefined;
        let flushPromise: Promise<void> = Promise.resolve();

        const scheduleFlush = () => {
            if (flushTimer) return;
            flushTimer = setTimeout(async () => {
                flushTimer = undefined;
                if (fullContent.length > lastFlushLen) {
                    lastFlushLen = fullContent.length;
                    try {
                        flushPromise = adapter.sendStreamUpdate!(handle, fullContent);
                        await flushPromise;
                    } catch (err) {
                        logger.warn('Stream update failed', { error: (err as Error).message });
                    }
                }
            }, FLUSH_INTERVAL_MS);
        };

        // gRPC protobuf enum map
        const enumMap: Record<string, number> = {
            'CONTENT_DELTA': 0, 'TOOL_CALL': 1, 'DONE': 2, 'ERROR': 3,
        };

        for await (const chunk of stream) {
            const chunkType = typeof chunk.type === 'number' ? chunk.type :
                (enumMap[chunk.type as string] ?? -1);

            switch (chunkType) {
                case 0: // CONTENT_DELTA
                    if (chunk.metadata?.agent_status && !chunk.content_delta) {
                        // Forward tool activity as a separate notification
                        if (adapter.sendToolActivity && chunk.metadata.agent_status !== 'routing_info') {
                            try {
                                await adapter.sendToolActivity(msg.userId, handle, {
                                    status: chunk.metadata.agent_status,
                                    toolName: chunk.metadata.tool_name || '',
                                    toolArgs: chunk.metadata.tool_args || '',
                                    toolResult: chunk.metadata.tool_result || '',
                                    thinking: chunk.metadata.thinking || '',
                                });
                            } catch (err) {
                                logger.debug('Tool activity send failed', { error: (err as Error).message });
                            }
                        }
                    } else if (chunk.content_delta) {
                        fullContent += chunk.content_delta;
                        scheduleFlush();
                    }
                    break;

                case 2: { // DONE
                    // Cancel pending timer
                    if (flushTimer) {
                        clearTimeout(flushTimer);
                        flushTimer = undefined;
                    }
                    // Wait for any in-flight update to finish
                    await flushPromise;

                    const returnedConvId = chunk.conversation_id || chunk.metadata?.conversation_id;
                    const finalConvId = returnedConvId || useConversationId;
                    if (finalConvId && msg.conversationId) {
                        this.knownConversations.set(conversationKey, finalConvId);
                    }

                    // Send final content
                    if (fullContent) {
                        try {
                            await adapter.sendStreamEnd!(handle, fullContent);
                        } catch (err) {
                            // Fallback: send as regular message if edit fails
                            logger.warn('Stream end failed, sending regular message', { error: (err as Error).message });
                            await adapter.send(msg.userId, {
                                conversationId: msg.conversationId || '',
                                content: fullContent,
                                options: { markdown: true },
                            });
                        }
                    }
                    break;
                }

                case 3: // ERROR
                    if (flushTimer) {
                        clearTimeout(flushTimer);
                        flushTimer = undefined;
                    }
                    logger.error('Brain error during streaming route', {
                        channel: adapter.channelType,
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

        // Safety: clean up timer if stream ended without DONE
        if (flushTimer) {
            clearTimeout(flushTimer);
        }
    }

    /**
     * Accumulate full response and send once (legacy behavior).
     */
    private async routeWithAccumulate(
        adapter: BaseChannelAdapter,
        msg: IncomingMessage,
        stream: AsyncIterable<any>,
        conversationKey: string,
        useConversationId: string,
    ): Promise<void> {
        let fullContent = '';

        for await (const chunk of stream) {
            switch (chunk.type) {
                case 'CONTENT_DELTA':
                    fullContent += chunk.content_delta || '';
                    break;

                case 'DONE': {
                    const returnedConvId = chunk.conversation_id || chunk.metadata?.conversation_id;
                    const finalConvId = returnedConvId || useConversationId;
                    if (finalConvId && msg.conversationId) {
                        this.knownConversations.set(conversationKey, finalConvId);
                    }

                    if (fullContent) {
                        const outgoing: OutgoingMessage = {
                            conversationId: returnedConvId || msg.conversationId || '',
                            content: fullContent,
                            options: { markdown: true },
                        };
                        const formatted = adapter.formatOutgoing(outgoing);
                        await adapter.send(msg.userId, formatted);
                    }
                    break;
                }

                case 'ERROR':
                    logger.error('Brain error during routing', {
                        channel: adapter.channelType,
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
