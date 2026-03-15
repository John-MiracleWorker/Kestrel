import { request } from './http';
import type { UIArtifactItem } from './types';

export const uiArtifacts = {
    list: (workspaceId: string) =>
        request<{ artifacts: UIArtifactItem[] }>(`/workspaces/${workspaceId}/ui-artifacts`).then(
            (res) => res.artifacts,
        ),

    get: (workspaceId: string, artifactId: string) =>
        request<UIArtifactItem>(`/workspaces/${workspaceId}/ui-artifacts/${artifactId}`),

    update: (workspaceId: string, artifactId: string, instruction: string) =>
        request<UIArtifactItem>(`/workspaces/${workspaceId}/ui-artifacts/${artifactId}`, {
            method: 'PUT',
            body: { instruction },
        }),
};
