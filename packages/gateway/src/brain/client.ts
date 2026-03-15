import {
    addWorkspaceMemberWithBrain,
    authenticateUserWithBrain,
    createConversationWithBrain,
    createUserWithBrain,
    createWorkspaceWithBrain,
    deleteConversationWithBrain,
    deleteWorkspaceWithBrain,
    generateTitleWithBrain,
    getMessagesWithBrain,
    getUpdatesWithBrain,
    listConversationsWithBrain,
    listWorkspacesWithBrain,
    registerPushTokenWithBrain,
    streamChatWithBrain,
    updateConversationWithBrain,
    updateWorkspaceWithBrain,
} from './client-conversations';
import {
    createCronJobWithBrain,
    createWebhookWithBrain,
    deleteCronJobWithBrain,
    deleteWebhookWithBrain,
    getCapabilitiesWithBrain,
    getMemoryGraphWithBrain,
    getWorkflowWithBrain,
    launchWorkflowWithBrain,
    listCronJobsWithBrain,
    listWebhooksWithBrain,
    listWorkflowsWithBrain,
    parseCronJobWithBrain,
    triggerWebhookWithBrain,
} from './client-automation';
import {
    callBrainMethod,
    closeBrainClient,
    connectBrainClient,
    createBrainClientRuntime,
    type BrainClientRuntime,
} from './client-runtime';
import {
    approveActionWithBrain,
    cancelTaskWithBrain,
    getApprovalAuditWithBrain,
    getRuntimeProfileWithBrain,
    getTaskDetailWithBrain,
    listModelsWithBrain,
    listOperatorTasksWithBrain,
    listPendingApprovalsWithBrain,
    listTaskArtifactsWithBrain,
    listTaskCheckpointsWithBrain,
    listTaskTimelineWithBrain,
    listTasksWithBrain,
    startTaskWithBrain,
    streamTaskEventsWithBrain,
} from './client-tasks';

export class BrainClient {
    private readonly runtime: BrainClientRuntime;

    constructor(address: string) {
        this.runtime = createBrainClientRuntime(address);
    }

    isLocalMode(): boolean {
        return this.runtime.localMode;
    }

    async connect(maxRetries = 10): Promise<void> {
        return connectBrainClient(this.runtime, maxRetries);
    }

    isConnected(): boolean {
        return this.runtime.connected;
    }

    close(): void {
        closeBrainClient(this.runtime);
    }

    async call(method: string, request: any): Promise<any> {
        return callBrainMethod(this.runtime, method, request);
    }

    streamChat(request: {
        userId: string;
        workspaceId: string;
        conversationId: string;
        messages: Array<{ role: number; content: string }>;
        provider: string;
        model: string;
        parameters?: Record<string, string>;
    }): AsyncIterable<any> {
        return streamChatWithBrain(this.runtime, request);
    }

    async createUser(email: string, password: string, displayName?: string): Promise<any> {
        return createUserWithBrain(this.runtime, email, password, displayName);
    }

    async authenticateUser(email: string, password: string): Promise<any> {
        return authenticateUserWithBrain(this.runtime, email, password);
    }

    async listWorkspaces(userId: string): Promise<any> {
        return listWorkspacesWithBrain(this.runtime, userId);
    }

    async deleteConversation(
        userId: string,
        workspaceId: string,
        conversationId: string,
    ): Promise<boolean> {
        return deleteConversationWithBrain(this.runtime, userId, workspaceId, conversationId);
    }

    async updateConversation(
        userId: string,
        workspaceId: string,
        conversationId: string,
        title: string,
    ): Promise<any> {
        return updateConversationWithBrain(
            this.runtime,
            userId,
            workspaceId,
            conversationId,
            title,
        );
    }

    async generateTitle(
        userId: string,
        workspaceId: string,
        conversationId: string,
    ): Promise<string> {
        return generateTitleWithBrain(this.runtime, userId, workspaceId, conversationId);
    }

    async createWorkspace(userId: string, name: string): Promise<any> {
        return createWorkspaceWithBrain(this.runtime, userId, name);
    }

    async listConversations(userId: string, workspaceId: string): Promise<any> {
        return listConversationsWithBrain(this.runtime, userId, workspaceId);
    }

    async createConversation(userId: string, workspaceId: string): Promise<any> {
        return createConversationWithBrain(this.runtime, userId, workspaceId);
    }

    async getMessages(userId: string, workspaceId: string, conversationId: string): Promise<any> {
        return getMessagesWithBrain(this.runtime, userId, workspaceId, conversationId);
    }

    async registerPushToken(userId: string, deviceToken: string, platform: string): Promise<any> {
        return registerPushTokenWithBrain(this.runtime, userId, deviceToken, platform);
    }

    async getUpdates(userId: string, since?: string): Promise<any> {
        return getUpdatesWithBrain(this.runtime, userId, since);
    }

    async updateWorkspace(
        workspaceId: string,
        data: { name?: string; description?: string; settings?: any },
    ): Promise<any> {
        return updateWorkspaceWithBrain(this.runtime, workspaceId, data);
    }

    async deleteWorkspace(workspaceId: string): Promise<void> {
        return deleteWorkspaceWithBrain(this.runtime, workspaceId);
    }

    async addWorkspaceMember(workspaceId: string, userId: string, role: string): Promise<any> {
        return addWorkspaceMemberWithBrain(this.runtime, workspaceId, userId, role);
    }

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
        return startTaskWithBrain(this.runtime, request);
    }

    streamTaskEvents(taskId: string, userId: string): AsyncIterable<any> {
        return streamTaskEventsWithBrain(this.runtime, taskId, userId);
    }

    async approveAction(approvalId: string, userId: string, approved: boolean): Promise<any> {
        return approveActionWithBrain(this.runtime, approvalId, userId, approved);
    }

    async listPendingApprovals(userId: string, workspaceId?: string): Promise<any[]> {
        return listPendingApprovalsWithBrain(this.runtime, userId, workspaceId);
    }

    async cancelTask(taskId: string, userId: string): Promise<any> {
        return cancelTaskWithBrain(this.runtime, taskId, userId);
    }

    async listTasks(userId: string, workspaceId?: string, status?: string): Promise<any> {
        return listTasksWithBrain(this.runtime, userId, workspaceId, status);
    }

    async getTaskDetail(workspaceId: string, userId: string, taskId: string): Promise<any> {
        return getTaskDetailWithBrain(this.runtime, workspaceId, userId, taskId);
    }

    async listTaskTimeline(workspaceId: string, userId: string, taskId: string): Promise<any> {
        return listTaskTimelineWithBrain(this.runtime, workspaceId, userId, taskId);
    }

    async listTaskCheckpoints(workspaceId: string, userId: string, taskId: string): Promise<any> {
        return listTaskCheckpointsWithBrain(this.runtime, workspaceId, userId, taskId);
    }

    async listTaskArtifacts(workspaceId: string, userId: string, taskId = ''): Promise<any> {
        return listTaskArtifactsWithBrain(this.runtime, workspaceId, userId, taskId);
    }

    async getApprovalAudit(
        workspaceId: string,
        userId: string,
        options: { taskId?: string; status?: string } = {},
    ): Promise<any> {
        return getApprovalAuditWithBrain(this.runtime, workspaceId, userId, options);
    }

    async listOperatorTasks(workspaceId: string, userId: string, status?: string): Promise<any> {
        return listOperatorTasksWithBrain(this.runtime, workspaceId, userId, status);
    }

    async listModels(provider: string, apiKey?: string, workspaceId?: string): Promise<any[]> {
        return listModelsWithBrain(this.runtime, provider, apiKey, workspaceId);
    }

    async createCronJob(data: any): Promise<any> {
        return createCronJobWithBrain(this.runtime, data);
    }

    async listCronJobs(workspaceId: string): Promise<any> {
        return listCronJobsWithBrain(this.runtime, workspaceId);
    }

    async deleteCronJob(jobId: string): Promise<any> {
        return deleteCronJobWithBrain(this.runtime, jobId);
    }

    async createWebhook(data: any): Promise<any> {
        return createWebhookWithBrain(this.runtime, data);
    }

    async listWebhooks(workspaceId: string): Promise<any> {
        return listWebhooksWithBrain(this.runtime, workspaceId);
    }

    async deleteWebhook(webhookId: string): Promise<any> {
        return deleteWebhookWithBrain(this.runtime, webhookId);
    }

    async triggerWebhook(data: any): Promise<any> {
        return triggerWebhookWithBrain(this.runtime, data);
    }

    async listWorkflows(category?: string): Promise<any> {
        return listWorkflowsWithBrain(this.runtime, category);
    }

    async getWorkflow(workflowId: string): Promise<any> {
        return getWorkflowWithBrain(this.runtime, workflowId);
    }

    launchWorkflow(data: any): AsyncIterable<any> {
        return launchWorkflowWithBrain(this.runtime, data);
    }

    async getCapabilities(workspaceId: string): Promise<any> {
        return getCapabilitiesWithBrain(this.runtime, workspaceId);
    }

    async getMemoryGraph(workspaceId: string, userId: string): Promise<any> {
        return getMemoryGraphWithBrain(this.runtime, workspaceId, userId);
    }

    async getRuntimeProfile(
        workspaceId: string,
        userId: string,
        includeSensitive = false,
    ): Promise<any> {
        return getRuntimeProfileWithBrain(this.runtime, workspaceId, userId, includeSensitive);
    }

    async parseCronJob(workspaceId: string, prompt: string): Promise<any> {
        return parseCronJobWithBrain(this.runtime, workspaceId, prompt);
    }
}
