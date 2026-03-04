import React, { useState, useEffect, useCallback } from 'react';
import { S } from '../constants';
import { request } from '../../../api/client';

interface OllamaModel {
    name: string;
    size?: number;
    parameterSize?: string;
    quantization?: string;
    family?: string;
}

interface OllamaServer {
    url: string;
    host: string;
    models: OllamaModel[];
    score: number;
}

function fmtSize(bytes?: number): string {
    if (!bytes) return '';
    const gb = bytes / 1e9;
    return gb >= 1 ? `${gb.toFixed(1)} GB` : `${(bytes / 1e6).toFixed(0)} MB`;
}

export function OllamaTab({ workspaceId }: { workspaceId: string }) {
    const [servers, setServers] = useState<OllamaServer[]>([]);
    const [scanning, setScanning] = useState(false);
    const [activeModel, setActiveModel] = useState<string | null>(null);
    const [activeServer, setActiveServer] = useState<string | null>(null);
    const [activating, setActivating] = useState<string | null>(null);
    const [statusMsg, setStatusMsg] = useState('');

    const scan = useCallback(
        async (force = false) => {
            setScanning(true);
            try {
                const endpoint = force ? '/ollama/servers/rescan' : '/ollama/servers';
                const data = (await request(endpoint)) as any;
                const srvs: OllamaServer[] = data.servers || [];
                if (srvs.length > 0 || force || servers.length === 0) {
                    setServers(srvs);
                }
            } catch {
                /* silent */
            } finally {
                setScanning(false);
            }
        },
        [servers.length],
    );

    useEffect(() => {
        scan();
    }, []);

    const useModel = async (server: OllamaServer, modelName: string) => {
        setActivating(`${server.url}::${modelName}`);
        setStatusMsg('');
        try {
            await request(`/workspaces/${workspaceId}/providers/ollama`, {
                method: 'PUT',
                body: {
                    model: modelName,
                    isDefault: true,
                    settings: { ollama_host: server.url },
                },
            });
            setActiveModel(modelName);
            setActiveServer(server.url);
            setStatusMsg(`✓ Now using ${modelName} from ${server.url}`);
            setTimeout(() => setStatusMsg(''), 5000);
        } catch (e: any) {
            setStatusMsg(`✗ Failed: ${e.message}`);
        } finally {
            setActivating(null);
        }
    };

    return (
        <div>
            {/* Header */}
            <div
                style={{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                    marginBottom: 16,
                }}
            >
                <div style={S.sectionTitle}>// OLLAMA NETWORK SERVERS</div>
                <button
                    style={{ ...S.btnGhost, fontSize: '0.7rem', padding: '5px 12px' }}
                    onClick={() => scan(true)}
                    disabled={scanning}
                >
                    {scanning ? '◌ Scanning...' : '⟳ Scan Network'}
                </button>
            </div>

            {/* Status message */}
            {statusMsg && (
                <div
                    style={{
                        fontSize: '0.75rem',
                        marginBottom: 14,
                        padding: '10px 14px',
                        borderRadius: 4,
                        color: statusMsg.startsWith('✓') ? '#00ff9d' : '#ff0055',
                        background: statusMsg.startsWith('✓')
                            ? 'rgba(0,255,157,0.06)'
                            : 'rgba(255,0,85,0.06)',
                        border: `1px solid ${statusMsg.startsWith('✓') ? 'rgba(0,255,157,0.25)' : 'rgba(255,0,85,0.25)'}`,
                    }}
                >
                    {statusMsg}
                </div>
            )}

            {/* Loading state */}
            {scanning && servers.length === 0 && (
                <div
                    style={{
                        textAlign: 'center',
                        padding: '30px 0',
                        color: '#444',
                        fontSize: '0.75rem',
                    }}
                >
                    Scanning local network for Ollama instances...
                </div>
            )}

            {/* Empty state */}
            {!scanning && servers.length === 0 && (
                <div
                    style={{
                        textAlign: 'center',
                        padding: '30px 0',
                        color: '#333',
                        fontSize: '0.75rem',
                    }}
                >
                    No Ollama servers found.{' '}
                    <span style={{ color: '#555' }}>
                        Run <code style={{ color: '#00f3ff' }}>OLLAMA_ORIGINS=* ollama serve</code>{' '}
                        on remote machines.
                    </span>
                </div>
            )}

            {/* Server + Model list — ALL MODELS VISIBLE, each with USE button */}
            {servers.map((srv) => (
                <div key={srv.url} style={{ marginBottom: 20 }}>
                    {/* Server header */}
                    <div
                        style={{
                            display: 'flex',
                            alignItems: 'center',
                            gap: 8,
                            padding: '10px 14px',
                            background: '#0c0c0c',
                            border: '1px solid #1a1a1a',
                            borderBottom: 'none',
                            borderRadius: '4px 4px 0 0',
                        }}
                    >
                        <span style={{ color: '#00f3ff', fontSize: '0.9rem' }}>⬡</span>
                        <div style={{ flex: 1 }}>
                            <div style={{ fontSize: '0.8rem', fontWeight: 500, color: '#c0c0c0' }}>
                                {srv.url}
                            </div>
                            <div style={{ fontSize: '0.62rem', color: '#444', marginTop: 2 }}>
                                {srv.models.length} model{srv.models.length !== 1 ? 's' : ''}{' '}
                                available
                            </div>
                        </div>
                    </div>

                    {/* Models — each is a selectable row with big USE button */}
                    <div
                        style={{
                            border: '1px solid #1a1a1a',
                            borderRadius: '0 0 4px 4px',
                            overflow: 'hidden',
                        }}
                    >
                        {srv.models.length === 0 && (
                            <div style={{ padding: '14px', fontSize: '0.72rem', color: '#444' }}>
                                No models installed on this server
                            </div>
                        )}
                        {srv.models.map((m, i) => {
                            const isActive = activeModel === m.name && activeServer === srv.url;
                            const isLoading = activating === `${srv.url}::${m.name}`;
                            return (
                                <div
                                    key={m.name}
                                    style={{
                                        display: 'flex',
                                        alignItems: 'center',
                                        gap: 12,
                                        padding: '12px 14px',
                                        background: isActive
                                            ? 'rgba(0,243,255,0.06)'
                                            : i % 2 === 0
                                              ? '#0e0e0e'
                                              : '#111',
                                        borderTop: i > 0 ? '1px solid #1a1a1a' : 'none',
                                        borderLeft: isActive
                                            ? '3px solid #00f3ff'
                                            : '3px solid transparent',
                                        transition: 'all 0.15s',
                                    }}
                                >
                                    {/* Model info */}
                                    <div style={{ flex: 1, minWidth: 0 }}>
                                        <div
                                            style={{
                                                fontSize: '0.82rem',
                                                color: isActive ? '#00f3ff' : '#d0d0d0',
                                                fontFamily: 'JetBrains Mono, monospace',
                                                fontWeight: isActive ? 600 : 400,
                                            }}
                                        >
                                            {m.name}
                                        </div>
                                        <div
                                            style={{
                                                fontSize: '0.62rem',
                                                color: '#555',
                                                marginTop: 3,
                                            }}
                                        >
                                            {[
                                                m.parameterSize,
                                                m.quantization,
                                                fmtSize(m.size),
                                                m.family,
                                            ]
                                                .filter(Boolean)
                                                .join(' · ')}
                                        </div>
                                    </div>

                                    {/* USE button — big and obvious */}
                                    {isActive ? (
                                        <div
                                            style={{
                                                fontSize: '0.7rem',
                                                padding: '6px 16px',
                                                borderRadius: 4,
                                                background: '#00f3ff',
                                                color: '#000',
                                                fontWeight: 700,
                                                fontFamily: 'JetBrains Mono, monospace',
                                                letterSpacing: '0.05em',
                                            }}
                                        >
                                            ✓ ACTIVE
                                        </div>
                                    ) : (
                                        <button
                                            disabled={isLoading}
                                            onClick={() => useModel(srv, m.name)}
                                            style={{
                                                fontSize: '0.72rem',
                                                padding: '7px 18px',
                                                borderRadius: 4,
                                                background: isLoading
                                                    ? 'rgba(0,243,255,0.1)'
                                                    : 'rgba(0,243,255,0.08)',
                                                color: '#00f3ff',
                                                border: '1px solid rgba(0,243,255,0.3)',
                                                cursor: isLoading ? 'wait' : 'pointer',
                                                fontFamily: 'JetBrains Mono, monospace',
                                                fontWeight: 600,
                                                transition: 'all 0.15s',
                                                opacity: isLoading ? 0.6 : 1,
                                                whiteSpace: 'nowrap' as const,
                                            }}
                                        >
                                            {isLoading ? '⟳ Activating...' : '▶ USE THIS MODEL'}
                                        </button>
                                    )}
                                </div>
                            );
                        })}
                    </div>
                </div>
            ))}

            {/* Help tip */}
            {servers.length > 0 && (
                <div
                    style={{
                        marginTop: 16,
                        padding: '10px 14px',
                        background: '#0a0a0a',
                        border: '1px solid #1a1a1a',
                        borderRadius: 4,
                    }}
                >
                    <div style={{ fontSize: '0.62rem', color: '#444', lineHeight: 1.7 }}>
                        <span style={{ color: '#00f3ff' }}>TIP:</span> Click{' '}
                        <strong style={{ color: '#555' }}>▶ USE THIS MODEL</strong> to set it as
                        your active Ollama model.
                    </div>
                </div>
            )}
        </div>
    );
}
