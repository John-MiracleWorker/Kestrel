import { S, RISK_COLORS } from '../constants';
import { SliderField } from '../Shared';

interface AgentTabProps {
    maxIterations: number;
    setMaxIterations: (v: number) => void;
    maxToolCalls: number;
    setMaxToolCalls: (v: number) => void;
    maxWallTime: number;
    setMaxWallTime: (v: number) => void;
    autoApproveRisk: string;
    setAutoApproveRisk: (level: string) => void;
}

export function AgentTab({
    maxIterations,
    setMaxIterations,
    maxToolCalls,
    setMaxToolCalls,
    maxWallTime,
    setMaxWallTime,
    autoApproveRisk,
    setAutoApproveRisk
}: AgentTabProps) {
    return (
        <div>
            <div style={S.sectionTitle}>// AUTONOMY</div>
            <p style={{ fontSize: '0.75rem', color: '#666', marginBottom: '20px', lineHeight: 1.5 }}>
                Control how far Kestrel can go autonomously. These guardrails apply to all tasks.
            </p>

            <SliderField label="Max Iterations" value={maxIterations}
                onChange={v => setMaxIterations(Math.round(v))} min={1} max={100} step={1}
                format={v => `${v} iterations`} />

            <SliderField label="Max Tool Calls" value={maxToolCalls}
                onChange={v => setMaxToolCalls(Math.round(v))} min={1} max={200} step={1}
                format={v => `${v} calls`} />

            <SliderField label="Max Wall Time" value={maxWallTime}
                onChange={v => setMaxWallTime(Math.round(v))} min={30} max={3600} step={30}
                format={v => v >= 60 ? `${Math.floor(v / 60)}m ${v % 60}s` : `${v}s`} />

            <div style={S.sectionTitle}>// APPROVAL POLICY</div>
            <p style={{ fontSize: '0.75rem', color: '#666', marginBottom: '16px', lineHeight: 1.5 }}>
                Auto-approve tool calls at or below this risk level. Higher-risk actions always need your OK.
            </p>
            <div style={S.field}>
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: '8px' }}>
                    {(['low', 'medium', 'high', 'critical'] as const).map(level => (
                        <button key={level} type="button" style={{
                            padding: '10px', borderRadius: '4px', cursor: 'pointer',
                            fontFamily: "'JetBrains Mono', monospace", fontSize: '0.75rem',
                            fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.05em',
                            background: autoApproveRisk === level ? 'rgba(0,243,255,0.08)' : '#111',
                            border: `1px solid ${autoApproveRisk === level ? RISK_COLORS[level] : '#222'}`,
                            color: RISK_COLORS[level],
                            transition: 'all 0.15s',
                        }} onClick={() => setAutoApproveRisk(level)}>
                            {level}
                        </button>
                    ))}
                </div>
                <div style={{ fontSize: '0.65rem', color: '#444', marginTop: '8px' }}>
                    {autoApproveRisk === 'low' && 'Only safe, read-only operations run automatically.'}
                    {autoApproveRisk === 'medium' && 'Web browsing and file writes run without approval.'}
                    {autoApproveRisk === 'high' && 'Code execution and most actions run automatically.'}
                    {autoApproveRisk === 'critical' && '⚠ All operations run without approval. Use with caution.'}
                </div>
            </div>

            <div style={{ padding: '14px', background: '#0d0d0d', borderRadius: '4px', border: '1px solid #1a1a1a', marginTop: '8px' }}>
                <div style={{ fontSize: '0.7rem', color: '#666', lineHeight: 1.6 }}>
                    <span style={{ color: '#00ff9d' }}>✦ Self-Extending:</span> Kestrel can create new tools during tasks using
                    the <span style={{ color: '#00f3ff' }}>create_skill</span> tool.
                    All skill creations require your explicit approval regardless of the auto-approve policy.
                </div>
            </div>
        </div>
    );
}
