import { useState } from 'react';

interface SettingsPanelProps {
    onClose: () => void;
    userEmail?: string;
    userDisplayName?: string;
}

export function SettingsPanel({ onClose, userEmail, userDisplayName }: SettingsPanelProps) {
    const [activeTab, setActiveTab] = useState<'profile' | 'providers' | 'api-keys'>('profile');

    const tabs = [
        { id: 'profile' as const, label: 'Profile', icon: '👤' },
        { id: 'providers' as const, label: 'Providers', icon: '🤖' },
        { id: 'api-keys' as const, label: 'API Keys', icon: '🔑' },
    ];

    return (
        <div style={{
            position: 'fixed',
            inset: 0,
            zIndex: 100,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            background: 'rgba(0, 0, 0, 0.6)',
            backdropFilter: 'blur(4px)',
        }} onClick={onClose}>
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
                            <button className="btn btn-primary" style={{ alignSelf: 'flex-start' }}>
                                Save Changes
                            </button>
                        </div>
                    )}

                    {activeTab === 'providers' && (
                        <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-4)' }}>
                            <p style={{ color: 'var(--color-text-secondary)', fontSize: '0.875rem' }}>
                                Configure which LLM provider to use for this workspace.
                            </p>

                            {['Local (llama.cpp)', 'OpenAI', 'Anthropic', 'Google'].map((name) => (
                                <div key={name} className="card" style={{
                                    display: 'flex',
                                    alignItems: 'center',
                                    justifyContent: 'space-between',
                                    padding: 'var(--space-4)',
                                }}>
                                    <div>
                                        <p style={{ fontWeight: 500 }}>{name}</p>
                                        <p style={{
                                            fontSize: '0.8125rem',
                                            color: 'var(--color-text-tertiary)',
                                        }}>
                                            {name === 'Local (llama.cpp)'
                                                ? 'On-device inference — no API key needed'
                                                : 'Cloud provider — requires API key'}
                                        </p>
                                    </div>
                                    <button className="btn btn-secondary">Configure</button>
                                </div>
                            ))}
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
                                <button className="btn btn-primary">
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
        </div>
    );
}
