import * as grpc from '@grpc/grpc-js';
import * as protoLoader from '@grpc/proto-loader';
import path from 'path';
import { logger } from '../utils/logger';

const PROTO_PATH = path.resolve(__dirname, '../../../shared/proto/brain.proto');

/**
 * gRPC client for the Brain service.
 * Wraps brain.proto BrainService with typed methods.
 */
export class BrainClient {
    private client: any;
    private connected = false;

    constructor(private address: string) { }

    async connect(): Promise<void> {
        const packageDef = protoLoader.loadSync(PROTO_PATH, {
            keepCase: true,
            longs: String,
            enums: String,
            defaults: true,
            oneofs: true,
        });
        const proto = grpc.loadPackageDefinition(packageDef) as any;
        const BrainService = proto.kestrel.brain.BrainService;

        this.client = new BrainService(
            this.address,
            grpc.credentials.createInsecure()
        );

        // Wait for connection (with 5s deadline)
        return new Promise((resolve, _reject) => {
            const deadline = new Date(Date.now() + 5000);
            this.client.waitForReady(deadline, (err: Error | null) => {
                if (err) {
                    logger.error('Brain gRPC connection failed', { error: err.message, address: this.address });
                    // Don't reject — allow Gateway to start without Brain
                    this.connected = false;
                    resolve();
                } else {
                    this.connected = true;
                    resolve();
                }
            });
        });
    }

    isConnected(): boolean {
        return this.connected;
    }

    close(): void {
        if (this.client) {
            grpc.closeClient(this.client);
            this.connected = false;
        }
    }

    /**
     * Stream chat — returns an async iterable of ChatResponse chunks.
     */
    async *streamChat(request: {
        userId: string;
        workspaceId: string;
        conversationId: string;
        messages: Array<{ role: number; content: string }>;
        provider: string;
        model: string;
        parameters?: Record<string, string>;
    }): AsyncIterable<any> {
        if (!this.connected) throw new Error('Brain service not connected');

        const grpcRequest = {
            user_id: request.userId,
            workspace_id: request.workspaceId,
            conversation_id: request.conversationId,
            messages: request.messages,
            provider: request.provider,
            model: request.model,
            parameters: request.parameters || {},
        };

        const stream = this.client.StreamChat(grpcRequest);

        for await (const chunk of stream) {
            yield chunk;
        }
    }

    /**
     * Create a new user.
     */
    async createUser(email: string, password: string, displayName?: string): Promise<any> {
        // For Phase 1, we use a simple unary RPC or REST fallback
        return new Promise((resolve, reject) => {
            this.client.CreateUser(
                { email, password, display_name: displayName || '' },
                (err: any, response: any) => {
                    if (err) reject(new Error(err.details || err.message));
                    else resolve(response);
                }
            );
        });
    }

    async authenticateUser(email: string, password: string): Promise<any> {
        return new Promise((resolve, reject) => {
            this.client.AuthenticateUser(
                { email, password },
                (err: any, response: any) => {
                    if (err) reject(new Error(err.details || err.message));
                    else resolve(response);
                }
            );
        });
    }

    async listWorkspaces(userId: string): Promise<any> {
        return new Promise((resolve, reject) => {
            this.client.ListWorkspaces(
                { user_id: userId },
                (err: any, response: any) => {
                    if (err) reject(new Error(err.details || err.message));
                    else resolve(response?.workspaces || []);
                }
            );
        });
    }

    async createWorkspace(userId: string, name: string): Promise<any> {
        return new Promise((resolve, reject) => {
            this.client.CreateWorkspace(
                { user_id: userId, name },
                (err: any, response: any) => {
                    if (err) reject(new Error(err.details || err.message));
                    else resolve(response);
                }
            );
        });
    }

    async listConversations(userId: string, workspaceId: string): Promise<any> {
        return new Promise((resolve, reject) => {
            this.client.ListConversations(
                { user_id: userId, workspace_id: workspaceId },
                (err: any, response: any) => {
                    if (err) reject(new Error(err.details || err.message));
                    else resolve(response?.conversations || []);
                }
            );
        });
    }

    async createConversation(userId: string, workspaceId: string): Promise<any> {
        return new Promise((resolve, reject) => {
            this.client.CreateConversation(
                { user_id: userId, workspace_id: workspaceId },
                (err: any, response: any) => {
                    if (err) reject(new Error(err.details || err.message));
                    else resolve(response);
                }
            );
        });
    }

    async getMessages(userId: string, workspaceId: string, conversationId: string): Promise<any> {
        return new Promise((resolve, reject) => {
            this.client.GetMessages(
                { user_id: userId, workspace_id: workspaceId, conversation_id: conversationId },
                (err: any, response: any) => {
                    if (err) reject(new Error(err.details || err.message));
                    else resolve(response?.messages || []);
                }
            );
        });
    }

    async registerPushToken(userId: string, deviceToken: string, platform: string): Promise<any> {
        return new Promise((resolve, reject) => {
            this.client.RegisterPushToken(
                { user_id: userId, device_token: deviceToken, platform },
                (err: any, response: any) => {
                    if (err) reject(new Error(err.details || err.message));
                    else resolve(response || { success: true });
                }
            );
        });
    }

    async getUpdates(userId: string, since?: string): Promise<any> {
        return new Promise((resolve, reject) => {
            this.client.GetUpdates(
                { user_id: userId, since: since || '' },
                (err: any, response: any) => {
                    if (err) reject(new Error(err.details || err.message));
                    else resolve(response || { messages: [], conversations: [] });
                }
            );
        });
    }
}
