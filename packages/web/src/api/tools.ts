import { request } from './http';
import type { ToolInfo } from './types';

export const tools = {
    list: (workspaceId: string) =>
        request<{ tools: ToolInfo[] }>(`/workspaces/${workspaceId}/tools`),
};
