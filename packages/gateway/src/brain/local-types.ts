import crypto from 'crypto';
import fs from 'fs';
import path from 'path';
import { getGatewayStateFile, getKestrelHome } from '../utils/paths';
import { logger } from '../utils/logger';

export type LocalUser = {
    id: string;
    email: string;
    displayName: string;
    passwordHash: string;
    createdAt: string;
    updatedAt: string;
};

export type LocalWorkspace = {
    id: string;
    name: string;
    description: string;
    settings: Record<string, any>;
    createdAt: string;
    updatedAt: string;
};

export type LocalMessage = {
    id: string;
    role: 'user' | 'assistant' | 'system';
    content: string;
    createdAt: string;
};

export type LocalConversation = {
    id: string;
    userId: string;
    workspaceId: string;
    title: string;
    createdAt: string;
    updatedAt: string;
    messages: LocalMessage[];
};

export type LocalProviderConfig = {
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

export type LocalNotification = {
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

export type LocalInstalledTool = {
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

export type LocalGatewayDocument = {
    version: 1;
    users: LocalUser[];
    workspaces: LocalWorkspace[];
    conversations: LocalConversation[];
    providerConfigs: LocalProviderConfig[];
    notifications: LocalNotification[];
    installedTools: LocalInstalledTool[];
};

export function nowIso(): string {
    return new Date().toISOString();
}

export function defaultDocument(): LocalGatewayDocument {
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

export function resolveToolCatalogPath(): string | null {
    const candidates = [
        process.env.KESTREL_TOOL_CATALOG,
        path.resolve(process.cwd(), 'packages', 'brain', '.kestrel', 'tool-catalog.json'),
        path.resolve(process.cwd(), '..', 'brain', '.kestrel', 'tool-catalog.json'),
        path.resolve(__dirname, '..', '..', '..', 'brain', '.kestrel', 'tool-catalog.json'),
    ].filter(Boolean) as string[];

    return candidates.find((candidate) => fs.existsSync(candidate)) || null;
}

export function normalizeToolCatalog(): Array<{
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

export function hashPassword(password: string): string {
    const salt = crypto.randomBytes(16).toString('hex');
    const derived = crypto.scryptSync(password, salt, 64).toString('hex');
    return `${salt}:${derived}`;
}

export function verifyPassword(password: string, encoded: string): boolean {
    const [salt, expected] = encoded.split(':');
    if (!salt || !expected) {
        return false;
    }
    const actual = crypto.scryptSync(password, salt, 64).toString('hex');
    return crypto.timingSafeEqual(Buffer.from(actual, 'hex'), Buffer.from(expected, 'hex'));
}
