import { ApiKey, ToolInfo, CapabilityItem } from '../../api/client';

export interface SettingsPanelProps {
    onClose: () => void;
    userEmail?: string;
    userDisplayName?: string;
    workspaceId: string;
}

export interface ProviderConfig {
    provider: string;
    model?: string;
    temperature?: number;
    maxTokens?: number;
    max_tokens?: number;
    systemPrompt?: string;
    system_prompt?: string;
    ragEnabled?: boolean;
    rag_enabled?: boolean;
    ragTopK?: number;
    rag_top_k?: number;
    ragMinSimilarity?: number;
    rag_min_similarity?: number;
    isDefault?: boolean;
    is_default?: boolean;
    apiKey?: string;
    apiKeyEncrypted?: string;
    api_key_encrypted?: string;
}

export type TabId = 'model' | 'persona' | 'memory' | 'tools' | 'agent' | 'capabilities' | 'integrations' | 'automation' | 'pr-reviews' | 'api-keys' | 'general' | 'profile';

export interface McpServer {
    id?: string;
    name: string;
    description: string;
    server_url: string;
    transport: string;
    enabled: boolean;
    installed_at?: string;
}
