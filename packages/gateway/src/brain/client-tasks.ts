import { logger } from '../utils/logger';
import { callBrainMethod, type BrainClientRuntime, streamToAsyncIterable } from './client-runtime';

export function startTaskWithBrain(
    runtime: BrainClientRuntime,
    request: {
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
    },
): AsyncIterable<any> {
    if (runtime.localMode) {
        return {
            async *[Symbol.asyncIterator]() {
                const start = await runtime.localTransport.request('task.start', {
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
                for await (const envelope of runtime.localTransport.stream('task.stream', {
                    task_id: taskId,
                })) {
                    if (envelope.event) {
                        yield envelope.event;
                    }
                }
            },
        };
    }

    const stream = runtime.client.StartTask({
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

export function streamTaskEventsWithBrain(
    runtime: BrainClientRuntime,
    taskId: string,
    userId: string,
): AsyncIterable<any> {
    if (runtime.localMode) {
        return {
            async *[Symbol.asyncIterator]() {
                for await (const envelope of runtime.localTransport.stream('task.stream', {
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

    const stream = runtime.client.StreamTaskEvents({
        task_id: taskId,
        user_id: userId,
    });
    return streamToAsyncIterable(stream);
}

export async function approveActionWithBrain(
    runtime: BrainClientRuntime,
    approvalId: string,
    userId: string,
    approved: boolean,
): Promise<any> {
    if (runtime.localMode) {
        return runtime.localTransport.request('approval', {
            action: 'resolve',
            approval_id: approvalId,
            user_id: userId,
            approved,
        });
    }
    return new Promise((resolve, reject) => {
        runtime.client.ApproveAction(
            { approval_id: approvalId, user_id: userId, approved },
            (err: any, response: any) => {
                if (err) {
                    reject(new Error(err.details || err.message));
                } else {
                    resolve(response);
                }
            },
        );
    });
}

export async function listPendingApprovalsWithBrain(
    runtime: BrainClientRuntime,
    userId: string,
    workspaceId?: string,
): Promise<any[]> {
    if (runtime.localMode) {
        const result = await runtime.localTransport.request('approval', {
            action: 'list',
            user_id: userId,
            workspace_id: workspaceId || '',
        });
        return result?.approvals || [];
    }
    return new Promise((resolve) => {
        if (!runtime.connected || typeof runtime.client?.ListPendingApprovals !== 'function') {
            resolve([]);
            return;
        }

        runtime.client.ListPendingApprovals(
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

export async function cancelTaskWithBrain(
    runtime: BrainClientRuntime,
    taskId: string,
    userId: string,
): Promise<any> {
    if (runtime.localMode) {
        return runtime.localTransport.request('task.cancel', {
            task_id: taskId,
            user_id: userId,
        });
    }
    return new Promise((resolve, reject) => {
        runtime.client.CancelTask(
            { task_id: taskId, user_id: userId },
            (err: any, response: any) => {
                if (err) {
                    reject(new Error(err.details || err.message));
                } else {
                    resolve(response);
                }
            },
        );
    });
}

export async function listTasksWithBrain(
    runtime: BrainClientRuntime,
    userId: string,
    workspaceId?: string,
    status?: string,
): Promise<any> {
    if (runtime.localMode) {
        return runtime.localTransport.request('task.list', {
            user_id: userId,
            workspace_id: workspaceId || '',
            status: status || '',
            limit: 100,
        });
    }
    return new Promise((resolve, reject) => {
        runtime.client.ListTasks(
            { user_id: userId, workspace_id: workspaceId || '', status: status || '' },
            (err: any, response: any) => {
                if (err) {
                    reject(new Error(err.details || err.message));
                } else {
                    resolve(response);
                }
            },
        );
    });
}

export async function getTaskDetailWithBrain(
    runtime: BrainClientRuntime,
    workspaceId: string,
    userId: string,
    taskId: string,
): Promise<any> {
    if (runtime.localMode) {
        return runtime.localTransport.request('task.detail', {
            workspace_id: workspaceId,
            user_id: userId,
            task_id: taskId,
        });
    }
    return callBrainMethod(runtime, 'GetTaskDetail', {
        workspace_id: workspaceId,
        user_id: userId,
        task_id: taskId,
    });
}

export async function listTaskTimelineWithBrain(
    runtime: BrainClientRuntime,
    workspaceId: string,
    userId: string,
    taskId: string,
): Promise<any> {
    if (runtime.localMode) {
        return runtime.localTransport.request('task.timeline', {
            workspace_id: workspaceId,
            user_id: userId,
            task_id: taskId,
        });
    }
    return callBrainMethod(runtime, 'ListTaskTimeline', {
        workspace_id: workspaceId,
        user_id: userId,
        task_id: taskId,
    });
}

export async function listTaskCheckpointsWithBrain(
    runtime: BrainClientRuntime,
    workspaceId: string,
    userId: string,
    taskId: string,
): Promise<any> {
    if (runtime.localMode) {
        return {
            checkpoints: [],
            workspace_id: workspaceId,
            user_id: userId,
            task_id: taskId,
        };
    }
    return callBrainMethod(runtime, 'ListTaskCheckpoints', {
        workspace_id: workspaceId,
        user_id: userId,
        task_id: taskId,
    });
}

export async function listTaskArtifactsWithBrain(
    runtime: BrainClientRuntime,
    workspaceId: string,
    userId: string,
    taskId = '',
): Promise<any> {
    if (runtime.localMode) {
        return runtime.localTransport.request('task.artifacts', {
            workspace_id: workspaceId,
            user_id: userId,
            task_id: taskId,
        });
    }
    return callBrainMethod(runtime, 'ListTaskArtifacts', {
        workspace_id: workspaceId,
        user_id: userId,
        task_id: taskId,
    });
}

export async function getApprovalAuditWithBrain(
    runtime: BrainClientRuntime,
    workspaceId: string,
    userId: string,
    options: { taskId?: string; status?: string } = {},
): Promise<any> {
    if (runtime.localMode) {
        return runtime.localTransport.request('task.approvals', {
            workspace_id: workspaceId,
            user_id: userId,
            task_id: options.taskId || '',
            status: options.status || '',
        });
    }
    return callBrainMethod(runtime, 'GetApprovalAudit', {
        workspace_id: workspaceId,
        user_id: userId,
        task_id: options.taskId || '',
        status: options.status || '',
    });
}

export async function listOperatorTasksWithBrain(
    runtime: BrainClientRuntime,
    workspaceId: string,
    userId: string,
    status?: string,
): Promise<any> {
    if (runtime.localMode) {
        return runtime.localTransport.request('task.list', {
            workspace_id: workspaceId,
            user_id: userId,
            status: status || '',
            limit: 100,
        });
    }
    return callBrainMethod(runtime, 'ListOperatorTasks', {
        workspace_id: workspaceId,
        user_id: userId,
        status: status || '',
    });
}

export async function listModelsWithBrain(
    runtime: BrainClientRuntime,
    provider: string,
    apiKey?: string,
    workspaceId?: string,
): Promise<any[]> {
    if (runtime.localMode) {
        const runtimeProfile = await runtime.localTransport.request('runtime.profile', {
            workspace_id: workspaceId || '',
            provider,
            api_key: apiKey || '',
        });
        const localModels = runtimeProfile?.local_models || {};
        const providers = localModels.providers || {};
        const modelInfo =
            providers[provider] || providers[localModels.default_provider || ''] || null;
        const model = modelInfo?.model || localModels.default_model || '';
        return model ? [{ id: model, name: model }] : [];
    }
    return new Promise((resolve) => {
        if (!runtime.connected || typeof runtime.client?.ListModels !== 'function') {
            resolve([]);
            return;
        }

        runtime.client.ListModels(
            { provider, api_key: apiKey, workspace_id: workspaceId },
            (err: any, response: any) => {
                if (err) {
                    logger.error('ListModels failed', { error: err.message, provider });
                    resolve([]);
                } else {
                    resolve(response.models || []);
                }
            },
        );
    });
}

export async function getRuntimeProfileWithBrain(
    runtime: BrainClientRuntime,
    workspaceId: string,
    userId: string,
    includeSensitive = false,
): Promise<any> {
    if (runtime.localMode) {
        const profile = await runtime.localTransport.request('runtime.profile', {
            workspace_id: workspaceId,
            user_id: userId,
            include_sensitive: includeSensitive,
        });
        return { profile };
    }
    return callBrainMethod(runtime, 'GetRuntimeProfile', {
        workspace_id: workspaceId,
        user_id: userId,
        include_sensitive: includeSensitive,
    });
}
