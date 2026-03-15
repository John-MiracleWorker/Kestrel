import { request } from './http';
import type { WorkspaceWebhookConfig } from './types';

export const webhooks = {
    getConfig: (workspaceId: string) =>
        request<{ webhook: WorkspaceWebhookConfig; supportedEvents: string[] }>(
            `/workspaces/${workspaceId}/webhooks/config`,
        ),
    saveConfig: (workspaceId: string, webhook: WorkspaceWebhookConfig) =>
        request<{ success: boolean; webhook: WorkspaceWebhookConfig }>(
            `/workspaces/${workspaceId}/webhooks/config`,
            { method: 'PUT', body: webhook },
        ),
    testConnection: (workspaceId: string) =>
        request<{
            success: boolean;
            delivery: { success: boolean; statusCode?: number; error?: string; attempt: number };
        }>(`/workspaces/${workspaceId}/webhooks/test`, { method: 'POST' }),
};
