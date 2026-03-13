import * as grpc from '@grpc/grpc-js';
import * as protoLoader from '@grpc/proto-loader';
import path from 'path';
import {
    getLocalControlTransport,
    getLocalGatewayStateStore,
    isLocalBrainMode,
    type ControlTransport,
    type LocalGatewayStateStore,
} from './local';
import { logger } from '../utils/logger';

const PROTO_PATH = path.resolve(__dirname, '../../../shared/proto/brain.proto');

function streamToAsyncIterable(stream: any): AsyncIterable<any> {
    const queue: any[] = [];
    const waiters: Array<{
        resolve: (value: IteratorResult<any>) => void;
        reject: (reason?: any) => void;
    }> = [];
    let ended = false;
    let streamError: any = null;

    const settleWaiters = () => {
        while (waiters.length > 0) {
            const waiter = waiters.shift();
            if (!waiter) continue;

            if (queue.length > 0) {
                waiter.resolve({ value: queue.shift(), done: false });
                continue;
            }

            if (streamError) {
                waiter.reject(streamError);
                continue;
            }

            if (ended) {
                waiter.resolve({ value: undefined, done: true });
            }
        }
    };

    stream.on('data', (data: any) => {
        queue.push(data);
        settleWaiters();
    });

    stream.on('end', () => {
        ended = true;
        settleWaiters();
    });

    stream.on('error', (err: any) => {
        streamError = err;
        settleWaiters();
    });

    return {
        [Symbol.asyncIterator]() {
            return {
                next(): Promise<IteratorResult<any>> {
                    if (queue.length > 0) {
                        return Promise.resolve({ value: queue.shift(), done: false });
                    }

                    if (streamError) {
                        return Promise.reject(streamError);
                    }

                    if (ended) {
                        return Promise.resolve({ value: undefined, done: true });
                    }

                    return new Promise((resolve, reject) => {
                        waiters.push({ resolve, reject });
                    });
                },
            };
        },
    };
}

/**
 * gRPC client for the Brain service.
 * Wraps brain.proto BrainService with typed methods.
 */
export class BrainClient {
    private client: any;
    private connected = false;
    private readonly localMode: boolean;
    private readonly localTransport: ControlTransport;
    private readonly localStore: LocalGatewayStateStore;

    constructor(private address: string) {
        this.localMode = isLocalBrainMode();
        this.localTransport = getLocalControlTransport();
        this.localStore = getLocalGatewayStateStore();
    }

    isLocalMode(): boolean {
        return this.localMode;
    }

    async connect(maxRetries = 10): Promise<void> {
        if (this.localMode) {
            await this.localTransport.request('status', {});
            this.connected = true;
            logger.info('Brain client attached to local daemon control plane');
            return;
        }

        const packageDef = protoLoader.loadSync(PROTO_PATH, {
            keepCase: true,
            longs: String,
            enums: String,
            defaults: true,
            oneofs: true,
        });
        const proto = grpc.loadPackageDefinition(packageDef) as any;
        const BrainService = proto.kestrel.brain.BrainService;

        this.client = new BrainService(this.address, grpc.credentials.createInsecure());

        // Retry connection with exponential backoff
        for (let attempt = 1; attempt <= maxRetries; attempt++) {
            try {
                await new Promise<void>((resolve, reject) => {
                    const deadline = new Date(Date.now() + 5000);
                    this.client.waitForReady(deadline, (err: Error | null) => {
                        if (err) reject(err);
                        else resolve();
                    });
                });
                this.connected = true;
                logger.info('Brain gRPC connected', { address: this.address, attempt });
                return;
            } catch (err: any) {
                const delay = Math.min(1000 * Math.pow(2, attempt - 1), 10000);
                logger.warn(
                    `Brain gRPC not ready (attempt ${attempt}/${maxRetries}), retrying in ${delay}ms...`,
                    {
                        address: this.address,
                        error: err.message,
                    },
                );
                if (attempt === maxRetries) {
                    this.connected = false;
                    throw new Error(
                        `Failed to connect to Brain gRPC at ${this.address} after ${maxRetries} attempts`,
                    );
                }
                await new Promise((r) => setTimeout(r, delay));
            }
        }
    }

    isConnected(): boolean {
        return this.connected;
    }

    close(): void {
        if (this.localMode) {
            this.connected = false;
            return;
        }
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
        if (this.localMode) {
            return this.localCall(method, request);
        }

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

    private async localCall(method: string, request: any): Promise<any> {
        switch (method) {
            case 'ListProviderConfigs':
                return this.localStore.listProviderConfigs(String(request.workspace_id || 'local'));
            case 'SetProviderConfig':
                return this.localStore.setProviderConfig(request || {});
            case 'DeleteProviderConfig':
                this.localStore.deleteProviderConfig(
                    String(request.workspace_id || 'local'),
                    String(request.provider || ''),
                );
                return { success: true };
            case 'ListTools':
                return { tools: this.localStore.listLocalTools() };
            case 'SubmitFeedback':
                return { id: `feedback-${Date.now()}` };
            case 'ListWorkspaceMembers':
                return {
                    members: this.localStore.listWorkspaceMembers(
                        String(request.workspaceId || request.workspace_id || 'local'),
                    ),
                };
            case 'InviteWorkspaceMember':
            case 'RemoveWorkspaceMember':
                throw new Error('Unsupported in local mode');
            case 'GetCapabilities':
                return {
                    capabilities: [
                        {
                            name: 'Telegram First Sessions',
                            description:
                                'Unified Telegram, desktop, CLI, and web sessions over the local gateway.',
                            status: 'active',
                            category: 'channels',
                            icon: '✈',
                        },
                        {
                            name: 'Native Runtime',
                            description:
                                'Local-native execution and daemon-backed task orchestration.',
                            status: 'active',
                            category: 'runtime',
                            icon: '⚙',
                        },
                        {
                            name: 'Media Artifacts',
                            description: 'Shared local media artifacts and delivery receipts.',
                            status: 'active',
                            category: 'media',
                            icon: '▣',
                        },
                    ],
                };
            case 'GetMemoryGraph':
                return { nodes: [], links: [] };
            case 'ListProcesses': {
                const tasks =
                    (await this.localTransport.request('task.list', { limit: 100 })).tasks || [];
                return {
                    processes: tasks,
                    running: tasks.filter((task: any) => task.status === 'running').length,
                };
            }
            default:
                logger.warn('Unsupported local Brain RPC method', { method });
                return {};
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
        if (this.localMode) {
            const conversation =
                request.conversationId ||
                this.localStore.ensureConversation(
                    request.userId,
                    request.workspaceId || 'local',
                    request.conversationId || undefined,
                ).id;
            const prompt = request.messages
                .map((message) => message.content)
                .join('\n\n')
                .trim();
            if (prompt) {
                this.localStore.appendMessage(
                    request.userId,
                    request.workspaceId || 'local',
                    conversation,
                    'user',
                    prompt,
                );
            }
            const completion = await this.localTransport.request('chat', {
                prompt,
                workspace_id: request.workspaceId || 'local',
                conversation_id: conversation,
                parameters: request.parameters || {},
            });
            const content = String(completion.message || '');
            this.localStore.appendMessage(
                request.userId,
                request.workspaceId || 'local',
                conversation,
                'assistant',
                content,
            );
            if (content) {
                yield {
                    type: 0,
                    content_delta: content,
                    conversation_id: conversation,
                    metadata: {
                        conversation_id: conversation,
                        provider: completion.provider || '',
                        model: completion.model || '',
                    },
                };
            }
            yield {
                type: 2,
                conversation_id: conversation,
                metadata: {
                    conversation_id: conversation,
                    provider: completion.provider || '',
                    model: completion.model || '',
                },
            };
            return;
        }

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
        if (this.localMode) {
            return this.localStore.createUser(email, password, displayName || '');
        }
        // For Phase 1, we use a simple unary RPC or REST fallback
        return new Promise((resolve, reject) => {
            this.client.CreateUser(
                { email, password, display_name: displayName || '' },
                (err: any, response: any) => {
                    if (err) reject(new Error(err.details || err.message));
                    else resolve(response);
                },
            );
        });
    }

    async authenticateUser(email: string, password: string): Promise<any> {
        if (this.localMode) {
            return this.localStore.authenticateUser(email, password);
        }
        return new Promise((resolve, reject) => {
            this.client.AuthenticateUser({ email, password }, (err: any, response: any) => {
                if (err) reject(new Error(err.details || err.message));
                else resolve(response);
            });
        });
    }

    async listWorkspaces(userId: string): Promise<any> {
        if (this.localMode) {
            return this.localStore.listWorkspaces(userId);
        }
        return new Promise((resolve, reject) => {
            this.client.ListWorkspaces({ user_id: userId }, (err: any, response: any) => {
                if (err) reject(new Error(err.details || err.message));
                else resolve(response?.workspaces || []);
            });
        });
    }

    async deleteConversation(
        userId: string,
        workspaceId: string,
        conversationId: string,
    ): Promise<boolean> {
        if (this.localMode) {
            return this.localStore.deleteConversation(userId, workspaceId, conversationId);
        }
        return new Promise((resolve, reject) => {
            this.client.DeleteConversation(
                { user_id: userId, workspace_id: workspaceId, conversation_id: conversationId },
                (err: any, response: any) => {
                    if (err) reject(new Error(err.details || err.message));
                    else resolve(response.success);
                },
            );
        });
    }

    async updateConversation(
        userId: string,
        workspaceId: string,
        conversationId: string,
        title: string,
    ): Promise<any> {
        if (this.localMode) {
            return this.localStore.updateConversation(userId, workspaceId, conversationId, title);
        }
        return new Promise((resolve, reject) => {
            this.client.UpdateConversation(
                {
                    user_id: userId,
                    workspace_id: workspaceId,
                    conversation_id: conversationId,
                    title,
                },
                (err: any, response: any) => {
                    if (err) reject(new Error(err.details || err.message));
                    else resolve(response);
                },
            );
        });
    }

    async generateTitle(
        userId: string,
        workspaceId: string,
        conversationId: string,
    ): Promise<string> {
        if (this.localMode) {
            return this.localStore.generateTitle(userId, workspaceId, conversationId);
        }
        return new Promise((resolve, reject) => {
            this.client.GenerateTitle(
                { user_id: userId, workspace_id: workspaceId, conversation_id: conversationId },
                (err: any, response: any) => {
                    if (err) reject(new Error(err.details || err.message));
                    else resolve(response.title);
                },
            );
        });
    }

    async createWorkspace(userId: string, name: string): Promise<any> {
        if (this.localMode) {
            return this.localStore.createWorkspace(userId, name);
        }
        return new Promise((resolve, reject) => {
            this.client.CreateWorkspace({ user_id: userId, name }, (err: any, response: any) => {
                if (err) reject(new Error(err.details || err.message));
                else resolve(response);
            });
        });
    }

    async listConversations(userId: string, workspaceId: string): Promise<any> {
        if (this.localMode) {
            return this.localStore.listConversations(userId, workspaceId);
        }
        return new Promise((resolve, reject) => {
            this.client.ListConversations(
                { user_id: userId, workspace_id: workspaceId },
                (err: any, response: any) => {
                    if (err) reject(new Error(err.details || err.message));
                    else resolve(response?.conversations || []);
                },
            );
        });
    }

    async createConversation(userId: string, workspaceId: string): Promise<any> {
        if (this.localMode) {
            return this.localStore.createConversation(userId, workspaceId);
        }
        return new Promise((resolve, reject) => {
            this.client.CreateConversation(
                { user_id: userId, workspace_id: workspaceId },
                (err: any, response: any) => {
                    if (err) reject(new Error(err.details || err.message));
                    else resolve(response);
                },
            );
        });
    }

    async getMessages(userId: string, workspaceId: string, conversationId: string): Promise<any> {
        if (this.localMode) {
            return this.localStore.getMessages(userId, workspaceId, conversationId);
        }
        return new Promise((resolve, reject) => {
            this.client.GetMessages(
                { user_id: userId, workspace_id: workspaceId, conversation_id: conversationId },
                (err: any, response: any) => {
                    if (err) reject(new Error(err.details || err.message));
                    else resolve(response?.messages || []);
                },
            );
        });
    }

    async registerPushToken(userId: string, deviceToken: string, platform: string): Promise<any> {
        if (this.localMode) {
            return { success: true, user_id: userId, device_token: deviceToken, platform };
        }
        return new Promise((resolve, reject) => {
            this.client.RegisterPushToken(
                { user_id: userId, device_token: deviceToken, platform },
                (err: any, response: any) => {
                    if (err) reject(new Error(err.details || err.message));
                    else resolve(response || { success: true });
                },
            );
        });
    }

    async getUpdates(userId: string, since?: string): Promise<any> {
        if (this.localMode) {
            const workspaces = this.localStore.listWorkspaces(userId);
            const workspaceId = workspaces[0]?.id || 'local';
            return {
                messages: [],
                conversations: this.localStore.listConversations(userId, workspaceId),
                since: since || '',
            };
        }
        return new Promise((resolve, reject) => {
            this.client.GetUpdates(
                { user_id: userId, since: since || '' },
                (err: any, response: any) => {
                    if (err) reject(new Error(err.details || err.message));
                    else resolve(response || { messages: [], conversations: [] });
                },
            );
        });
    }

    async updateWorkspace(
        workspaceId: string,
        data: { name?: string; description?: string; settings?: any },
    ): Promise<any> {
        if (this.localMode) {
            return this.localStore.updateWorkspace(workspaceId, data);
        }
        return this.call('UpdateWorkspace', { workspace_id: workspaceId, ...data });
    }

    async deleteWorkspace(workspaceId: string): Promise<void> {
        if (this.localMode) {
            this.localStore.deleteWorkspace(workspaceId);
            return;
        }
        return new Promise((resolve, reject) => {
            this.client.DeleteWorkspace({ workspace_id: workspaceId }, (err: any) => {
                if (err) reject(new Error(err.details || err.message));
                else resolve();
            });
        });
    }

    async addWorkspaceMember(workspaceId: string, userId: string, role: string): Promise<any> {
        if (this.localMode) {
            throw new Error(
                `Workspace membership changes are unsupported in local mode (${workspaceId}, ${userId}, ${role})`,
            );
        }
        return new Promise((resolve, reject) => {
            this.client.AddWorkspaceMember(
                { workspace_id: workspaceId, user_id: userId, role },
                (err: any, response: any) => {
                    if (err) reject(new Error(err.details || err.message));
                    else resolve(response);
                },
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
        if (this.localMode) {
            const self = this;
            return {
                async *[Symbol.asyncIterator]() {
                    const start = await self.localTransport.request('task.start', {
                        user_id: request.userId,
                        workspace_id: request.workspaceId,
                        goal: request.goal,
                        conversation_id: request.conversationId || '',
                        guardrails: request.guardrails || {},
                    });
                    const taskId = start?.task?.id;
                    if (!taskId) {
                        throw new Error('Local daemon did not return a task id');
                    }
                    for await (const envelope of self.localTransport.stream('task.stream', {
                        task_id: taskId,
                    })) {
                        if (envelope.event) {
                            yield envelope.event;
                        }
                    }
                },
            };
        }
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
        return streamToAsyncIterable(stream);
    }

    streamTaskEvents(taskId: string, userId: string): AsyncIterable<any> {
        if (this.localMode) {
            const self = this;
            return {
                async *[Symbol.asyncIterator]() {
                    for await (const envelope of self.localTransport.stream('task.stream', {
                        task_id: taskId,
                        user_id: userId,
                    })) {
                        if (envelope.event) {
                            yield envelope.event;
                        }
                    }
                },
            };
        }
        const stream = this.client.StreamTaskEvents({
            task_id: taskId,
            user_id: userId,
        });
        return streamToAsyncIterable(stream);
    }

    /**
     * Approve or deny a pending agent action.
     */
    async approveAction(approvalId: string, userId: string, approved: boolean): Promise<any> {
        if (this.localMode) {
            return this.localTransport.request('approval', {
                action: 'resolve',
                approval_id: approvalId,
                user_id: userId,
                approved,
            });
        }
        return new Promise((resolve, reject) => {
            this.client.ApproveAction(
                { approval_id: approvalId, user_id: userId, approved },
                (err: any, response: any) => {
                    if (err) reject(new Error(err.details || err.message));
                    else resolve(response);
                },
            );
        });
    }

    async listPendingApprovals(userId: string, workspaceId?: string): Promise<any[]> {
        if (this.localMode) {
            const result = await this.localTransport.request('approval', {
                action: 'list',
                user_id: userId,
                workspace_id: workspaceId || '',
            });
            return result?.approvals || [];
        }
        return new Promise((resolve) => {
            if (!this.connected || typeof this.client?.ListPendingApprovals !== 'function') {
                resolve([]);
                return;
            }

            this.client.ListPendingApprovals(
                { user_id: userId, workspace_id: workspaceId || '' },
                (err: any, response: any) => {
                    if (err) {
                        logger.warn('ListPendingApprovals failed', {
                            error: err.message,
                            userId,
                            workspaceId,
                        });
                        resolve([]);
                    } else {
                        resolve(response?.approvals || []);
                    }
                },
            );
        });
    }

    /**
     * Cancel a running agent task.
     */
    async cancelTask(taskId: string, userId: string): Promise<any> {
        if (this.localMode) {
            return this.localTransport.request('task.cancel', {
                task_id: taskId,
                user_id: userId,
            });
        }
        return new Promise((resolve, reject) => {
            this.client.CancelTask(
                { task_id: taskId, user_id: userId },
                (err: any, response: any) => {
                    if (err) reject(new Error(err.details || err.message));
                    else resolve(response);
                },
            );
        });
    }

    /**
     * List agent tasks for a user.
     */
    async listTasks(userId: string, workspaceId?: string, status?: string): Promise<any> {
        if (this.localMode) {
            return this.localTransport.request('task.list', {
                user_id: userId,
                workspace_id: workspaceId || '',
                status: status || '',
                limit: 100,
            });
        }
        return new Promise((resolve, reject) => {
            this.client.ListTasks(
                { user_id: userId, workspace_id: workspaceId || '', status: status || '' },
                (err: any, response: any) => {
                    if (err) reject(new Error(err.details || err.message));
                    else resolve(response);
                },
            );
        });
    }

    async getTaskDetail(workspaceId: string, userId: string, taskId: string): Promise<any> {
        if (this.localMode) {
            return this.localTransport.request('task.detail', {
                workspace_id: workspaceId,
                user_id: userId,
                task_id: taskId,
            });
        }
        return this.call('GetTaskDetail', {
            workspace_id: workspaceId,
            user_id: userId,
            task_id: taskId,
        });
    }

    async listTaskTimeline(workspaceId: string, userId: string, taskId: string): Promise<any> {
        if (this.localMode) {
            return this.localTransport.request('task.timeline', {
                workspace_id: workspaceId,
                user_id: userId,
                task_id: taskId,
            });
        }
        return this.call('ListTaskTimeline', {
            workspace_id: workspaceId,
            user_id: userId,
            task_id: taskId,
        });
    }

    async listTaskCheckpoints(workspaceId: string, userId: string, taskId: string): Promise<any> {
        if (this.localMode) {
            return {
                checkpoints: [],
                workspace_id: workspaceId,
                user_id: userId,
                task_id: taskId,
            };
        }
        return this.call('ListTaskCheckpoints', {
            workspace_id: workspaceId,
            user_id: userId,
            task_id: taskId,
        });
    }

    async listTaskArtifacts(workspaceId: string, userId: string, taskId = ''): Promise<any> {
        if (this.localMode) {
            return this.localTransport.request('task.artifacts', {
                workspace_id: workspaceId,
                user_id: userId,
                task_id: taskId,
            });
        }
        return this.call('ListTaskArtifacts', {
            workspace_id: workspaceId,
            user_id: userId,
            task_id: taskId,
        });
    }

    async getApprovalAudit(
        workspaceId: string,
        userId: string,
        options: { taskId?: string; status?: string } = {},
    ): Promise<any> {
        if (this.localMode) {
            return this.localTransport.request('task.approvals', {
                workspace_id: workspaceId,
                user_id: userId,
                task_id: options.taskId || '',
                status: options.status || '',
            });
        }
        return this.call('GetApprovalAudit', {
            workspace_id: workspaceId,
            user_id: userId,
            task_id: options.taskId || '',
            status: options.status || '',
        });
    }

    async listOperatorTasks(workspaceId: string, userId: string, status?: string): Promise<any> {
        if (this.localMode) {
            return this.localTransport.request('task.list', {
                workspace_id: workspaceId,
                user_id: userId,
                status: status || '',
                limit: 100,
            });
        }
        return this.call('ListOperatorTasks', {
            workspace_id: workspaceId,
            user_id: userId,
            status: status || '',
        });
    }

    async listModels(provider: string, apiKey?: string, workspaceId?: string): Promise<any[]> {
        if (this.localMode) {
            const runtime = await this.localTransport.request('runtime.profile', {
                workspace_id: workspaceId || '',
                provider,
                api_key: apiKey || '',
            });
            const localModels = runtime?.local_models || {};
            const providers = localModels.providers || {};
            const modelInfo =
                providers[provider] || providers[localModels.default_provider || ''] || null;
            const model = modelInfo?.model || localModels.default_model || '';
            return model ? [{ id: model, name: model }] : [];
        }
        return new Promise((resolve, reject) => {
            // If not connected or method missing (during dev/migration), return empty
            if (!this.connected || typeof this.client?.ListModels !== 'function') {
                resolve([]);
                return;
            }

            this.client.ListModels(
                { provider, api_key: apiKey, workspace_id: workspaceId },
                (err: any, response: any) => {
                    if (err) {
                        logger.error('ListModels failed', { error: err.message, provider });
                        resolve([]); // Fail gracefully for dropdowns
                    } else {
                        resolve(response.models || []);
                    }
                },
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

    async *launchWorkflow(data: any): AsyncIterable<any> {
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

    async getCapabilities(workspaceId: string): Promise<any> {
        if (this.localMode) {
            return this.localCall('GetCapabilities', { workspace_id: workspaceId });
        }
        return this.call('GetCapabilities', { workspace_id: workspaceId });
    }

    async getMemoryGraph(workspaceId: string, userId: string): Promise<any> {
        if (this.localMode) {
            return this.localCall('GetMemoryGraph', { workspace_id: workspaceId, user_id: userId });
        }
        return this.call('GetMemoryGraph', { workspace_id: workspaceId, user_id: userId });
    }

    async getRuntimeProfile(
        workspaceId: string,
        userId: string,
        includeSensitive = false,
    ): Promise<any> {
        if (this.localMode) {
            const profile = await this.localTransport.request('runtime.profile', {
                workspace_id: workspaceId,
                user_id: userId,
                include_sensitive: includeSensitive,
            });
            return { profile };
        }
        return this.call('GetRuntimeProfile', {
            workspace_id: workspaceId,
            user_id: userId,
            include_sensitive: includeSensitive,
        });
    }

    async parseCronJob(workspaceId: string, prompt: string): Promise<any> {
        if (this.localMode) {
            return {
                workspace_id: workspaceId,
                schedule: '',
                command: prompt,
                valid: false,
                error: 'Cron parsing is unsupported in local mode',
            };
        }
        return this.call('ParseCronJob', { workspace_id: workspaceId, prompt });
    }
}
