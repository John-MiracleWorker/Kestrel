import { callBrainMethod, type BrainClientRuntime } from './client-runtime';

export async function* streamChatWithBrain(
    runtime: BrainClientRuntime,
    request: {
        userId: string;
        workspaceId: string;
        conversationId: string;
        messages: Array<{ role: number; content: string }>;
        provider: string;
        model: string;
        parameters?: Record<string, string>;
    },
): AsyncIterable<any> {
    if (!runtime.connected) {
        throw new Error('Brain service not connected');
    }
    if (runtime.localMode) {
        const conversation =
            request.conversationId ||
            runtime.localStore.ensureConversation(
                request.userId,
                request.workspaceId || 'local',
                request.conversationId || undefined,
            ).id;
        const prompt = request.messages
            .map((message) => message.content)
            .join('\n\n')
            .trim();
        if (prompt) {
            runtime.localStore.appendMessage(
                request.userId,
                request.workspaceId || 'local',
                conversation,
                'user',
                prompt,
            );
        }

        const allMessages = runtime.localStore.getMessages(
            request.userId,
            request.workspaceId || 'local',
            conversation,
        );
        const historyMessages = allMessages
            .slice(0, -1)
            .slice(-50)
            .map((message: any) => ({ role: message.role, content: message.content }));

        const completion = await runtime.localTransport.request('chat', {
            prompt,
            workspace_id: request.workspaceId || 'local',
            conversation_id: conversation,
            history: historyMessages,
            parameters: request.parameters || {},
        });
        const content = String(completion.message || '');
        runtime.localStore.appendMessage(
            request.userId,
            request.workspaceId || 'local',
            conversation,
            'assistant',
            content,
        );
        if (content) {
            yield {
                type: 0,
                content_delta: content,
                conversation_id: conversation,
                metadata: {
                    conversation_id: conversation,
                    provider: completion.provider || '',
                    model: completion.model || '',
                },
            };
        }
        yield {
            type: 2,
            conversation_id: conversation,
            metadata: {
                conversation_id: conversation,
                provider: completion.provider || '',
                model: completion.model || '',
            },
        };
        return;
    }

    const grpcRequest = {
        user_id: request.userId,
        workspace_id: request.workspaceId,
        conversation_id: request.conversationId,
        messages: request.messages,
        provider: request.provider,
        model: request.model,
        parameters: request.parameters || {},
    };
    const stream = runtime.client.StreamChat(grpcRequest);
    for await (const chunk of stream) {
        yield chunk;
    }
}

export async function createUserWithBrain(
    runtime: BrainClientRuntime,
    email: string,
    password: string,
    displayName?: string,
): Promise<any> {
    if (runtime.localMode) {
        return runtime.localStore.createUser(email, password, displayName || '');
    }
    return new Promise((resolve, reject) => {
        runtime.client.CreateUser(
            { email, password, display_name: displayName || '' },
            (err: any, response: any) => {
                if (err) {
                    reject(new Error(err.details || err.message));
                } else {
                    resolve(response);
                }
            },
        );
    });
}

export async function authenticateUserWithBrain(
    runtime: BrainClientRuntime,
    email: string,
    password: string,
): Promise<any> {
    if (runtime.localMode) {
        return runtime.localStore.authenticateUser(email, password);
    }
    return new Promise((resolve, reject) => {
        runtime.client.AuthenticateUser({ email, password }, (err: any, response: any) => {
            if (err) {
                reject(new Error(err.details || err.message));
            } else {
                resolve(response);
            }
        });
    });
}

export async function listWorkspacesWithBrain(
    runtime: BrainClientRuntime,
    userId: string,
): Promise<any> {
    if (runtime.localMode) {
        return runtime.localStore.listWorkspaces(userId);
    }
    return new Promise((resolve, reject) => {
        runtime.client.ListWorkspaces({ user_id: userId }, (err: any, response: any) => {
            if (err) {
                reject(new Error(err.details || err.message));
            } else {
                resolve(response?.workspaces || []);
            }
        });
    });
}

export async function deleteConversationWithBrain(
    runtime: BrainClientRuntime,
    userId: string,
    workspaceId: string,
    conversationId: string,
): Promise<boolean> {
    if (runtime.localMode) {
        return runtime.localStore.deleteConversation(userId, workspaceId, conversationId);
    }
    return new Promise((resolve, reject) => {
        runtime.client.DeleteConversation(
            { user_id: userId, workspace_id: workspaceId, conversation_id: conversationId },
            (err: any, response: any) => {
                if (err) {
                    reject(new Error(err.details || err.message));
                } else {
                    resolve(response.success);
                }
            },
        );
    });
}

export async function updateConversationWithBrain(
    runtime: BrainClientRuntime,
    userId: string,
    workspaceId: string,
    conversationId: string,
    title: string,
): Promise<any> {
    if (runtime.localMode) {
        return runtime.localStore.updateConversation(userId, workspaceId, conversationId, title);
    }
    return new Promise((resolve, reject) => {
        runtime.client.UpdateConversation(
            {
                user_id: userId,
                workspace_id: workspaceId,
                conversation_id: conversationId,
                title,
            },
            (err: any, response: any) => {
                if (err) {
                    reject(new Error(err.details || err.message));
                } else {
                    resolve(response);
                }
            },
        );
    });
}

export async function generateTitleWithBrain(
    runtime: BrainClientRuntime,
    userId: string,
    workspaceId: string,
    conversationId: string,
): Promise<string> {
    if (runtime.localMode) {
        return runtime.localStore.generateTitle(userId, workspaceId, conversationId);
    }
    return new Promise((resolve, reject) => {
        runtime.client.GenerateTitle(
            { user_id: userId, workspace_id: workspaceId, conversation_id: conversationId },
            (err: any, response: any) => {
                if (err) {
                    reject(new Error(err.details || err.message));
                } else {
                    resolve(response.title);
                }
            },
        );
    });
}

export async function createWorkspaceWithBrain(
    runtime: BrainClientRuntime,
    userId: string,
    name: string,
): Promise<any> {
    if (runtime.localMode) {
        return runtime.localStore.createWorkspace(userId, name);
    }
    return new Promise((resolve, reject) => {
        runtime.client.CreateWorkspace({ user_id: userId, name }, (err: any, response: any) => {
            if (err) {
                reject(new Error(err.details || err.message));
            } else {
                resolve(response);
            }
        });
    });
}

export async function listConversationsWithBrain(
    runtime: BrainClientRuntime,
    userId: string,
    workspaceId: string,
): Promise<any> {
    if (runtime.localMode) {
        return runtime.localStore.listConversations(userId, workspaceId);
    }
    return new Promise((resolve, reject) => {
        runtime.client.ListConversations(
            { user_id: userId, workspace_id: workspaceId },
            (err: any, response: any) => {
                if (err) {
                    reject(new Error(err.details || err.message));
                } else {
                    resolve(response?.conversations || []);
                }
            },
        );
    });
}

export async function createConversationWithBrain(
    runtime: BrainClientRuntime,
    userId: string,
    workspaceId: string,
): Promise<any> {
    if (runtime.localMode) {
        return runtime.localStore.createConversation(userId, workspaceId);
    }
    return new Promise((resolve, reject) => {
        runtime.client.CreateConversation(
            { user_id: userId, workspace_id: workspaceId },
            (err: any, response: any) => {
                if (err) {
                    reject(new Error(err.details || err.message));
                } else {
                    resolve(response);
                }
            },
        );
    });
}

export async function getMessagesWithBrain(
    runtime: BrainClientRuntime,
    userId: string,
    workspaceId: string,
    conversationId: string,
): Promise<any> {
    if (runtime.localMode) {
        return runtime.localStore.getMessages(userId, workspaceId, conversationId);
    }
    return new Promise((resolve, reject) => {
        runtime.client.GetMessages(
            { user_id: userId, workspace_id: workspaceId, conversation_id: conversationId },
            (err: any, response: any) => {
                if (err) {
                    reject(new Error(err.details || err.message));
                } else {
                    resolve(response?.messages || []);
                }
            },
        );
    });
}

export async function registerPushTokenWithBrain(
    runtime: BrainClientRuntime,
    userId: string,
    deviceToken: string,
    platform: string,
): Promise<any> {
    if (runtime.localMode) {
        return { success: true, user_id: userId, device_token: deviceToken, platform };
    }
    return new Promise((resolve, reject) => {
        runtime.client.RegisterPushToken(
            { user_id: userId, device_token: deviceToken, platform },
            (err: any, response: any) => {
                if (err) {
                    reject(new Error(err.details || err.message));
                } else {
                    resolve(response || { success: true });
                }
            },
        );
    });
}

export async function getUpdatesWithBrain(
    runtime: BrainClientRuntime,
    userId: string,
    since?: string,
): Promise<any> {
    if (runtime.localMode) {
        const workspaces = runtime.localStore.listWorkspaces(userId);
        const workspaceId = workspaces[0]?.id || 'local';
        return {
            messages: [],
            conversations: runtime.localStore.listConversations(userId, workspaceId),
            since: since || '',
        };
    }
    return new Promise((resolve, reject) => {
        runtime.client.GetUpdates(
            { user_id: userId, since: since || '' },
            (err: any, response: any) => {
                if (err) {
                    reject(new Error(err.details || err.message));
                } else {
                    resolve(response || { messages: [], conversations: [] });
                }
            },
        );
    });
}

export async function updateWorkspaceWithBrain(
    runtime: BrainClientRuntime,
    workspaceId: string,
    data: { name?: string; description?: string; settings?: any },
): Promise<any> {
    if (runtime.localMode) {
        return runtime.localStore.updateWorkspace(workspaceId, data);
    }
    return callBrainMethod(runtime, 'UpdateWorkspace', { workspace_id: workspaceId, ...data });
}

export async function deleteWorkspaceWithBrain(
    runtime: BrainClientRuntime,
    workspaceId: string,
): Promise<void> {
    if (runtime.localMode) {
        runtime.localStore.deleteWorkspace(workspaceId);
        return;
    }
    return new Promise((resolve, reject) => {
        runtime.client.DeleteWorkspace({ workspace_id: workspaceId }, (err: any) => {
            if (err) {
                reject(new Error(err.details || err.message));
            } else {
                resolve();
            }
        });
    });
}

export async function addWorkspaceMemberWithBrain(
    runtime: BrainClientRuntime,
    workspaceId: string,
    userId: string,
    role: string,
): Promise<any> {
    if (runtime.localMode) {
        throw new Error(
            `Workspace membership changes are unsupported in local mode (${workspaceId}, ${userId}, ${role})`,
        );
    }
    return new Promise((resolve, reject) => {
        runtime.client.AddWorkspaceMember(
            { workspace_id: workspaceId, user_id: userId, role },
            (err: any, response: any) => {
                if (err) {
                    reject(new Error(err.details || err.message));
                } else {
                    resolve(response);
                }
            },
        );
    });
}
