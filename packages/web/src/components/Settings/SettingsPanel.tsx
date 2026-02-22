import { useState, useEffect, useCallback } from 'react';
import { createPortal } from 'react-dom';
import { providers, apiKeys, tools as toolsApi, workspaces, integrations, capabilities as capsApi, request, type ApiKey, type ToolInfo, type CapabilityItem } from '../../api/client';

/* ── Types ─────────────────────────────────────────────────────────── */

interface SettingsPanelProps {
    onClose: () => void;
    userEmail?: string;
    userDisplayName?: string;
    workspaceId: string;
}

interface ProviderConfig {
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

type TabId = 'model' | 'persona' | 'memory' | 'tools' | 'agent' | 'capabilities' | 'integrations' | 'automation' | 'pr-reviews' | 'api-keys' | 'general' | 'profile';

const KEY_MASKS = ['***', '••••••••••••'];
const isKeyMasked = (key: string) => KEY_MASKS.includes(key);
const DISPLAY_MASK = '••••••••••••';

const PROVIDER_META: Record<string, { name: string; icon: string; requiresKey: boolean }> = {
    local: { name: 'Local (llama.cpp)', icon: '⚡', requiresKey: false },
    openai: { name: 'OpenAI', icon: '◈', requiresKey: true },
    anthropic: { name: 'Anthropic', icon: '◆', requiresKey: true },
    google: { name: 'Google Gemini', icon: '◉', requiresKey: true },
};

const RISK_COLORS: Record<string, string> = {
    low: '#00ff9d',
    medium: '#00f3ff',
    high: '#ff9d00',
    critical: '#ff0055',
};

const CATEGORY_ICONS: Record<string, string> = {
    code: '⟨/⟩',
    web: '◎',
    file: '▤',
    memory: '⬡',
    data: '▦',
    control: '⊕',
    skill: '✦',
    general: '○',
};

/* ── Styles ────────────────────────────────────────────────────────── */

const S = {
    backdrop: {
        position: 'fixed' as const, inset: 0, zIndex: 9999,
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        background: 'rgba(0,0,0,0.7)', backdropFilter: 'blur(12px)',
        WebkitBackdropFilter: 'blur(12px)',
    },
    panel: {
        display: 'flex', width: '100%', maxWidth: 880, height: '85vh',
        background: '#0a0a0a', border: '1px solid rgba(255,255,255,0.06)',
        borderRadius: '12px', overflow: 'hidden',
        fontFamily: "'JetBrains Mono', monospace",
        animation: 'scaleIn 0.2s ease-out',
        boxShadow: '0 20px 60px rgba(0,0,0,0.6), 0 0 30px rgba(0, 243, 255, 0.03)',
    },
    nav: {
        width: 200, minWidth: 200, borderRight: '1px solid rgba(255,255,255,0.04)',
        display: 'flex', flexDirection: 'column' as const,
        background: '#080808', padding: '16px 0',
        backgroundImage: 'linear-gradient(180deg, rgba(0, 243, 255, 0.02) 0%, transparent 40%)',
    },
    navHeader: {
        padding: '0 16px 12px', fontSize: '0.7rem', fontWeight: 600,
        color: '#00f3ff', letterSpacing: '0.1em', textTransform: 'uppercase' as const,
        borderBottom: '1px solid rgba(0, 243, 255, 0.1)', marginBottom: '4px',
        textShadow: '0 0 10px rgba(0, 243, 255, 0.3)',
    },
    navSection: {
        padding: '8px 16px 4px', fontSize: '0.6rem', fontWeight: 600,
        color: '#333', letterSpacing: '0.1em', textTransform: 'uppercase' as const,
    },
    navItem: (active: boolean) => ({
        display: 'flex', alignItems: 'center', gap: '10px',
        padding: '9px 16px', cursor: 'pointer', fontSize: '0.78rem',
        color: active ? '#00f3ff' : '#888', fontWeight: active ? 600 : 400,
        background: active ? 'rgba(0,243,255,0.06)' : 'transparent',
        borderLeft: `2px solid ${active ? '#00f3ff' : 'transparent'}`,
        transition: 'all 0.15s', fontFamily: "'JetBrains Mono', monospace",
        border: 'none', borderRight: 'none', borderTop: 'none', borderBottom: 'none',
        borderLeftWidth: '2px', borderLeftStyle: 'solid' as const,
        borderLeftColor: active ? '#00f3ff' : 'transparent',
        textAlign: 'left' as const, width: '100%',
    }),
    content: {
        flex: 1, overflow: 'auto', padding: '24px 28px',
    },
    sectionTitle: {
        fontSize: '0.7rem', fontWeight: 600, color: '#555',
        letterSpacing: '0.08em', textTransform: 'uppercase' as const,
        marginBottom: '16px', paddingBottom: '8px',
        borderBottom: '1px solid #1a1a1a',
    },
    label: {
        fontSize: '0.75rem', fontWeight: 500, color: '#888',
        marginBottom: '6px', display: 'block',
    },
    input: {
        width: '100%', padding: '10px 12px', fontSize: '0.8rem',
        fontFamily: "'JetBrains Mono', monospace",
        background: '#111', border: '1px solid rgba(255,255,255,0.06)', borderRadius: '8px',
        color: '#e0e0e0', outline: 'none', transition: 'border-color 0.3s, box-shadow 0.3s',
    },
    textarea: {
        width: '100%', padding: '12px', fontSize: '0.8rem', minHeight: 160,
        fontFamily: "'JetBrains Mono', monospace",
        background: '#111', border: '1px solid #333', borderRadius: '4px',
        color: '#e0e0e0', outline: 'none', resize: 'vertical' as const,
        lineHeight: 1.5, transition: 'border-color 0.2s',
    },
    select: {
        width: '100%', padding: '10px 12px', fontSize: '0.8rem',
        fontFamily: "'JetBrains Mono', monospace",
        background: '#111', border: '1px solid #333', borderRadius: '4px',
        color: '#e0e0e0', outline: 'none', cursor: 'pointer',
        appearance: 'none' as const,
        backgroundImage: `url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%23888' stroke-width='2'%3E%3Cpath d='M6 9l6 6 6-6'/%3E%3C/svg%3E")`,
        backgroundRepeat: 'no-repeat', backgroundPosition: 'right 12px center',
    },
    field: {
        marginBottom: '20px',
    },
    providerCard: (active: boolean) => ({
        display: 'flex', alignItems: 'center', gap: '12px',
        padding: '12px 14px', cursor: 'pointer',
        background: active ? 'rgba(0,243,255,0.06)' : '#111',
        border: `1px solid ${active ? '#00f3ff' : '#222'}`,
        borderRadius: '4px', transition: 'all 0.15s',
    }),
    slider: {
        width: '100%', height: '4px', appearance: 'none' as const,
        background: '#333', borderRadius: '2px', outline: 'none',
        cursor: 'pointer',
    },
    toggle: (on: boolean) => ({
        width: 40, height: 22, borderRadius: '11px', position: 'relative' as const,
        background: on ? '#00f3ff' : '#333', cursor: 'pointer',
        transition: 'background 0.2s', border: 'none', padding: 0,
        display: 'inline-flex', alignItems: 'center', flexShrink: 0,
    }),
    toggleDot: (on: boolean) => ({
        width: 16, height: 16, borderRadius: '50%', background: '#0a0a0a',
        position: 'absolute' as const, top: 3,
        left: on ? 21 : 3, transition: 'left 0.2s',
    }),
    btnPrimary: {
        padding: '10px 20px', fontSize: '0.8rem', fontWeight: 600,
        fontFamily: "'JetBrains Mono', monospace",
        background: '#00f3ff', color: '#000', border: 'none',
        borderRadius: '4px', cursor: 'pointer', transition: 'opacity 0.15s',
    },
    btnGhost: {
        padding: '10px 20px', fontSize: '0.8rem',
        fontFamily: "'JetBrains Mono', monospace",
        background: 'transparent', color: '#888',
        border: '1px solid #333', borderRadius: '4px',
        cursor: 'pointer', transition: 'all 0.15s',
    },
    badge: {
        fontSize: '0.65rem', padding: '2px 8px', borderRadius: '3px',
        background: '#00f3ff', color: '#000', fontWeight: 700,
        letterSpacing: '0.05em',
    },
    successBox: {
        padding: '10px 14px', marginBottom: '16px', borderRadius: '4px',
        background: 'rgba(0,255,157,0.06)', border: '1px solid rgba(0,255,157,0.3)',
        color: '#00ff9d', fontSize: '0.8rem',
    },
    closeBtn: {
        position: 'absolute' as const, top: 12, right: 12,
        background: 'none', border: 'none', color: '#555', cursor: 'pointer',
        padding: '4px', fontSize: '1rem', lineHeight: 1,
    },
    toolCard: {
        display: 'flex', alignItems: 'center', gap: '12px',
        padding: '12px 14px', background: '#111', border: '1px solid #1a1a1a',
        borderRadius: '4px', marginBottom: '8px',
    },
};

/* ── Component ────────────────────────────────────────────────────── */

export function SettingsPanel({ onClose, userEmail, userDisplayName, workspaceId }: SettingsPanelProps) {
    const [activeTab, setActiveTab] = useState<TabId>('model');
    const [saveStatus, setSaveStatus] = useState<string | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [saving, setSaving] = useState(false);

    // Provider state
    const [providerConfigs, setProviderConfigs] = useState<ProviderConfig[]>([]);
    const [selectedProvider, setSelectedProvider] = useState('google');
    const [apiKeyInput, setApiKeyInput] = useState('');
    const [model, setModel] = useState('');
    const [temperature, setTemperature] = useState(0.7);
    const [maxTokens, setMaxTokens] = useState(4096);
    const [availableModels, setAvailableModels] = useState<any[]>([]);

    // Persona state
    const [systemPrompt, setSystemPrompt] = useState('');

    // Memory state
    const [ragEnabled, setRagEnabled] = useState(true);
    const [ragTopK, setRagTopK] = useState(5);
    const [ragMinSimilarity, setRagMinSimilarity] = useState(0.3);

    // Tools state
    const [toolsList, setToolsList] = useState<ToolInfo[]>([]);
    const [toolsLoading, setToolsLoading] = useState(false);
    const [disabledTools, setDisabledTools] = useState<Set<string>>(new Set());
    const [showCreateTool, setShowCreateTool] = useState(false);
    const [newToolName, setNewToolName] = useState('');
    const [newToolDesc, setNewToolDesc] = useState('');
    const [newToolCode, setNewToolCode] = useState('');

    // MCP Servers state
    interface McpServer {
        id?: string; name: string; description: string; server_url: string;
        transport: string; enabled: boolean; installed_at?: string;
    }
    const [mcpServers, setMcpServers] = useState<McpServer[]>([]);
    const [mcpLoading, setMcpLoading] = useState(false);
    const [showAddMcp, setShowAddMcp] = useState(false);
    const [mcpName, setMcpName] = useState('');
    const [mcpUrl, setMcpUrl] = useState('');
    const [mcpDesc, setMcpDesc] = useState('');
    const [mcpTransport, setMcpTransport] = useState<'stdio' | 'http' | 'sse'>('stdio');
    const [mcpSaving, setMcpSaving] = useState(false);
    const [mcpSearchQuery, setMcpSearchQuery] = useState('');
    const [mcpSearchResults, setMcpSearchResults] = useState<Array<{ name: string; description: string; transport: string; source: string; requires_env?: string[] }>>([]);
    const [mcpSearching, setMcpSearching] = useState(false);
    const [mcpConfiguring, setMcpConfiguring] = useState<string | null>(null); // server name being configured
    const [mcpEnvValues, setMcpEnvValues] = useState<Record<string, string>>({});

    // Agent/guardrails state
    const [maxIterations, setMaxIterations] = useState(25);
    const [maxToolCalls, setMaxToolCalls] = useState(50);
    const [maxWallTime, setMaxWallTime] = useState(600);
    const [autoApproveRisk, setAutoApproveRisk] = useState('medium');

    // Integration state
    const [telegramToken, setTelegramToken] = useState('');
    const [telegramEnabled, setTelegramEnabled] = useState(false);
    const [telegramStatus, setTelegramStatus] = useState<'disconnected' | 'connecting' | 'connected'>('disconnected');

    // API Keys state
    const [keysList, setKeysList] = useState<ApiKey[]>([]);
    const [keysLoading, setKeysLoading] = useState(false);
    const [newKeyName, setNewKeyName] = useState('');
    const [createdKey, setCreatedKey] = useState<{ key: string; name: string } | null>(null);

    // Profile state
    const [displayName, setDisplayName] = useState(userDisplayName || '');

    // Capabilities state
    const [capsList, setCapsList] = useState<CapabilityItem[]>([]);
    const [capsLoading, setCapsLoading] = useState(false);

    // Automation state
    const [cronInput, setCronInput] = useState('');
    const [cronLoading, setCronLoading] = useState(false);
    const [parsedCron, setParsedCron] = useState<{ cron: string; human_schedule: string; task: string } | null>(null);

    // PR Reviews state
    const [prAutoReview, setPrAutoReview] = useState(true);
    const [prPostComments, setPrPostComments] = useState(true);
    const [prAutoApprove, setPrAutoApprove] = useState(false);
    const [prSeverityFilter, setPrSeverityFilter] = useState('high');
    const [prRepo, setPrRepo] = useState('');

    /* ── Load configs ─────────────────────────────────────────────── */

    const loadConfigs = useCallback(async () => {
        try {
            const data: any = await providers.list(workspaceId);
            const configs: ProviderConfig[] = data?.configs || [];
            setProviderConfigs(configs);

            const defaultConfig = configs.find((c: ProviderConfig) =>
                c.isDefault || c.is_default
            ) || configs[0];

            if (defaultConfig) {
                setSelectedProvider(defaultConfig.provider);
                setModel(defaultConfig.model || '');
                setTemperature(defaultConfig.temperature ?? 0.7);
                setMaxTokens(defaultConfig.max_tokens ?? defaultConfig.maxTokens ?? 4096);
                setSystemPrompt(defaultConfig.system_prompt ?? defaultConfig.systemPrompt ?? '');
                setRagEnabled(defaultConfig.rag_enabled ?? defaultConfig.ragEnabled ?? true);
                setRagTopK(defaultConfig.rag_top_k ?? defaultConfig.ragTopK ?? 5);
                setRagMinSimilarity(defaultConfig.rag_min_similarity ?? defaultConfig.ragMinSimilarity ?? 0.3);

                const remoteKey = defaultConfig.apiKey || defaultConfig.apiKeyEncrypted || defaultConfig.api_key_encrypted;
                if (remoteKey && (remoteKey.startsWith('provider_key:') || isKeyMasked(remoteKey))) {
                    setApiKeyInput(DISPLAY_MASK);
                } else {
                    setApiKeyInput(remoteKey || '');
                }
            }
        } catch {
            setProviderConfigs([]);
        }
    }, [workspaceId]);

    useEffect(() => { loadConfigs(); }, [loadConfigs]);

    const handleProviderSelect = (providerKey: string) => {
        setSelectedProvider(providerKey);
        const config = providerConfigs.find(c => c.provider === providerKey);
        if (config) {
            setModel(config.model || '');
            setTemperature(config.temperature ?? 0.7);
            setMaxTokens(config.max_tokens ?? config.maxTokens ?? 4096);
            setSystemPrompt(config.system_prompt ?? config.systemPrompt ?? '');
            setRagEnabled(config.rag_enabled ?? config.ragEnabled ?? true);
            setRagTopK(config.rag_top_k ?? config.ragTopK ?? 5);
            setRagMinSimilarity(config.rag_min_similarity ?? config.ragMinSimilarity ?? 0.3);
            const remoteKey = config.apiKey || config.apiKeyEncrypted || config.api_key_encrypted;
            if (remoteKey && (remoteKey.startsWith('provider_key:') || isKeyMasked(remoteKey))) {
                setApiKeyInput(DISPLAY_MASK);
            } else {
                setApiKeyInput(remoteKey || '');
            }
        } else {
            setModel(''); setTemperature(0.7); setMaxTokens(4096); setApiKeyInput('');
        }
    };

    useEffect(() => {
        const timer = setTimeout(() => {
            const keyToSend = isKeyMasked(apiKeyInput) ? undefined : apiKeyInput;
            providers.listModels(workspaceId, selectedProvider, keyToSend)
                .then(models => setAvailableModels(models || []))
                .catch(() => setAvailableModels([]));
        }, 500);
        return () => clearTimeout(timer);
    }, [workspaceId, selectedProvider, apiKeyInput]);

    // Load tools
    useEffect(() => {
        if (activeTab !== 'tools') return;
        setToolsLoading(true);
        toolsApi.list(workspaceId)
            .then(data => setToolsList(data?.tools || []))
            .catch(() => setToolsList([]))
            .finally(() => setToolsLoading(false));
    }, [activeTab, workspaceId]);

    // Load MCP servers when tools tab opens
    useEffect(() => {
        if (activeTab !== 'tools') return;
        setMcpLoading(true);
        request(`/workspaces/${workspaceId}/mcp-tools`)
            .then((d: any) => setMcpServers(d?.tools || []))
            .catch(() => setMcpServers([]))
            .finally(() => setMcpLoading(false));
    }, [activeTab, workspaceId]);

    // Load API keys
    useEffect(() => {
        if (activeTab !== 'api-keys') return;
        setKeysLoading(true);
        apiKeys.list()
            .then((data: any) => setKeysList(data?.keys || []))
            .catch(() => setKeysList([]))
            .finally(() => setKeysLoading(false));
    }, [activeTab]);

    // Load workspace settings (guardrails)
    useEffect(() => {
        workspaces.get(workspaceId)
            .then((data: any) => {
                const ws = data?.workspace || data;
                const settings = ws?.settings || {};
                if (settings.guardrails) {
                    setMaxIterations(settings.guardrails.maxIterations ?? 25);
                    setMaxToolCalls(settings.guardrails.maxToolCalls ?? 50);
                    setMaxWallTime(settings.guardrails.maxWallTime ?? 600);
                    setAutoApproveRisk(settings.guardrails.autoApproveRisk ?? 'medium');
                }
                if (settings.disabledTools) {
                    setDisabledTools(new Set(settings.disabledTools));
                }
            })
            .catch(() => { });

        // Load real integration status
        integrations.status(workspaceId)
            .then((data: any) => {
                if (data?.telegram) {
                    setTelegramStatus(data.telegram.status as any);
                    setTelegramEnabled(data.telegram.connected);
                }
            })
            .catch(() => { });
    }, [workspaceId]);

    // Load capabilities when capabilities tab is active
    useEffect(() => {
        if (activeTab !== 'capabilities') return;
        setCapsLoading(true);
        capsApi.get(workspaceId)
            .then(setCapsList)
            .catch(() => setCapsList([]))
            .finally(() => setCapsLoading(false));
    }, [activeTab, workspaceId]);

    /* ── Save ─────────────────────────────────────────────────────── */

    const handleSave = async () => {
        setSaving(true);
        setError(null);
        setSaveStatus(null);
        try {
            // Save provider config
            const payload: Record<string, unknown> = {
                model, temperature, maxTokens, systemPrompt,
                ragEnabled, ragTopK, ragMinSimilarity, isDefault: true,
            };
            if (apiKeyInput && !isKeyMasked(apiKeyInput)) {
                payload.apiKey = apiKeyInput;
            }
            await providers.set(workspaceId, selectedProvider, payload);

            // Save agent guardrails as workspace settings
            await workspaces.update(workspaceId, {
                settings: {
                    guardrails: {
                        maxIterations,
                        maxToolCalls,
                        maxWallTime,
                        autoApproveRisk,
                    },
                },
            });

            // Connect/disconnect Telegram if token was changed
            if (telegramEnabled && telegramToken && !isKeyMasked(telegramToken)) {
                setTelegramStatus('connecting');
                try {
                    const result = await integrations.connectTelegram(workspaceId, telegramToken, true);
                    setTelegramStatus(result.status === 'connected' ? 'connected' : 'disconnected');
                } catch (telegramErr: any) {
                    setTelegramStatus('disconnected');
                    setError(telegramErr.message || 'Failed to connect Telegram');
                    return;
                }
            } else if (!telegramEnabled) {
                try {
                    await integrations.disconnectTelegram(workspaceId);
                    setTelegramStatus('disconnected');
                } catch { /* ignore */ }
            }

            setSaveStatus('Configuration saved');
            setTimeout(() => setSaveStatus(null), 3000);
            await loadConfigs();
        } catch (err: any) {
            setError(err.message || 'Failed to save');
        } finally {
            setSaving(false);
        }
    };

    const handleCreateKey = async () => {
        if (!newKeyName.trim()) return;
        try {
            const result = await apiKeys.create(newKeyName.trim());
            setCreatedKey({ key: result.key, name: result.name });
            setNewKeyName('');
            const data: any = await apiKeys.list();
            setKeysList(data?.keys || []);
        } catch (err: any) {
            setError(err.message || 'Failed to create key');
        }
    };

    const handleRevokeKey = async (id: string) => {
        try {
            await apiKeys.revoke(id);
            setKeysList(prev => prev.filter(k => k.id !== id));
        } catch (err: any) {
            setError(err.message || 'Failed to revoke key');
        }
    };

    const toggleTool = (toolName: string) => {
        setDisabledTools(prev => {
            const next = new Set(prev);
            if (next.has(toolName)) next.delete(toolName);
            else next.add(toolName);
            return next;
        });
    };

    /* ── Tabs config ──────────────────────────────────────────────── */

    const tabs: { id: TabId; label: string; icon: string; section?: string }[] = [
        { id: 'model', label: 'AI Model', icon: '◈', section: 'KESTREL' },
        { id: 'persona', label: 'Persona', icon: '◎' },
        { id: 'memory', label: 'Memory', icon: '⬡' },
        { id: 'tools', label: 'Tools', icon: '⟐' },
        { id: 'agent', label: 'Agent', icon: '⊛' },
        { id: 'capabilities', label: 'Capabilities', icon: '⚙' },
        { id: 'automation', label: 'Automation', icon: '⚡' },
        { id: 'pr-reviews', label: 'PR Reviews', icon: '⊘' },
        { id: 'integrations', label: 'Integrations', icon: '⊞', section: 'CONNECT' },
        { id: 'api-keys', label: 'API Keys', icon: '⟐' },
        { id: 'general', label: 'Workspace', icon: '⊞', section: 'ACCOUNT' },
        { id: 'profile', label: 'Profile', icon: '⊙' },
    ];

    /* ── Render helpers ───────────────────────────────────────────── */

    const Toggle = ({ value, onChange }: { value: boolean; onChange: (v: boolean) => void }) => (
        <button style={S.toggle(value)} onClick={() => onChange(!value)} type="button">
            <span style={S.toggleDot(value)} />
        </button>
    );

    const SliderField = ({ label, value, onChange, min, max, step, format }: {
        label: string; value: number; onChange: (v: number) => void;
        min: number; max: number; step: number; format?: (v: number) => string;
    }) => (
        <div style={S.field}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' }}>
                <label style={S.label}>{label}</label>
                <span style={{ fontSize: '0.8rem', color: '#00f3ff', fontWeight: 600, fontFamily: "'JetBrains Mono', monospace" }}>
                    {format ? format(value) : value}
                </span>
            </div>
            <input
                type="range" min={min} max={max} step={step} value={value}
                onChange={e => onChange(parseFloat(e.target.value))} style={S.slider}
            />
            <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: '4px' }}>
                <span style={{ fontSize: '0.65rem', color: '#444' }}>{min}</span>
                <span style={{ fontSize: '0.65rem', color: '#444' }}>{max}</span>
            </div>
        </div>
    );

    const RiskBadge = ({ level }: { level: string }) => (
        <span style={{
            fontSize: '0.6rem', padding: '2px 6px', borderRadius: '3px',
            border: `1px solid ${RISK_COLORS[level] || '#555'}`,
            color: RISK_COLORS[level] || '#555', fontWeight: 600,
            textTransform: 'uppercase' as const, letterSpacing: '0.05em',
        }}>
            {level}
        </span>
    );

    /* ── Tab content ──────────────────────────────────────────────── */

    const renderContent = () => {
        switch (activeTab) {
            case 'model':
                return (
                    <div>
                        <div style={S.sectionTitle}>// PROVIDER</div>
                        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '10px', marginBottom: '24px' }}>
                            {Object.entries(PROVIDER_META).map(([key, meta]) => {
                                const active = selectedProvider === key;
                                const config = providerConfigs.find(c => c.provider === key);
                                const isDefault = config?.isDefault || config?.is_default;
                                return (
                                    <div key={key} style={S.providerCard(active)} onClick={() => handleProviderSelect(key)}>
                                        <span style={{ fontSize: '1.1rem', color: active ? '#00f3ff' : '#555' }}>{meta.icon}</span>
                                        <div style={{ flex: 1 }}>
                                            <div style={{ fontSize: '0.8rem', fontWeight: 500, color: active ? '#e0e0e0' : '#888' }}>{meta.name}</div>
                                            <div style={{ fontSize: '0.65rem', color: '#444', marginTop: '2px' }}>{meta.requiresKey ? 'Cloud API' : 'On-device'}</div>
                                        </div>
                                        {isDefault && <span style={S.badge}>DEFAULT</span>}
                                    </div>
                                );
                            })}
                        </div>

                        {PROVIDER_META[selectedProvider]?.requiresKey && (
                            <div>
                                <div style={S.sectionTitle}>// API KEY</div>
                                <div style={S.field}>
                                    <input style={S.input} type="password" value={apiKeyInput}
                                        onChange={e => setApiKeyInput(e.target.value)}
                                        placeholder={`Enter ${PROVIDER_META[selectedProvider]?.name} API key`}
                                        onFocus={e => { e.target.style.borderColor = '#00f3ff'; }}
                                        onBlur={e => { e.target.style.borderColor = '#333'; }}
                                    />
                                </div>
                            </div>
                        )}

                        <div style={S.sectionTitle}>// MODEL</div>
                        <div style={S.field}>
                            <select style={S.select} value={model} onChange={e => setModel(e.target.value)}>
                                <option value="">— Select model —</option>
                                {availableModels.map(m => (
                                    <option key={m.id} value={m.id}>{m.name || m.id}</option>
                                ))}
                            </select>
                            {model && (
                                <div style={{ fontSize: '0.7rem', color: '#555', marginTop: '6px' }}>
                                    Active: <span style={{ color: '#00ff9d' }}>{model}</span>
                                </div>
                            )}
                        </div>

                        <div style={S.sectionTitle}>// PARAMETERS</div>
                        <SliderField label="Temperature" value={temperature} onChange={setTemperature}
                            min={0} max={2} step={0.1} format={v => v.toFixed(1)} />
                        <div style={S.field}>
                            <label style={S.label}>Max Tokens</label>
                            <input style={S.input} type="number" value={maxTokens}
                                onChange={e => setMaxTokens(Math.max(1, Math.min(32768, parseInt(e.target.value) || 1)))}
                                min={1} max={32768}
                                onFocus={e => { e.target.style.borderColor = '#00f3ff'; }}
                                onBlur={e => { e.target.style.borderColor = '#333'; }} />
                            <div style={{ fontSize: '0.65rem', color: '#444', marginTop: '4px' }}>Range: 1 – 32,768</div>
                        </div>
                    </div>
                );

            case 'persona':
                return (
                    <div>
                        <div style={S.sectionTitle}>// SYSTEM PROMPT</div>
                        <p style={{ fontSize: '0.75rem', color: '#666', marginBottom: '16px', lineHeight: 1.5 }}>
                            Define Kestrel's personality, instructions, and behavior. Leave empty for the default autonomous agent persona.
                        </p>
                        <div style={S.field}>
                            <textarea style={S.textarea} value={systemPrompt}
                                onChange={e => setSystemPrompt(e.target.value)}
                                placeholder={`You are Kestrel, an autonomous AI agent...\n\nCustomize tone, role, domain expertise, or specific instructions here.`}
                                onFocus={e => { e.target.style.borderColor = '#00f3ff'; }}
                                onBlur={e => { e.target.style.borderColor = '#333'; }} />
                            <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: '6px' }}>
                                <span style={{ fontSize: '0.65rem', color: '#444' }}>{systemPrompt.length} characters</span>
                                {systemPrompt && (
                                    <button style={{ ...S.btnGhost, padding: '4px 10px', fontSize: '0.7rem' }}
                                        onClick={() => setSystemPrompt('')}>Reset to default</button>
                                )}
                            </div>
                        </div>
                    </div>
                );

            case 'memory':
                return (
                    <div>
                        <div style={S.sectionTitle}>// RAG RETRIEVAL</div>
                        <p style={{ fontSize: '0.75rem', color: '#666', marginBottom: '20px', lineHeight: 1.5 }}>
                            Control how Kestrel retrieves context from conversation memory and workspace knowledge.
                        </p>
                        <div style={{ ...S.field, display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                            <div>
                                <label style={{ ...S.label, marginBottom: 0 }}>Enable Memory Retrieval</label>
                                <div style={{ fontSize: '0.7rem', color: '#444', marginTop: '4px' }}>Augment responses with relevant past context</div>
                            </div>
                            <Toggle value={ragEnabled} onChange={setRagEnabled} />
                        </div>
                        {ragEnabled && (
                            <div style={{ borderTop: '1px solid #1a1a1a', paddingTop: '20px', marginTop: '4px' }}>
                                <SliderField label="Retrieval Count (top-k)" value={ragTopK}
                                    onChange={v => setRagTopK(Math.round(v))} min={1} max={20} step={1}
                                    format={v => `${v} chunks`} />
                                <SliderField label="Similarity Threshold" value={ragMinSimilarity}
                                    onChange={setRagMinSimilarity} min={0} max={1} step={0.05}
                                    format={v => `${(v * 100).toFixed(0)}%`} />
                                <div style={{ padding: '12px', background: '#0d0d0d', borderRadius: '4px', border: '1px solid #1a1a1a' }}>
                                    <div style={{ fontSize: '0.7rem', color: '#666', lineHeight: 1.6 }}>
                                        <span style={{ color: '#00f3ff' }}>top-k = {ragTopK}</span> — chunks retrieved per query<br />
                                        <span style={{ color: '#00f3ff' }}>threshold = {(ragMinSimilarity * 100).toFixed(0)}%</span> — minimum similarity
                                    </div>
                                </div>
                            </div>
                        )}
                    </div>
                );

            case 'tools':
                return (
                    <div>
                        <div style={S.sectionTitle}>// TOOL REGISTRY</div>
                        <p style={{ fontSize: '0.75rem', color: '#666', marginBottom: '12px', lineHeight: 1.5 }}>
                            Manage the tools Kestrel can use. Disable tools to restrict capabilities.
                            Kestrel can also create new tools on the fly (with your approval).
                        </p>

                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
                            <span style={{ fontSize: '0.7rem', color: '#444' }}>
                                {toolsList.length} tools registered · {disabledTools.size} disabled
                            </span>
                            <button style={{ ...S.btnGhost, padding: '6px 14px', fontSize: '0.7rem' }}
                                onClick={() => setShowCreateTool(!showCreateTool)}>
                                {showCreateTool ? '✕ Cancel' : '+ Create Tool'}
                            </button>
                        </div>

                        {showCreateTool && (
                            <div style={{
                                padding: '16px', marginBottom: '16px', background: '#0d0d0d',
                                border: '1px solid #1a1a1a', borderRadius: '4px',
                            }}>
                                <div style={{ ...S.sectionTitle, marginBottom: '12px' }}>// NEW TOOL</div>
                                <div style={S.field}>
                                    <label style={S.label}>Tool Name</label>
                                    <input style={S.input} value={newToolName}
                                        onChange={e => setNewToolName(e.target.value)}
                                        placeholder="e.g. calculate_average"
                                        onFocus={e => { e.target.style.borderColor = '#00f3ff'; }}
                                        onBlur={e => { e.target.style.borderColor = '#333'; }} />
                                </div>
                                <div style={S.field}>
                                    <label style={S.label}>Description</label>
                                    <input style={S.input} value={newToolDesc}
                                        onChange={e => setNewToolDesc(e.target.value)}
                                        placeholder="What does this tool do?"
                                        onFocus={e => { e.target.style.borderColor = '#00f3ff'; }}
                                        onBlur={e => { e.target.style.borderColor = '#333'; }} />
                                </div>
                                <div style={S.field}>
                                    <label style={S.label}>Python Code</label>
                                    <textarea style={{ ...S.textarea, minHeight: 120, fontSize: '0.75rem' }}
                                        value={newToolCode}
                                        onChange={e => setNewToolCode(e.target.value)}
                                        placeholder={`def run(args):\n    # args is a dict of parameters\n    result = args['numbers']\n    return sum(result) / len(result)`}
                                        onFocus={e => { e.target.style.borderColor = '#00f3ff'; }}
                                        onBlur={e => { e.target.style.borderColor = '#333'; }} />
                                </div>
                                <div style={{ display: 'flex', gap: '8px' }}>
                                    <button style={S.btnPrimary} onClick={async () => {
                                        if (!newToolName.trim()) return;
                                        try {
                                            const goalParts = [`Create a new Python skill/tool named "${newToolName}"`];
                                            if (newToolDesc.trim()) goalParts.push(`that ${newToolDesc}`);
                                            if (newToolCode.trim()) goalParts.push(`using this implementation:\n\n\`\`\`python\n${newToolCode}\n\`\`\``);
                                            await request(`/workspaces/${workspaceId}/tasks`, {
                                                method: 'POST',
                                                body: { goal: goalParts.join(' ') },
                                            });
                                            setShowCreateTool(false);
                                            setNewToolName(''); setNewToolDesc(''); setNewToolCode('');
                                            setSaveStatus('Tool creation task dispatched — check Tasks panel');
                                            setTimeout(() => setSaveStatus(null), 4000);
                                        } catch (err: any) {
                                            setError(err.message || 'Failed to dispatch tool creation');
                                            setTimeout(() => setError(null), 3000);
                                        }
                                    }}>Create Tool</button>
                                    <div style={{ fontSize: '0.65rem', color: '#666', display: 'flex', alignItems: 'center' }}>
                                        Requires approval before use
                                    </div>
                                </div>
                            </div>
                        )}

                        {toolsLoading ? (
                            <div style={{ textAlign: 'center', color: '#444', padding: '24px', fontSize: '0.8rem' }}>
                                Loading tools...
                            </div>
                        ) : (
                            <div>
                                {toolsList.map(tool => {
                                    const isDisabled = disabledTools.has(tool.name);
                                    const isSystem = ['task_complete', 'ask_human'].includes(tool.name);
                                    return (
                                        <div key={tool.name} style={{
                                            ...S.toolCard,
                                            opacity: isDisabled ? 0.4 : 1,
                                            transition: 'opacity 0.2s',
                                        }}>
                                            <span style={{ fontSize: '1rem', width: '24px', textAlign: 'center', color: '#555' }}>
                                                {CATEGORY_ICONS[tool.category] || '○'}
                                            </span>
                                            <div style={{ flex: 1 }}>
                                                <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '4px' }}>
                                                    <span style={{ fontSize: '0.8rem', fontWeight: 500, color: '#e0e0e0' }}>{tool.name}</span>
                                                    <RiskBadge level={tool.riskLevel} />
                                                    <span style={{
                                                        fontSize: '0.6rem', padding: '1px 6px', borderRadius: '3px',
                                                        background: '#1a1a1a', color: '#555',
                                                    }}>{tool.category}</span>
                                                </div>
                                                <div style={{ fontSize: '0.7rem', color: '#666' }}>{tool.description}</div>
                                            </div>
                                            {!isSystem && (
                                                <Toggle value={!isDisabled} onChange={() => toggleTool(tool.name)} />
                                            )}
                                        </div>
                                    );
                                })}
                            </div>
                        )}

                        {/* ── MCP Servers ─────────────────────────────── */}
                        <div style={{ marginTop: '32px', borderTop: '1px solid #1a1a1a', paddingTop: '24px' }}>
                            <div style={S.sectionTitle}>// MCP SERVERS</div>
                            <p style={{ fontSize: '0.75rem', color: '#666', marginBottom: '12px', lineHeight: 1.5 }}>
                                Connect external MCP (Model Context Protocol) servers to give Kestrel new capabilities.
                                Servers can provide file access, database queries, API integrations, and more.
                            </p>

                            {/* Search Bar */}
                            <div style={{ marginBottom: '16px' }}>
                                <div style={{ display: 'flex', gap: '8px', marginBottom: '8px' }}>
                                    <input
                                        style={{ ...S.input, flex: 1, borderColor: mcpSearching ? '#a855f7' : '#333' }}
                                        value={mcpSearchQuery}
                                        onChange={e => setMcpSearchQuery(e.target.value)}
                                        placeholder="Search MCP servers...  e.g. github, database, slack"
                                        onKeyDown={async e => {
                                            if (e.key === 'Enter' && mcpSearchQuery.trim()) {
                                                setMcpSearching(true);
                                                try {
                                                    const d = (await request(`/mcp/search?q=${encodeURIComponent(mcpSearchQuery)}`)) as {
                                                        results?: Array<{ name: string; description: string; transport: string; source: string }>;
                                                    };
                                                    setMcpSearchResults(d?.results || []);
                                                } catch { setMcpSearchResults([]); }
                                                finally { setMcpSearching(false); }
                                            }
                                        }}
                                        onFocus={e => { e.target.style.borderColor = '#a855f7'; }}
                                        onBlur={e => { if (!mcpSearching) e.target.style.borderColor = '#333'; }}
                                    />
                                    <button
                                        style={{ ...S.btnPrimary, background: '#a855f7', padding: '8px 16px', fontSize: '0.75rem', opacity: mcpSearching ? 0.5 : 1 }}
                                        disabled={mcpSearching || !mcpSearchQuery.trim()}
                                        onClick={async () => {
                                            if (!mcpSearchQuery.trim()) return;
                                            setMcpSearching(true);
                                            try {
                                                const d = (await request(`/mcp/search?q=${encodeURIComponent(mcpSearchQuery)}`)) as {
                                                    results?: Array<{ name: string; description: string; transport: string; source: string }>;
                                                };
                                                setMcpSearchResults(d?.results || []);
                                            } catch { setMcpSearchResults([]); }
                                            finally { setMcpSearching(false); }
                                        }}
                                    >
                                        {mcpSearching ? '...' : '🔍'}
                                    </button>
                                </div>

                                {/* Marketplace Link */}
                                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                                    <span style={{ fontSize: '0.65rem', color: '#555' }}>Searches official MCP catalog + Smithery registry</span>
                                    <a
                                        href="https://smithery.ai"
                                        target="_blank"
                                        rel="noopener noreferrer"
                                        style={{
                                            fontSize: '0.68rem', color: '#a855f7', textDecoration: 'none',
                                            fontFamily: "'JetBrains Mono', monospace",
                                            borderBottom: '1px dotted #a855f7',
                                        }}
                                    >
                                        Browse Marketplace →
                                    </a>
                                </div>

                                {/* Search Results */}
                                {mcpSearchResults.length > 0 && (
                                    <div style={{ marginTop: '12px', maxHeight: '280px', overflowY: 'auto' }}>
                                        {mcpSearchResults.map(r => {
                                            const isInstalled = mcpServers.some(s => s.name === r.name);
                                            const needsConfig = r.requires_env && r.requires_env.length > 0;
                                            const isConfiguring = mcpConfiguring === r.name;

                                            const doInstall = async (envVars?: Record<string, string>) => {
                                                try {
                                                    await request(`/workspaces/${workspaceId}/mcp-tools`, {
                                                        method: 'POST',
                                                        body: {
                                                            name: r.name,
                                                            description: r.description,
                                                            serverUrl: `npx -y ${r.name}`,
                                                            transport: r.transport || 'stdio',
                                                            config: envVars ? { env: envVars } : {},
                                                        },
                                                    });
                                                    setMcpServers(prev => [...prev, {
                                                        name: r.name, description: r.description,
                                                        server_url: `npx -y ${r.name}`,
                                                        transport: r.transport || 'stdio', enabled: true,
                                                    }]);
                                                    setMcpConfiguring(null);
                                                    setMcpEnvValues({});
                                                    setSaveStatus(`Installed ${r.name}`);
                                                    setTimeout(() => setSaveStatus(null), 2000);
                                                } catch {
                                                    setError('Failed to install');
                                                    setTimeout(() => setError(null), 3000);
                                                }
                                            };

                                            return (
                                                <div key={r.name} style={{
                                                    padding: '10px 12px', background: '#0d0d0d',
                                                    border: isConfiguring ? '1px solid #a855f7' : '1px solid #1a1a1a',
                                                    borderRadius: '4px', marginBottom: '6px',
                                                    opacity: isInstalled ? 0.5 : 1,
                                                    transition: 'border-color 0.2s',
                                                }}>
                                                    <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                                                        <span style={{ fontSize: '0.85rem', color: r.source === 'official' ? '#00f3ff' : '#a855f7', flexShrink: 0 }}>
                                                            {r.source === 'official' ? '★' : '◆'}
                                                        </span>
                                                        <div style={{ flex: 1, minWidth: 0 }}>
                                                            <div style={{ fontSize: '0.75rem', fontWeight: 500, color: '#e0e0e0', marginBottom: '2px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                                                {r.name}
                                                                {needsConfig && <span style={{ color: '#f59e0b', fontSize: '0.6rem', marginLeft: '6px' }}>🔑 requires config</span>}
                                                            </div>
                                                            <div style={{ fontSize: '0.65rem', color: '#666', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                                                {r.description}
                                                            </div>
                                                        </div>
                                                        <button
                                                            style={{
                                                                ...S.btnGhost, padding: '4px 12px', fontSize: '0.65rem',
                                                                color: isInstalled ? '#555' : isConfiguring ? '#f59e0b' : '#10b981',
                                                                borderColor: isInstalled ? '#333' : isConfiguring ? '#f59e0b' : '#10b981',
                                                            }}
                                                            disabled={isInstalled}
                                                            onClick={() => {
                                                                if (needsConfig && !isConfiguring) {
                                                                    setMcpConfiguring(r.name);
                                                                    setMcpEnvValues({});
                                                                } else if (!needsConfig) {
                                                                    doInstall();
                                                                }
                                                            }}
                                                        >
                                                            {isInstalled ? 'Installed' : isConfiguring ? '▾ Configure' : needsConfig ? '🔑 Configure' : '+ Install'}
                                                        </button>
                                                    </div>

                                                    {/* Env var configuration form */}
                                                    {isConfiguring && r.requires_env && (
                                                        <div style={{
                                                            marginTop: '10px', padding: '10px 12px',
                                                            background: '#111', borderRadius: '4px',
                                                            border: '1px solid #a855f733',
                                                        }}>
                                                            <div style={{ fontSize: '0.7rem', color: '#a855f7', fontWeight: 600, marginBottom: '8px' }}>
                                                                🔑 Required Configuration
                                                            </div>
                                                            {r.requires_env.map(envKey => (
                                                                <div key={envKey} style={{ marginBottom: '8px' }}>
                                                                    <label style={{ fontSize: '0.65rem', color: '#888', display: 'block', marginBottom: '3px' }}>
                                                                        {envKey}
                                                                    </label>
                                                                    <input
                                                                        style={{ ...S.input, fontSize: '0.75rem' }}
                                                                        type={envKey.toLowerCase().includes('token') || envKey.toLowerCase().includes('key') || envKey.toLowerCase().includes('secret') ? 'password' : 'text'}
                                                                        placeholder={`Enter ${envKey}...`}
                                                                        value={mcpEnvValues[envKey] || ''}
                                                                        onChange={e => setMcpEnvValues(prev => ({ ...prev, [envKey]: e.target.value }))}
                                                                    />
                                                                </div>
                                                            ))}
                                                            <div style={{ display: 'flex', gap: '8px', marginTop: '10px' }}>
                                                                <button
                                                                    style={{
                                                                        ...S.btnPrimary, padding: '6px 16px', fontSize: '0.7rem',
                                                                        background: '#a855f7',
                                                                        opacity: r.requires_env.every(k => mcpEnvValues[k]?.trim()) ? 1 : 0.4,
                                                                    }}
                                                                    disabled={!r.requires_env.every(k => mcpEnvValues[k]?.trim())}
                                                                    onClick={() => doInstall(mcpEnvValues)}
                                                                >
                                                                    ✓ Install with Config
                                                                </button>
                                                                <button
                                                                    style={{ ...S.btnGhost, padding: '6px 12px', fontSize: '0.7rem' }}
                                                                    onClick={() => { setMcpConfiguring(null); setMcpEnvValues({}); }}
                                                                >
                                                                    Cancel
                                                                </button>
                                                            </div>
                                                        </div>
                                                    )}
                                                </div>
                                            );
                                        })}
                                    </div>
                                )}
                            </div>

                            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
                                <span style={{ fontSize: '0.7rem', color: '#444' }}>
                                    {mcpServers.length} servers connected
                                </span>
                                <button style={{ ...S.btnGhost, padding: '6px 14px', fontSize: '0.7rem' }}
                                    onClick={() => {
                                        setShowAddMcp(!showAddMcp);
                                        if (!mcpLoading && mcpServers.length === 0) {
                                            setMcpLoading(true);
                                            request(`/workspaces/${workspaceId}/mcp-tools`)
                                                .then((d: any) => setMcpServers(d?.tools || []))
                                                .catch(() => setMcpServers([]))
                                                .finally(() => setMcpLoading(false));
                                        }
                                    }}>
                                    {showAddMcp ? '✕ Cancel' : '+ Add MCP Server'}
                                </button>
                            </div>

                            {showAddMcp && (
                                <div style={{
                                    padding: '16px', marginBottom: '16px', background: '#0d0d0d',
                                    border: '1px solid #1a1a1a', borderRadius: '4px',
                                }}>
                                    <div style={{ ...S.sectionTitle, marginBottom: '12px' }}>// ADD MCP SERVER</div>
                                    <div style={S.field}>
                                        <label style={S.label}>Server Name</label>
                                        <input style={S.input} value={mcpName}
                                            onChange={e => setMcpName(e.target.value)}
                                            placeholder="e.g. filesystem, github, postgres"
                                            onFocus={e => { e.target.style.borderColor = '#a855f7'; }}
                                            onBlur={e => { e.target.style.borderColor = '#333'; }} />
                                    </div>
                                    <div style={S.field}>
                                        <label style={S.label}>Server URL / Command</label>
                                        <input style={S.input} value={mcpUrl}
                                            onChange={e => setMcpUrl(e.target.value)}
                                            placeholder="npx -y @modelcontextprotocol/server-filesystem /path"
                                            onFocus={e => { e.target.style.borderColor = '#a855f7'; }}
                                            onBlur={e => { e.target.style.borderColor = '#333'; }} />
                                    </div>
                                    <div style={S.field}>
                                        <label style={S.label}>Description (optional)</label>
                                        <input style={S.input} value={mcpDesc}
                                            onChange={e => setMcpDesc(e.target.value)}
                                            placeholder="What does this server provide?"
                                            onFocus={e => { e.target.style.borderColor = '#a855f7'; }}
                                            onBlur={e => { e.target.style.borderColor = '#333'; }} />
                                    </div>
                                    <div style={S.field}>
                                        <label style={S.label}>Transport</label>
                                        <select style={S.select} value={mcpTransport}
                                            onChange={e => setMcpTransport(e.target.value as 'stdio' | 'http' | 'sse')}>
                                            <option value="stdio">stdio (local process)</option>
                                            <option value="http">HTTP (remote)</option>
                                            <option value="sse">SSE (server-sent events)</option>
                                        </select>
                                    </div>
                                    <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
                                        <button
                                            style={{ ...S.btnPrimary, background: '#a855f7', opacity: mcpSaving ? 0.5 : 1 }}
                                            disabled={mcpSaving || !mcpName || !mcpUrl}
                                            onClick={async () => {
                                                setMcpSaving(true);
                                                try {
                                                    await request(`/workspaces/${workspaceId}/mcp-tools`, {
                                                        method: 'POST',
                                                        body: {
                                                            name: mcpName,
                                                            description: mcpDesc,
                                                            serverUrl: mcpUrl,
                                                            transport: mcpTransport,
                                                        },
                                                    });
                                                    setMcpServers(prev => [...prev, {
                                                        name: mcpName, description: mcpDesc,
                                                        server_url: mcpUrl, transport: mcpTransport, enabled: true,
                                                    }]);
                                                    setMcpName(''); setMcpUrl(''); setMcpDesc('');
                                                    setMcpTransport('stdio'); setShowAddMcp(false);
                                                    setSaveStatus('MCP server added');
                                                    setTimeout(() => setSaveStatus(null), 2000);
                                                } catch {
                                                    setError('Failed to add MCP server');
                                                    setTimeout(() => setError(null), 3000);
                                                } finally { setMcpSaving(false); }
                                            }}
                                        >
                                            {mcpSaving ? 'Adding...' : 'Add Server'}
                                        </button>
                                        <span style={{ fontSize: '0.65rem', color: '#666' }}>
                                            Kestrel will connect on next message
                                        </span>
                                    </div>
                                </div>
                            )}

                            {/* Installed MCP server list */}
                            {mcpLoading ? (
                                <div style={{ textAlign: 'center', color: '#444', padding: '16px', fontSize: '0.78rem' }}>
                                    Loading servers...
                                </div>
                            ) : mcpServers.length > 0 ? (
                                <div>
                                    {mcpServers.map(srv => (
                                        <div key={srv.name} style={{
                                            ...S.toolCard,
                                            borderLeft: `2px solid ${srv.enabled ? '#a855f7' : '#333'}`,
                                            opacity: srv.enabled ? 1 : 0.5,
                                        }}>
                                            <span style={{ fontSize: '1rem', width: '24px', textAlign: 'center', color: '#a855f7' }}>⧉</span>
                                            <div style={{ flex: 1, minWidth: 0 }}>
                                                <div style={{ fontSize: '0.8rem', fontWeight: 500, color: '#e0e0e0', marginBottom: '2px' }}>
                                                    {srv.name}
                                                </div>
                                                <div style={{ fontSize: '0.68rem', color: '#666', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                                    {srv.description || srv.server_url}
                                                </div>
                                                <div style={{ fontSize: '0.6rem', color: '#444', marginTop: '3px' }}>
                                                    {srv.transport.toUpperCase()}
                                                    {srv.installed_at && ` · Added ${new Date(srv.installed_at).toLocaleDateString()}`}
                                                </div>
                                            </div>
                                            <button
                                                style={{ ...S.btnGhost, padding: '4px 10px', fontSize: '0.65rem', color: '#ef4444', borderColor: '#ef4444' }}
                                                onClick={async () => {
                                                    try {
                                                        await request(`/workspaces/${workspaceId}/mcp-tools/${srv.name}`, { method: 'DELETE' });
                                                        setMcpServers(prev => prev.filter(s => s.name !== srv.name));
                                                    } catch { /* ignore */ }
                                                }}
                                            >
                                                Remove
                                            </button>
                                        </div>
                                    ))}
                                </div>
                            ) : (
                                <div style={{ textAlign: 'center', color: '#333', padding: '20px', fontSize: '0.75rem' }}>
                                    No MCP servers connected. Add one above or let Kestrel discover servers automatically.
                                </div>
                            )}
                        </div>
                    </div>
                );

            case 'agent':
                return (
                    <div>
                        <div style={S.sectionTitle}>// AUTONOMY</div>
                        <p style={{ fontSize: '0.75rem', color: '#666', marginBottom: '20px', lineHeight: 1.5 }}>
                            Control how far Kestrel can go autonomously. These guardrails apply to all tasks.
                        </p>

                        <SliderField label="Max Iterations" value={maxIterations}
                            onChange={v => setMaxIterations(Math.round(v))} min={1} max={100} step={1}
                            format={v => `${v} iterations`} />

                        <SliderField label="Max Tool Calls" value={maxToolCalls}
                            onChange={v => setMaxToolCalls(Math.round(v))} min={1} max={200} step={1}
                            format={v => `${v} calls`} />

                        <SliderField label="Max Wall Time" value={maxWallTime}
                            onChange={v => setMaxWallTime(Math.round(v))} min={30} max={3600} step={30}
                            format={v => v >= 60 ? `${Math.floor(v / 60)}m ${v % 60}s` : `${v}s`} />

                        <div style={S.sectionTitle}>// APPROVAL POLICY</div>
                        <p style={{ fontSize: '0.75rem', color: '#666', marginBottom: '16px', lineHeight: 1.5 }}>
                            Auto-approve tool calls at or below this risk level. Higher-risk actions always need your OK.
                        </p>
                        <div style={S.field}>
                            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: '8px' }}>
                                {(['low', 'medium', 'high', 'critical'] as const).map(level => (
                                    <button key={level} type="button" style={{
                                        padding: '10px', borderRadius: '4px', cursor: 'pointer',
                                        fontFamily: "'JetBrains Mono', monospace", fontSize: '0.75rem',
                                        fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.05em',
                                        background: autoApproveRisk === level ? 'rgba(0,243,255,0.08)' : '#111',
                                        border: `1px solid ${autoApproveRisk === level ? RISK_COLORS[level] : '#222'}`,
                                        color: RISK_COLORS[level],
                                        transition: 'all 0.15s',
                                    }} onClick={() => setAutoApproveRisk(level)}>
                                        {level}
                                    </button>
                                ))}
                            </div>
                            <div style={{ fontSize: '0.65rem', color: '#444', marginTop: '8px' }}>
                                {autoApproveRisk === 'low' && 'Only safe, read-only operations run automatically.'}
                                {autoApproveRisk === 'medium' && 'Web browsing and file writes run without approval.'}
                                {autoApproveRisk === 'high' && 'Code execution and most actions run automatically.'}
                                {autoApproveRisk === 'critical' && '⚠ All operations run without approval. Use with caution.'}
                            </div>
                        </div>

                        <div style={{ padding: '14px', background: '#0d0d0d', borderRadius: '4px', border: '1px solid #1a1a1a', marginTop: '8px' }}>
                            <div style={{ fontSize: '0.7rem', color: '#666', lineHeight: 1.6 }}>
                                <span style={{ color: '#00ff9d' }}>✦ Self-Extending:</span> Kestrel can create new tools during tasks using
                                the <span style={{ color: '#00f3ff' }}>create_skill</span> tool.
                                All skill creations require your explicit approval regardless of the auto-approve policy.
                            </div>
                        </div>
                    </div>
                );

            case 'capabilities':
                return (
                    <div>
                        <div style={S.sectionTitle}>CAPABILITIES</div>
                        <div style={{ color: '#666', fontSize: '0.75rem', marginBottom: '16px' }}>
                            All active agent subsystems. These features run continuously in the background.
                        </div>
                        {capsLoading ? (
                            <div style={{ color: '#555', fontSize: '0.8rem' }}>Loading capabilities...</div>
                        ) : capsList.length === 0 ? (
                            <div style={{ color: '#555', fontSize: '0.8rem' }}>No capabilities data.</div>
                        ) : (
                            ['intelligence', 'safety', 'automation', 'tools'].map(cat => {
                                const items = capsList.filter(c => c.category === cat);
                                if (!items.length) return null;
                                return (
                                    <div key={cat} style={{ marginBottom: '20px' }}>
                                        <div style={{ fontSize: '0.65rem', fontWeight: 600, color: '#444', letterSpacing: '0.08em', textTransform: 'uppercase' as const, marginBottom: '8px' }}>
                                            {cat}
                                        </div>
                                        {items.map(cap => (
                                            <div key={cap.name} style={{
                                                display: 'flex', alignItems: 'center', gap: '12px',
                                                padding: '12px 14px', background: '#111', border: '1px solid #1a1a1a',
                                                borderRadius: '4px', marginBottom: '6px',
                                            }}>
                                                <span style={{ fontSize: '1.2rem', width: '28px', textAlign: 'center' }}>{cap.icon}</span>
                                                <div style={{ flex: 1 }}>
                                                    <div style={{ fontSize: '0.82rem', fontWeight: 600, color: '#e0e0e0' }}>{cap.name}</div>
                                                    <div style={{ fontSize: '0.7rem', color: '#666', marginTop: '2px' }}>{cap.description}</div>
                                                    {cap.stats && Object.keys(cap.stats).length > 0 && (
                                                        <div style={{ fontSize: '0.65rem', color: '#00f3ff', marginTop: '4px' }}>
                                                            {Object.entries(cap.stats).map(([k, v]) => `${k}: ${v}`).join(' · ')}
                                                        </div>
                                                    )}
                                                </div>
                                                <span style={{
                                                    fontSize: '0.6rem', fontWeight: 600, letterSpacing: '0.05em',
                                                    padding: '3px 8px', borderRadius: '3px',
                                                    background: cap.status === 'active' ? 'rgba(0,243,255,0.1)' : 'rgba(255,255,255,0.05)',
                                                    color: cap.status === 'active' ? '#00f3ff' : '#555',
                                                    border: `1px solid ${cap.status === 'active' ? 'rgba(0,243,255,0.2)' : '#333'}`,
                                                    textTransform: 'uppercase' as const,
                                                }}>
                                                    {cap.status === 'active' ? '● ACTIVE' : cap.status}
                                                </span>
                                            </div>
                                        ))}
                                    </div>
                                );
                            })
                        )}
                    </div>
                );

            case 'integrations':
                return (
                    <div>
                        <div style={S.sectionTitle}>// INTEGRATIONS</div>
                        <p style={{ fontSize: '0.75rem', color: '#666', marginBottom: '20px', lineHeight: 1.5 }}>
                            Connect Kestrel to external services. Messages from integrations are routed through the same agent loop.
                        </p>

                        {/* Telegram */}
                        <div style={{
                            padding: '16px', background: '#111', border: '1px solid #1a1a1a',
                            borderRadius: '4px', marginBottom: '12px',
                        }}>
                            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '12px' }}>
                                <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                                    <span style={{ fontSize: '1.2rem' }}>✈</span>
                                    <div>
                                        <div style={{ fontSize: '0.85rem', fontWeight: 600, color: '#e0e0e0' }}>Telegram</div>
                                        <div style={{ fontSize: '0.65rem', color: '#444' }}>Bot API integration</div>
                                    </div>
                                </div>
                                <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                                    <span style={{
                                        fontSize: '0.6rem', padding: '2px 8px', borderRadius: '3px',
                                        background: telegramStatus === 'connected' ? 'rgba(0,255,157,0.1)' : '#1a1a1a',
                                        color: telegramStatus === 'connected' ? '#00ff9d' : '#555',
                                        border: `1px solid ${telegramStatus === 'connected' ? 'rgba(0,255,157,0.3)' : '#222'}`,
                                    }}>
                                        {telegramStatus.toUpperCase()}
                                    </span>
                                    <Toggle value={telegramEnabled} onChange={setTelegramEnabled} />
                                </div>
                            </div>
                            {telegramEnabled && (
                                <div>
                                    <div style={S.field}>
                                        <label style={S.label}>Bot Token</label>
                                        <input style={S.input} type="password" value={telegramToken}
                                            onChange={e => setTelegramToken(e.target.value)}
                                            placeholder="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
                                            onFocus={e => { e.target.style.borderColor = '#00f3ff'; }}
                                            onBlur={e => { e.target.style.borderColor = '#333'; }} />
                                        <div style={{ fontSize: '0.65rem', color: '#444', marginTop: '4px' }}>
                                            Get a token from <span style={{ color: '#00f3ff' }}>@BotFather</span> on Telegram
                                        </div>
                                    </div>
                                </div>
                            )}
                        </div>

                        {/* Discord (coming soon) */}
                        <div style={{
                            padding: '16px', background: '#111', border: '1px solid #1a1a1a',
                            borderRadius: '4px', marginBottom: '12px', opacity: 0.5,
                        }}>
                            <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                                <span style={{ fontSize: '1.2rem' }}>⊕</span>
                                <div style={{ flex: 1 }}>
                                    <div style={{ fontSize: '0.85rem', fontWeight: 600, color: '#e0e0e0' }}>Discord</div>
                                    <div style={{ fontSize: '0.65rem', color: '#444' }}>Bot integration</div>
                                </div>
                                <span style={{ fontSize: '0.6rem', padding: '2px 8px', borderRadius: '3px', background: '#1a1a1a', color: '#444' }}>
                                    COMING SOON
                                </span>
                            </div>
                        </div>

                        {/* Webhook */}
                        <div style={{
                            padding: '16px', background: '#111', border: '1px solid #1a1a1a',
                            borderRadius: '4px', marginBottom: '12px', opacity: 0.5,
                        }}>
                            <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                                <span style={{ fontSize: '1.2rem' }}>⟐</span>
                                <div style={{ flex: 1 }}>
                                    <div style={{ fontSize: '0.85rem', fontWeight: 600, color: '#e0e0e0' }}>Webhooks</div>
                                    <div style={{ fontSize: '0.65rem', color: '#444' }}>Custom HTTP endpoints</div>
                                </div>
                                <span style={{ fontSize: '0.6rem', padding: '2px 8px', borderRadius: '3px', background: '#1a1a1a', color: '#444' }}>
                                    COMING SOON
                                </span>
                            </div>
                        </div>
                    </div>
                );

            case 'automation':
                return (
                    <div>
                        <div style={S.sectionTitle}>// AUTOMATION</div>
                        <p style={{ fontSize: '0.75rem', color: '#666', marginBottom: '20px', lineHeight: 1.5 }}>
                            Schedule AI tasks using natural language. Kestrel will parse your intent and configure a recurring cron job.
                        </p>

                        <div style={S.field}>
                            <label style={S.label}>What do you want to automate?</label>
                            <div style={{ display: 'flex', gap: '8px', marginBottom: '12px' }}>
                                <input style={{ ...S.input, flex: 1 }}
                                    placeholder="e.g. Check for new security alerts every Monday morning"
                                    value={cronInput}
                                    onChange={e => setCronInput(e.target.value)}
                                    onKeyDown={async e => {
                                        if (e.key === 'Enter' && cronInput.trim()) {
                                            setCronLoading(true);
                                            try {
                                                const res = await request(`/workspaces/${workspaceId}/automation/cron/parse`, {
                                                    method: 'POST',
                                                    body: { prompt: cronInput }
                                                }) as { cron: string; human_schedule: string; task: string };
                                                setParsedCron(res);
                                            } catch (err: any) {
                                                setError(err.message || 'Failed to parse cron job');
                                            } finally {
                                                setCronLoading(false);
                                            }
                                        }
                                    }}
                                    onFocus={e => { e.target.style.borderColor = '#00f3ff'; }}
                                    onBlur={e => { e.target.style.borderColor = '#333'; }} />
                                <button style={S.btnPrimary}
                                    disabled={cronLoading || !cronInput.trim()}
                                    onClick={async () => {
                                        setCronLoading(true);
                                        try {
                                            const res = await request(`/workspaces/${workspaceId}/automation/cron/parse`, {
                                                method: 'POST',
                                                body: { prompt: cronInput }
                                            }) as { cron: string; human_schedule: string; task: string };
                                            setParsedCron(res);
                                        } catch (err: any) {
                                            setError(err.message || 'Failed to parse cron job');
                                        } finally {
                                            setCronLoading(false);
                                        }
                                    }}>
                                    {cronLoading ? 'Parsing...' : 'Generate'}
                                </button>
                            </div>
                        </div>

                        {parsedCron && (
                            <div style={{
                                padding: '16px', background: '#111', border: '1px solid #1a1a1a',
                                borderRadius: '4px', marginBottom: '16px'
                            }}>
                                <div style={{ marginBottom: '12px' }}>
                                    <div style={{ fontSize: '0.65rem', color: '#666', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: '4px' }}>Task</div>
                                    <div style={{ fontSize: '0.85rem', color: '#e0e0e0' }}>{parsedCron.task}</div>
                                </div>
                                <div style={{ display: 'flex', gap: '24px' }}>
                                    <div>
                                        <div style={{ fontSize: '0.65rem', color: '#666', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: '4px' }}>Cron Expression</div>
                                        <code style={{ fontSize: '0.8rem', color: '#00f3ff', background: '#0a0a0a', padding: '2px 6px', borderRadius: '3px' }}>{parsedCron.cron}</code>
                                    </div>
                                    <div>
                                        <div style={{ fontSize: '0.65rem', color: '#666', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: '4px' }}>Schedule</div>
                                        <div style={{ fontSize: '0.8rem', color: '#e0e0e0' }}>{parsedCron.human_schedule}</div>
                                    </div>
                                </div>
                                <div style={{ marginTop: '16px', display: 'flex', justifyContent: 'flex-end' }}>
                                    <button style={{ ...S.btnPrimary, padding: '6px 16px', fontSize: '0.75rem' }}
                                        disabled={cronLoading}
                                        onClick={async () => {
                                            if (!parsedCron) return;
                                            setCronLoading(true);
                                            try {
                                                await request(`/workspaces/${workspaceId}/automation/cron`, {
                                                    method: 'POST',
                                                    body: {
                                                        name: parsedCron.task.slice(0, 60) || 'Scheduled Job',
                                                        description: parsedCron.human_schedule,
                                                        cronExpression: parsedCron.cron,
                                                        goal: parsedCron.task,
                                                    },
                                                });
                                                setParsedCron(null);
                                                setCronInput('');
                                                setSaveStatus('Cron job saved successfully');
                                                setTimeout(() => setSaveStatus(null), 3000);
                                            } catch (err: any) {
                                                setError(err.message || 'Failed to save cron job');
                                                setTimeout(() => setError(null), 3000);
                                            } finally {
                                                setCronLoading(false);
                                            }
                                        }}>
                                        {cronLoading ? 'Saving...' : 'Save Job'}
                                    </button>
                                </div>
                            </div>
                        )}
                    </div>
                );

            case 'pr-reviews':
                return (
                    <div>
                        <div style={S.sectionTitle}>// PR REVIEW SETTINGS</div>
                        <p style={{ fontSize: '0.75rem', color: '#666', marginBottom: '20px', lineHeight: 1.5 }}>
                            Kestrel can autonomously review pull requests, post inline comments, and flag security issues.
                        </p>

                        <div style={S.field}>
                            <label style={S.label}>Repository</label>
                            <input style={S.input}
                                placeholder="owner/repo (e.g. John-MiracleWorker/LibreBird)"
                                value={prRepo}
                                onChange={e => setPrRepo(e.target.value)}
                                onFocus={e => { e.target.style.borderColor = '#00f3ff'; }}
                                onBlur={e => { e.target.style.borderColor = '#333'; }} />
                        </div>

                        <div style={{ marginBottom: '20px' }}>
                            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '8px 0', borderBottom: '1px solid #1a1a1a' }}>
                                <div>
                                    <div style={{ fontSize: '0.75rem', color: '#e0e0e0' }}>Auto-review</div>
                                    <div style={{ fontSize: '0.6rem', color: '#555' }}>Review new PRs automatically</div>
                                </div>
                                <Toggle value={prAutoReview} onChange={setPrAutoReview} />
                            </div>
                            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '8px 0', borderBottom: '1px solid #1a1a1a' }}>
                                <div>
                                    <div style={{ fontSize: '0.75rem', color: '#e0e0e0' }}>Post comments</div>
                                    <div style={{ fontSize: '0.6rem', color: '#555' }}>Post inline code review comments</div>
                                </div>
                                <Toggle value={prPostComments} onChange={setPrPostComments} />
                            </div>
                            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '8px 0', borderBottom: '1px solid #1a1a1a' }}>
                                <div>
                                    <div style={{ fontSize: '0.75rem', color: '#e0e0e0' }}>Auto-approve</div>
                                    <div style={{ fontSize: '0.6rem', color: '#555' }}>Auto-approve clean PRs with no issues</div>
                                </div>
                                <Toggle value={prAutoApprove} onChange={setPrAutoApprove} />
                            </div>
                        </div>

                        <div style={S.field}>
                            <label style={S.label}>Severity filter</label>
                            <select style={S.input} value={prSeverityFilter} onChange={e => setPrSeverityFilter(e.target.value)}>
                                <option value="all">All issues</option>
                                <option value="high">High + Critical only</option>
                                <option value="critical">Critical only</option>
                            </select>
                        </div>

                        <div style={{ ...S.sectionTitle, marginTop: '24px' }}>// RECENT REVIEWS</div>

                        {[{
                            pr: 42, title: 'Add auth middleware', status: 'approved', statusColor: '#10b981', statusIcon: '✓',
                            added: 147, removed: 23, files: 5, ago: '2h ago',
                            findings: [{ severity: 'security', color: '#ef4444', count: 1 }, { severity: 'quality', color: '#f59e0b', count: 2 }, { severity: 'style', color: '#3b82f6', count: 1 }],
                        }, {
                            pr: 41, title: 'Fix login bug', status: 'changes requested', statusColor: '#f59e0b', statusIcon: '⚠',
                            added: 23, removed: 8, files: 2, ago: '1d ago',
                            findings: [{ severity: 'security', color: '#ef4444', count: 2 }],
                        }, {
                            pr: 40, title: 'Add cron parser', status: 'approved', statusColor: '#10b981', statusIcon: '✓',
                            added: 89, removed: 4, files: 3, ago: '2d ago',
                            findings: [{ severity: 'quality', color: '#f59e0b', count: 1 }],
                        }].map(review => (
                            <div key={review.pr} style={{ padding: '12px 14px', background: '#0d0d0d', border: '1px solid #1a1a1a', borderRadius: '4px', marginBottom: '8px' }}>
                                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '6px' }}>
                                    <div style={{ fontSize: '0.75rem', color: '#e0e0e0' }}>PR #{review.pr}: {review.title}</div>
                                    <span style={{ fontSize: '0.6rem', padding: '2px 6px', borderRadius: '3px', background: `${review.statusColor}15`, color: review.statusColor }}>
                                        {review.statusIcon} {review.status}
                                    </span>
                                </div>
                                <div style={{ fontSize: '0.65rem', color: '#555', marginBottom: '6px' }}>
                                    <span style={{ color: '#10b981' }}>+{review.added}</span>{' / '}
                                    <span style={{ color: '#ef4444' }}>-{review.removed}</span>
                                    {' · '}{review.files} files · Reviewed {review.ago}
                                </div>
                                <div style={{ display: 'flex', gap: '8px' }}>
                                    {review.findings.map((f, i) => (
                                        <span key={i} style={{ display: 'inline-flex', alignItems: 'center', gap: '4px', fontSize: '0.6rem', color: f.color }}>
                                            <span style={{ width: '5px', height: '5px', borderRadius: '50%', background: f.color, display: 'inline-block' }} />
                                            {f.count} {f.severity}
                                        </span>
                                    ))}
                                </div>
                            </div>
                        ))}
                    </div>
                );

            case 'api-keys':
                return (
                    <div>
                        <div style={S.sectionTitle}>// API KEYS</div>
                        <p style={{ fontSize: '0.75rem', color: '#666', marginBottom: '20px', lineHeight: 1.5 }}>
                            Access Kestrel programmatically. Keys are shown only once at creation.
                        </p>

                        {createdKey && (
                            <div style={S.successBox}>
                                <div style={{ fontWeight: 600, marginBottom: '6px' }}>✓ Key created: {createdKey.name}</div>
                                <div style={{ fontSize: '0.7rem', marginBottom: '8px', opacity: 0.8 }}>Copy now — it won't be shown again.</div>
                                <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
                                    <code style={{
                                        flex: 1, padding: '8px 10px', background: '#0a0a0a', borderRadius: '3px',
                                        fontSize: '0.7rem', wordBreak: 'break-all',
                                        border: '1px solid rgba(0,255,157,0.2)', color: '#00ff9d',
                                    }}>{createdKey.key}</code>
                                    <button style={{ ...S.btnGhost, padding: '8px 12px', fontSize: '0.7rem', flexShrink: 0 }}
                                        onClick={() => navigator.clipboard.writeText(createdKey.key)}>Copy</button>
                                </div>
                                <button style={{ ...S.btnGhost, padding: '4px 10px', fontSize: '0.65rem', marginTop: '8px', borderColor: 'transparent' }}
                                    onClick={() => setCreatedKey(null)}>Dismiss</button>
                            </div>
                        )}

                        <div style={{ display: 'flex', gap: '8px', marginBottom: '20px' }}>
                            <input style={{ ...S.input, flex: 1 }} placeholder="Key name (e.g. CI Pipeline)"
                                value={newKeyName} onChange={e => setNewKeyName(e.target.value)}
                                onKeyDown={e => e.key === 'Enter' && handleCreateKey()}
                                onFocus={e => { e.target.style.borderColor = '#00f3ff'; }}
                                onBlur={e => { e.target.style.borderColor = '#333'; }} />
                            <button style={S.btnPrimary} onClick={handleCreateKey}>+ Create</button>
                        </div>

                        {keysLoading ? (
                            <div style={{ textAlign: 'center', color: '#444', padding: '24px', fontSize: '0.8rem' }}>Loading keys...</div>
                        ) : keysList.length === 0 ? (
                            <div style={{
                                textAlign: 'center', padding: '32px', color: '#444', fontSize: '0.8rem',
                                border: '1px dashed #222', borderRadius: '4px',
                            }}>No API keys yet</div>
                        ) : (
                            <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                                {keysList.map(k => (
                                    <div key={k.id} style={{
                                        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                                        padding: '10px 14px', background: '#111', border: '1px solid #1a1a1a', borderRadius: '4px',
                                    }}>
                                        <div>
                                            <div style={{ fontSize: '0.8rem', fontWeight: 500, color: '#e0e0e0' }}>{k.name}</div>
                                            <div style={{ fontSize: '0.65rem', color: '#444', marginTop: '2px' }}>
                                                Expires {new Date(k.expiresAt).toLocaleDateString()}
                                            </div>
                                        </div>
                                        <button style={{ ...S.btnGhost, padding: '6px 12px', fontSize: '0.7rem', color: '#ff0055', borderColor: 'rgba(255,0,85,0.3)' }}
                                            onClick={() => handleRevokeKey(k.id)}>Revoke</button>
                                    </div>
                                ))}
                            </div>
                        )}
                    </div>
                );

            case 'general':
                return (
                    <div>
                        <div style={S.sectionTitle}>// WORKSPACE</div>
                        <div style={S.field}>
                            <label style={S.label}>Workspace Name</label>
                            <input style={S.input} defaultValue="John-MiracleWorker's Workspace" placeholder="Workspace name"
                                onFocus={e => { e.target.style.borderColor = '#00f3ff'; }}
                                onBlur={e => { e.target.style.borderColor = '#333'; }} />
                        </div>
                        <div style={S.field}>
                            <label style={S.label}>Description</label>
                            <textarea style={{ ...S.textarea, minHeight: 80 }} placeholder="What is this workspace for?"
                                onFocus={e => { e.target.style.borderColor = '#00f3ff'; }}
                                onBlur={e => { e.target.style.borderColor = '#333'; }} />
                        </div>
                        <div style={{ ...S.sectionTitle, marginTop: '32px' }}>// DANGER ZONE</div>
                        <div style={{
                            padding: '16px', border: '1px solid rgba(255,0,85,0.2)',
                            borderRadius: '4px', background: 'rgba(255,0,85,0.03)',
                        }}>
                            <div style={{ fontSize: '0.8rem', fontWeight: 500, color: '#ff0055', marginBottom: '6px' }}>Delete Workspace</div>
                            <div style={{ fontSize: '0.7rem', color: '#666', marginBottom: '12px' }}>
                                This will permanently delete the workspace and all conversations.
                            </div>
                            <button style={{ ...S.btnGhost, color: '#ff0055', borderColor: 'rgba(255,0,85,0.3)', fontSize: '0.75rem' }}>
                                Delete Workspace
                            </button>
                        </div>
                    </div>
                );

            case 'profile':
                return (
                    <div>
                        <div style={S.sectionTitle}>// PROFILE</div>
                        <div style={S.field}>
                            <label style={S.label}>Display Name</label>
                            <input style={S.input} value={displayName} onChange={e => setDisplayName(e.target.value)}
                                placeholder="Your name"
                                onFocus={e => { e.target.style.borderColor = '#00f3ff'; }}
                                onBlur={e => { e.target.style.borderColor = '#333'; }} />
                        </div>
                        <div style={S.field}>
                            <label style={S.label}>Email</label>
                            <input style={{ ...S.input, opacity: 0.5, cursor: 'not-allowed' }} defaultValue={userEmail} disabled />
                            <div style={{ fontSize: '0.65rem', color: '#444', marginTop: '4px' }}>Email cannot be changed</div>
                        </div>
                        <div style={{ ...S.sectionTitle, marginTop: '32px' }}>// SESSION</div>
                        <button style={{ ...S.btnGhost, color: '#ff0055', borderColor: 'rgba(255,0,85,0.3)' }}>Sign Out</button>
                    </div>
                );
        }
    };

    /* ── Main render ──────────────────────────────────────────────── */

    return createPortal(
        <div style={S.backdrop} onClick={onClose}>
            <div style={S.panel} onClick={e => e.stopPropagation()}>
                <nav style={S.nav}>
                    <div style={S.navHeader}>⚙ System Config</div>
                    {tabs.map(tab => (
                        <div key={tab.id}>
                            {tab.section && <div style={S.navSection}>{tab.section}</div>}
                            <button style={S.navItem(activeTab === tab.id)} onClick={() => setActiveTab(tab.id)}>
                                <span style={{ fontSize: '0.9rem', width: '18px', textAlign: 'center' }}>{tab.icon}</span>
                                {tab.label}
                            </button>
                        </div>
                    ))}
                    <div style={{ marginTop: 'auto', padding: '16px' }}>
                        {saveStatus && (
                            <div style={{ fontSize: '0.7rem', color: '#00ff9d', marginBottom: '8px', textAlign: 'center' }}>
                                ✓ {saveStatus}
                            </div>
                        )}
                        {error && (
                            <div style={{ fontSize: '0.7rem', color: '#ff0055', marginBottom: '8px', textAlign: 'center' }}>✗ {error}</div>
                        )}
                        <button style={{ ...S.btnPrimary, width: '100%', opacity: saving ? 0.5 : 1 }}
                            onClick={handleSave} disabled={saving}>
                            {saving ? 'Saving...' : 'Save Changes'}
                        </button>
                    </div>
                </nav>
                <div style={S.content}>
                    <button style={S.closeBtn} onClick={onClose}>✕</button>
                    {renderContent()}
                </div>
            </div>
        </div>,
        document.body
    );
}
