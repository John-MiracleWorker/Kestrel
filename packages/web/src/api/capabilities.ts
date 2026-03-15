import { request } from './http';
import type { CapabilityItem } from './types';

export const capabilities = {
    get: (workspaceId: string) =>
        request<{ capabilities: CapabilityItem[] }>(`/workspaces/${workspaceId}/capabilities`).then(
            (res) => res.capabilities,
        ),
};
