import { request } from './http';
import type { ApiKey } from './types';

export const apiKeys = {
    list: (workspaceId: string) =>
        request<{ keys: ApiKey[] }>(`/workspaces/${workspaceId}/api-keys`),
    create: (
        workspaceId: string,
        name: string,
        options: { expiresInDays?: number; role?: ApiKey['role'] } = {},
    ) =>
        request<{
            id: string;
            name: string;
            role: ApiKey['role'];
            workspaceId: string;
            key: string;
            expiresAt: string;
        }>(`/workspaces/${workspaceId}/api-keys`, {
            method: 'POST',
            body: {
                name,
                expiresInDays: options.expiresInDays,
                role: options.role || 'member',
            },
        }),
    revoke: (workspaceId: string, id: string) =>
        request(`/workspaces/${workspaceId}/api-keys/${id}`, { method: 'DELETE' }),
};
