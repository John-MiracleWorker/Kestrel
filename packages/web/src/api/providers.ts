import { request } from './http';
import type { ProviderInfo } from './types';

export const providers = {
    catalog: () => request<{ providers: ProviderInfo[] }>('/providers'),
    list: (workspaceId: string) => request(`/workspaces/${workspaceId}/providers`),
    listModels: (workspaceId: string, provider: string, apiKey?: string) =>
        request<{ models: { id: string; name: string; context_window: string }[] }>(
            `/workspaces/${workspaceId}/providers/${provider}/models${apiKey ? `?apiKey=${encodeURIComponent(apiKey)}` : ''}`,
        ).then((res) => res.models),
    set: (workspaceId: string, provider: string, config: Record<string, unknown>) =>
        request(`/workspaces/${workspaceId}/providers/${provider}`, {
            method: 'PUT',
            body: config,
        }),
    delete: (workspaceId: string, provider: string) =>
        request(`/workspaces/${workspaceId}/providers/${provider}`, { method: 'DELETE' }),
};
