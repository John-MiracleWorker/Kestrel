import { request } from './http';
import {
    mapApprovalAuditItem,
    mapOperatorTaskItem,
    mapRuntimeProfile,
    mapTaskArtifactItem,
    mapTaskCheckpointItem,
    mapTaskDetail,
    mapTaskTimelineItem,
} from './task-mappers';
import type {
    ApprovalAuditItem,
    OperatorTaskItem,
    RuntimeProfile,
    TaskArtifactItem,
    TaskCheckpointItem,
    TaskDetail,
    TaskTimelineItem,
} from './types';

export const operations = {
    listTasks: (workspaceId: string, status?: string) =>
        request<{ tasks: OperatorTaskItem[] }>(
            `/workspaces/${workspaceId}/operations/tasks${status ? `?status=${encodeURIComponent(status)}` : ''}`,
        ).then((res) => (res.tasks || []).map(mapOperatorTaskItem)),

    getTaskDetail: (workspaceId: string, taskId: string) =>
        request<{ task: TaskDetail }>(`/workspaces/${workspaceId}/operations/tasks/${taskId}`).then(
            (res) => mapTaskDetail(res.task),
        ),

    listTimeline: (workspaceId: string, taskId: string) =>
        request<{ events: TaskTimelineItem[] }>(
            `/workspaces/${workspaceId}/operations/tasks/${taskId}/timeline`,
        ).then((res) => (res.events || []).map(mapTaskTimelineItem)),

    listCheckpoints: (workspaceId: string, taskId: string) =>
        request<{ checkpoints: TaskCheckpointItem[] }>(
            `/workspaces/${workspaceId}/operations/tasks/${taskId}/checkpoints`,
        ).then((res) => (res.checkpoints || []).map(mapTaskCheckpointItem)),

    listArtifacts: (workspaceId: string, taskId?: string) =>
        request<{ artifacts: TaskArtifactItem[] }>(
            taskId
                ? `/workspaces/${workspaceId}/operations/tasks/${taskId}/artifacts`
                : `/workspaces/${workspaceId}/operations/artifacts`,
        ).then((res) => (res.artifacts || []).map(mapTaskArtifactItem)),

    listApprovals: (workspaceId: string, options: { taskId?: string; status?: string } = {}) => {
        const search = new URLSearchParams();
        if (options.taskId) {
            search.set('taskId', options.taskId);
        }
        if (options.status) {
            search.set('status', options.status);
        }
        const suffix = search.toString() ? `?${search.toString()}` : '';
        return request<{ approvals: ApprovalAuditItem[] }>(
            `/workspaces/${workspaceId}/operations/approvals${suffix}`,
        ).then((res) => (res.approvals || []).map(mapApprovalAuditItem));
    },

    getRuntimeProfile: (workspaceId: string) =>
        request<{ profile: RuntimeProfile }>(
            `/workspaces/${workspaceId}/operations/runtime-profile`,
        ).then((res) => mapRuntimeProfile(res.profile)),
};
