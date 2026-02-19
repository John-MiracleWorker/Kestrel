import { useState, useEffect } from 'react';
import { createPortal } from 'react-dom';
import { ConfigureProviderModal } from './ConfigureProviderModal';
import { providers } from '../../api/client';

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

    // Fetch provider configs whenever the providers tab is shown (or after a modal closes)
    useEffect(() => {
        if (activeTab !== 'providers' || !workspaceId) return;
        providers.list(workspaceId)
            .then((data: any) => setProviderConfigs(data?.configs || []))
            .catch(() => setProviderConfigs([]));
    }, [activeTab, workspaceId, configuringProvider]); // re-fetch after modal closes

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
                                        Configure which LLM provider to use for this workspace. The <strong>Default</strong> provider is used when no provider is selected in chat.
                                    </p>

                                    {['Local (llama.cpp)', 'OpenAI', 'Anthropic', 'Google'].map((name) => {
                                        const key = name.toLowerCase().split(' ')[0];
                                        const cfg = providerConfigs.find((c: any) => c.provider === key);
                                        const isConfigured = !!cfg;
                                        const isDefault = cfg?.is_default || cfg?.isDefault;
                                        const configuredModel = cfg?.model;

                                        return (
                                            <div key={name} className="card" style={{
                                                display: 'flex',
                                                alignItems: 'center',
                                                justifyContent: 'space-between',
                                                padding: 'var(--space-4)',
                                                borderColor: isDefault ? 'var(--color-brand)' : undefined,
                                            }}>
                                                <div>
                                                    <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)', flexWrap: 'wrap' }}>
                                                        <p style={{ fontWeight: 500 }}>{name}</p>
                                                        {isDefault && (
                                                            <span className="badge badge-success" style={{ fontSize: '0.7rem' }}>
                                                                Default
                                                            </span>
                                                        )}
                                                        {isConfigured && !isDefault && (
                                                            <span className="badge" style={{ fontSize: '0.7rem', opacity: 0.7 }}>
                                                                Configured
                                                            </span>
                                                        )}
                                                    </div>
                                                    <p style={{
                                                        fontSize: '0.8125rem',
                                                        color: 'var(--color-text-tertiary)',
                                                    }}>
                                                        {configuredModel
                                                            ? `Model: ${configuredModel}`
                                                            : name === 'Local (llama.cpp)'
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
                                    <div style={{
                                        display: 'flex',
                                        justifyContent: 'space-between',
                                        alignItems: 'center',
                                    }}>
                                        <p style={{ color: 'var(--color-text-secondary)', fontSize: '0.875rem' }}>
                                            API keys for programmatic access to Kestrel.
                                        </p>
                                        <button className="btn btn-primary" onClick={() => alert('API key management coming soon')}>
                                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                                                <path d="M12 5v14M5 12h14" />
                                            </svg>
                                            Create Key
                                        </button>
                                    </div>

                                    <div className="card" style={{
                                        textAlign: 'center',
                                        padding: 'var(--space-8)',
                                        color: 'var(--color-text-tertiary)',
                                        fontSize: '0.875rem',
                                    }}>
                                        No API keys created yet.
                                    </div>
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
