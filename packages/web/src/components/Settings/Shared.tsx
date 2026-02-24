import { S, RISK_COLORS } from './constants';

export const Toggle = ({ value, onChange }: { value: boolean; onChange: (v: boolean) => void }) => (
    <button style={S.toggle(value)} onClick={() => onChange(!value)} type="button">
        <span style={S.toggleDot(value)} />
    </button>
);

export const SliderField = ({ label, value, onChange, min, max, step, format }: {
    label: string; value: number; onChange: (v: number) => void;
    min: number; max: number; step: number; format?: (v: number) => string;
}) => (
    <div style={S.field}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' }}>
            <label style={S.label}>{label}</label>
            <span style={{ fontSize: '0.8rem', color: '#00f3ff', fontWeight: 600, fontFamily: "'JetBrains Mono', monospace" }}>
                {format ? format(value) : value}
            </span>
        </div>
        <input
            type="range" min={min} max={max} step={step} value={value}
            onChange={e => onChange(parseFloat(e.target.value))} style={S.slider}
        />
        <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: '4px' }}>
            <span style={{ fontSize: '0.65rem', color: '#444' }}>{min}</span>
            <span style={{ fontSize: '0.65rem', color: '#444' }}>{max}</span>
        </div>
    </div>
);

export const RiskBadge = ({ level }: { level: string }) => (
    <span style={{
        fontSize: '0.6rem', padding: '2px 6px', borderRadius: '3px',
        border: `1px solid ${RISK_COLORS[level] || '#555'}`,
        color: RISK_COLORS[level] || '#555', fontWeight: 600,
        textTransform: 'uppercase' as const, letterSpacing: '0.05em',
    }}>
        {level}
    </span>
);
