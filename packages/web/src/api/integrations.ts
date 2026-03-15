import { request } from './http';

export const integrations = {
    status: (workspaceId: string) =>
        request<{
            telegram: {
                connected: boolean;
                status: string;
                botId?: number;
                botUsername?: string;
                tokenConfigured?: boolean;
            };
            discord: { connected: boolean; status: string };
            whatsapp: { connected: boolean; status: string };
        }>(`/workspaces/${workspaceId}/integrations/status`),
    connectTelegram: (workspaceId: string, token: string, enabled: boolean) =>
        request<{ success: boolean; status: string; botId?: number; botUsername?: string }>(
            `/workspaces/${workspaceId}/integrations/telegram`,
            { method: 'POST', body: { token, enabled } },
        ),
    disconnectTelegram: (workspaceId: string) =>
        request<{ success: boolean; status: string }>(
            `/workspaces/${workspaceId}/integrations/telegram`,
            { method: 'DELETE' },
        ),
};
