import { WebSocketServer, WebSocket } from 'ws';
import { randomUUID } from 'crypto';
import jwt from 'jsonwebtoken';
import { BaseChannelAdapter, IncomingMessage, OutgoingMessage, ChannelType } from './base';
import { JWTPayload } from '../auth/middleware';
import { SessionManager } from '../session/manager';
import { BrainClient } from '../brain/client';
import { logger } from '../utils/logger';
import { wsConnectionsGauge } from '../utils/metrics';

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
                                socket.send(JSON.stringify({ type: 'error', error: 'Token required' }));
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
                                socket.send(JSON.stringify({ type: 'error', error: 'Invalid token' }));
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

                            logger.info('WebSocket authenticated', { userId: socket.userId, sessionId: socket.sessionId });
                            socket.send(JSON.stringify({ type: 'connected', sessionId: socket.sessionId }));
                        } else {
                            socket.send(JSON.stringify({ type: 'error', error: 'Authentication required' }));
                        }
                        return;
                    }

                    if (msg.type === 'auth') return; // Ignore subsequent auth
                    await this.handleMessage(socket, msg);
                } catch (err) {
                    logger.error('WebSocket message error', { error: (err as Error).message, userId: socket.userId || 'unauthenticated' });
                    socket.send(JSON.stringify({ type: 'error', error: 'Invalid message format' }));
                }
            });

            // Handle pong (heartbeat)
            socket.on('pong', () => { socket.isAlive = true; });

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
        this.wss.clients.forEach(ws => ws.close(1001, 'Server shutting down'));
        this.connections.clear();
    }

    async send(userId: string, message: OutgoingMessage): Promise<void> {
        const socket = this.connections.get(userId);
        if (!socket || socket.readyState !== WebSocket.OPEN) {
            logger.warn('Cannot send — user not connected', { userId });
            return;
        }

        socket.send(JSON.stringify({
            type: 'message',
            conversationId: message.conversationId,
            content: message.content,
            attachments: message.attachments,
            options: message.options,
        }));
    }

    /**
     * Route incoming WebSocket messages to appropriate handlers.
     */
    private async handleMessage(socket: AuthenticatedSocket, msg: any): Promise<void> {
        switch (msg.type) {
            case 'chat': {
                // Forward to Brain for LLM processing
                const incoming: IncomingMessage = {
                    id: randomUUID(),
                    channel: 'web',
                    userId: socket.userId,
                    workspaceId: msg.workspaceId || 'default',
                    conversationId: msg.conversationId,
                    content: msg.content,
                    metadata: {
                        channelUserId: socket.userId,
                        channelMessageId: randomUUID(),
                        timestamp: new Date(),
                    },
                };

                this.emit('message', incoming);

                // Stream response from Brain
                try {
                    const stream = this.brain.streamChat({
                        userId: socket.userId,
                        workspaceId: incoming.workspaceId,
                        conversationId: incoming.conversationId || '',
                        messages: [{ role: 0, content: msg.content }], // USER = 0
                        provider: msg.provider || '',
                        model: msg.model || '',
                        parameters: msg.parameters || {},
                    });

                    for await (const chunk of stream) {
                        if (socket.readyState !== WebSocket.OPEN) break;

                        // Map gRPC ChunkType to WebSocket event
                        switch (chunk.type) {
                            case 'CONTENT_DELTA':
                                socket.send(JSON.stringify({
                                    type: 'token',
                                    conversationId: incoming.conversationId,
                                    content: chunk.content_delta,
                                }));
                                break;

                            case 'TOOL_CALL':
                                socket.send(JSON.stringify({
                                    type: 'tool_call',
                                    conversationId: incoming.conversationId,
                                    tool: chunk.tool_call,
                                }));
                                break;

                            case 'DONE':
                                socket.send(JSON.stringify({
                                    type: 'done',
                                    conversationId: incoming.conversationId,
                                    metadata: chunk.metadata,
                                }));
                                break;

                            case 'ERROR':
                                socket.send(JSON.stringify({
                                    type: 'error',
                                    conversationId: incoming.conversationId,
                                    error: chunk.error_message,
                                }));
                                break;
                        }
                    }
                } catch (err) {
                    socket.send(JSON.stringify({
                        type: 'error',
                        error: (err as Error).message,
                    }));
                }
                break;
            }

            case 'set_workspace':
                socket.workspaceId = msg.workspaceId;
                socket.send(JSON.stringify({ type: 'workspace_set', workspaceId: msg.workspaceId }));
                break;

            case 'ping':
                socket.send(JSON.stringify({ type: 'pong' }));
                break;

            default:
                socket.send(JSON.stringify({ type: 'error', error: `Unknown message type: ${msg.type}` }));
        }
    }
}
