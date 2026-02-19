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
     * Generic unary RPC call — used for forward-compatible Brain methods.
     * Falls back to a no-op if the method doesn't exist yet.
     */
    async call(method: string, request: any): Promise<any> {
        if (!this.connected) throw new Error('Brain service not connected');

        return new Promise((resolve, reject) => {
            if (typeof this.client[method] !== 'function') {
                logger.warn(`Brain RPC method ${method} not available — returning empty`);
                resolve({});
                return;
            }
            this.client[method](request, (err: Error | null, response: any) => {
                if (err) reject(err);
                else resolve(response);
            });
        });
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

    async deleteConversation(userId: string, workspaceId: string, conversationId: string): Promise<boolean> {
        return new Promise((resolve, reject) => {
            this.client.DeleteConversation(
                { user_id: userId, workspace_id: workspaceId, conversation_id: conversationId },
                (err: any, response: any) => {
                    if (err) reject(new Error(err.details || err.message));
                    else resolve(response.success);
                }
            );
        });
    }

    async updateConversation(userId: string, workspaceId: string, conversationId: string, title: string): Promise<any> {
        return new Promise((resolve, reject) => {
            this.client.UpdateConversation(
                { user_id: userId, workspace_id: workspaceId, conversation_id: conversationId, title },
                (err: any, response: any) => {
                    if (err) reject(new Error(err.details || err.message));
                    else resolve(response);
                }
            );
        });
    }

    async generateTitle(userId: string, workspaceId: string, conversationId: string): Promise<string> {
        return new Promise((resolve, reject) => {
            this.client.GenerateTitle(
                { user_id: userId, workspace_id: workspaceId, conversation_id: conversationId },
                (err: any, response: any) => {
                    if (err) reject(new Error(err.details || err.message));
                    else resolve(response.title);
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

    async updateWorkspace(workspaceId: string, data: { name?: string; description?: string; settings?: any }): Promise<any> {
        return new Promise((resolve, reject) => {
            this.client.UpdateWorkspace(
                { workspace_id: workspaceId, ...data },
                (err: any, response: any) => {
                    if (err) reject(new Error(err.details || err.message));
                    else resolve(response);
                }
            );
        });
    }

    async deleteWorkspace(workspaceId: string): Promise<void> {
        return new Promise((resolve, reject) => {
            this.client.DeleteWorkspace(
                { workspace_id: workspaceId },
                (err: any) => {
                    if (err) reject(new Error(err.details || err.message));
                    else resolve();
                }
            );
        });
    }

    async addWorkspaceMember(workspaceId: string, userId: string, role: string): Promise<any> {
        return new Promise((resolve, reject) => {
            this.client.AddWorkspaceMember(
                { workspace_id: workspaceId, user_id: userId, role },
                (err: any, response: any) => {
                    if (err) reject(new Error(err.details || err.message));
                    else resolve(response);
                }
            );
        });
    }

    // ── Autonomous Agent ────────────────────────────────────────

    /**
     * Start an agent task — returns an async iterable of TaskEvent objects.
     */
    startTask(request: {
        userId: string;
        workspaceId: string;
        goal: string;
        conversationId?: string;
        guardrails?: {
            maxIterations?: number;
            maxToolCalls?: number;
            maxTokens?: number;
            maxWallTimeSeconds?: number;
            autoApproveRisk?: string;
            blockedPatterns?: string[];
            requireApprovalTools?: string[];
        };
    }): AsyncIterable<any> {
        const stream = this.client.StartTask({
            user_id: request.userId,
            workspace_id: request.workspaceId,
            goal: request.goal,
            conversation_id: request.conversationId || '',
            guardrails: request.guardrails
                ? {
                    max_iterations: request.guardrails.maxIterations || 0,
                    max_tool_calls: request.guardrails.maxToolCalls || 0,
                    max_tokens: request.guardrails.maxTokens || 0,
                    max_wall_time_seconds: request.guardrails.maxWallTimeSeconds || 0,
                    auto_approve_risk: request.guardrails.autoApproveRisk || '',
                    blocked_patterns: request.guardrails.blockedPatterns || [],
                    require_approval_tools: request.guardrails.requireApprovalTools || [],
                }
                : undefined,
        });

        return {
            [Symbol.asyncIterator]() {
                return {
                    next(): Promise<IteratorResult<any>> {
                        return new Promise((resolve) => {
                            stream.once('data', (data: any) => {
                                resolve({ value: data, done: false });
                            });
                            stream.once('end', () => {
                                resolve({ value: undefined, done: true });
                            });
                            stream.once('error', (err: any) => {
                                resolve({ value: undefined, done: true });
                            });
                        });
                    },
                };
            },
        };
    }

    /**
     * Approve or deny a pending agent action.
     */
    async approveAction(approvalId: string, userId: string, approved: boolean): Promise<any> {
        return new Promise((resolve, reject) => {
            this.client.ApproveAction(
                { approval_id: approvalId, user_id: userId, approved },
                (err: any, response: any) => {
                    if (err) reject(new Error(err.details || err.message));
                    else resolve(response);
                }
            );
        });
    }

    /**
     * Cancel a running agent task.
     */
    async cancelTask(taskId: string, userId: string): Promise<any> {
        return new Promise((resolve, reject) => {
            this.client.CancelTask(
                { task_id: taskId, user_id: userId },
                (err: any, response: any) => {
                    if (err) reject(new Error(err.details || err.message));
                    else resolve(response);
                }
            );
        });
    }

    /**
     * List agent tasks for a user.
     */
    async listTasks(
        userId: string,
        workspaceId?: string,
        status?: string,
    ): Promise<any> {
        return new Promise((resolve, reject) => {
            this.client.ListTasks(
                { user_id: userId, workspace_id: workspaceId || '', status: status || '' },
                (err: any, response: any) => {
                    if (err) reject(new Error(err.details || err.message));
                    else resolve(response);
                }
            );
        });
    }

    // ── Automation (Stubbed for Build) ──────────────────────────

    async createCronJob(data: any): Promise<any> {
        return this.call('CreateCronJob', data);
    }

    async listCronJobs(workspaceId: string): Promise<any> {
        return this.call('ListCronJobs', { workspace_id: workspaceId });
    }

    async deleteCronJob(jobId: string): Promise<any> {
        return this.call('DeleteCronJob', { job_id: jobId });
    }

    async createWebhook(data: any): Promise<any> {
        return this.call('CreateWebhook', data);
    }

    async listWebhooks(workspaceId: string): Promise<any> {
        return this.call('ListWebhooks', { workspace_id: workspaceId });
    }

    async deleteWebhook(webhookId: string): Promise<any> {
        return this.call('DeleteWebhook', { webhook_id: webhookId });
    }

    async triggerWebhook(data: any): Promise<any> {
        return this.call('TriggerWebhook', data);
    }

    async listWorkflows(category?: string): Promise<any> {
        return this.call('ListWorkflows', { category });
    }

    async getWorkflow(workflowId: string): Promise<any> {
        return this.call('GetWorkflow', { workflow_id: workflowId });
    }

    async * launchWorkflow(data: any): AsyncIterable<any> {
        // Workflows are streamed, so we need a stream method
        if (!this.connected) throw new Error('Brain service not connected');

        // If method doesn't exist, we can't really stream. 
        // We'll throw or return empty stream.
        if (typeof this.client?.LaunchWorkflow !== 'function') {
            logger.warn('Brain RPC method LaunchWorkflow not available');
            return;
        }

        const stream = this.client.LaunchWorkflow(data);
        for await (const chunk of stream) {
            yield chunk;
        }
    }
}
