import { S, PROVIDER_META, isKeyMasked, DISPLAY_MASK } from '../constants';
import { ProviderConfig } from '../types';
import { SliderField } from '../Shared';

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
    setMaxTokens
}: ModelTabProps) {
    return (
        <div>
            <div style={S.sectionTitle}>// PROVIDER</div>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '10px', marginBottom: '24px' }}>
                {Object.entries(PROVIDER_META).map(([key, meta]) => {
                    const active = selectedProvider === key;
                    const config = providerConfigs.find((c: ProviderConfig) => c.provider === key);
                    const isDefault = config?.isDefault || config?.is_default;
                    return (
                        <div key={key} style={S.providerCard(active)} onClick={() => onProviderSelect(key)}>
                            <span style={{ fontSize: '1.1rem', color: active ? '#00f3ff' : '#555' }}>{meta.icon}</span>
                            <div style={{ flex: 1 }}>
                                <div style={{ fontSize: '0.8rem', fontWeight: 500, color: active ? '#e0e0e0' : '#888' }}>{meta.name}</div>
                                <div style={{ fontSize: '0.65rem', color: '#444', marginTop: '2px' }}>{meta.requiresKey ? 'Cloud API' : 'On-device'}</div>
                            </div>
                            {isDefault && <span style={S.badge}>DEFAULT</span>}
                        </div>
                    );
                })}
            </div>

            {PROVIDER_META[selectedProvider]?.requiresKey && (
                <div>
                    <div style={S.sectionTitle}>// API KEY</div>
                    <div style={S.field}>
                        <input style={S.input} type="password" value={apiKeyInput}
                            onChange={e => setApiKeyInput(e.target.value)}
                            placeholder={`Enter ${PROVIDER_META[selectedProvider]?.name} API key`}
                            onFocus={e => { e.target.style.borderColor = '#00f3ff'; }}
                            onBlur={e => { e.target.style.borderColor = '#333'; }}
                        />
                    </div>
                </div>
            )}

            <div style={S.sectionTitle}>// MODEL</div>
            <div style={S.field}>
                <select style={S.select} value={model} onChange={e => setModel(e.target.value)}>
                    <option value="">— Select model —</option>
                    {availableModels.map(m => (
                        <option key={m.id} value={m.id}>{m.name || m.id}</option>
                    ))}
                </select>
                {model && (
                    <div style={{ fontSize: '0.7rem', color: '#555', marginTop: '6px' }}>
                        Active: <span style={{ color: '#00ff9d' }}>{model}</span>
                    </div>
                )}
            </div>

            <div style={S.sectionTitle}>// PARAMETERS</div>
            <SliderField label="Temperature" value={temperature} onChange={setTemperature}
                min={0} max={2} step={0.1} format={v => v.toFixed(1)} />
            <div style={S.field}>
                <label style={S.label}>Max Tokens</label>
                <input style={S.input} type="number" value={maxTokens}
                    onChange={e => setMaxTokens(Math.max(1, Math.min(32768, parseInt(e.target.value) || 1)))}
                    min={1} max={32768}
                    onFocus={e => { e.target.style.borderColor = '#00f3ff'; }}
                    onBlur={e => { e.target.style.borderColor = '#333'; }} />
                <div style={{ fontSize: '0.65rem', color: '#444', marginTop: '4px' }}>Range: 1 – 32,768</div>
            </div>
        </div>
    );
}
