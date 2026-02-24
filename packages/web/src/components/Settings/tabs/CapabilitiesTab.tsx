import { useState } from 'react';
import { S } from '../constants';
import { Toggle } from '../Shared';
import { request, CapabilityItem } from '../../../api/client';

interface CapabilitiesTabProps {
    workspaceId: string;
    capabilities: CapabilityItem[];
    setCapabilities: React.Dispatch<React.SetStateAction<CapabilityItem[]>>;
    setError: (err: string | null) => void;
}

export function CapabilitiesTab({
    workspaceId,
    capabilities,
    setCapabilities,
    setError
}: CapabilitiesTabProps) {
    const [installingCap, setInstallingCap] = useState<string | null>(null);

    const toggleCapability = async (capId: string) => {
        const cap = capabilities.find(c => c.id === capId);
        if (!cap) return;

        if (!cap.installed) {
            setInstallingCap(capId);
            try {
                await request(`/workspaces/${workspaceId}/capabilities/${capId}/install`, { method: 'POST' });
                setCapabilities(prev => prev.map(c => c.id === capId ? { ...c, installed: true, enabled: true } : c));
            } catch (err: any) {
                setError(err.message || 'Failed to install capability');
                setTimeout(() => setError(null), 3000);
            } finally {
                setInstallingCap(null);
            }
        } else {
            try {
                const newEnabledState = !cap.enabled;
                setCapabilities(prev => prev.map(c => c.id === capId ? { ...c, enabled: newEnabledState } : c));
                await request(`/workspaces/${workspaceId}/capabilities/${capId}`, {
                    method: 'PATCH',
                    body: { enabled: newEnabledState }
                });
            } catch (err: any) {
                setCapabilities(prev => prev.map(c => c.id === capId ? { ...c, enabled: cap.enabled } : c));
                setError(err.message || 'Failed to toggle capability');
                setTimeout(() => setError(null), 3000);
            }
        }
    };

    return (
        <div>
            <div style={S.sectionTitle}>// CORE CAPABILITIES</div>
            <p style={{ fontSize: '0.75rem', color: '#666', marginBottom: '20px', lineHeight: 1.5 }}>
                Enable or disable deep system integrations. These are highly privileged capabilities built directly into Kestrel's core.
            </p>

            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(380px, 1fr))', gap: '12px' }}>
                {capabilities.map(cap => (
                    <div key={cap.id} style={{
                        padding: '16px', background: '#111', border: '1px solid #1a1a1a',
                        borderRadius: '6px', display: 'flex', flexDirection: 'column'
                    }}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: '12px' }}>
                            <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                                <span style={{ fontSize: '1.2rem' }}>{cap.icon || '⚡'}</span>
                                <div>
                                    <div style={{ fontSize: '0.85rem', fontWeight: 600, color: '#e0e0e0' }}>{cap.name}</div>
                                    <div style={{ fontSize: '0.65rem', color: '#555', marginTop: '2px' }}>{cap.id}</div>
                                </div>
                            </div>
                            {cap.installed ? (
                                <Toggle value={cap.enabled || false} onChange={() => toggleCapability(cap.id)} />
                            ) : (
                                <button
                                    style={{
                                        ...S.btnGhost, padding: '4px 12px', fontSize: '0.65rem',
                                        color: '#00f3ff', borderColor: 'rgba(0,243,255,0.3)',
                                        opacity: installingCap === cap.id ? 0.5 : 1
                                    }}
                                    disabled={installingCap === cap.id}
                                    onClick={() => toggleCapability(cap.id)}
                                >
                                    {installingCap === cap.id ? 'Installing...' : 'Install'}
                                </button>
                            )}
                        </div>
                        <div style={{ fontSize: '0.75rem', color: '#888', lineHeight: 1.5, flex: 1 }}>
                            {cap.description}
                        </div>
                        {cap.requires_mcp && cap.requires_mcp.length > 0 && (
                            <div style={{ marginTop: '12px', fontSize: '0.65rem', color: '#555', borderTop: '1px solid #1a1a1a', paddingTop: '10px' }}>
                                Requires MCP: {cap.requires_mcp.join(', ')}
                            </div>
                        )}
                    </div>
                ))}
            </div>
            {capabilities.length === 0 && (
                <div style={{ textAlign: 'center', padding: '40px', color: '#555', fontSize: '0.8rem', border: '1px dashed #222', borderRadius: '8px' }}>
                    No capabilities available for this workspace.
                </div>
            )}
        </div>
    );
}
