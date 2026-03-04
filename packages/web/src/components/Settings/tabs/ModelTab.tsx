import React, { useState, useEffect } from 'react';
import { S, PROVIDER_META, DISPLAY_MASK } from '../constants';
import { ProviderConfig } from '../types';
import { SliderField } from '../Shared';
import { request } from '../../../api/client';

interface OllamaModel {
    name: string;
    size?: number;
    parameterSize?: string;
    quantization?: string;
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

interface ModelTabProps {
    providerConfigs: ProviderConfig[];
    selectedProvider: string;
    onProviderSelect: (providerKey: string) => void;
    apiKeyInput: string;
    setApiKeyInput: (key: string) => void;
    model: string;
    setModel: (model: string) => void;
    availableModels: any[];
    temperature: number;
    setTemperature: (temp: number) => void;
    maxTokens: number;
    setMaxTokens: (tokens: number) => void;
    workspaceId: string;
}

export function ModelTab({
    providerConfigs,
    selectedProvider,
    onProviderSelect,
    apiKeyInput,
    setApiKeyInput,
    model,
    setModel,
    availableModels,
    temperature,
    setTemperature,
    maxTokens,
    setMaxTokens,
    workspaceId,
}: ModelTabProps) {
    const [ollamaServers, setOllamaServers] = useState<OllamaServer[]>([]);
    const [ollamaScanning, setOllamaScanning] = useState(false);
    const [usingModel, setUsingModel] = useState<string | null>(null);
    const [useStatus, setUseStatus] = useState('');

    useEffect(() => {
        scanOllama();
    }, []);

    const scanOllama = async (forceRescan = false) => {
        setOllamaScanning(true);
        try {
            const endpoint = forceRescan ? '/ollama/servers/rescan' : '/ollama/servers';
            const data = (await request(endpoint)) as any;
            const servers: OllamaServer[] = data.servers || [];
            // Only update if we got results, or if this was forced
            if (servers.length > 0 || forceRescan || ollamaServers.length === 0) {
                setOllamaServers(servers);
            }
        } catch {
            /* silent */
        } finally {
            setOllamaScanning(false);
        }
    };

    const activateModel = async (server: OllamaServer, modelName: string) => {
        setUsingModel(`${server.url}::${modelName}`);
        setUseStatus('');
        try {
            await request(`/workspaces/${workspaceId}/providers/ollama`, {
                method: 'PUT',
                body: { model: modelName, isDefault: true },
            });
            onProviderSelect('ollama');
            setModel(modelName);
            setUseStatus(`✓ Now using ${modelName}`);
            setTimeout(() => setUseStatus(''), 4000);
        } catch (e: any) {
            setUseStatus(`✗ ${e.message}`);
        } finally {
            setUsingModel(null);
        }
    };

    return (
        <div>
            {/* ── Cloud / Local Providers ─────────────────────────────── */}
            <div style={S.sectionTitle}>// AI PROVIDER</div>
            <div
                style={{
                    display: 'grid',
                    gridTemplateColumns: '1fr 1fr',
                    gap: '10px',
                    marginBottom: '24px',
                }}
            >
                {Object.entries(PROVIDER_META).map(([key, meta]) => {
                    const active = selectedProvider === key;
                    const config = providerConfigs.find((c: ProviderConfig) => c.provider === key);
                    const isDefault = config?.isDefault || config?.is_default;
                    return (
                        <div
                            key={key}
                            style={S.providerCard(active)}
                            onClick={() => onProviderSelect(key)}
                        >
                            <span
                                style={{ fontSize: '1.1rem', color: active ? '#00f3ff' : '#555' }}
                            >
                                {meta.icon}
                            </span>
                            <div style={{ flex: 1 }}>
                                <div
                                    style={{
                                        fontSize: '0.8rem',
                                        fontWeight: 500,
                                        color: active ? '#e0e0e0' : '#888',
                                    }}
                                >
                                    {meta.name}
                                </div>
                                <div
                                    style={{ fontSize: '0.65rem', color: '#444', marginTop: '2px' }}
                                >
                                    {meta.requiresKey ? 'Cloud API' : 'On-device'}
                                </div>
                            </div>
                            {isDefault && <span style={S.badge}>DEFAULT</span>}
                        </div>
                    );
                })}
            </div>

            {/* ── Ollama Network Servers — ALWAYS VISIBLE ─────────────── */}
            <div
                style={{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                    marginBottom: 6,
                }}
            >
                <div style={S.sectionTitle}>// OLLAMA ON NETWORK</div>
                <button
                    style={{ ...S.btnGhost, fontSize: '0.65rem', padding: '4px 10px' }}
                    onClick={() => scanOllama(true)}
                    disabled={ollamaScanning}
                >
                    {ollamaScanning ? '◌ Scanning...' : '⟳ Scan'}
                </button>
            </div>

            {useStatus && (
                <div
                    style={{
                        fontSize: '0.72rem',
                        marginBottom: 10,
                        padding: '8px 12px',
                        borderRadius: 4,
                        color: useStatus.startsWith('✓') ? '#00ff9d' : '#ff0055',
                        background: useStatus.startsWith('✓')
                            ? 'rgba(0,255,157,0.06)'
                            : 'rgba(255,0,85,0.06)',
                        border: `1px solid ${useStatus.startsWith('✓') ? 'rgba(0,255,157,0.2)' : 'rgba(255,0,85,0.2)'}`,
                    }}
                >
                    {useStatus}
                </div>
            )}

            {/* Show servers (even while scanning in background) */}
            {ollamaServers.length === 0 && !ollamaScanning && (
                <div
                    style={{
                        fontSize: '0.72rem',
                        color: '#333',
                        padding: '10px 0 16px',
                        textAlign: 'center',
                    }}
                >
                    No Ollama servers found —{' '}
                    <span style={{ color: '#555' }}>
                        start Ollama with <code style={{ color: '#00f3ff' }}>OLLAMA_ORIGINS=*</code>
                    </span>
                </div>
            )}
            {ollamaServers.length === 0 && ollamaScanning && (
                <div
                    style={{
                        fontSize: '0.72rem',
                        color: '#444',
                        padding: '10px 0',
                        textAlign: 'center',
                    }}
                >
                    Scanning local network for Ollama instances...
                </div>
            )}

            {/* FLAT list of every model on every server — no expand needed */}
            {ollamaServers.map((srv) => (
                <div key={srv.url} style={{ marginBottom: 12 }}>
                    {/* Server label */}
                    <div
                        style={{
                            fontSize: '0.62rem',
                            color: '#555',
                            marginBottom: 6,
                            letterSpacing: '0.05em',
                            display: 'flex',
                            alignItems: 'center',
                            gap: 6,
                        }}
                    >
                        <span style={{ color: '#00f3ff' }}>⬡</span>
                        {srv.url}
                        <span style={{ color: '#333' }}>·</span>
                        <span style={{ color: '#444' }}>
                            {srv.models.length} model{srv.models.length !== 1 ? 's' : ''}
                        </span>
                    </div>

                    {/* Model rows — always shown */}
                    {srv.models.map((m) => {
                        const isActive = selectedProvider === 'ollama' && model === m.name;
                        const isLoading = usingModel === `${srv.url}::${m.name}`;
                        return (
                            <div
                                key={m.name}
                                onClick={() =>
                                    !isActive && !isLoading && activateModel(srv, m.name)
                                }
                                style={{
                                    display: 'flex',
                                    alignItems: 'center',
                                    gap: 10,
                                    padding: '10px 12px',
                                    marginBottom: 4,
                                    borderRadius: 4,
                                    background: isActive ? 'rgba(0,243,255,0.06)' : '#111',
                                    border: `1px solid ${isActive ? 'rgba(0,243,255,0.25)' : '#1a1a1a'}`,
                                    cursor: isActive ? 'default' : 'pointer',
                                    transition: 'all 0.15s',
                                }}
                            >
                                {/* Active dot */}
                                <div
                                    style={{
                                        width: 8,
                                        height: 8,
                                        borderRadius: '50%',
                                        flexShrink: 0,
                                        background: isActive ? '#00f3ff' : '#2a2a2a',
                                        boxShadow: isActive
                                            ? '0 0 8px rgba(0,243,255,0.6)'
                                            : 'none',
                                        transition: 'all 0.2s',
                                    }}
                                />

                                {/* Model info */}
                                <div style={{ flex: 1, minWidth: 0 }}>
                                    <div
                                        style={{
                                            fontSize: '0.8rem',
                                            color: isActive ? '#00f3ff' : '#c0c0c0',
                                            fontFamily: 'JetBrains Mono, monospace',
                                            fontWeight: isActive ? 600 : 400,
                                        }}
                                    >
                                        {m.name}
                                    </div>
                                    <div
                                        style={{ fontSize: '0.6rem', color: '#444', marginTop: 2 }}
                                    >
                                        {[m.parameterSize, m.quantization, fmtSize(m.size)]
                                            .filter(Boolean)
                                            .join(' · ') || 'Ollama model'}
                                    </div>
                                </div>

                                {/* Action */}
                                {isActive ? (
                                    <span
                                        style={{
                                            ...S.badge,
                                            background: '#00f3ff',
                                            color: '#000',
                                        }}
                                    >
                                        ACTIVE
                                    </span>
                                ) : (
                                    <button
                                        disabled={isLoading}
                                        onClick={(e) => {
                                            e.stopPropagation();
                                            activateModel(srv, m.name);
                                        }}
                                        style={{
                                            fontSize: '0.7rem',
                                            padding: '5px 14px',
                                            borderRadius: 4,
                                            background: 'rgba(0,243,255,0.08)',
                                            color: '#00f3ff',
                                            border: '1px solid rgba(0,243,255,0.25)',
                                            cursor: 'pointer',
                                            fontFamily: 'JetBrains Mono, monospace',
                                            fontWeight: 500,
                                            transition: 'all 0.15s',
                                            opacity: isLoading ? 0.5 : 1,
                                        }}
                                    >
                                        {isLoading ? '⟳ Activating...' : '▶ Use This Model'}
                                    </button>
                                )}
                            </div>
                        );
                    })}
                </div>
            ))}

            {/* ── API Key (cloud providers only) ──────────────────────── */}
            {PROVIDER_META[selectedProvider]?.requiresKey && (
                <div style={{ marginTop: 20 }}>
                    <div style={S.sectionTitle}>// API KEY</div>
                    <div style={S.field}>
                        <input
                            style={S.input}
                            type="password"
                            value={apiKeyInput}
                            onChange={(e) => setApiKeyInput(e.target.value)}
                            placeholder={`Enter ${PROVIDER_META[selectedProvider]?.name} API key`}
                            onFocus={(e) => {
                                e.target.style.borderColor = '#00f3ff';
                            }}
                            onBlur={(e) => {
                                e.target.style.borderColor = '#333';
                            }}
                        />
                    </div>
                </div>
            )}

            {/* ── Model selector (cloud / non-ollama) ─────────────────── */}
            {selectedProvider !== 'ollama' && (
                <>
                    <div style={S.sectionTitle}>// MODEL</div>
                    <div style={S.field}>
                        <select
                            style={S.select}
                            value={model}
                            onChange={(e) => setModel(e.target.value)}
                        >
                            <option value="">— Select model —</option>
                            {availableModels.map((m) => (
                                <option key={m.id} value={m.id}>
                                    {m.name || m.id}
                                </option>
                            ))}
                        </select>
                        {model && (
                            <div style={{ fontSize: '0.7rem', color: '#555', marginTop: '6px' }}>
                                Active: <span style={{ color: '#00ff9d' }}>{model}</span>
                            </div>
                        )}
                    </div>
                </>
            )}

            {/* ── Parameters ──────────────────────────────────────────── */}
            <div style={S.sectionTitle}>// PARAMETERS</div>
            <SliderField
                label="Temperature"
                value={temperature}
                onChange={setTemperature}
                min={0}
                max={2}
                step={0.1}
                format={(v) => v.toFixed(1)}
            />
            <div style={S.field}>
                <label style={S.label}>Max Tokens</label>
                <input
                    style={S.input}
                    type="number"
                    value={maxTokens}
                    onChange={(e) =>
                        setMaxTokens(Math.max(1, Math.min(32768, parseInt(e.target.value) || 1)))
                    }
                    min={1}
                    max={32768}
                    onFocus={(e) => {
                        e.target.style.borderColor = '#00f3ff';
                    }}
                    onBlur={(e) => {
                        e.target.style.borderColor = '#333';
                    }}
                />
                <div style={{ fontSize: '0.65rem', color: '#444', marginTop: '4px' }}>
                    Range: 1 – 32,768
                </div>
            </div>
        </div>
    );
}
