import React, { useState, useEffect } from 'react';
import { S } from '../constants';
import { ApiKey, apiKeys as keysApi } from '../../../api/client';

interface ApiKeysTabProps {
    workspaceId: string;
}

export function ApiKeysTab({ workspaceId }: ApiKeysTabProps) {
    const [apiKeys, setApiKeys] = useState<ApiKey[]>([]);
    const [keysLoading, setKeysLoading] = useState(false);
    const [newKeyName, setNewKeyName] = useState('');
    const [createdKey, setCreatedKey] = useState<{ id: string; key: string, name: string } | null>(null);
    const [error, setError] = useState<string | null>(null);

    useEffect(() => {
        setKeysLoading(true);
        keysApi.list()
            .then((data: any) => setApiKeys(data?.keys || []))
            .catch(() => setApiKeys([]))
            .finally(() => setKeysLoading(false));
    }, [workspaceId]);

    const handleCreateKey = async () => {
        if (!newKeyName.trim()) return;
        try {
            const data = await keysApi.create(newKeyName);
            setCreatedKey(data);
            setNewKeyName('');
            setApiKeys(prev => [...prev, data as unknown as ApiKey]);
        } catch (err) {
            console.error('Failed to create key');
        }
    };

    const handleRevokeKey = async (keyId: string) => {
        try {
            await keysApi.revoke(keyId);
            setApiKeys(prev => prev.filter(k => k.id !== keyId));
        } catch (err) {
            console.error('Failed to revoke key');
        }
    };

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
            ) : apiKeys.length === 0 ? (
                <div style={{
                    textAlign: 'center', padding: '32px', color: '#444', fontSize: '0.8rem',
                    border: '1px dashed #222', borderRadius: '4px',
                }}>No API keys yet</div>
            ) : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                    {apiKeys.map((k: any) => (
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
}
