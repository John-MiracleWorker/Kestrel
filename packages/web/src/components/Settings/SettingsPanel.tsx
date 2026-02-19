import { useState, useEffect } from 'react';
import { createPortal } from 'react-dom';
import { ConfigureProviderModal } from './ConfigureProviderModal';
import { providers, apiKeys, type ApiKey } from '../../api/client';

interface SettingsPanelProps {
    onClose: () => void;
    userEmail?: string;
    userDisplayName?: string;
    workspaceId: string;
}

export function SettingsPanel({ onClose, userEmail, userDisplayName, workspaceId }: SettingsPanelProps) {
    const [activeTab, setActiveTab] = useState<'profile' | 'providers' | 'api-keys'>('profile');
    const [configuringProvider, setConfiguringProvider] = useState<{ key: string; name: string } | null>(null);
    const [saveStatus, setSaveStatus] = useState<string | null>(null);
    const [providerConfigs, setProviderConfigs] = useState<any[]>([]);
    const [keysList, setKeysList] = useState<ApiKey[]>([]);
    const [keysLoading, setKeysLoading] = useState(false);
    const [newKeyName, setNewKeyName] = useState('');
    const [createdKey, setCreatedKey] = useState<{ key: string; name: string } | null>(null);
    const [keyError, setKeyError] = useState<string | null>(null);

    // Assuming currentWorkspace is derived from workspaceId or passed as a prop.
    // For this change, we'll use workspaceId directly where currentWorkspace.id was implied.
    const currentWorkspace = { id: workspaceId };

    useEffect(() => {
        providers.list(workspaceId)
            .then((data: any) => setProviderConfigs(data?.configs || []))
            .catch(() => setProviderConfigs([]));
    }, [activeTab, workspaceId, configuringProvider]); // re-fetch after modal closes

    // Load API Keys when that tab is active
    useEffect(() => {
        if (activeTab !== 'api-keys') return;
        setKeysLoading(true);
        apiKeys.list()
            .then((data: any) => setKeysList(data?.keys || []))
            .catch(() => setKeysList([]))
            .finally(() => setKeysLoading(false));
    }, [activeTab]);

    const handleCreateKey = async () => {
        if (!newKeyName.trim()) {
            setKeyError('Key name is required');
            return;
        }
        setKeyError(null);
        try {
            const result = await apiKeys.create(newKeyName.trim());
            setCreatedKey({ key: result.key, name: result.name });
            setNewKeyName('');
            // Refresh list
            const data: any = await apiKeys.list();
            setKeysList(data?.keys || []);
        } catch (err: any) {
            setKeyError(err.message || 'Failed to create API key');
        }
    };

    const handleRevokeKey = async (id: string) => {
        try {
            await apiKeys.revoke(id);
            setKeysList(prev => prev.filter(k => k.id !== id));
        } catch (err: any) {
            setKeyError(err.message || 'Failed to revoke key');
        }
    };

    const tabs = [
        { id: 'profile' as const, label: 'Profile', icon: '👤' },
        { id: 'providers' as const, label: 'Providers', icon: '🤖' },
        { id: 'api-keys' as const, label: 'API Keys', icon: '🔑' },
    ];

    const handleSaveProfile = () => {
        // TODO: wire to API when backend supports profile updates
        setSaveStatus('Profile saved!');
        setTimeout(() => setSaveStatus(null), 2000);
    };

    // Don't close backdrop when a sub-modal is open
    const handleBackdropClick = () => {
        if (!configuringProvider) {
            onClose();
        }
    };

    return (
        <>
            {createPortal(
                <div style={{
                    position: 'fixed',
                    inset: 0,
                    zIndex: 9999,
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    background: 'rgba(0, 0, 0, 0.6)',
                    backdropFilter: 'blur(4px)',
                }} onClick={handleBackdropClick}>
                    <div
                        className="card-glass animate-fade-in"
                        style={{
                            width: '100%',
                            maxWidth: 640,
                            maxHeight: '80vh',
                            overflow: 'auto',
                            padding: 0,
                        }}
                        onClick={(e) => e.stopPropagation()}
                    >
                        {/* Header */}
                        <div style={{
                            display: 'flex',
                            alignItems: 'center',
                            justifyContent: 'space-between',
                            padding: 'var(--space-4) var(--space-6)',
                            borderBottom: '1px solid var(--color-border)',
                        }}>
                            <h2 style={{ fontSize: '1.125rem', fontWeight: 600 }}>Settings</h2>
                            <button className="btn btn-ghost" onClick={onClose}>
                                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                                    <line x1="18" y1="6" x2="6" y2="18" />
                                    <line x1="6" y1="6" x2="18" y2="18" />
                                </svg>
                            </button>
                        </div>

                        {/* Tabs */}
                        <div style={{
                            display: 'flex',
                            gap: 'var(--space-1)',
                            padding: 'var(--space-3) var(--space-6)',
                            borderBottom: '1px solid var(--color-border)',
                        }}>
                            {tabs.map((tab) => (
                                <button
                                    key={tab.id}
                                    className="btn"
                                    style={{
                                        padding: 'var(--space-2) var(--space-3)',
                                        background: activeTab === tab.id ? 'var(--color-bg-hover)' : 'transparent',
                                        color: activeTab === tab.id ? 'var(--color-text)' : 'var(--color-text-secondary)',
                                        borderRadius: 'var(--radius-sm)',
                                        fontSize: '0.8125rem',
                                    }}
                                    onClick={() => setActiveTab(tab.id)}
                                >
                                    <span>{tab.icon}</span> {tab.label}
                                </button>
                            ))}
                        </div>

                        {/* Content */}
                        <div style={{ padding: 'var(--space-6)' }}>
                            {activeTab === 'profile' && (
                                <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-4)' }}>
                                    <div className="form-group">
                                        <label>Display Name</label>
                                        <input className="input" defaultValue={userDisplayName} placeholder="Your name" />
                                    </div>
                                    <div className="form-group">
                                        <label>Email</label>
                                        <input className="input" defaultValue={userEmail} disabled
                                            style={{ opacity: 0.6 }} />
                                    </div>
                                    <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-3)' }}>
                                        <button className="btn btn-primary" onClick={handleSaveProfile}>
                                            Save Changes
                                        </button>
                                        {saveStatus && (
                                            <span style={{ color: 'var(--color-success, #22c55e)', fontSize: '0.875rem' }}>
                                                ✓ {saveStatus}
                                            </span>
                                        )}
                                    </div>
                                </div>
                            )}

                            {activeTab === 'providers' && (
                                <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-4)' }}>
                                    <p style={{ color: 'var(--color-text-secondary)', fontSize: '0.875rem' }}>
                                        Configure which LLM provider to use for this workspace.
                                    </p>

                                    {['Local (llama.cpp)', 'OpenAI', 'Anthropic', 'Google'].map((name) => {
                                        const key = name.toLowerCase().split(' ')[0];
                                        const config = providerConfigs.find((c: any) => c.provider === key);
                                        const isDefault = config?.isDefault || config?.is_default;

                                        return (
                                            <div key={name} className="card" style={{
                                                display: 'flex',
                                                alignItems: 'center',
                                                justifyContent: 'space-between',
                                                padding: 'var(--space-4)',
                                                border: isDefault ? '1px solid var(--color-primary)' : undefined
                                            }}>
                                                <div>
                                                    <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)' }}>
                                                        <p style={{ fontWeight: 500 }}>{name}</p>
                                                        {isDefault && (
                                                            <span style={{
                                                                fontSize: '0.75rem',
                                                                padding: '2px 6px',
                                                                borderRadius: '4px',
                                                                background: 'var(--color-primary)',
                                                                color: 'white',
                                                                fontWeight: 600
                                                            }}>
                                                                Default
                                                            </span>
                                                        )}
                                                    </div>
                                                    <p style={{
                                                        fontSize: '0.8125rem',
                                                        color: 'var(--color-text-tertiary)',
                                                    }}>
                                                        {name === 'Local (llama.cpp)'
                                                            ? 'On-device inference — no API key needed'
                                                            : 'Cloud provider — requires API key'}
                                                    </p>
                                                </div>
                                                <button
                                                    className="btn btn-secondary"
                                                    onClick={(e) => {
                                                        e.stopPropagation();
                                                        setConfiguringProvider({ key, name });
                                                    }}
                                                >
                                                    Configure
                                                </button>
                                            </div>
                                        );
                                    })}
                                </div>
                            )}

                            {activeTab === 'api-keys' && (
                                <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-4)' }}>
                                    <p style={{ color: 'var(--color-text-secondary)', fontSize: '0.875rem' }}>
                                        API keys let you access Kestrel programmatically. Keys are shown only once at creation.
                                    </p>

                                    {/* One-time key reveal */}
                                    {createdKey && (
                                        <div style={{
                                            padding: 'var(--space-4)',
                                            background: 'rgba(34, 197, 94, 0.08)',
                                            border: '1px solid rgba(34, 197, 94, 0.3)',
                                            borderRadius: 'var(--radius-md)',
                                        }}>
                                            <p style={{ fontSize: '0.8125rem', fontWeight: 600, color: 'var(--color-success)', marginBottom: 'var(--space-2)' }}>
                                                Key created: {createdKey.name}
                                            </p>
                                            <p style={{ fontSize: '0.75rem', color: 'var(--color-text-secondary)', marginBottom: 'var(--space-2)' }}>
                                                Copy this now — it won't be shown again.
                                            </p>
                                            <div style={{ display: 'flex', gap: 'var(--space-2)', alignItems: 'center' }}>
                                                <code style={{
                                                    flex: 1,
                                                    padding: 'var(--space-2) var(--space-3)',
                                                    background: 'var(--color-bg-surface)',
                                                    borderRadius: 'var(--radius-sm)',
                                                    fontSize: '0.75rem',
                                                    wordBreak: 'break-all',
                                                    fontFamily: 'monospace',
                                                }}>
                                                    {createdKey.key}
                                                </code>
                                                <button
                                                    className="btn btn-secondary"
                                                    style={{ flexShrink: 0, fontSize: '0.75rem' }}
                                                    onClick={() => navigator.clipboard.writeText(createdKey.key)}
                                                >
                                                    Copy
                                                </button>
                                            </div>
                                            <button
                                                className="btn btn-ghost"
                                                style={{ fontSize: '0.75rem', marginTop: 'var(--space-2)' }}
                                                onClick={() => setCreatedKey(null)}
                                            >
                                                Dismiss
                                            </button>
                                        </div>
                                    )}

                                    {/* Create new key */}
                                    <div style={{ display: 'flex', gap: 'var(--space-2)' }}>
                                        <input
                                            className="input"
                                            type="text"
                                            placeholder="Key name (e.g. My Script)"
                                            value={newKeyName}
                                            onChange={e => setNewKeyName(e.target.value)}
                                            onKeyDown={e => e.key === 'Enter' && handleCreateKey()}
                                            style={{ flex: 1 }}
                                        />
                                        <button className="btn btn-primary" onClick={handleCreateKey}>
                                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                                                <path d="M12 5v14M5 12h14" />
                                            </svg>
                                            Create
                                        </button>
                                    </div>
                                    {keyError && <p style={{ color: 'var(--color-error)', fontSize: '0.8125rem' }}>{keyError}</p>}

                                    {/* Keys list */}
                                    {keysLoading ? (
                                        <div style={{ textAlign: 'center', color: 'var(--color-text-tertiary)', padding: 'var(--space-4)' }}>
                                            Loading keys...
                                        </div>
                                    ) : keysList.length === 0 ? (
                                        <div className="card" style={{
                                            textAlign: 'center',
                                            padding: 'var(--space-8)',
                                            color: 'var(--color-text-tertiary)',
                                            fontSize: '0.875rem',
                                        }}>
                                            No API keys yet. Create one above.
                                        </div>
                                    ) : (
                                        <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-2)' }}>
                                            {keysList.map(k => (
                                                <div key={k.id} className="card" style={{
                                                    display: 'flex',
                                                    alignItems: 'center',
                                                    justifyContent: 'space-between',
                                                    padding: 'var(--space-3) var(--space-4)',
                                                }}>
                                                    <div>
                                                        <div style={{ fontWeight: 500, fontSize: '0.875rem' }}>{k.name}</div>
                                                        <div style={{ fontSize: '0.75rem', color: 'var(--color-text-tertiary)', marginTop: 2 }}>
                                                            Expires {new Date(k.expiresAt).toLocaleDateString()}
                                                        </div>
                                                    </div>
                                                    <button
                                                        className="btn btn-ghost"
                                                        style={{ color: 'var(--color-error)', fontSize: '0.8125rem' }}
                                                        onClick={() => handleRevokeKey(k.id)}
                                                    >
                                                        Revoke
                                                    </button>
                                                </div>
                                            ))}
                                        </div>
                                    )}
                                </div>
                            )}
                        </div>
                    </div>
                </div>,
                document.body
            )}

            {configuringProvider && createPortal(
                <ConfigureProviderModal
                    workspaceId={workspaceId}
                    providerKey={configuringProvider.key}
                    providerName={configuringProvider.name}
                    onClose={() => setConfiguringProvider(null)}
                />,
                document.body
            )}
        </>
    );
}
