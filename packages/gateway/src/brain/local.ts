import crypto from 'crypto';
import fs from 'fs';
import net from 'net';
import path from 'path';
import { getGatewayStateFile, getKestrelHome } from '../utils/paths';
import { logger } from '../utils/logger';

const DEFAULT_CONTROL_PORT = parseInt(process.env.KESTREL_CONTROL_PORT || '8749', 10);
const DEFAULT_CONTROL_HOST = process.env.KESTREL_CONTROL_HOST || '127.0.0.1';

export interface ControlEnvelope {
    request_id: string;
    ok: boolean;
    done?: boolean;
    result?: any;
    event?: any;
    error?: {
        message?: string;
        code?: string;
    };
}

export interface ControlTransport {
    request(method: string, params?: Record<string, any>): Promise<any>;
    stream(method: string, params?: Record<string, any>): AsyncIterable<ControlEnvelope>;
}

export function isLocalBrainMode(): boolean {
    const runtimeMode = (process.env.KESTREL_RUNTIME_MODE || '').toLowerCase();
    const brainTransport = (process.env.BRAIN_TRANSPORT || '').toLowerCase();
    return runtimeMode === 'native' || runtimeMode === 'local' || brainTransport === 'local';
}

function parseJsonLines(socket: net.Socket, requestId: string): AsyncIterable<ControlEnvelope> {
    const queue: ControlEnvelope[] = [];
    const waiters: Array<{
        resolve: (value: IteratorResult<ControlEnvelope>) => void;
        reject: (reason?: unknown) => void;
    }> = [];
    let buffer = '';
    let ended = false;
    let streamError: Error | null = null;

    const settle = () => {
        while (waiters.length > 0) {
            const waiter = waiters.shift();
            if (!waiter) continue;

            if (queue.length > 0) {
                waiter.resolve({ value: queue.shift()!, done: false });
                continue;
            }

            if (streamError) {
                waiter.reject(streamError);
                continue;
            }

            if (ended) {
                waiter.resolve({ value: undefined, done: true });
            }
        }
    };

    socket.on('data', (chunk: Buffer | string) => {
        buffer += chunk.toString();
        while (buffer.includes('\n')) {
            const newlineIndex = buffer.indexOf('\n');
            const line = buffer.slice(0, newlineIndex).trim();
            buffer = buffer.slice(newlineIndex + 1);
            if (!line) continue;

            try {
                const payload = JSON.parse(line) as ControlEnvelope;
                if (payload.request_id !== requestId) {
                    continue;
                }
                queue.push(payload);
                settle();
            } catch (error: any) {
                streamError = new Error(error?.message || 'Invalid daemon control response');
                settle();
                socket.destroy(streamError);
                return;
            }
        }
    });

    socket.on('error', (error) => {
        streamError = error;
        settle();
    });

    socket.on('close', () => {
        ended = true;
        settle();
    });

    return {
        [Symbol.asyncIterator]() {
            return {
                next(): Promise<IteratorResult<ControlEnvelope>> {
                    if (queue.length > 0) {
                        return Promise.resolve({ value: queue.shift()!, done: false });
                    }
                    if (streamError) {
                        return Promise.reject(streamError);
                    }
                    if (ended) {
                        return Promise.resolve({ value: undefined, done: true });
                    }
                    return new Promise((resolve, reject) => {
                        waiters.push({ resolve, reject });
                    });
                },
            };
        },
    };
}

export class DaemonControlTransport implements ControlTransport {
    constructor(
        private readonly options: {
            host?: string;
            port?: number;
            socketPath?: string;
            platform?: NodeJS.Platform;
        } = {},
    ) {}

    private platform(): NodeJS.Platform {
        return this.options.platform || process.platform;
    }

    private socketPath(): string {
        if (this.options.socketPath) {
            return this.options.socketPath;
        }
        return path.join(getKestrelHome(), 'run', 'control.sock');
    }

    private host(): string {
        return this.options.host || DEFAULT_CONTROL_HOST;
    }

    private port(): number {
        return this.options.port || DEFAULT_CONTROL_PORT;
    }

    private async openConnection(): Promise<net.Socket> {
        return new Promise((resolve, reject) => {
            const socket =
                this.platform() === 'win32'
                    ? net.createConnection({ host: this.host(), port: this.port() })
                    : net.createConnection(this.socketPath());

            socket.once('connect', () => resolve(socket));
            socket.once('error', (error) => reject(error));
        });
    }

    async *stream(
        method: string,
        params: Record<string, any> = {},
    ): AsyncIterable<ControlEnvelope> {
        const socket = await this.openConnection();
        const requestId = crypto.randomUUID();
        const payload = JSON.stringify({
            request_id: requestId,
            method,
            params,
        });
        socket.write(`${payload}\n`);

        try {
            for await (const response of parseJsonLines(socket, requestId)) {
                if (!response.ok) {
                    throw new Error(response.error?.message || 'Daemon control request failed');
                }
                yield response;
                if (response.done) {
                    break;
                }
            }
        } finally {
            socket.end();
            socket.destroy();
        }
    }

    async request(method: string, params: Record<string, any> = {}): Promise<any> {
        for await (const response of this.stream(method, params)) {
            if (response.result !== undefined) {
                return response.result;
            }
            if (response.done) {
                return {};
            }
        }
        throw new Error(`No result received for daemon method ${method}`);
    }
}

type LocalUser = {
    id: string;
    email: string;
    displayName: string;
    passwordHash: string;
    createdAt: string;
    updatedAt: string;
};

type LocalWorkspace = {
    id: string;
    name: string;
    description: string;
    settings: Record<string, any>;
    createdAt: string;
    updatedAt: string;
};

type LocalMessage = {
    id: string;
    role: 'user' | 'assistant' | 'system';
    content: string;
    createdAt: string;
};

type LocalConversation = {
    id: string;
    userId: string;
    workspaceId: string;
    title: string;
    createdAt: string;
    updatedAt: string;
    messages: LocalMessage[];
};

type LocalProviderConfig = {
    workspaceId: string;
    provider: string;
    model: string;
    temperature: number;
    maxTokens: number;
    systemPrompt: string;
    ragEnabled: boolean;
    ragTopK: number;
    ragMinSimilarity: number;
    isDefault: boolean;
    settings: Record<string, any>;
    apiKey?: string;
    updatedAt: string;
};

type LocalNotification = {
    id: string;
    userId: string;
    type: string;
    title: string;
    body: string;
    source: string;
    data: Record<string, any>;
    read: boolean;
    createdAt: string;
};

type LocalInstalledTool = {
    id: string;
    workspaceId: string;
    name: string;
    description: string;
    serverUrl: string;
    transport: string;
    config: Record<string, any>;
    enabled: boolean;
    installedAt: string;
    updatedAt: string;
};

type LocalGatewayDocument = {
    version: 1;
    users: LocalUser[];
    workspaces: LocalWorkspace[];
    conversations: LocalConversation[];
    providerConfigs: LocalProviderConfig[];
    notifications: LocalNotification[];
    installedTools: LocalInstalledTool[];
};

function nowIso(): string {
    return new Date().toISOString();
}

function defaultDocument(): LocalGatewayDocument {
    return {
        version: 1,
        users: [],
        workspaces: [],
        conversations: [],
        providerConfigs: [],
        notifications: [],
        installedTools: [],
    };
}

function resolveToolCatalogPath(): string | null {
    const candidates = [
        process.env.KESTREL_TOOL_CATALOG,
        path.resolve(process.cwd(), 'packages', 'brain', '.kestrel', 'tool-catalog.json'),
        path.resolve(process.cwd(), '..', 'brain', '.kestrel', 'tool-catalog.json'),
        path.resolve(__dirname, '..', '..', '..', 'brain', '.kestrel', 'tool-catalog.json'),
    ].filter(Boolean) as string[];

    return candidates.find((candidate) => fs.existsSync(candidate)) || null;
}

function normalizeToolCatalog(): Array<{
    name: string;
    description: string;
    category: string;
    riskLevel: string;
    enabled: boolean;
}> {
    const catalogPath = resolveToolCatalogPath();
    if (!catalogPath) {
        return [];
    }

    try {
        const raw = JSON.parse(fs.readFileSync(catalogPath, 'utf-8')) as Array<Record<string, any>>;
        return raw.map((tool) => ({
            name: String(tool.name || ''),
            description: String(tool.description || tool.name || ''),
            category: String(tool.category || 'general'),
            riskLevel: String(tool.risk_level || tool.riskLevel || 'low'),
            enabled: Boolean(tool.available ?? tool.enabled ?? true),
        }));
    } catch (error: any) {
        logger.warn('Failed to parse local tool catalog', {
            catalogPath,
            error: error?.message,
        });
        return [];
    }
}

function hashPassword(password: string): string {
    const salt = crypto.randomBytes(16).toString('hex');
    const derived = crypto.scryptSync(password, salt, 64).toString('hex');
    return `${salt}:${derived}`;
}

function verifyPassword(password: string, encoded: string): boolean {
    const [salt, expected] = encoded.split(':');
    if (!salt || !expected) {
        return false;
    }
    const actual = crypto.scryptSync(password, salt, 64).toString('hex');
    return crypto.timingSafeEqual(Buffer.from(actual, 'hex'), Buffer.from(expected, 'hex'));
}

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

export function getLocalGatewayStateStore(): LocalGatewayStateStore {
    if (!sharedLocalStore) {
        sharedLocalStore = new LocalGatewayStateStore();
    }
    return sharedLocalStore;
}

let sharedControlTransport: ControlTransport | null = null;

export function getLocalControlTransport(): ControlTransport {
    if (!sharedControlTransport) {
        sharedControlTransport = new DaemonControlTransport();
    }
    return sharedControlTransport;
}

export function daemonTransportEndpoint(): { kind: 'unix' | 'tcp'; address: string } {
    if (process.platform === 'win32') {
        return {
            kind: 'tcp',
            address: `${DEFAULT_CONTROL_HOST}:${DEFAULT_CONTROL_PORT}`,
        };
    }
    return {
        kind: 'unix',
        address: path.join(getKestrelHome(), 'run', 'control.sock'),
    };
}
