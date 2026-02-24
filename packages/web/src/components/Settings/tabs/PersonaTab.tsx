import { S } from '../constants';

interface PersonaTabProps {
    systemPrompt: string;
    setSystemPrompt: (prompt: string) => void;
}

export function PersonaTab({ systemPrompt, setSystemPrompt }: PersonaTabProps) {
    return (
        <div>
            <div style={S.sectionTitle}>// SYSTEM PROMPT</div>
            <p style={{ fontSize: '0.75rem', color: '#666', marginBottom: '16px', lineHeight: 1.5 }}>
                Define Kestrel's personality, instructions, and behavior. Leave empty for the default autonomous agent persona.
            </p>
            <div style={S.field}>
                <textarea style={S.textarea} value={systemPrompt}
                    onChange={e => setSystemPrompt(e.target.value)}
                    placeholder={`You are Kestrel, an autonomous AI agent...\n\nCustomize tone, role, domain expertise, or specific instructions here.`}
                    onFocus={e => { e.target.style.borderColor = '#00f3ff'; }}
                    onBlur={e => { e.target.style.borderColor = '#333'; }} />
                <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: '6px' }}>
                    <span style={{ fontSize: '0.65rem', color: '#444' }}>{systemPrompt.length} characters</span>
                    {systemPrompt && (
                        <button style={{ ...S.btnGhost, padding: '4px 10px', fontSize: '0.7rem' }}
                            onClick={() => setSystemPrompt('')}>Reset to default</button>
                    )}
                </div>
            </div>
        </div>
    );
}
