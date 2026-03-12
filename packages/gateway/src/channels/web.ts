import { WebSocketServer, WebSocket } from 'ws';
import { randomUUID } from 'crypto';
import jwt from 'jsonwebtoken';
import { BaseChannelAdapter, OutgoingMessage, ChannelType } from './base';
import { JWTPayload } from '../auth/middleware';
import { SessionManager } from '../session/manager';
import { BrainClient } from '../brain/client';
import { logger } from '../utils/logger';
import { wsConnectionsGauge } from '../utils/metrics';
import { buildBrainStreamChatRequest, buildSessionCommand, createIngressEnvelope } from './ingress';

interface AuthenticatedSocket extends WebSocket {
    userId: string;
    email: string;
    workspaceId?: string;
    sessionId: string;
    isAlive: boolean;
}

/**
 * WebSocket channel adapter — handles real-time bidirectional
 * communication between web/mobile clients and the Gateway.
 */
export class WebChannelAdapter extends BaseChannelAdapter {
    readonly channelType: ChannelType = 'web';

    private connections = new Map<string, AuthenticatedSocket>(); // userId → socket
    private heartbeatInterval?: NodeJS.Timeout;

    constructor(
        private wss: WebSocketServer,
        private sessions: SessionManager,
        private brain: BrainClient,
        private jwtSecret: string,
    ) {
        super();
    }

    async connect(): Promise<void> {
        this.wss.on('connection', async (ws: WebSocket, req) => {
            const socket = ws as AuthenticatedSocket;

            let authenticated = false;

            const authTimeout = setTimeout(() => {
                if (!authenticated) {
                    socket.send(JSON.stringify({ type: 'error', error: 'Authentication timeout' }));
                    socket.close(4008, 'Authentication timeout');
                }
            }, 5000);

            // Handle messages
            socket.on('message', async (data) => {
                try {
                    const msg = JSON.parse(data.toString());

                    if (!authenticated) {
                        if (msg.type === 'auth') {
                            const token = msg.token;
                            if (!token) {
                                socket.send(
                                    JSON.stringify({ type: 'error', error: 'Token required' }),
                                );
                                socket.close(4001, 'Authentication required');
                                return;
                            }

                            let payload: JWTPayload | null = null;
                            try {
                                payload = jwt.verify(token, this.jwtSecret) as JWTPayload;
                            } catch (err: any) {
                                logger.warn('WS Token verification failed', {
                                    error: err.message,
                                    tokenPreview: token.substring(0, 10) + '...',
                                });
                            }

                            if (!payload) {
                                socket.send(
                                    JSON.stringify({ type: 'error', error: 'Invalid token' }),
                                );
                                socket.close(4001, 'Invalid token');
                                return;
                            }

                            clearTimeout(authTimeout);
                            authenticated = true;

                            // Set up authenticated socket
                            socket.userId = payload.sub;
                            socket.email = payload.email;
                            socket.sessionId = randomUUID();
                            socket.isAlive = true;

                            // Register connection
                            this.connections.set(socket.userId, socket);
                            wsConnectionsGauge.inc();

                            await this.sessions.create(socket.sessionId, {
                                userId: socket.userId,
                                email: socket.email,
                                channel: 'web',
                                connectedAt: new Date().toISOString(),
                            });

                            logger.info('WebSocket authenticated', {
                                userId: socket.userId,
                                sessionId: socket.sessionId,
                            });
                            socket.send(
                                JSON.stringify({ type: 'connected', sessionId: socket.sessionId }),
                            );
                        } else {
                            socket.send(
                                JSON.stringify({ type: 'error', error: 'Authentication required' }),
                            );
                        }
                        return;
                    }

                    if (msg.type === 'auth') return; // Ignore subsequent auth
                    await this.handleMessage(socket, msg);
                } catch (err) {
                    logger.error('WebSocket message error', {
                        error: (err as Error).message,
                        userId: socket.userId || 'unauthenticated',
                    });
                    socket.send(JSON.stringify({ type: 'error', error: 'Invalid message format' }));
                }
            });

            // Handle pong (heartbeat)
            socket.on('pong', () => {
                socket.isAlive = true;
            });

            // Cleanup on close
            socket.on('close', () => {
                this.connections.delete(socket.userId);
                wsConnectionsGauge.dec();
                this.sessions.destroy(socket.sessionId);
                logger.info('WebSocket disconnected', { userId: socket.userId });
            });

            socket.on('error', (err) => {
                logger.error('WebSocket error', { error: err.message, userId: socket.userId });
            });
        });

        // Heartbeat every 30s to detect dead connections
        this.heartbeatInterval = setInterval(() => {
            this.wss.clients.forEach((ws) => {
                const socket = ws as AuthenticatedSocket;
                if (!socket.isAlive) {
                    logger.warn('Terminating dead WebSocket', { userId: socket.userId });
                    return socket.terminate();
                }
                socket.isAlive = false;
                socket.ping();
            });
        }, 30000);
    }

    async disconnect(): Promise<void> {
        if (this.heartbeatInterval) clearInterval(this.heartbeatInterval);
        this.wss.clients.forEach((ws) => ws.close(1001, 'Server shutting down'));
        this.connections.clear();
    }

    async send(userId: string, message: OutgoingMessage): Promise<void> {
        const socket = this.connections.get(userId);
        if (!socket || socket.readyState !== WebSocket.OPEN) {
            logger.warn('Cannot send — user not connected', { userId });
            return;
        }

        socket.send(
            JSON.stringify({
                type: 'message',
                conversationId: message.conversationId,
                content: message.content,
                attachments: message.attachments,
                options: message.options,
            }),
        );
    }

    async sendNotification(userId: string, notification: any): Promise<void> {
        const socket = this.connections.get(userId);
        if (!socket || socket.readyState !== WebSocket.OPEN) {
            logger.debug('Cannot send notification — user not connected', { userId });
            return;
        }

        socket.send(
            JSON.stringify({
                type: 'notification',
                notification,
            }),
        );
    }

    /**
     * Route incoming WebSocket messages to appropriate handlers.
     */
    private async handleMessage(socket: AuthenticatedSocket, msg: any): Promise<void> {
        switch (msg.type) {
            case 'chat': {
                // Stream directly from Brain → WebSocket client.
                // We bypass the generic registry routeMessage because it
                // accumulates the full response and sends {type:'message'},
                // but the frontend expects streaming {type:'token'|'done'|'error'}.
                const clientMessageId =
                    typeof msg.clientMessageId === 'string' && msg.clientMessageId.trim()
                        ? msg.clientMessageId
                        : randomUUID();
                const ingressEvent = createIngressEnvelope({
                    id: clientMessageId,
                    channel: 'web',
                    userId: socket.userId,
                    workspaceId: msg.workspaceId || socket.workspaceId || 'default',
                    conversationId: msg.conversationId || '',
                    content: typeof msg.content === 'string' ? msg.content : '',
                    attachments: Array.isArray(msg.attachments) ? msg.attachments : undefined,
                    metadata: {
                        channelUserId: socket.userId,
                        channelMessageId: clientMessageId,
                        timestamp: new Date(),
                        sessionId: socket.sessionId,
                        source: 'websocket',
                    },
                    externalUserId: socket.userId,
                    externalConversationId: msg.conversationId || '',
                    correlationId:
                        typeof msg.correlationId === 'string' && msg.correlationId.trim()
                            ? msg.correlationId
                            : undefined,
                    authContext: {
                        transport: 'websocket_jwt',
                        authenticatedUserId: socket.userId,
                        sessionId: socket.sessionId,
                        isProvisionalUser: false,
                    },
                    payloadKind: 'message',
                    rawMetadata: {
                        socketWorkspaceId: socket.workspaceId || null,
                        requestedWorkspaceId: msg.workspaceId || null,
                        requestedConversationId: msg.conversationId || null,
                        provider: msg.provider || '',
                        model: msg.model || '',
                        attachments: Array.isArray(msg.attachments) ? msg.attachments : [],
                    },
                });
                const command = buildSessionCommand(ingressEvent, {
                    sessionId: socket.sessionId,
                    conversationId: msg.conversationId || '',
                });
                const messageId = ingressEvent.id;

                logger.info('Web ingress event normalized', {
                    userId: ingressEvent.userId,
                    workspaceId: ingressEvent.workspaceId,
                    correlationId: ingressEvent.correlationId,
                    dedupeKey: ingressEvent.dedupeKey,
                    hasAttachments: !!ingressEvent.attachments?.length,
                });

                await this.sessions.update(socket.sessionId, {
                    workspaceId: ingressEvent.workspaceId,
                    conversationId: command.conversationId,
                    externalConversationId: ingressEvent.externalConversationId,
                    externalThreadId: ingressEvent.externalThreadId,
                    correlationId: ingressEvent.correlationId,
                    returnRoute: command.returnRoute,
                });

                // Send a "thinking" indicator immediately so the user knows we're working
                if (socket.readyState === WebSocket.OPEN) {
                    socket.send(
                        JSON.stringify({
                            type: 'thinking',
                            messageId,
                        }),
                    );
                }

                try {
                    const stream = this.brain.streamChat(
                        buildBrainStreamChatRequest(command, {
                            conversationId: command.conversationId || '',
                            provider: msg.provider || '',
                            model: msg.model || '',
                        }),
                    );

                    let doneSent = false;

                    for await (const chunk of stream) {
                        if (socket.readyState !== WebSocket.OPEN) break;

                        // Debug: log raw chunk type to diagnose streaming
                        logger.info('Stream chunk received', {
                            rawType: chunk.type,
                            typeOf: typeof chunk.type,
                            hasContentDelta: !!chunk.content_delta,
                            contentLen: chunk.content_delta?.length || 0,
                        });

                        // gRPC protobuf enums are numbers:
                        // 0 = CONTENT_DELTA, 1 = TOOL_CALL, 2 = DONE, 3 = ERROR
                        const enumMap: Record<string, number> = {
                            CONTENT_DELTA: 0,
                            TOOL_CALL: 1,
                            DONE: 2,
                            ERROR: 3,
                        };
                        const chunkType =
                            typeof chunk.type === 'number'
                                ? chunk.type
                                : (enumMap[chunk.type as string] ?? -1);

                        switch (chunkType) {
                            case 0: // CONTENT_DELTA
                                // Check for agent metadata (tool activity events)
                                if (chunk.metadata?.agent_status && !chunk.content_delta) {
                                    // Forward routing info as a separate event type
                                    if (chunk.metadata.agent_status === 'routing_info') {
                                        socket.send(
                                            JSON.stringify({
                                                type: 'routing_info',
                                                provider: chunk.metadata.provider || '',
                                                model: chunk.metadata.model || '',
                                                wasEscalated:
                                                    chunk.metadata.was_escalated === 'true',
                                                complexity: parseFloat(
                                                    chunk.metadata.complexity || '0',
                                                ),
                                                messageId,
                                            }),
                                        );
                                    } else {
                                        socket.send(
                                            JSON.stringify({
                                                type: 'tool_activity',
                                                status: chunk.metadata.agent_status,
                                                toolName: chunk.metadata.tool_name || '',
                                                toolArgs: chunk.metadata.tool_args || '',
                                                toolResult: chunk.metadata.tool_result || '',
                                                thinking: chunk.metadata.thinking || '',
                                                activity: chunk.metadata.activity || '',
                                                // Delegation panel fields
                                                delegationType:
                                                    chunk.metadata.delegation_type || '',
                                                delegation: chunk.metadata.delegation || '',
                                                // Approval fields (for inline approve/deny buttons)
                                                approvalId: chunk.metadata.approval_id || '',
                                                taskId: chunk.metadata.task_id || '',
                                                question: chunk.metadata.question || '',
                                                messageId,
                                            }),
                                        );
                                    }
                                } else if (chunk.content_delta) {
                                    socket.send(
                                        JSON.stringify({
                                            type: 'token',
                                            content: chunk.content_delta,
                                            messageId,
                                        }),
                                    );
                                }
                                break;

                            case 2: // DONE
                                doneSent = true;
                                socket.send(
                                    JSON.stringify({
                                        type: 'done',
                                        messageId,
                                    }),
                                );
                                break;

                            case 3: // ERROR
                                doneSent = true;
                                socket.send(
                                    JSON.stringify({
                                        type: 'error',
                                        error: chunk.error_message || 'Unknown error from Brain',
                                        messageId,
                                    }),
                                );
                                break;
                        }
                    }

                    // Safety: if the stream ended without an explicit DONE, send one
                    if (!doneSent && socket.readyState === WebSocket.OPEN) {
                        socket.send(JSON.stringify({ type: 'done', messageId }));
                    }
                } catch (err) {
                    logger.error('Brain stream failed', {
                        userId: socket.userId,
                        correlationId: ingressEvent.correlationId,
                        error: (err as Error).message,
                    });
                    if (socket.readyState === WebSocket.OPEN) {
                        socket.send(
                            JSON.stringify({
                                type: 'error',
                                error: (err as Error).message || 'Failed to process message',
                                messageId,
                            }),
                        );
                    }
                }
                break;
            }

            case 'set_workspace':
                socket.workspaceId = msg.workspaceId;
                socket.send(
                    JSON.stringify({ type: 'workspace_set', workspaceId: msg.workspaceId }),
                );
                break;

            case 'ping':
                socket.send(JSON.stringify({ type: 'pong' }));
                break;

            default:
                socket.send(
                    JSON.stringify({ type: 'error', error: `Unknown message type: ${msg.type}` }),
                );
        }
    }
}
