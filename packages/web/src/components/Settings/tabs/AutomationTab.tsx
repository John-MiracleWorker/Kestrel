import { S } from '../constants';
import { Toggle, SliderField } from '../Shared';

interface AutomationTabProps {
    cronEnabled: boolean;
    setCronEnabled: (enabled: boolean) => void;
    cronSchedule: string;
    setCronSchedule: (schedule: string) => void;
    cronMaxRuns: number;
    setCronMaxRuns: (runs: number) => void;
    cronSystemPrompt: string;
    setCronSystemPrompt: (prompt: string) => void;
}

export function AutomationTab({
    cronEnabled,
    setCronEnabled,
    cronSchedule,
    setCronSchedule,
    cronMaxRuns,
    setCronMaxRuns,
    cronSystemPrompt,
    setCronSystemPrompt
}: AutomationTabProps) {
    return (
        <div>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
                <div style={S.sectionTitle}>// BACKGROUND TASKS</div>
                <Toggle value={cronEnabled} onChange={setCronEnabled} />
            </div>

            <p style={{ fontSize: '0.75rem', color: '#666', marginBottom: '20px', lineHeight: 1.5 }}>
                Allow Kestrel to wake up automatically on a schedule to perform routine tasks, checks, and cleanups in this workspace.
            </p>

            {cronEnabled && (
                <div style={{ padding: '16px', background: '#0d0d0d', border: '1px solid #1a1a1a', borderRadius: '4px' }}>
                    <div style={S.field}>
                        <label style={S.label}>Cron Schedule</label>
                        <input style={{ ...S.input, fontFamily: "'JetBrains Mono', monospace" }}
                            value={cronSchedule} onChange={e => setCronSchedule(e.target.value)}
                            placeholder="0 * * * * (Hourly)" />
                        <div style={{ fontSize: '0.65rem', color: '#555', marginTop: '6px' }}>
                            Standard cron syntax (e.g. <code>0 9 * * 1-5</code> for weekdays at 9am). Defaults to <code>0 * * * *</code> (every hour).
                        </div>
                    </div>

                    <SliderField label="Max Consecutive Runs" value={cronMaxRuns}
                        onChange={v => setCronMaxRuns(Math.round(v))} min={1} max={10} step={1}
                        format={v => `${v} runs`} />

                    <div style={{ ...S.field, marginBottom: 0 }}>
                        <label style={S.label}>Custom Instructions for Scheduled Runs</label>
                        <textarea style={{ ...S.textarea, minHeight: 100 }}
                            value={cronSystemPrompt} onChange={e => setCronSystemPrompt(e.target.value)}
                            placeholder="What should Kestrel do when it wakes up? (e.g., 'Check my emails, summarize unread ones, and check the weather.')" />
                    </div>
                </div>
            )}
        </div>
    );
}
