import React, { useState, useEffect } from 'react';
import { createPortal } from 'react-dom';
import { request, CapabilityItem } from '../../api/client';
import { TabId, ProviderConfig, SettingsPanelProps } from './types';
import { S, DISPLAY_MASK } from './constants';

// Import Tabs
import { ModelTab } from './tabs/ModelTab';
import { PersonaTab } from './tabs/PersonaTab';
import { MemoryTab } from './tabs/MemoryTab';
import { ToolsTab } from './tabs/ToolsTab';
import { AgentTab } from './tabs/AgentTab';
import { CapabilitiesTab } from './tabs/CapabilitiesTab';
import { IntegrationsTab } from './tabs/IntegrationsTab';
import { AutomationTab } from './tabs/AutomationTab';
import { PrReviewsTab } from './tabs/PrReviewsTab';
import { ApiKeysTab } from './tabs/ApiKeysTab';
import { GeneralTab } from './tabs/GeneralTab';
import { ProfileTab } from './tabs/ProfileTab';

export function SettingsPanel({ onClose, userEmail, userDisplayName, workspaceId }: SettingsPanelProps) {
    const [activeTab, setActiveTab] = useState<TabId>('model');
    const [saving, setSaving] = useState(false);
    const [saveStatus, setSaveStatus] = useState<string | null>(null);
    const [error, setError] = useState<string | null>(null);

    // --- State ---
    const [selectedProvider, setSelectedProvider] = useState('local');
    const [apiKeyInput, setApiKeyInput] = useState('');
    const [model, setModel] = useState('');
    const [temperature, setTemperature] = useState(0.7);
    const [maxTokens, setMaxTokens] = useState(2048);
    const [systemPrompt, setSystemPrompt] = useState('');
    const [ragEnabled, setRagEnabled] = useState(true);
    const [ragTopK, setRagTopK] = useState(5);
    const [ragMinSimilarity, setRagMinSimilarity] = useState(0.7);
    const [providerConfigs, setProviderConfigs] = useState<ProviderConfig[]>([]);
    const [availableModels, setAvailableModels] = useState<any[]>([]);

    // Tools / Agent
    const [disabledTools, setDisabledTools] = useState<Set<string>>(new Set());
    const [maxIterations, setMaxIterations] = useState(30);
    const [maxToolCalls, setMaxToolCalls] = useState(100);
    const [maxWallTime, setMaxWallTime] = useState(600);
    const [autoApproveRisk, setAutoApproveRisk] = useState('high');

    // Capabilities
    const [capabilities, setCapabilities] = useState<CapabilityItem[]>([]);

    // Integrations
    const [telegramEnabled, setTelegramEnabled] = useState(false);
    const [telegramToken, setTelegramToken] = useState('');
    const [discordEnabled, setDiscordEnabled] = useState(false);
    const [discordToken, setDiscordToken] = useState('');
    const [webhookUrl, setWebhookUrl] = useState('');
    const [webhookSecret, setWebhookSecret] = useState('');
    const [webhookEvents, setWebhookEvents] = useState<string[]>([]);
    const [cronEnabled, setCronEnabled] = useState(false);
    const [cronSchedule, setCronSchedule] = useState('0 * * * *');
    const [cronMaxRuns, setCronMaxRuns] = useState(3);
    const [cronSystemPrompt, setCronSystemPrompt] = useState('');
    const [prEnabled, setPrEnabled] = useState(false);
    const [prAutoApprove, setPrAutoApprove] = useState(false);
    const [prPostComments, setPrPostComments] = useState(true);
    const [prSeverityFilter, setPrSeverityFilter] = useState('all');

    // Profile
    const [displayName, setDisplayName] = useState(userDisplayName || '');

    // --- Loading config ---
    useEffect(() => {
        request(`/workspaces/${workspaceId}/config`)
            .then((data: any) => {
                if (data.providers) setProviderConfigs(data.providers);
                const defaultProv = data.providers?.find((p: any) => p.isDefault || p.is_default);
                if (defaultProv) {
                    setSelectedProvider(defaultProv.provider);
                    setModel(defaultProv.model || '');
                    setTemperature(defaultProv.temperature ?? 0.7);
                    setMaxTokens(defaultProv.maxTokens || defaultProv.max_tokens || 2048);
                    setSystemPrompt(defaultProv.systemPrompt || defaultProv.system_prompt || '');
                    setRagEnabled(defaultProv.ragEnabled ?? defaultProv.rag_enabled ?? true);
                    setRagTopK(defaultProv.ragTopK || defaultProv.rag_top_k || 5);
                    setRagMinSimilarity(defaultProv.ragMinSimilarity || defaultProv.rag_min_similarity || 0.7);
                }
                if (data.agent) {
                    setDisabledTools(new Set(data.agent.disabledTools || data.agent.disabled_tools || []));
                    setMaxIterations(data.agent.maxIterations || data.agent.max_iterations || 30);
                    setMaxToolCalls(data.agent.maxToolCalls || data.agent.max_tool_calls || 100);
                    setMaxWallTime(data.agent.maxWallTime || data.agent.max_wall_time || 600);
                    setAutoApproveRisk(data.agent.autoApproveRisk || data.agent.auto_approve_risk || 'high');
                }
            })
            .catch(err => setError(err.message));

        request(`/workspaces/${workspaceId}/capabilities`)
            .then((data: any) => setCapabilities(data.capabilities || []))
            .catch(err => console.error('Capabilities fail', err));

        request(`/workspaces/${workspaceId}/integrations`)
            .then((data: any) => {
                const tg = data.telegram || {};
                setTelegramEnabled(tg.enabled || false);
                setTelegramToken(tg.token || '');
                const dc = data.discord || {};
                setDiscordEnabled(dc.enabled || false);
                setDiscordToken(dc.token || '');
                const wh = data.webhooks || {};
                setWebhookUrl(wh.url || '');
                setWebhookSecret(wh.secret || '');
                setWebhookEvents(wh.events || []);
                const pr = data.pr_reviews || {};
                setPrEnabled(pr.enabled || false);
                setPrAutoApprove(pr.auto_approve || false);
                setPrPostComments(pr.post_comments ?? true);
                setPrSeverityFilter(pr.severity_filter || 'all');
                const cron = data.cron || {};
                setCronEnabled(cron.enabled || false);
                setCronSchedule(cron.schedule || '0 * * * *');
                setCronMaxRuns(cron.max_runs || 3);
                setCronSystemPrompt(cron.system_prompt || '');
            })
            .catch(err => console.error('Integrations config parse fail', err));
    }, [workspaceId]);

    useEffect(() => {
        request(`/models?provider=${selectedProvider}`)
            .then((data: any) => setAvailableModels(data.models || []))
            .catch(() => setAvailableModels([]));

        const conf = providerConfigs.find(c => c.provider === selectedProvider);
        if (conf) {
            setModel(conf.model || '');
            setTemperature(conf.temperature ?? 0.7);
            setMaxTokens(conf.maxTokens || conf.max_tokens || 2048);
            setApiKeyInput(conf.apiKeyEncrypted ? DISPLAY_MASK : '');
        } else {
            setModel(''); setApiKeyInput('');
        }
    }, [selectedProvider, providerConfigs]);

    const handleWebhookEventToggle = (ev: string) => {
        setWebhookEvents(prev => prev.includes(ev) ? prev.filter(e => e !== ev) : [...prev, ev]);
    };

    const handleSave = async () => {
        setSaving(true);
        setError(null);
        setSaveStatus(null);
        try {
            const apiReq = apiKeyInput && apiKeyInput !== DISPLAY_MASK ? { apiKey: apiKeyInput } : {};
            await request(`/workspaces/${workspaceId}/config`, {
                method: 'PATCH',
                body: {
                    provider: selectedProvider,
                    model, temperature, maxTokens, systemPrompt,
                    ragEnabled, ragTopK, ragMinSimilarity,
                    isDefault: true, ...apiReq,
                    agent: {
                        disabledTools: Array.from(disabledTools),
                        maxIterations, maxToolCalls, maxWallTime, autoApproveRisk
                    }
                }
            });

            await request(`/workspaces/${workspaceId}/integrations`, {
                method: 'PATCH',
                body: {
                    telegram: { enabled: telegramEnabled, token: telegramToken },
                    discord: { enabled: discordEnabled, token: discordToken },
                    webhooks: { enabled: !!webhookUrl, url: webhookUrl, secret: webhookSecret, events: webhookEvents },
                    pr_reviews: { enabled: prEnabled, auto_approve: prAutoApprove, post_comments: prPostComments, severity_filter: prSeverityFilter },
                    cron: { enabled: cronEnabled, schedule: cronSchedule, max_runs: cronMaxRuns, system_prompt: cronSystemPrompt },
                }
            });

            setSaveStatus('Settings applied successfully');
            setTimeout(() => setSaveStatus(null), 3000);
        } catch (err: any) {
            setError(err.message || 'Failed to save settings');
            setTimeout(() => setError(null), 4000);
        } finally {
            setSaving(false);
        }
    };

    const tabs: Array<{ id: TabId, label: string, icon: string, section?: string }> = [
        { id: 'model', label: 'AI Model', icon: '⚡', section: 'Core' },
        { id: 'persona', label: 'Persona', icon: '🎭' },
        { id: 'memory', label: 'Memory (RAG)', icon: '🧠' },
        { id: 'tools', label: 'Tools', icon: '⚒', section: 'Agent' },
        { id: 'agent', label: 'Autonomy', icon: '🤖' },
        { id: 'capabilities', label: 'Capabilities', icon: '✨' },
        { id: 'integrations', label: 'Integrations', icon: '🔌', section: 'External' },
        { id: 'automation', label: 'Automation', icon: '⏰' },
        { id: 'pr-reviews', label: 'PR Reviews', icon: '📝' },
        { id: 'api-keys', label: 'API Keys', icon: '🔑', section: 'Account' },
        { id: 'general', label: 'Workspace', icon: '⚙' },
        { id: 'profile', label: 'Profile', icon: '👤' },
    ];

    const renderContent = () => {
        switch (activeTab) {
            case 'model': return <ModelTab {...{ providerConfigs, selectedProvider, onProviderSelect: setSelectedProvider, apiKeyInput, setApiKeyInput, model, setModel, availableModels, temperature, setTemperature, maxTokens, setMaxTokens }} />;
            case 'persona': return <PersonaTab {...{ systemPrompt, setSystemPrompt }} />;
            case 'memory': return <MemoryTab {...{ ragEnabled, setRagEnabled, ragTopK, setRagTopK, ragMinSimilarity, setRagMinSimilarity }} />;
            case 'tools': return <ToolsTab {...{ workspaceId, disabledTools, setDisabledTools, setSaveStatus, setError }} />;
            case 'agent': return <AgentTab {...{ maxIterations, setMaxIterations, maxToolCalls, setMaxToolCalls, maxWallTime, setMaxWallTime, autoApproveRisk, setAutoApproveRisk }} />;
            case 'capabilities': return <CapabilitiesTab {...{ workspaceId, capabilities, setCapabilities, setError }} />;
            case 'integrations': return <IntegrationsTab {...{ telegramEnabled, setTelegramEnabled, telegramToken, setTelegramToken, discordEnabled, setDiscordEnabled, discordToken, setDiscordToken, webhookUrl, setWebhookUrl, webhookSecret, setWebhookSecret, webhookEvents, handleWebhookEventToggle }} />;
            case 'automation': return <AutomationTab {...{ cronEnabled, setCronEnabled, cronSchedule, setCronSchedule, cronMaxRuns, setCronMaxRuns, cronSystemPrompt, setCronSystemPrompt }} />;
            case 'pr-reviews': return <PrReviewsTab {...{ prEnabled, setPrEnabled, prAutoApprove, setPrAutoApprove, prPostComments, setPrPostComments, prSeverityFilter, setPrSeverityFilter }} />;
            case 'api-keys': return <ApiKeysTab workspaceId={workspaceId} />;
            case 'general': return <GeneralTab />;
            case 'profile': return <ProfileTab {...{ displayName, setDisplayName, userEmail }} />;
        }
    };

    return createPortal(
        <div style={S.backdrop} onClick={onClose}>
            <div style={S.panel} onClick={e => e.stopPropagation()}>
                <nav style={S.nav}>
                    <div style={S.navHeader}>⚙ System Config</div>
                    {tabs.map(tab => (
                        <div key={tab.id}>
                            {tab.section && <div style={S.navSection}>{tab.section}</div>}
                            <button style={S.navItem(activeTab === tab.id)} onClick={() => setActiveTab(tab.id as TabId)}>
                                <span style={{ fontSize: '0.9rem', width: '18px', textAlign: 'center' }}>{tab.icon}</span>
                                {tab.label}
                            </button>
                        </div>
                    ))}
                    <div style={{ marginTop: 'auto', padding: '16px' }}>
                        {saveStatus && <div style={{ fontSize: '0.7rem', color: '#00ff9d', marginBottom: '8px', textAlign: 'center' }}>✓ {saveStatus}</div>}
                        {error && <div style={{ fontSize: '0.7rem', color: '#ff0055', marginBottom: '8px', textAlign: 'center' }}>✗ {error}</div>}
                        <button style={{ ...S.btnPrimary, width: '100%', opacity: saving ? 0.5 : 1 }} onClick={handleSave} disabled={saving}>
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
