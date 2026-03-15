import { logger } from '../utils/logger';
import { callBrainMethod, localCall, type BrainClientRuntime } from './client-runtime';

export async function createCronJobWithBrain(runtime: BrainClientRuntime, data: any): Promise<any> {
    return callBrainMethod(runtime, 'CreateCronJob', data);
}

export async function listCronJobsWithBrain(
    runtime: BrainClientRuntime,
    workspaceId: string,
): Promise<any> {
    return callBrainMethod(runtime, 'ListCronJobs', { workspace_id: workspaceId });
}

export async function deleteCronJobWithBrain(
    runtime: BrainClientRuntime,
    jobId: string,
): Promise<any> {
    return callBrainMethod(runtime, 'DeleteCronJob', { job_id: jobId });
}

export async function createWebhookWithBrain(runtime: BrainClientRuntime, data: any): Promise<any> {
    return callBrainMethod(runtime, 'CreateWebhook', data);
}

export async function listWebhooksWithBrain(
    runtime: BrainClientRuntime,
    workspaceId: string,
): Promise<any> {
    return callBrainMethod(runtime, 'ListWebhooks', { workspace_id: workspaceId });
}

export async function deleteWebhookWithBrain(
    runtime: BrainClientRuntime,
    webhookId: string,
): Promise<any> {
    return callBrainMethod(runtime, 'DeleteWebhook', { webhook_id: webhookId });
}

export async function triggerWebhookWithBrain(
    runtime: BrainClientRuntime,
    data: any,
): Promise<any> {
    return callBrainMethod(runtime, 'TriggerWebhook', data);
}

export async function listWorkflowsWithBrain(
    runtime: BrainClientRuntime,
    category?: string,
): Promise<any> {
    return callBrainMethod(runtime, 'ListWorkflows', { category });
}

export async function getWorkflowWithBrain(
    runtime: BrainClientRuntime,
    workflowId: string,
): Promise<any> {
    return callBrainMethod(runtime, 'GetWorkflow', { workflow_id: workflowId });
}

export async function* launchWorkflowWithBrain(
    runtime: BrainClientRuntime,
    data: any,
): AsyncIterable<any> {
    if (!runtime.connected) {
        throw new Error('Brain service not connected');
    }
    if (typeof runtime.client?.LaunchWorkflow !== 'function') {
        logger.warn('Brain RPC method LaunchWorkflow not available');
        return;
    }

    const stream = runtime.client.LaunchWorkflow(data);
    for await (const chunk of stream) {
        yield chunk;
    }
}

export async function getCapabilitiesWithBrain(
    runtime: BrainClientRuntime,
    workspaceId: string,
): Promise<any> {
    if (runtime.localMode) {
        return localCall(runtime, 'GetCapabilities', { workspace_id: workspaceId });
    }
    return callBrainMethod(runtime, 'GetCapabilities', { workspace_id: workspaceId });
}

export async function getMemoryGraphWithBrain(
    runtime: BrainClientRuntime,
    workspaceId: string,
    userId: string,
): Promise<any> {
    if (runtime.localMode) {
        return localCall(runtime, 'GetMemoryGraph', { workspace_id: workspaceId, user_id: userId });
    }
    return callBrainMethod(runtime, 'GetMemoryGraph', {
        workspace_id: workspaceId,
        user_id: userId,
    });
}

export async function parseCronJobWithBrain(
    runtime: BrainClientRuntime,
    workspaceId: string,
    prompt: string,
): Promise<any> {
    if (runtime.localMode) {
        return {
            workspace_id: workspaceId,
            schedule: '',
            command: prompt,
            valid: false,
            error: 'Cron parsing is unsupported in local mode',
        };
    }
    return callBrainMethod(runtime, 'ParseCronJob', { workspace_id: workspaceId, prompt });
}
