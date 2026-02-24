import React from 'react';

/* ── KestrelProcessBar ────────────────────────────────────────────── */

type Activity = { activity_type: string;[key: string]: unknown };

interface PhaseData {
    key: string;
    icon: string;
    label: string;
    color: string;
    summary: string;
    items: Activity[];
}

const VOTE_COLORS: Record<string, string> = {
    approve: '#22c55e',
    reject: '#ef4444',
    conditional: '#f59e0b',
    abstain: '#6b7280',
};
const ROLE_ICONS: Record<string, string> = {
    architect: '🏗️',
    implementer: '⚙️',
    security: '🔒',
    devils_advocate: '😈',
    user_advocate: '👤',
};

function buildPhases(
    activities: Activity[],
    toolActivity?: { status: string; toolName?: string } | null,
): PhaseData[] {
    const phases: PhaseData[] = [];
    const byType = (prefix: string) =>
        activities.filter((a) => a?.activity_type?.startsWith(prefix));

    // Memory
    const memories = byType('memory_recalled');
    if (memories.length > 0) {
        const count = (memories[0].count as number) || 0;
        phases.push({
            key: 'memory',
            icon: '🧠',
            label: 'Memory',
            color: '#06b6d4',
            summary: `${count} recalled`,
            items: memories,
        });
    }

    // Lessons
    const lessons = byType('lessons_loaded');
    if (lessons.length > 0) {
        const count = (lessons[0].count as number) || 0;
        phases.push({
            key: 'lessons',
            icon: '📖',
            label: 'Lessons',
            color: '#8b5cf6',
            summary: `${count} loaded`,
            items: lessons,
        });
    }

    // Skills
    const skills = byType('skill_activated');
    if (skills.length > 0) {
        const count = (skills[0].count as number) || 0;
        phases.push({
            key: 'skills',
            icon: '🔧',
            label: 'Skills',
            color: '#f97316',
            summary: `${count} active`,
            items: skills,
        });
    }

    // Plan
    const plans = byType('plan_created');
    if (plans.length > 0) {
        const stepCount = (plans[0].step_count as number) || 0;
        phases.push({
            key: 'plan',
            icon: '📋',
            label: 'Plan',
            color: '#3b82f6',
            summary: `${stepCount} steps`,
            items: plans,
        });
    }

    // Tools (from toolActivity state)
    if (toolActivity) {
        const toolItems = activities.filter(
            (a) =>
                a.activity_type === 'tool_calling' ||
                a.activity_type === 'tool_result' ||
                a.activity_type === 'calling' ||
                a.activity_type === 'result',
        );
        const toolLabel =
            toolActivity.status === 'thinking'
                ? 'Reasoning'
                : toolActivity.status === 'planning'
                    ? 'Planning'
                    : toolActivity.status === 'calling' || toolActivity.status === 'tool_calling'
                        ? toolActivity.toolName || 'Tool'
                        : toolActivity.status === 'result' || toolActivity.status === 'tool_result'
                            ? `${toolActivity.toolName} ✓`
                            : 'Working';
        phases.push({
            key: 'tools',
            icon: '⚡',
            label: toolLabel,
            color: '#8b5cf6',
            summary:
                toolActivity.status === 'result' || toolActivity.status === 'tool_result'
                    ? 'done'
                    : '…',
            items: toolItems,
        });
    }

    // Council
    const council = byType('council_');
    if (council.length > 0) {
        const verdict = council.find((a) => a.activity_type === 'council_verdict');
        const consensus = verdict
            ? (verdict.has_consensus as boolean)
                ? 'consensus'
                : 'divided'
            : '…';
        phases.push({
            key: 'council',
            icon: '🤔',
            label: 'Council',
            color: '#f59e0b',
            summary: consensus,
            items: council,
        });
    }

    // Delegation
    const delegation = byType('delegation_');
    if (delegation.length > 0) {
        const done = delegation.find((a) => a.activity_type === 'delegation_complete');
        phases.push({
            key: 'delegation',
            icon: '🔀',
            label: 'Delegation',
            color: '#3b82f6',
            summary: done ? String(done.specialist) : '…',
            items: delegation,
        });
    }

    // Reflection
    const reflection = byType('reflection_');
    if (reflection.length > 0) {
        const verdict = reflection.find((a) => a.activity_type === 'reflection_verdict');
        const conf = verdict ? `${((verdict.confidence as number) * 100).toFixed(0)}%` : '…';
        phases.push({
            key: 'reflection',
            icon: '🔍',
            label: 'Reflection',
            color: '#a855f7',
            summary: conf,
            items: reflection,
        });
    }

    // Evidence
    const evidence = byType('evidence_summary');
    if (evidence.length > 0) {
        const count = (evidence[0].decision_count as number) || 0;
        phases.push({
            key: 'evidence',
            icon: '📎',
            label: 'Evidence',
            color: '#14b8a6',
            summary: `${count} decisions`,
            items: evidence,
        });
    }

    // Confidence (from reflection verdict)
    const reflVerdict = activities.find((a) => a.activity_type === 'reflection_verdict');
    if (reflVerdict) {
        const conf = ((reflVerdict.confidence as number) || 0) * 100;
        phases.push({
            key: 'confidence',
            icon: '🎯',
            label: 'Confidence',
            color: conf >= 80 ? '#22c55e' : conf >= 50 ? '#f59e0b' : '#ef4444',
            summary: `${conf.toFixed(0)}%`,
            items: [reflVerdict],
        });
    }

    // Tokens
    const tokens = byType('token_usage');
    if (tokens.length > 0) {
        const total = (tokens[0].total_tokens as number) || 0;
        const display = total >= 1000 ? `${(total / 1000).toFixed(1)}k` : String(total);
        phases.push({
            key: 'tokens',
            icon: '💰',
            label: 'Tokens',
            color: '#6b7280',
            summary: display,
            items: tokens,
        });
    }

    return phases;
}

export function KestrelProcessBar({
    activities,
    toolActivity,
}: {
    activities: Activity[];
    toolActivity?: {
        status: string;
        toolName?: string;
        toolArgs?: string;
        toolResult?: string;
        thinking?: string;
    } | null;
}) {
    const phases = buildPhases(activities, toolActivity);

    if (phases.length === 0 && !toolActivity) return null;

    // Determine current active status label
    const currentLabel = toolActivity
        ? toolActivity.status === 'thinking'
            ? '🧠 Reasoning…'
            : toolActivity.status === 'planning'
                ? '📋 Planning…'
                : toolActivity.status === 'calling' || toolActivity.status === 'tool_calling'
                    ? `⚡ Using ${toolActivity.toolName || 'tool'}…`
                    : toolActivity.status === 'result' || toolActivity.status === 'tool_result'
                        ? `✅ ${toolActivity.toolName || 'Tool'} complete`
                        : '🔄 Working…'
        : '🔄 Processing…';

    const isActive =
        toolActivity?.status === 'calling' ||
        toolActivity?.status === 'thinking' ||
        toolActivity?.status === 'planning';
    const isDone = toolActivity?.status === 'result';
    const accentColor = isActive ? '#a855f7' : isDone ? '#10b981' : '#00f3ff';

    return (
        <div
            style={{
                marginBottom: '12px',
                borderRadius: '8px',
                overflow: 'hidden',
                border: `1px solid ${accentColor}44`,
                background: `linear-gradient(135deg, ${accentColor}08, ${accentColor}15)`,
                animation: 'processbar-in 0.3s ease-out',
            }}
        >
            {/* Animated progress line */}
            {isActive && (
                <div
                    style={{
                        height: '2px',
                        background: `linear-gradient(90deg, transparent, ${accentColor}, transparent)`,
                        animation: 'progress-slide 1.5s ease-in-out infinite',
                    }}
                />
            )}

            {/* Main status */}
            <div
                style={{
                    padding: '10px 14px',
                    display: 'flex',
                    alignItems: 'center',
                    gap: '10px',
                }}
            >
                {/* Pulsing dot */}
                {isActive && (
                    <div
                        style={{
                            width: '8px',
                            height: '8px',
                            borderRadius: '50%',
                            background: accentColor,
                            boxShadow: `0 0 8px ${accentColor}`,
                            animation: 'pulse-dot 1.2s ease-in-out infinite',
                            flexShrink: 0,
                        }}
                    />
                )}
                <span
                    style={{
                        fontFamily: 'var(--font-mono)',
                        fontSize: '0.82rem',
                        fontWeight: 600,
                        color: accentColor,
                        letterSpacing: '0.02em',
                    }}
                >
                    {currentLabel}
                </span>
                {toolActivity?.toolArgs && isActive && (
                    <span
                        style={{
                            fontFamily: 'var(--font-mono)',
                            fontSize: '0.7rem',
                            color: 'var(--text-dim)',
                            overflow: 'hidden',
                            textOverflow: 'ellipsis',
                            whiteSpace: 'nowrap',
                            maxWidth: '200px',
                        }}
                    >
                        {toolActivity.toolArgs.slice(0, 60)}
                    </span>
                )}
            </div>

            {/* Phase pills (completed phases) */}
            {phases.length > 1 && (
                <div
                    style={{
                        padding: '0 14px 10px',
                        display: 'flex',
                        flexWrap: 'wrap',
                        gap: '6px',
                    }}
                >
                    {phases.map((phase) => (
                        <span
                            key={phase.key}
                            style={{
                                display: 'inline-flex',
                                alignItems: 'center',
                                gap: '4px',
                                padding: '2px 10px',
                                borderRadius: '12px',
                                background: `${phase.color}20`,
                                border: `1px solid ${phase.color}40`,
                                fontFamily: 'var(--font-mono)',
                                fontSize: '0.68rem',
                                color: phase.color,
                                fontWeight: 500,
                            }}
                        >
                            {phase.icon} {phase.label}
                        </span>
                    ))}
                </div>
            )}

            {/* Thinking preview */}
            {toolActivity?.thinking && (
                <div
                    style={{
                        padding: '0 14px 10px',
                        fontFamily: 'var(--font-mono)',
                        fontSize: '0.7rem',
                        color: 'var(--text-dim)',
                        lineHeight: 1.4,
                        maxHeight: '40px',
                        overflow: 'hidden',
                        opacity: 0.7,
                    }}
                >
                    💭 {toolActivity.thinking.slice(0, 150)}
                </div>
            )}

            <style>{`
                @keyframes processbar-in {
                    from { opacity: 0; transform: translateY(-4px); }
                    to { opacity: 1; transform: translateY(0); }
                }
                @keyframes progress-slide {
                    0% { transform: translateX(-100%); }
                    100% { transform: translateX(100%); }
                }
                @keyframes pulse-dot {
                    0%, 100% { opacity: 1; transform: scale(1); }
                    50% { opacity: 0.4; transform: scale(0.8); }
                }
            `}</style>
        </div>
    );
}

export function PhaseDetail({ item, phaseKey }: { item: Activity; phaseKey: string }) {
    const dim = { color: 'var(--text-dim)' };

    // Memory — show entities and preview
    if (phaseKey === 'memory') {
        return (
            <div style={dim}>
                Queried: {String((item.entities as string[])?.join(', ') || '—')}
                <br />
                {String((item.preview as string)?.substring(0, 150) || '')}
            </div>
        );
    }

    // Plan — show numbered steps
    if (phaseKey === 'plan' && item.steps) {
        return (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '2px' }}>
                {(item.steps as Array<{ index: number; description: string }>).map((step, i) => (
                    <div key={i} style={{ ...dim, display: 'flex', gap: '6px' }}>
                        <span style={{ color: '#3b82f6', minWidth: '16px' }}>
                            {step.index + 1}.
                        </span>
                        <span>{step.description}</span>
                    </div>
                ))}
            </div>
        );
    }

    // Council opinions — show role, vote, analysis
    if (item.activity_type === 'council_opinion') {
        const voteColor = VOTE_COLORS[item.vote as string] || '#888';
        return (
            <div
                style={{
                    display: 'flex',
                    gap: '8px',
                    padding: '2px 0',
                    borderBottom: '1px solid rgba(255,255,255,0.05)',
                }}
            >
                <span style={{ minWidth: '24px' }}>{ROLE_ICONS[item.role as string] || '•'}</span>
                <span
                    style={{
                        color: voteColor,
                        fontWeight: 600,
                        minWidth: '90px',
                        textTransform: 'uppercase',
                    }}
                >
                    {String(item.vote)}
                </span>
                <span style={dim}>
                    {String((item.analysis as string)?.substring(0, 120) || '')}
                </span>
            </div>
        );
    }

    // Council verdict
    if (item.activity_type === 'council_verdict') {
        return (
            <div
                style={{
                    color: (item.has_consensus as boolean) ? '#22c55e' : '#ef4444',
                    fontWeight: 600,
                    marginTop: '4px',
                }}
            >
                {(item.has_consensus as boolean) ? '✓ CONSENSUS REACHED' : '⚠ NO CONSENSUS'}
                {item.requires_user_review ? ' — User review required' : ''}
            </div>
        );
    }

    // Reflection critique
    if (item.activity_type === 'reflection_critique') {
        const sevColor =
            item.severity === 'critical'
                ? '#ef4444'
                : item.severity === 'high'
                    ? '#f59e0b'
                    : '#6b7280';
        return (
            <div style={{ display: 'flex', gap: '8px', padding: '2px 0' }}>
                <span
                    style={{
                        color: sevColor,
                        fontWeight: 600,
                        textTransform: 'uppercase',
                        fontSize: '0.68rem',
                        minWidth: '55px',
                    }}
                >
                    {String(item.severity)}
                </span>
                <span style={dim}>
                    {String((item.description as string)?.substring(0, 150) || '')}
                </span>
            </div>
        );
    }

    // Evidence decisions
    if (phaseKey === 'evidence' && item.decisions) {
        return (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '2px' }}>
                {(item.decisions as Array<{ type: string; description: string }>).map((d, i) => (
                    <div key={i} style={{ ...dim, display: 'flex', gap: '6px' }}>
                        <span style={{ color: '#14b8a6', minWidth: '80px' }}>{d.type}</span>
                        <span>{d.description}</span>
                    </div>
                ))}
            </div>
        );
    }

    // Token usage
    if (phaseKey === 'tokens') {
        return (
            <div style={dim}>
                Total: {String(item.total_tokens)} tokens · {String(item.iterations)} iterations ·{' '}
                {String(item.tool_calls)} tool calls
            </div>
        );
    }

    // Generic — show preview or message
    const text = String(item.preview || item.message || item.description || '');
    if (text) return <div style={dim}>{text.substring(0, 200)}</div>;
    return null;
}

