import { request } from './http';
import type { MoltbookActivityItem } from './types';

export const moltbook = {
    getActivity: (workspaceId: string, limit = 20) =>
        request<{ activity: MoltbookActivityItem[] }>(
            `/workspaces/${workspaceId}/moltbook/activity?limit=${limit}`,
        ).then((res) => res.activity),
};
