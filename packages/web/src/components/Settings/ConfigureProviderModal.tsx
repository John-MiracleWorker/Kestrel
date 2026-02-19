import { useState, useEffect } from 'react';
import { providers } from '../../api/client';

interface ConfigureProviderModalProps {
    workspaceId: string;
    providerKey: string;     // 'local', 'openai', 'anthropic', 'google'
    providerName: string;
    onClose: () => void;
}

export function ConfigureProviderModal({ workspaceId, providerKey, providerName, onClose }: ConfigureProviderModalProps) {
    const [isLoading, setIsLoading] = useState(false);
    const [isDefault, setIsDefault] = useState(false);
    const [apiKey, setApiKey] = useState('');
    const [model, setModel] = useState('');
    const [error, setError] = useState<string | null>(null);

    // Load existing config
    useEffect(() => {
        if (!workspaceId) return;
        setIsLoading(true);
        setError(null);
        providers.list(workspaceId)
            .then((data: any) => {
                const configs = data.configs || [];
                const config = configs.find((c: any) => c.provider === providerKey);
                if (config) {
                    // Check for either camelCase (from gRPC-web sometimes) or snake_case
                    const remoteKey = config.apiKey || config.apiKeyEncrypted || config.api_key_encrypted;
                    if (remoteKey && remoteKey.startsWith('provider_key:')) {
                        setApiKey('••••••••••••'); // Placeholder — key is saved
                    } else {
                        setApiKey(remoteKey || '');
                    }
                    setModel(config.model || '');
                    setIsDefault(config.isDefault || config.is_default || false);
                }
            })
            .catch((err: any) => {
                setError('Could not load saved configuration: ' + (err?.message || 'service unavailable'));
            })
            .finally(() => setIsLoading(false));
    }, [workspaceId, providerKey]);

    const handleSave = async () => {
        setIsLoading(true);
        setError(null);
        try {
            const payload: any = {
                model: model || getDefaultModel(providerKey),
                enabled: true,
                isDefault
            };

            // Only send API Key if it's not the placeholder
            if (apiKey && apiKey !== '••••••••••••') {
                payload.apiKey = apiKey;
            }

            await providers.set(workspaceId, providerKey, payload);
            onClose();
        } catch (err: any) {
            setError(err.message || 'Failed to save configuration');
        } finally {
            setIsLoading(false);
        }
    };

    const getDefaultModel = (key: string) => {
        switch (key) {
            case 'openai': return 'gpt-4-turbo';
            case 'anthropic': return 'claude-3-opus-20240229';
            case 'google': return 'gemini-1.5-flash';
            case 'local': return 'llama-3-8b-instruct';
            default: return '';
        }
    };

    return (
        <div style={{
            position: 'fixed',
            inset: 0,
            zIndex: 10000,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            background: 'rgba(0, 0, 0, 0.7)',
            backdropFilter: 'blur(2px)',
        }} onClick={onClose}>
            <div
                className="card animate-scale-in"
                style={{
                    width: '100%',
                    maxWidth: 480,
                    padding: 'var(--space-6)',
                    background: 'var(--color-bg)',
                    border: '1px solid var(--color-border)',
                }}
                onClick={(e) => e.stopPropagation()}
            >
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 'var(--space-4)' }}>
                    <h3 style={{ fontSize: '1.25rem', fontWeight: 600 }}>
                        Configure {providerName}
                    </h3>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)' }}>
                        <input
                            type="checkbox"
                            id="isDefault"
                            checked={isDefault}
                            onChange={(e) => setIsDefault(e.target.checked)}
                        />
                        <label htmlFor="isDefault" style={{ fontSize: '0.875rem', cursor: 'pointer' }}>Set as Default</label>
                    </div>
                </div>

                {error && (
                    <div style={{
                        padding: 'var(--space-3)',
                        background: 'rgba(239, 68, 68, 0.1)',
                        color: '#ef4444',
                        borderRadius: 'var(--radius-sm)',
                        marginBottom: 'var(--space-4)',
                        fontSize: '0.875rem'
                    }}>
                        {error}
                    </div>
                )}

                <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-4)' }}>
                    {providerKey !== 'local' && (
                        <div className="form-group">
                            <label>API Key</label>
                            <input
                                className="input"
                                type="password"
                                value={apiKey}
                                onChange={(e) => setApiKey(e.target.value)}
                                placeholder={`Enter your ${providerName} API Key`}
                            />
                        </div>
                    )}

                    <div className="form-group">
                        <label>Model</label>
                        <input
                            className="input"
                            value={model}
                            onChange={(e) => setModel(e.target.value)}
                            placeholder={getDefaultModel(providerKey)}
                        />
                        <p style={{ fontSize: '0.75rem', color: 'var(--color-text-tertiary)', marginTop: 'var(--space-1)' }}>
                            Leave blank to use default: {getDefaultModel(providerKey)}
                        </p>
                    </div>

                    <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 'var(--space-3)', marginTop: 'var(--space-2)' }}>
                        <button className="btn btn-ghost" onClick={onClose} disabled={isLoading}>
                            Cancel
                        </button>
                        <button className="btn btn-primary" onClick={handleSave} disabled={isLoading}>
                            {isLoading ? 'Saving...' : 'Save Configuration'}
                        </button>
                    </div>
                </div>
            </div>
        </div>
    );
}
