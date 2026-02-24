/**
 * ActivityTimeline – Collapsible vertical timeline of agent tool activity.
 * Groups activities by phase and shows expandable detail panels.
 */
import { useState } from 'react';

interface ToolEntry {
    name: string;
    args?: string;
    result?: string;
    timestamp: number;
}

interface AgentActivity {
    activity_type: string;
    [key: string]: unknown;
}

interface ActivityTimelineProps {
    toolActivity?: {
        status: string;
        toolName?: string;
        toolArgs?: string;
        toolResult?: string;
        thinking?: string;
    } | null;
    agentActivities?: AgentActivity[];
}

interface TimelineNode {
    id: string;
    icon: string;
    label: string;
    detail?: string;
    color: string;
    timestamp: number;
    status: 'active' | 'complete' | 'pending';
}

function buildTimeline(
    toolActivity: ActivityTimelineProps['toolActivity'],
    agentActivities: AgentActivity[] = [],
): TimelineNode[] {
    const nodes: TimelineNode[] = [];
    let idx = 0;

    // Process agent activities in order
    for (const act of agentActivities) {
        const aType = act.activity_type || '';

        if (aType.startsWith('planning')) {
            nodes.push({
                id: `act-${idx++}`,
                icon: '📋',
                label: 'Planning',
                detail: String(act.plan || act.content || '').slice(0, 120),
                color: '#3b82f6',
                timestamp: Date.now() - (agentActivities.length - idx) * 1000,
                status: 'complete',
            });
        } else if (aType.startsWith('council_')) {
            const decision = String(act.decision || '');
            nodes.push({
                id: `act-${idx++}`,
                icon: '🗳️',
                label: 'Council Vote',
                detail: decision ? `Decision: ${decision}` : undefined,
                color: '#ec4899',
                timestamp: Date.now() - (agentActivities.length - idx) * 1000,
                status: 'complete',
            });
        } else if (aType.startsWith('memory')) {
            nodes.push({
                id: `act-${idx++}`,
                icon: '🧠',
                label: 'Memory Recall',
                detail: String(act.content || act.query || '').slice(0, 100),
                color: '#10b981',
                timestamp: Date.now() - (agentActivities.length - idx) * 1000,
                status: 'complete',
            });
        } else if (aType.startsWith('reflection')) {
            nodes.push({
                id: `act-${idx++}`,
                icon: '🪞',
                label: 'Reflection',
                detail: String(act.content || '').slice(0, 100),
                color: '#06b6d4',
                timestamp: Date.now() - (agentActivities.length - idx) * 1000,
                status: 'complete',
            });
        } else if (aType === 'tool_called' || aType === 'tool_result') {
            nodes.push({
                id: `act-${idx++}`,
                icon: aType === 'tool_called' ? '⚡' : '✓',
                label: String(act.tool_name || act.tool || 'Tool'),
                detail: String(act.tool_result || act.tool_args || '').slice(0, 100),
                color: aType === 'tool_called' ? '#f59e0b' : '#10b981',
                timestamp: Date.now() - (agentActivities.length - idx) * 1000,
                status: 'complete',
            });
        }
    }

    // Current active tool
    if (toolActivity?.status === 'calling' && toolActivity.toolName) {
        nodes.push({
            id: 'current-tool',
            icon: '⚡',
            label: toolActivity.toolName,
            detail: toolActivity.toolArgs?.slice(0, 100),
            color: '#f59e0b',
            timestamp: Date.now(),
            status: 'active',
        });
    } else if (toolActivity?.status === 'result' && toolActivity.toolName) {
        nodes.push({
            id: 'current-result',
            icon: '✓',
            label: toolActivity.toolName,
            detail: toolActivity.toolResult?.slice(0, 100),
            color: '#10b981',
            timestamp: Date.now(),
            status: 'complete',
        });
    } else if (toolActivity?.thinking) {
        nodes.push({
            id: 'thinking',
            icon: '💭',
            label: 'Reasoning',
            detail: toolActivity.thinking.slice(0, 120),
            color: '#8b5cf6',
            timestamp: Date.now(),
            status: 'active',
        });
    }

    return nodes;
}

export function ActivityTimeline({ toolActivity, agentActivities = [] }: ActivityTimelineProps) {
    const [expanded, setExpanded] = useState<Set<string>>(new Set());
    const nodes = buildTimeline(toolActivity, agentActivities);

    if (nodes.length === 0) return null;

    const toggle = (id: string) => {
        setExpanded(prev => {
            const next = new Set(prev);
            if (next.has(id)) next.delete(id);
            else next.add(id);
            return next;
        });
    };

    return (
        <div
            style={{
                display: 'flex',
                flexDirection: 'column',
                gap: '0',
                padding: '8px 0',
                marginBottom: '8px',
            }}
        >
            {nodes.map((node, i) => {
                const isLast = i === nodes.length - 1;
                const isExpanded = expanded.has(node.id);

                return (
                    <div
                        key={node.id}
                        style={{ display: 'flex', gap: '10px', cursor: node.detail ? 'pointer' : 'default' }}
                        onClick={() => node.detail && toggle(node.id)}
                    >
                        {/* Timeline line + dot */}
                        <div
                            style={{
                                display: 'flex',
                                flexDirection: 'column',
                                alignItems: 'center',
                                width: '20px',
                                flexShrink: 0,
                            }}
                        >
                            <div
                                style={{
                                    width: '8px',
                                    height: '8px',
                                    borderRadius: '50%',
                                    background: node.color,
                                    boxShadow: node.status === 'active' ? `0 0 8px ${node.color}` : 'none',
                                    animation: node.status === 'active' ? 'timeline-pulse 1.5s ease-in-out infinite' : 'none',
                                    flexShrink: 0,
                                    marginTop: '4px',
                                }}
                            />
                            {!isLast && (
                                <div
                                    style={{
                                        width: '1px',
                                        flex: 1,
                                        minHeight: '16px',
                                        background: `linear-gradient(to bottom, ${node.color}44, transparent)`,
                                    }}
                                />
                            )}
                        </div>

                        {/* Content */}
                        <div style={{ flex: 1, paddingBottom: isLast ? '0' : '6px', minWidth: 0 }}>
                            <div
                                style={{
                                    display: 'flex',
                                    alignItems: 'center',
                                    gap: '6px',
                                    fontFamily: 'var(--font-mono)',
                                    fontSize: '0.72rem',
                                }}
                            >
                                <span>{node.icon}</span>
                                <span style={{ color: node.color, fontWeight: 600 }}>{node.label}</span>
                                {node.status === 'active' && (
                                    <span
                                        style={{
                                            fontSize: '0.6rem',
                                            color: node.color,
                                            opacity: 0.7,
                                            animation: 'timeline-pulse 1.5s ease-in-out infinite',
                                        }}
                                    >
                                        ●
                                    </span>
                                )}
                                {node.detail && (
                                    <span
                                        style={{
                                            marginLeft: 'auto',
                                            fontSize: '0.55rem',
                                            color: 'var(--text-dim)',
                                            transform: isExpanded ? 'rotate(180deg)' : 'none',
                                            transition: 'transform 0.2s',
                                        }}
                                    >
                                        ▼
                                    </span>
                                )}
                            </div>

                            {/* Expandable detail */}
                            {isExpanded && node.detail && (
                                <div
                                    style={{
                                        marginTop: '4px',
                                        padding: '6px 8px',
                                        background: 'rgba(255,255,255,0.03)',
                                        borderRadius: '4px',
                                        fontFamily: 'var(--font-mono)',
                                        fontSize: '0.65rem',
                                        color: 'var(--text-dim)',
                                        lineHeight: '1.4',
                                        wordBreak: 'break-word',
                                        animation: 'fadeIn 0.15s ease-out',
                                    }}
                                >
                                    {node.detail}
                                </div>
                            )}
                        </div>
                    </div>
                );
            })}

            <style>{`
                @keyframes timeline-pulse {
                    0%, 100% { opacity: 0.7; }
                    50% { opacity: 1; }
                }
            `}</style>
        </div>
    );
}
