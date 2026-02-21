/**
 * LiveCanvas – Real-time agent activity HUD.
 * Shows agent phase, tool activity, memory recalls, confidence, token stats.
 */
import { useState, useMemo, useEffect, useRef } from 'react';

// ── Types ────────────────────────────────────────────────────────────
interface ToolActivityData {
    status: string;
    toolName?: string;
    toolArgs?: string;
    toolResult?: string;
    thinking?: string;
}

interface AgentActivity {
    activity_type: string;
    [key: string]: unknown;
}

export interface LiveCanvasProps {
    isVisible: boolean;
    activeTask: string;
    isStreaming: boolean;
    content?: string;
    toolActivity?: ToolActivityData | null;
    agentActivities?: AgentActivity[];
}

// ── Phase Detection ──────────────────────────────────────────────────
interface PhaseInfo {
    label: string;
    icon: string;
    color: string;
}

function detectPhase(
    isStreaming: boolean,
    toolActivity?: ToolActivityData | null,
    agentActivities?: AgentActivity[],
): PhaseInfo {
    if (!isStreaming) return { label: 'IDLE', icon: '⏸', color: '#6b7280' };

    if (toolActivity?.status === 'calling') {
        return { label: 'EXECUTING', icon: '⚡', color: '#f59e0b' };
    }
    if (toolActivity?.status === 'result') {
        return { label: 'ANALYZING', icon: '🔍', color: '#8b5cf6' };
    }

    const lastActivity = agentActivities?.[agentActivities.length - 1];
    const aType = lastActivity?.activity_type || '';

    if (aType.startsWith('council_')) return { label: 'DELIBERATING', icon: '🗳️', color: '#ec4899' };
    if (aType.startsWith('reflection')) return { label: 'REFLECTING', icon: '🪞', color: '#06b6d4' };
    if (aType.startsWith('planning')) return { label: 'PLANNING', icon: '📋', color: '#3b82f6' };
    if (aType.startsWith('memory')) return { label: 'RECALLING', icon: '🧠', color: '#10b981' };

    return { label: 'THINKING', icon: '🧠', color: '#00f3ff' };
}

// ── Tool History Entry ───────────────────────────────────────────────
interface ToolHistoryEntry {
    name: string;
    args: string;
    result: string;
    timestamp: number;
}

// ── Component ────────────────────────────────────────────────────────
export function LiveCanvas({
    isVisible,
    activeTask: _activeTask,
    isStreaming,
    content = '',
    toolActivity,
    agentActivities,
}: LiveCanvasProps) {
    const [toolHistory, setToolHistory] = useState<ToolHistoryEntry[]>([]);
    const [startTime, setStartTime] = useState<number | null>(null);
    const [elapsedMs, setElapsedMs] = useState(0);
    const prevToolRef = useRef<string>('');

    // Track streaming duration
    useEffect(() => {
        if (isStreaming && !startTime) {
            setStartTime(Date.now());
        }
        if (!isStreaming && startTime) {
            setElapsedMs(Date.now() - startTime);
            setStartTime(null);
        }
    }, [isStreaming, startTime]);

    // Timer tick
    useEffect(() => {
        if (!isStreaming || !startTime) return;
        const interval = setInterval(() => {
            setElapsedMs(Date.now() - startTime);
        }, 100);
        return () => clearInterval(interval);
    }, [isStreaming, startTime]);

    // Collect tool history
    useEffect(() => {
        if (toolActivity?.status === 'result' && toolActivity.toolName) {
            const key = `${toolActivity.toolName}-${toolActivity.toolResult?.slice(0, 50)}`;
            if (key !== prevToolRef.current) {
                prevToolRef.current = key;
                setToolHistory((prev) => [
                    ...prev.slice(-9),
                    {
                        name: toolActivity.toolName || 'unknown',
                        args: toolActivity.toolArgs || '',
                        result: toolActivity.toolResult || '',
                        timestamp: Date.now(),
                    },
                ]);
            }
        }
    }, [toolActivity]);

    // Reset on new stream
    useEffect(() => {
        if (isStreaming) {
            setToolHistory([]);
        }
    }, [isStreaming]);

    const phase = useMemo(
        () => detectPhase(isStreaming, toolActivity, agentActivities),
        [isStreaming, toolActivity, agentActivities],
    );

    // Derive stats
    const charCount = content.length;
    const wordCount = content ? content.split(/\s+/).filter(Boolean).length : 0;
    const elapsedSec = (elapsedMs / 1000).toFixed(1);
    const tokPerSec = elapsedMs > 500 ? ((charCount / 4) / (elapsedMs / 1000)).toFixed(1) : '–';

    // Council activities
    const councilActivities = (agentActivities || []).filter(
        (a) => a?.activity_type?.startsWith('council_'),
    );
    const lastCouncil = councilActivities[councilActivities.length - 1];
    const councilVotes = lastCouncil?.opinions as Array<{ role: string; vote: string }> | undefined;
    const councilDecision = lastCouncil?.decision as string | undefined;

    // Memory activities
    const memoryActivities = (agentActivities || []).filter(
        (a) => a?.activity_type?.startsWith('memory'),
    );

    if (!isVisible) return null;

    return (
        <div
            style={{
                width: '380px',
                minWidth: '380px',
                background: 'var(--bg-primary)',
                borderLeft: '1px solid var(--border-subtle)',
                display: 'flex',
                flexDirection: 'column',
                height: '100%',
                overflow: 'hidden',
            }}
        >
            {/* Header */}
            <div
                style={{
                    padding: '14px 16px',
                    borderBottom: '1px solid var(--border-subtle)',
                    display: 'flex',
                    alignItems: 'center',
                    gap: '10px',
                }}
            >
                <div
                    style={{
                        width: '10px',
                        height: '10px',
                        borderRadius: '50%',
                        background: phase.color,
                        boxShadow: isStreaming ? `0 0 8px ${phase.color}` : 'none',
                        animation: isStreaming ? 'pulse 1.5s ease-in-out infinite' : 'none',
                    }}
                />
                <span
                    style={{
                        fontFamily: 'var(--font-mono)',
                        fontSize: '0.8rem',
                        color: phase.color,
                        fontWeight: 'bold',
                        letterSpacing: '0.08em',
                    }}
                >
                    {phase.icon} {phase.label}
                </span>
                <span
                    style={{
                        marginLeft: 'auto',
                        fontFamily: 'var(--font-mono)',
                        fontSize: '0.7rem',
                        color: 'var(--text-dim)',
                    }}
                >
                    LIVE HUD
                </span>
            </div>

            {/* Scrollable panels */}
            <div
                style={{
                    flex: 1,
                    overflowY: 'auto',
                    padding: '12px',
                    display: 'flex',
                    flexDirection: 'column',
                    gap: '12px',
                }}
            >
                {/* ── Response Stats Panel ─────────────────────────────── */}
                <HudPanel title="RESPONSE METRICS" accent="#00f3ff">
                    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '8px' }}>
                        <Stat label="TIME" value={`${elapsedSec}s`} />
                        <Stat label="TOK/S" value={String(tokPerSec)} />
                        <Stat label="CHARS" value={String(charCount)} />
                        <Stat label="WORDS" value={String(wordCount)} />
                    </div>
                </HudPanel>

                {/* ── Tool Activity Panel ──────────────────────────────── */}
                <HudPanel title="TOOL ACTIVITY" accent="#f59e0b" count={toolHistory.length}>
                    {toolHistory.length === 0 && !toolActivity?.toolName ? (
                        <div style={dimText}>No tools invoked yet</div>
                    ) : (
                        <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                            {/* Current active tool */}
                            {toolActivity?.status === 'calling' && toolActivity.toolName && (
                                <div
                                    style={{
                                        padding: '8px',
                                        background: 'rgba(245,158,11,0.1)',
                                        border: '1px solid rgba(245,158,11,0.3)',
                                        borderRadius: '4px',
                                        fontFamily: 'var(--font-mono)',
                                        fontSize: '0.7rem',
                                    }}
                                >
                                    <div style={{ color: '#f59e0b', fontWeight: 'bold' }}>
                                        ⚡ {toolActivity.toolName}
                                    </div>
                                    {toolActivity.toolArgs && (
                                        <div style={{ color: 'var(--text-dim)', marginTop: '4px', maxHeight: '40px', overflow: 'hidden' }}>
                                            {toolActivity.toolArgs.slice(0, 100)}
                                        </div>
                                    )}
                                </div>
                            )}
                            {/* History */}
                            {toolHistory.slice(-5).reverse().map((t, i) => (
                                <div
                                    key={i}
                                    style={{
                                        padding: '6px 8px',
                                        background: 'rgba(255,255,255,0.03)',
                                        borderRadius: '4px',
                                        fontFamily: 'var(--font-mono)',
                                        fontSize: '0.68rem',
                                    }}
                                >
                                    <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                                        <span style={{ color: '#10b981' }}>✓</span>
                                        <span style={{ color: 'var(--text-primary)' }}>{t.name}</span>
                                    </div>
                                    {t.result && (
                                        <div style={{ color: 'var(--text-dim)', marginTop: '2px', maxHeight: '30px', overflow: 'hidden' }}>
                                            {t.result.slice(0, 80)}…
                                        </div>
                                    )}
                                </div>
                            ))}
                        </div>
                    )}
                </HudPanel>

                {/* ── Council / Confidence Panel ──────────────────────── */}
                {councilVotes && councilVotes.length > 0 && (
                    <HudPanel title="COUNCIL VOTES" accent="#ec4899">
                        <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
                            {councilVotes.map((v, i) => (
                                <div
                                    key={i}
                                    style={{
                                        display: 'flex',
                                        justifyContent: 'space-between',
                                        fontFamily: 'var(--font-mono)',
                                        fontSize: '0.7rem',
                                        padding: '4px 8px',
                                        background: 'rgba(255,255,255,0.03)',
                                        borderRadius: '4px',
                                    }}
                                >
                                    <span style={{ color: 'var(--text-primary)' }}>{v.role}</span>
                                    <span
                                        style={{
                                            color:
                                                v.vote === 'approve' ? '#10b981' :
                                                    v.vote === 'reject' ? '#ef4444' :
                                                        v.vote === 'modify' ? '#f59e0b' : 'var(--text-dim)',
                                            fontWeight: 'bold',
                                        }}
                                    >
                                        {v.vote?.toUpperCase()}
                                    </span>
                                </div>
                            ))}
                            {councilDecision && (
                                <div style={{ color: '#ec4899', fontFamily: 'var(--font-mono)', fontSize: '0.7rem', marginTop: '4px', fontWeight: 'bold' }}>
                                    DECISION: {councilDecision.toUpperCase()}
                                </div>
                            )}
                        </div>
                    </HudPanel>
                )}

                {/* ── Memory Recall Panel ─────────────────────────────── */}
                {memoryActivities.length > 0 && (
                    <HudPanel title="MEMORY RECALL" accent="#10b981">
                        <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
                            {memoryActivities.slice(-5).map((m, i) => (
                                <div
                                    key={i}
                                    style={{
                                        fontFamily: 'var(--font-mono)',
                                        fontSize: '0.68rem',
                                        color: 'var(--text-dim)',
                                        padding: '4px 8px',
                                        background: 'rgba(16,185,129,0.06)',
                                        borderRadius: '4px',
                                    }}
                                >
                                    🧠 {String(m.content || m.query || m.activity_type).slice(0, 80)}
                                </div>
                            ))}
                        </div>
                    </HudPanel>
                )}

                {/* ── Thinking Preview ────────────────────────────────── */}
                {toolActivity?.thinking && (
                    <HudPanel title="AGENT REASONING" accent="#8b5cf6">
                        <div
                            style={{
                                fontFamily: 'var(--font-mono)',
                                fontSize: '0.68rem',
                                color: 'var(--text-dim)',
                                maxHeight: '80px',
                                overflow: 'hidden',
                                lineHeight: '1.4',
                            }}
                        >
                            {toolActivity.thinking.slice(0, 300)}
                        </div>
                    </HudPanel>
                )}

                {/* ── Data Stream ─────────────────────────────────────── */}
                {isStreaming && content && (
                    <HudPanel title="OUTPUT STREAM" accent="#6366f1">
                        <div
                            style={{
                                fontFamily: 'var(--font-mono)',
                                fontSize: '0.65rem',
                                color: 'var(--text-dim)',
                                maxHeight: '80px',
                                overflow: 'hidden',
                                lineHeight: '1.4',
                                wordBreak: 'break-all',
                                opacity: 0.6,
                            }}
                        >
                            {content.slice(-200)}
                        </div>
                    </HudPanel>
                )}
            </div>

            {/* Footer status bar */}
            <div
                style={{
                    padding: '8px 16px',
                    borderTop: '1px solid var(--border-subtle)',
                    fontFamily: 'var(--font-mono)',
                    fontSize: '0.65rem',
                    color: 'var(--text-dim)',
                    display: 'flex',
                    justifyContent: 'space-between',
                }}
            >
                <span>TOOLS: {toolHistory.length}</span>
                <span>ACTIVITIES: {(agentActivities || []).length}</span>
            </div>

            <style>{`
                @keyframes pulse {
                    0%, 100% { opacity: 1; }
                    50% { opacity: 0.4; }
                }
            `}</style>
        </div>
    );
}

// ── Subcomponents ────────────────────────────────────────────────────
function HudPanel({
    title,
    accent,
    count,
    children,
}: {
    title: string;
    accent: string;
    count?: number;
    children: React.ReactNode;
}) {
    return (
        <div
            style={{
                background: 'rgba(255,255,255,0.02)',
                border: '1px solid var(--border-subtle)',
                borderRadius: '6px',
                overflow: 'hidden',
            }}
        >
            <div
                style={{
                    padding: '8px 12px',
                    borderBottom: '1px solid var(--border-subtle)',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                }}
            >
                <span
                    style={{
                        fontFamily: 'var(--font-mono)',
                        fontSize: '0.65rem',
                        color: accent,
                        letterSpacing: '0.1em',
                        fontWeight: 'bold',
                    }}
                >
                    {title}
                </span>
                {count !== undefined && (
                    <span
                        style={{
                            fontFamily: 'var(--font-mono)',
                            fontSize: '0.6rem',
                            color: 'var(--text-dim)',
                            background: 'rgba(255,255,255,0.05)',
                            padding: '2px 6px',
                            borderRadius: '10px',
                        }}
                    >
                        {count}
                    </span>
                )}
            </div>
            <div style={{ padding: '10px 12px' }}>{children}</div>
        </div>
    );
}

function Stat({ label, value }: { label: string; value: string }) {
    return (
        <div style={{ textAlign: 'center' }}>
            <div
                style={{
                    fontFamily: 'var(--font-mono)',
                    fontSize: '1.1rem',
                    color: 'var(--text-primary)',
                    fontWeight: 'bold',
                }}
            >
                {value}
            </div>
            <div
                style={{
                    fontFamily: 'var(--font-mono)',
                    fontSize: '0.6rem',
                    color: 'var(--text-dim)',
                    letterSpacing: '0.1em',
                }}
            >
                {label}
            </div>
        </div>
    );
}

const dimText: React.CSSProperties = {
    fontFamily: 'var(--font-mono)',
    fontSize: '0.7rem',
    color: 'var(--text-dim)',
};
