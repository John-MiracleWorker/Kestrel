import { BASE_URL, request } from './http';
import { mapTaskSummary } from './task-mappers';
import type { StartTaskOptions, TaskSummary } from './types';

export const tasks = {
    list: (workspaceId: string, status?: string) =>
        request<{ tasks: TaskSummary[] }>(
            `/workspaces/${workspaceId}/tasks${status ? `?status=${status}` : ''}`,
        ).then((res) => (res.tasks || []).map(mapTaskSummary)),

    start: (workspaceId: string, _options: StartTaskOptions): EventSource => {
        const url = `${BASE_URL}/workspaces/${workspaceId}/tasks`;
        return new EventSource(url);
    },

    approve: (taskId: string, approvalId: string, approved: boolean) =>
        request<{ success: boolean; error?: string }>(`/tasks/${taskId}/approve`, {
            method: 'POST',
            body: { approvalId, approved },
        }),

    cancel: (taskId: string) =>
        request<{ success: boolean }>(`/tasks/${taskId}/cancel`, {
            method: 'POST',
        }),
};
