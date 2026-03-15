import type { CapabilityItem, RuntimeProfile, TaskArtifactItem } from '../../api/client';

import { panelStyle } from './OperationsShared';

type OperationsMetaSectionProps = {
    taskArtifacts: TaskArtifactItem[];
    artifacts: TaskArtifactItem[];
    runtimeProfile: RuntimeProfile | null;
    integrationStatus: Record<string, any> | null;
    capabilityItems: CapabilityItem[];
};

export function OperationsMetaSection({
    taskArtifacts,
    artifacts,
    runtimeProfile,
    integrationStatus,
    capabilityItems,
}: OperationsMetaSectionProps) {
    return (
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: '16px' }}>
            <div style={panelStyle()}>
                <div
                    style={{
                        fontSize: '1rem',
                        fontWeight: 700,
                        color: 'var(--text-primary)',
                        marginBottom: '8px',
                    }}
                >
                    Artifact Explorer
                </div>
                <div
                    style={{
                        display: 'grid',
                        gap: '8px',
                        maxHeight: '280px',
                        overflowY: 'auto',
                    }}
                >
                    {(taskArtifacts.length > 0 ? taskArtifacts : artifacts).map((artifact) => (
                        <div
                            key={artifact.id}
                            style={{
                                padding: '10px',
                                borderRadius: 'var(--radius-md)',
                                background: 'rgba(255, 255, 255, 0.03)',
                            }}
                        >
                            <div
                                style={{
                                    display: 'flex',
                                    justifyContent: 'space-between',
                                    gap: '8px',
                                }}
                            >
                                <div style={{ fontWeight: 600 }}>{artifact.title}</div>
                                <div style={{ fontSize: '0.72rem', color: 'var(--text-dim)' }}>
                                    v{artifact.version}
                                </div>
                            </div>
                            <div
                                style={{
                                    fontSize: '0.78rem',
                                    color: 'var(--text-secondary)',
                                    marginTop: '4px',
                                }}
                            >
                                {artifact.componentType} ·{' '}
                                {artifact.updatedAt
                                    ? new Date(artifact.updatedAt).toLocaleString()
                                    : ''}
                            </div>
                        </div>
                    ))}
                    {artifacts.length === 0 && (
                        <div style={{ color: 'var(--text-dim)' }}>No artifacts available.</div>
                    )}
                </div>
            </div>

            <div style={panelStyle()}>
                <div
                    style={{
                        fontSize: '1rem',
                        fontWeight: 700,
                        color: 'var(--text-primary)',
                        marginBottom: '8px',
                    }}
                >
                    Runtime Profile
                </div>
                {!runtimeProfile && (
                    <div style={{ color: 'var(--text-dim)' }}>Runtime profile unavailable.</div>
                )}
                {runtimeProfile && (
                    <div
                        style={{
                            display: 'grid',
                            gap: '8px',
                            fontSize: '0.84rem',
                            color: 'var(--text-secondary)',
                        }}
                    >
                        <div>
                            Mode:{' '}
                            <span style={{ color: 'var(--text-primary)' }}>
                                {runtimeProfile.runtimeMode}
                            </span>
                        </div>
                        <div>
                            Policy:{' '}
                            <span style={{ color: 'var(--text-primary)' }}>
                                {runtimeProfile.policyName} v{runtimeProfile.policyVersion}
                            </span>
                        </div>
                        <div>
                            Docker:{' '}
                            <span
                                style={{
                                    color: runtimeProfile.dockerEnabled
                                        ? 'var(--accent-green)'
                                        : '#ff8ca5',
                                }}
                            >
                                {runtimeProfile.dockerEnabled ? 'enabled' : 'disabled'}
                            </span>
                        </div>
                        <div>
                            Native:{' '}
                            <span
                                style={{
                                    color: runtimeProfile.nativeEnabled
                                        ? '#ffb36a'
                                        : 'var(--text-dim)',
                                }}
                            >
                                {runtimeProfile.nativeEnabled ? 'enabled' : 'disabled'}
                            </span>
                        </div>
                        <div>
                            Fallback visible:{' '}
                            <span style={{ color: 'var(--text-primary)' }}>
                                {runtimeProfile.hybridFallbackVisible ? 'yes' : 'no'}
                            </span>
                        </div>
                        {runtimeProfile.hostMounts.length > 0 && (
                            <div>
                                Mounts:
                                {runtimeProfile.hostMounts.map((mount) => (
                                    <div
                                        key={mount.path}
                                        style={{
                                            marginTop: '4px',
                                            color: 'var(--text-primary)',
                                        }}
                                    >
                                        {mount.path} ({mount.mode})
                                    </div>
                                ))}
                            </div>
                        )}
                    </div>
                )}
            </div>

            <div style={panelStyle()}>
                <div
                    style={{
                        fontSize: '1rem',
                        fontWeight: 700,
                        color: 'var(--text-primary)',
                        marginBottom: '8px',
                    }}
                >
                    Channel And Capability Health
                </div>
                <div style={{ display: 'grid', gap: '8px', fontSize: '0.84rem' }}>
                    {integrationStatus &&
                        Object.entries(integrationStatus).map(([name, value]) => (
                            <div
                                key={name}
                                style={{
                                    color: (value as any).connected
                                        ? 'var(--accent-green)'
                                        : '#ff8ca5',
                                }}
                            >
                                {name}:{' '}
                                {(value as any).status ||
                                    ((value as any).connected ? 'connected' : 'disconnected')}
                            </div>
                        ))}
                    {capabilityItems.slice(0, 5).map((item) => (
                        <div key={item.name} style={{ color: 'var(--text-secondary)' }}>
                            {item.name}:{' '}
                            <span style={{ color: 'var(--text-primary)' }}>{item.status}</span>
                        </div>
                    ))}
                </div>
            </div>
        </div>
    );
}
