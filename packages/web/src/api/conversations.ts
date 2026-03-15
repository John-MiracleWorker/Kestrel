import { request } from './http';
import type { Conversation, Message } from './types';

export const conversations = {
    list: (workspaceId: string) =>
        request<{ conversations: Conversation[] }>(`/workspaces/${workspaceId}/conversations`),
    create: (workspaceId: string) =>
        request<{ conversation: Conversation }>(`/workspaces/${workspaceId}/conversations`, {
            method: 'POST',
            body: {},
        }).then((res) => res.conversation),
    messages: (workspaceId: string, conversationId: string) =>
        request<{ messages: Message[] }>(
            `/workspaces/${workspaceId}/conversations/${conversationId}/messages`,
        ),
    delete: (workspaceId: string, conversationId: string) =>
        request<{ success: boolean }>(
            `/workspaces/${workspaceId}/conversations/${conversationId}`,
            {
                method: 'DELETE',
            },
        ),
    rename: (workspaceId: string, conversationId: string, title: string) =>
        request<{ conversation: Conversation }>(
            `/workspaces/${workspaceId}/conversations/${conversationId}`,
            { method: 'PATCH', body: { title } },
        ).then((res) => res.conversation),
    generateTitle: (workspaceId: string, conversationId: string) =>
        request<{ title: string }>(
            `/workspaces/${workspaceId}/conversations/${conversationId}/generate-title`,
            { method: 'POST', body: {} },
        ).then((res) => res.title),
};
