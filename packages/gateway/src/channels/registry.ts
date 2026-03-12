import {
    Attachment,
    BaseChannelAdapter,
    ChannelType,
    IncomingMessage,
    OutgoingMessage,
} from './base';
import { BrainClient } from '../brain/client';
import { Deduplicator } from '../sync/deduplicator';
import { logger } from '../utils/logger';
import {
    IngressEnvelope,
    SessionCommand,
    buildBrainStreamChatRequest,
    buildSessionCommand,
} from './ingress';
import { SessionManager } from '../session/manager';

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
    private activeStreams = new Map<string, AbortController>(); // userId → active stream controller

    constructor(
        private brain: BrainClient,
        private deduplicator?: Deduplicator,
        private sessions?: SessionManager,
    ) {}

    /**
     * Cancel the active streaming response for a user (e.g. from /stop).
     */
    cancelActiveStream(userId: string): void {
        const controller = this.activeStreams.get(userId);
        if (controller) {
            controller.abort();
            this.activeStreams.delete(userId);
            logger.info('Active stream cancelled by user', { userId });
        }
    }

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

        // Wire cancel-stream handler so adapters can abort active responses (e.g. /stop)
        if (typeof (adapter as any).setCancelStreamHandler === 'function') {
            (adapter as any).setCancelStreamHandler((userId: string) => {
                this.cancelActiveStream(userId);
            });
        }

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
        const event = msg as IngressEnvelope;
        const channel = adapter.channelType;
        logger.info('Routing ingress event', {
            channel,
            userId: event.userId,
            workspaceId: event.workspaceId,
            ingressKind: event.payload.kind,
            correlationId: event.correlationId,
            dedupeKey: event.dedupeKey,
            hasAttachments: !!event.attachments?.length,
        });

        // Track user on this channel
        this.trackUserChannel(event.userId, channel);

        // Deduplicate: skip if this message was already seen within the dedup window
        if (this.deduplicator) {
            const isDup = await this.deduplicator.isDuplicate(
                event.userId,
                event.payload.content,
                channel,
            );
            if (isDup) {
                logger.info('Duplicate ingress event suppressed', {
                    channel,
                    userId: event.userId,
                    dedupeKey: event.dedupeKey,
                });
                return;
            }
        }

        // Process attachments through the adapter's handler
        if (event.attachments?.length) {
            event.attachments = await Promise.all(
                event.attachments.map((a: Attachment) => adapter.handleAttachment(a)),
            );
            event.payload.attachments = event.attachments;
        }

        const conversationKey = `${channel}:${event.workspaceId}:${event.userId}:${event.externalConversationId || event.conversationId || ''}`;
        const knownRoute = this.sessions ? await this.sessions.getRoute(conversationKey) : null;
        const command = buildSessionCommand(event, {
            sessionId: knownRoute?.sessionId,
            conversationId: knownRoute?.conversationId || event.conversationId,
        });

        // Track active stream so it can be cancelled (e.g. /stop)
        const controller = new AbortController();
        this.activeStreams.set(event.userId, controller);

        // Stream response from Brain
        try {
            const stream = this.brain.streamChat(
                buildBrainStreamChatRequest(command, {
                    conversationId: command.conversationId || '',
                }),
            );

            // ── Streaming path (Telegram, Discord, etc.) ─────────────
            if (adapter.supportsStreaming) {
                await this.routeWithStreaming(
                    adapter,
                    event,
                    command,
                    stream,
                    conversationKey,
                    controller.signal,
                );
            } else {
                // ── Accumulate path (legacy adapters) ────────────────
                await this.routeWithAccumulate(
                    adapter,
                    event,
                    command,
                    stream,
                    conversationKey,
                    controller.signal,
                );
            }
        } catch (err) {
            logger.error('Failed to route message through Brain', {
                channel,
                userId: event.userId,
                correlationId: event.correlationId,
                error: (err as Error).message,
            });

            await adapter.send(event.userId, {
                conversationId: event.conversationId || '',
                content: "Sorry, I couldn't process your message. Please try again later.",
            });
        } finally {
            // Clean up the abort controller once the stream finishes or errors
            if (this.activeStreams.get(event.userId) === controller) {
                this.activeStreams.delete(event.userId);
            }
        }
    }

    /**
     * Stream response progressively via adapter's streaming interface.
     * Throttles updates to respect platform rate limits.
     */
    private async routeWithStreaming(
        adapter: BaseChannelAdapter,
        msg: IngressEnvelope,
        command: SessionCommand,
        stream: AsyncIterable<any>,
        conversationKey: string,
        signal?: AbortSignal,
    ): Promise<void> {
        const FLUSH_INTERVAL_MS = 1500; // Telegram: ~1 edit/sec max
        const HEARTBEAT_INTERVAL_MS = 25000; // Send "still working" after 25s silence

        // Start streaming — sends "Thinking..." placeholder
        logger.info('routeWithStreaming: starting stream', {
            userId: msg.userId,
            channel: adapter.channelType,
        });
        const handle = await adapter.sendStreamStart!(msg.userId, {
            conversationId: msg.conversationId || '',
        });
        logger.info('routeWithStreaming: stream started, handle obtained', {
            messageId: handle.messageId,
        });

        let fullContent = '';
        let lastFlushLen = 0;
        let flushTimer: NodeJS.Timeout | undefined;
        let flushPromise: Promise<void> = Promise.resolve();
        let doneReceived = false;
        let lastActivityAt = Date.now();

        // Heartbeat: send a silent "still working" status if no events arrive for a while.
        // This prevents the user thinking Kestrel died during long LLM/tool calls.
        const heartbeatInterval = setInterval(async () => {
            if (doneReceived || signal?.aborted) {
                clearInterval(heartbeatInterval);
                return;
            }
            const silenceMs = Date.now() - lastActivityAt;
            if (silenceMs >= HEARTBEAT_INTERVAL_MS && adapter.sendToolActivity) {
                try {
                    await adapter.sendToolActivity(msg.userId, handle, {
                        status: 'thinking',
                        toolName: '',
                        thinking: `Still working... (${Math.round(silenceMs / 1000)}s)`,
                    });
                } catch {
                    /* best effort */
                }
            }
        }, HEARTBEAT_INTERVAL_MS);

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
            CONTENT_DELTA: 0,
            TOOL_CALL: 1,
            DONE: 2,
            ERROR: 3,
        };

        let chunkCount = 0;
        for await (const chunk of stream) {
            chunkCount++;
            lastActivityAt = Date.now();
            if (chunkCount <= 3 || chunk.type === 2) {
                logger.info('routeWithStreaming: chunk received', {
                    chunkNum: chunkCount,
                    type: chunk.type,
                    hasContentDelta: !!chunk.content_delta,
                    contentDeltaLen: chunk.content_delta?.length || 0,
                    hasMetadata: !!chunk.metadata,
                    agentStatus: chunk.metadata?.agent_status,
                });
            }

            // Honour cancellation (e.g. user sent /stop)
            if (signal?.aborted) {
                if (flushTimer) {
                    clearTimeout(flushTimer);
                    flushTimer = undefined;
                }
                clearInterval(heartbeatInterval);
                await flushPromise;
                if (fullContent) {
                    try {
                        await adapter.sendStreamEnd!(handle, fullContent);
                    } catch {
                        /* ignore */
                    }
                }
                return;
            }

            const chunkType =
                typeof chunk.type === 'number' ? chunk.type : (enumMap[chunk.type as string] ?? -1);

            switch (chunkType) {
                case 0: // CONTENT_DELTA
                    if (chunk.metadata?.agent_status && !chunk.content_delta) {
                        // Forward tool activity as a separate notification
                        if (
                            adapter.sendToolActivity &&
                            chunk.metadata.agent_status !== 'routing_info'
                        ) {
                            try {
                                await adapter.sendToolActivity(msg.userId, handle, {
                                    status: chunk.metadata.agent_status,
                                    toolName: chunk.metadata.tool_name || '',
                                    toolArgs: chunk.metadata.tool_args || '',
                                    toolResult: chunk.metadata.tool_result || '',
                                    thinking: chunk.metadata.thinking || '',
                                    approvalId: chunk.metadata.approval_id || '',
                                    question: chunk.metadata.question || '',
                                });
                            } catch (err) {
                                logger.debug('Tool activity send failed', {
                                    error: (err as Error).message,
                                });
                            }
                        }

                        if (
                            chunk.metadata.agent_status === 'waiting_for_human' &&
                            chunk.metadata.approval_id
                        ) {
                            const approvalId = chunk.metadata.approval_id;
                            try {
                                logger.info('Dispatching approval request for user', {
                                    approval_id: approvalId,
                                    userId: msg.userId,
                                    channel: adapter.channelType,
                                });

                                const approvalSender = (adapter as any).sendApprovalRequestForUser;
                                if (typeof approvalSender === 'function') {
                                    await approvalSender.call(
                                        adapter,
                                        msg.userId,
                                        approvalId,
                                        chunk.metadata.question ||
                                            'The agent needs your approval to continue.',
                                        chunk.metadata.task_id || '',
                                    );
                                }
                            } catch (err) {
                                logger.warn('Approval request send failed', {
                                    approval_id: approvalId,
                                    userId: msg.userId,
                                    channel: adapter.channelType,
                                    error: (err as Error).message,
                                });
                            }
                        }
                    } else if (chunk.content_delta) {
                        fullContent += chunk.content_delta;
                        if (
                            fullContent.length <= 50 ||
                            fullContent.length % 100 < chunk.content_delta.length
                        ) {
                            logger.info('routeWithStreaming: content accumulated', {
                                totalLen: fullContent.length,
                            });
                        }
                        scheduleFlush();
                    }
                    break;

                case 2: {
                    // DONE
                    logger.info('routeWithStreaming: DONE received', {
                        fullContentLen: fullContent.length,
                        chunkCount,
                    });
                    doneReceived = true;
                    clearInterval(heartbeatInterval);

                    // Cancel pending timer
                    if (flushTimer) {
                        clearTimeout(flushTimer);
                        flushTimer = undefined;
                    }
                    // Wait for any in-flight update to finish
                    await flushPromise;

                    const returnedConvId = chunk.conversation_id || chunk.metadata?.conversation_id;
                    const finalConvId = returnedConvId || command.conversationId;
                    if (
                        this.sessions &&
                        finalConvId &&
                        (msg.externalConversationId || msg.conversationId)
                    ) {
                        await this.sessions.saveRoute(conversationKey, {
                            sessionId: command.sessionId,
                            conversationId: finalConvId,
                            externalConversationId: msg.externalConversationId,
                            externalThreadId: msg.externalThreadId,
                            channel: msg.channel,
                            userId: msg.userId,
                            workspaceId: msg.workspaceId,
                            returnRoute: command.returnRoute,
                            lastIngressId: msg.id,
                            updatedAt: new Date().toISOString(),
                        });
                    }

                    // Send final content
                    if (fullContent) {
                        try {
                            await adapter.sendStreamEnd!(handle, fullContent);
                        } catch (err) {
                            // Fallback: send as regular message if edit fails
                            logger.warn('Stream end failed, sending regular message', {
                                error: (err as Error).message,
                            });
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

                    // If we already streamed some content, finalize it
                    // with the error appended. Otherwise send the error
                    // message directly so the user sees context.
                    if (fullContent) {
                        fullContent += `\n\n⚠️ ${chunk.error_message || 'An error occurred.'}`;
                    } else {
                        fullContent = `⚠️ ${chunk.error_message || 'Sorry, something went wrong. Please try again.'}`;
                    }
                    break;
            }
        }

        // Safety: clean up timer and heartbeat if stream ended
        clearInterval(heartbeatInterval);
        if (flushTimer) {
            clearTimeout(flushTimer);
        }

        // If stream ended without a DONE chunk (e.g. gRPC timeout / network drop),
        // send whatever content was accumulated so the user is never left hanging.
        if (!doneReceived && fullContent) {
            try {
                await adapter.sendStreamEnd!(handle, fullContent);
            } catch (err) {
                logger.warn('Fallback stream-end flush failed', { error: (err as Error).message });
                try {
                    await adapter.send(msg.userId, {
                        conversationId: msg.conversationId || '',
                        content: fullContent,
                        options: { markdown: true },
                    });
                } catch {
                    /* best effort */
                }
            }
        } else if (!doneReceived && !fullContent) {
            // Nothing at all was produced — send a generic failure so the user
            // knows something went wrong rather than seeing a frozen "Thinking..." bubble.
            try {
                await adapter.sendStreamEnd!(
                    handle,
                    '⚠️ The request timed out or was interrupted. Please try again.',
                );
            } catch {
                /* best effort */
            }
        }
    }

    /**
     * Accumulate full response and send once (legacy behavior).
     */
    private async routeWithAccumulate(
        adapter: BaseChannelAdapter,
        msg: IngressEnvelope,
        command: SessionCommand,
        stream: AsyncIterable<any>,
        conversationKey: string,
        signal?: AbortSignal,
    ): Promise<void> {
        let fullContent = '';
        const enumMap: Record<string, number> = {
            CONTENT_DELTA: 0,
            TOOL_CALL: 1,
            DONE: 2,
            ERROR: 3,
        };

        for await (const chunk of stream) {
            if (signal?.aborted) {
                return;
            }
            const chunkType =
                typeof chunk.type === 'number' ? chunk.type : (enumMap[chunk.type as string] ?? -1);

            switch (chunkType) {
                case 0:
                    fullContent += chunk.content_delta || '';
                    break;

                case 2: {
                    const returnedConvId = chunk.conversation_id || chunk.metadata?.conversation_id;
                    const finalConvId = returnedConvId || command.conversationId;
                    if (
                        this.sessions &&
                        finalConvId &&
                        (msg.externalConversationId || msg.conversationId)
                    ) {
                        await this.sessions.saveRoute(conversationKey, {
                            sessionId: command.sessionId,
                            conversationId: finalConvId,
                            externalConversationId: msg.externalConversationId,
                            externalThreadId: msg.externalThreadId,
                            channel: msg.channel,
                            userId: msg.userId,
                            workspaceId: msg.workspaceId,
                            returnRoute: command.returnRoute,
                            lastIngressId: msg.id,
                            updatedAt: new Date().toISOString(),
                        });
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

                case 3:
                    logger.error('Brain error during routing', {
                        channel: adapter.channelType,
                        userId: msg.userId,
                        error: chunk.error_message,
                    });
                    // Append or set error content — will be sent on DONE
                    if (fullContent) {
                        fullContent += `\n\n⚠️ ${chunk.error_message || 'An error occurred.'}`;
                    } else {
                        fullContent = `⚠️ ${chunk.error_message || 'Sorry, something went wrong. Please try again.'}`;
                    }
                    break;
            }
        }
    }

    /**
     * Send a message to a user on a specific channel.
     */
    async sendToChannel(
        type: ChannelType,
        userId: string,
        message: OutgoingMessage,
    ): Promise<void> {
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
    async broadcastToUser(
        userId: string,
        message: OutgoingMessage,
        excludeChannel?: ChannelType,
    ): Promise<void> {
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
