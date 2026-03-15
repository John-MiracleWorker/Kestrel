import crypto from 'crypto';
import fs from 'fs';
import path from 'path';
import { getGatewayStateFile } from '../utils/paths';
import { logger } from '../utils/logger';
import {
    LocalConversation,
    LocalGatewayDocument,
    LocalInstalledTool,
    LocalMessage,
    LocalNotification,
    LocalProviderConfig,
    LocalUser,
    LocalWorkspace,
    defaultDocument,
    hashPassword,
    normalizeToolCatalog,
    nowIso,
    verifyPassword,
} from './local-types';

export class LocalGatewayStateStore {
    constructor(private readonly filePath = getGatewayStateFile('gateway-local.json')) {}

    private read(): LocalGatewayDocument {
        if (!fs.existsSync(this.filePath)) {
            return defaultDocument();
        }
        try {
            const raw = JSON.parse(
                fs.readFileSync(this.filePath, 'utf-8'),
            ) as Partial<LocalGatewayDocument>;
            return {
                ...defaultDocument(),
                ...raw,
                users: raw.users || [],
                workspaces: raw.workspaces || [],
                conversations: raw.conversations || [],
                providerConfigs: raw.providerConfigs || [],
                notifications: raw.notifications || [],
                installedTools: raw.installedTools || [],
            };
        } catch {
            return defaultDocument();
        }
    }

    private write(document: LocalGatewayDocument): void {
        fs.mkdirSync(path.dirname(this.filePath), { recursive: true });
        const tempPath = `${this.filePath}.tmp`;
        fs.writeFileSync(tempPath, JSON.stringify(document, null, 2), 'utf-8');
        fs.renameSync(tempPath, this.filePath);
    }

    private ensureWorkspaceRecord(
        document: LocalGatewayDocument,
        workspaceId?: string,
    ): LocalWorkspace {
        const existing =
            (workspaceId &&
                document.workspaces.find((workspace) => workspace.id === workspaceId)) ||
            document.workspaces[0];
        if (existing) {
            return existing;
        }

        const createdAt = nowIso();
        const workspace: LocalWorkspace = {
            id: workspaceId || crypto.randomUUID(),
            name: 'Local Workspace',
            description: '',
            settings: {},
            createdAt,
            updatedAt: createdAt,
        };
        document.workspaces.push(workspace);
        return workspace;
    }

    private sanitizeWorkspace(workspace: LocalWorkspace) {
        return {
            id: workspace.id,
            name: workspace.name,
            description: workspace.description,
            settings: workspace.settings || {},
            created_at: workspace.createdAt,
            updated_at: workspace.updatedAt,
        };
    }

    createUser(email: string, password: string, displayName = ''): any {
        const document = this.read();
        const existingUser = document.users[0];
        if (existingUser) {
            if (existingUser.email.toLowerCase() === email.toLowerCase()) {
                throw new Error('Email already exists');
            }
            throw new Error('Local mode supports a single operator account');
        }

        const createdAt = nowIso();
        const user: LocalUser = {
            id: crypto.randomUUID(),
            email,
            displayName,
            passwordHash: hashPassword(password),
            createdAt,
            updatedAt: createdAt,
        };
        document.users.push(user);
        this.ensureWorkspaceRecord(document);
        this.write(document);
        return {
            id: user.id,
            email: user.email,
            display_name: user.displayName,
        };
    }

    authenticateUser(email: string, password: string): any {
        const document = this.read();
        const user = document.users.find(
            (candidate) => candidate.email.toLowerCase() === email.toLowerCase(),
        );
        if (!user || !verifyPassword(password, user.passwordHash)) {
            throw new Error('Invalid credentials');
        }
        return {
            id: user.id,
            email: user.email,
            display_name: user.displayName,
        };
    }

    listWorkspaces(_userId: string): any[] {
        const document = this.read();
        this.ensureWorkspaceRecord(document);
        this.write(document);
        return document.workspaces.map((item) => ({
            id: item.id,
            name: item.name,
            description: item.description,
            settings: item.settings,
            created_at: item.createdAt,
            updated_at: item.updatedAt,
            role: 'owner',
        }));
    }

    createWorkspace(_userId: string, name: string): any {
        const document = this.read();
        const createdAt = nowIso();
        const workspace: LocalWorkspace = {
            id: crypto.randomUUID(),
            name,
            description: '',
            settings: {},
            createdAt,
            updatedAt: createdAt,
        };
        document.workspaces.push(workspace);
        this.write(document);
        return {
            id: workspace.id,
            name: workspace.name,
            description: workspace.description,
            settings: {},
            created_at: workspace.createdAt,
        };
    }

    getWorkspace(workspaceId: string): any {
        const document = this.read();
        const workspace = this.ensureWorkspaceRecord(document, workspaceId);
        this.write(document);
        return this.sanitizeWorkspace(workspace);
    }

    updateWorkspace(
        workspaceId: string,
        data: { name?: string; description?: string; settings?: Record<string, any> },
    ): any {
        const document = this.read();
        const workspace = this.ensureWorkspaceRecord(document, workspaceId);
        workspace.name = data.name ?? workspace.name;
        workspace.description = data.description ?? workspace.description;
        workspace.settings = {
            ...(workspace.settings || {}),
            ...(data.settings || {}),
        };
        workspace.updatedAt = nowIso();
        this.write(document);
        return this.sanitizeWorkspace(workspace);
    }

    deleteWorkspace(workspaceId: string): void {
        const document = this.read();
        document.workspaces = document.workspaces.filter(
            (workspace) => workspace.id !== workspaceId,
        );
        document.conversations = document.conversations.filter(
            (conversation) => conversation.workspaceId !== workspaceId,
        );
        document.providerConfigs = document.providerConfigs.filter(
            (config) => config.workspaceId !== workspaceId,
        );
        document.installedTools = document.installedTools.filter(
            (tool) => tool.workspaceId !== workspaceId,
        );
        this.ensureWorkspaceRecord(document);
        this.write(document);
    }

    listConversations(userId: string, workspaceId: string): any[] {
        const document = this.read();
        this.ensureWorkspaceRecord(document, workspaceId);
        this.write(document);
        return document.conversations
            .filter(
                (conversation) =>
                    conversation.workspaceId === workspaceId && conversation.userId === userId,
            )
            .sort((left, right) => right.updatedAt.localeCompare(left.updatedAt))
            .map((conversation) => ({
                id: conversation.id,
                workspace_id: conversation.workspaceId,
                title: conversation.title,
                created_at: conversation.createdAt,
                updated_at: conversation.updatedAt,
            }));
    }

    createConversation(userId: string, workspaceId: string): any {
        const document = this.read();
        this.ensureWorkspaceRecord(document, workspaceId);
        const createdAt = nowIso();
        const conversation: LocalConversation = {
            id: crypto.randomUUID(),
            userId,
            workspaceId,
            title: 'New Chat',
            createdAt,
            updatedAt: createdAt,
            messages: [],
        };
        document.conversations.push(conversation);
        this.write(document);
        return {
            id: conversation.id,
            workspace_id: conversation.workspaceId,
            title: conversation.title,
            created_at: conversation.createdAt,
            updated_at: conversation.updatedAt,
        };
    }

    ensureConversation(
        userId: string,
        workspaceId: string,
        conversationId?: string,
    ): LocalConversation {
        const document = this.read();
        this.ensureWorkspaceRecord(document, workspaceId);
        let conversation =
            (conversationId &&
                document.conversations.find(
                    (candidate) =>
                        candidate.id === conversationId &&
                        candidate.workspaceId === workspaceId &&
                        candidate.userId === userId,
                )) ||
            null;
        if (!conversation) {
            const createdAt = nowIso();
            conversation = {
                id: conversationId || crypto.randomUUID(),
                userId,
                workspaceId,
                title: 'New Chat',
                createdAt,
                updatedAt: createdAt,
                messages: [],
            };
            document.conversations.push(conversation);
            this.write(document);
        }
        return conversation;
    }

    getMessages(userId: string, workspaceId: string, conversationId: string): any[] {
        const document = this.read();
        const conversation = document.conversations.find(
            (candidate) =>
                candidate.id === conversationId &&
                candidate.workspaceId === workspaceId &&
                candidate.userId === userId,
        );
        return (conversation?.messages || []).map((message) => ({
            id: message.id,
            role: message.role,
            content: message.content,
            created_at: message.createdAt,
        }));
    }

    appendMessage(
        userId: string,
        workspaceId: string,
        conversationId: string,
        role: 'user' | 'assistant' | 'system',
        content: string,
    ): void {
        const document = this.read();
        let conversation = document.conversations.find(
            (candidate) =>
                candidate.id === conversationId &&
                candidate.workspaceId === workspaceId &&
                candidate.userId === userId,
        );
        if (!conversation) {
            const createdAt = nowIso();
            conversation = {
                id: conversationId,
                userId,
                workspaceId,
                title: 'New Chat',
                createdAt,
                updatedAt: createdAt,
                messages: [],
            };
            document.conversations.push(conversation);
        }

        conversation.messages.push({
            id: crypto.randomUUID(),
            role,
            content,
            createdAt: nowIso(),
        });
        conversation.updatedAt = nowIso();
        if (!conversation.title || conversation.title === 'New Chat') {
            const firstUserMessage = conversation.messages.find(
                (message) => message.role === 'user',
            );
            if (firstUserMessage) {
                conversation.title = this.generateTitleFromText(firstUserMessage.content);
            }
        }
        this.write(document);
    }

    deleteConversation(userId: string, workspaceId: string, conversationId: string): boolean {
        const document = this.read();
        const before = document.conversations.length;
        document.conversations = document.conversations.filter(
            (candidate) =>
                !(
                    candidate.id === conversationId &&
                    candidate.workspaceId === workspaceId &&
                    candidate.userId === userId
                ),
        );
        this.write(document);
        return document.conversations.length < before;
    }

    updateConversation(
        userId: string,
        workspaceId: string,
        conversationId: string,
        title: string,
    ): any {
        const document = this.read();
        let conversation = document.conversations.find(
            (candidate) =>
                candidate.id === conversationId &&
                candidate.workspaceId === workspaceId &&
                candidate.userId === userId,
        );
        if (!conversation) {
            const createdAt = nowIso();
            conversation = {
                id: conversationId,
                userId,
                workspaceId,
                title: 'New Chat',
                createdAt,
                updatedAt: createdAt,
                messages: [],
            };
            document.conversations.push(conversation);
        }
        conversation.title = title;
        conversation.updatedAt = nowIso();
        this.write(document);
        return {
            id: conversation.id,
            title: conversation.title,
            workspace_id: conversation.workspaceId,
            updated_at: conversation.updatedAt,
        };
    }

    generateTitle(userId: string, workspaceId: string, conversationId: string): string {
        const document = this.read();
        const conversation = document.conversations.find(
            (candidate) =>
                candidate.id === conversationId &&
                candidate.workspaceId === workspaceId &&
                candidate.userId === userId,
        );
        if (!conversation) {
            return 'New Chat';
        }
        const firstUserMessage = conversation.messages.find((message) => message.role === 'user');
        const title = this.generateTitleFromText(firstUserMessage?.content || conversation.title);
        conversation.title = title;
        conversation.updatedAt = nowIso();
        this.write(document);
        return title;
    }

    private generateTitleFromText(input: string): string {
        const normalized = (input || '').replace(/\s+/g, ' ').trim();
        if (!normalized) {
            return 'New Chat';
        }
        const words = normalized.split(' ').slice(0, 7);
        return words.join(' ').slice(0, 80);
    }

    listProviderConfigs(workspaceId: string): any {
        const document = this.read();
        const configs = document.providerConfigs
            .filter((config) => config.workspaceId === workspaceId)
            .map((config) => ({
                workspace_id: config.workspaceId,
                provider: config.provider,
                model: config.model,
                temperature: config.temperature,
                max_tokens: config.maxTokens,
                system_prompt: config.systemPrompt,
                rag_enabled: config.ragEnabled,
                rag_top_k: config.ragTopK,
                rag_min_similarity: config.ragMinSimilarity,
                is_default: config.isDefault,
                settings: config.settings,
                updated_at: config.updatedAt,
            }));
        return { configs };
    }

    setProviderConfig(config: Record<string, any>): any {
        const document = this.read();
        const workspace = this.ensureWorkspaceRecord(
            document,
            String(config.workspace_id || 'local'),
        );
        const provider = String(config.provider || '');
        const existingIndex = document.providerConfigs.findIndex(
            (candidate) =>
                candidate.workspaceId === workspace.id && candidate.provider === provider,
        );

        const nextConfig: LocalProviderConfig = {
            workspaceId: workspace.id,
            provider,
            model: String(config.model || ''),
            temperature: Number(config.temperature ?? 0.7),
            maxTokens: Number(config.max_tokens ?? 2048),
            systemPrompt: String(config.system_prompt || ''),
            ragEnabled: Boolean(config.rag_enabled ?? true),
            ragTopK: Number(config.rag_top_k ?? 5),
            ragMinSimilarity: Number(config.rag_min_similarity ?? 0.3),
            isDefault: Boolean(config.is_default),
            settings: (config.settings || {}) as Record<string, any>,
            apiKey: config.api_key_encrypted ? String(config.api_key_encrypted) : undefined,
            updatedAt: nowIso(),
        };

        if (existingIndex >= 0) {
            document.providerConfigs[existingIndex] = nextConfig;
        } else {
            document.providerConfigs.push(nextConfig);
        }
        this.write(document);
        return { success: true, config: nextConfig };
    }

    deleteProviderConfig(workspaceId: string, provider: string): void {
        const document = this.read();
        document.providerConfigs = document.providerConfigs.filter(
            (config) => !(config.workspaceId === workspaceId && config.provider === provider),
        );
        this.write(document);
    }

    getWorkspaceSettings(workspaceId: string): Record<string, any> {
        const document = this.read();
        const workspace = this.ensureWorkspaceRecord(document, workspaceId);
        this.write(document);
        return workspace.settings || {};
    }

    mergeWorkspaceSettings(workspaceId: string, payload: Record<string, any>): Record<string, any> {
        const document = this.read();
        const workspace = this.ensureWorkspaceRecord(document, workspaceId);
        workspace.settings = {
            ...(workspace.settings || {}),
            ...payload,
        };
        workspace.updatedAt = nowIso();
        this.write(document);
        return workspace.settings;
    }

    listNotifications(userId: string, limit: number): LocalNotification[] {
        const document = this.read();
        return document.notifications
            .filter((notification) => notification.userId === userId && !notification.read)
            .sort((left, right) => right.createdAt.localeCompare(left.createdAt))
            .slice(0, limit);
    }

    markNotificationRead(notificationId: string): boolean {
        const document = this.read();
        const notification = document.notifications.find(
            (candidate) => candidate.id === notificationId,
        );
        if (!notification) {
            return false;
        }
        notification.read = true;
        this.write(document);
        return true;
    }

    markAllNotificationsRead(userId: string): void {
        const document = this.read();
        for (const notification of document.notifications) {
            if (notification.userId === userId) {
                notification.read = true;
            }
        }
        this.write(document);
    }

    listInstalledTools(workspaceId: string): any[] {
        const document = this.read();
        return document.installedTools
            .filter((tool) => tool.workspaceId === workspaceId)
            .sort((left, right) => right.installedAt.localeCompare(left.installedAt))
            .map((tool) => ({
                id: tool.id,
                name: tool.name,
                description: tool.description,
                server_url: tool.serverUrl,
                transport: tool.transport,
                config: tool.config,
                enabled: tool.enabled,
                installed_at: tool.installedAt,
                updated_at: tool.updatedAt,
            }));
    }

    upsertInstalledTool(
        workspaceId: string,
        payload: {
            name: string;
            description: string;
            serverUrl: string;
            transport: string;
            config: Record<string, any>;
        },
    ): any {
        const document = this.read();
        const existing = document.installedTools.find(
            (tool) => tool.workspaceId === workspaceId && tool.name === payload.name,
        );
        const timestamp = nowIso();
        if (existing) {
            existing.description = payload.description;
            existing.serverUrl = payload.serverUrl;
            existing.transport = payload.transport;
            existing.config = payload.config;
            existing.enabled = true;
            existing.updatedAt = timestamp;
        } else {
            document.installedTools.push({
                id: crypto.randomUUID(),
                workspaceId,
                name: payload.name,
                description: payload.description,
                serverUrl: payload.serverUrl,
                transport: payload.transport,
                config: payload.config,
                enabled: true,
                installedAt: timestamp,
                updatedAt: timestamp,
            });
        }
        this.write(document);
        return { success: true, name: payload.name };
    }

    deleteInstalledTool(workspaceId: string, toolName: string): boolean {
        const document = this.read();
        const before = document.installedTools.length;
        document.installedTools = document.installedTools.filter(
            (tool) => !(tool.workspaceId === workspaceId && tool.name === toolName),
        );
        this.write(document);
        return document.installedTools.length < before;
    }

    listWorkspaceMembers(workspaceId: string): any[] {
        const document = this.read();
        const workspace = this.ensureWorkspaceRecord(document, workspaceId);
        this.write(document);
        const user = document.users[0];
        if (!user) {
            return [
                {
                    id: 'local-operator',
                    email: 'local@kestrel',
                    displayName: 'Local Operator',
                    role: 'owner',
                    workspaceId: workspace.id,
                },
            ];
        }
        return [
            {
                id: user.id,
                email: user.email,
                displayName: user.displayName,
                role: 'owner',
                workspaceId: workspace.id,
            },
        ];
    }

    listLocalTools(): any[] {
        return normalizeToolCatalog();
    }

    listRecentConversationTitles(
        workspaceId: string,
        limit = 20,
    ): Array<{ title: string; createdAt: string }> {
        const document = this.read();
        return document.conversations
            .filter((conversation) => conversation.workspaceId === workspaceId)
            .sort((left, right) => right.updatedAt.localeCompare(left.updatedAt))
            .slice(0, limit)
            .map((conversation) => ({
                title: conversation.title,
                createdAt: conversation.createdAt,
            }));
    }
}

let sharedLocalStore: LocalGatewayStateStore | null = null;
