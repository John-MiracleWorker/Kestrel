/**
 * AgentDebatePanel — Live visualization of specialist agents during delegation.
 * Shows per-agent cards with status, thought bubbles, tool indicators, and results.
 * Inspired by AGENT_SWARM's AgentsPanel pattern.
 */
import { useState, useEffect, useMemo, useRef } from 'react';
import type { DelegationEvent } from '../../hooks/useChat';

// ── Agent State ──────────────────────────────────────────────────────
interface AgentState {
    specialist: string;
    status: 'spawning' | 'thinking' | 'tool_calling' | 'tool_result' | 'step_done' | 'complete' | 'failed';
    goal?: string;
    tools?: string[];
    lastThinking?: string;
    lastTool?: string;
    lastToolArgs?: string;
    lastToolResult?: string;
    lastContent?: string;
    result?: string;
    toolHistory: Array<{ tool: string; result?: string; timestamp: number }>;
    spawnTime: number;
}

// ── Specialist Colors & Emojis ───────────────────────────────────────
const SPECIALIST_THEME: Record<string, { emoji: string; color: string; label: string }> = {
    'Researcher': { emoji: '🔬', color: '#f59e0b', label: 'RESEARCHER' },
    'Coder': { emoji: '💻', color: '#3b82f6', label: 'CODER' },
    'Data Analyst': { emoji: '📊', color: '#8b5cf6', label: 'ANALYST' },
    'Reviewer': { emoji: '🔍', color: '#ec4899', label: 'REVIEWER' },
    'Code Explorer': { emoji: '🗺️', color: '#10b981', label: 'EXPLORER' },
};

function getTheme(specialist: string) {
    return SPECIALIST_THEME[specialist] || { emoji: '🤖', color: '#00f3ff', label: specialist.toUpperCase() };
}

// ── Status Badge Colors ──────────────────────────────────────────────
function statusColor(status: string): string {
    switch (status) {
        case 'spawning': return '#6b7280';
        case 'thinking': return '#00f3ff';
        case 'tool_calling': return '#f59e0b';
        case 'tool_result': return '#8b5cf6';
        case 'step_done': return '#3b82f6';
        case 'complete': return '#10b981';
        case 'failed': return '#ef4444';
        default: return '#6b7280';
    }
}

function statusLabel(status: string): string {
    switch (status) {
        case 'spawning': return 'SPAWNING';
        case 'thinking': return 'THINKING';
        case 'tool_calling': return 'USING TOOL';
        case 'tool_result': return 'ANALYZING';
        case 'step_done': return 'PROCESSING';
        case 'complete': return 'COMPLETE';
        case 'failed': return 'FAILED';
        default: return status.toUpperCase();
    }
}

// ── Props ────────────────────────────────────────────────────────────
interface AgentDebatePanelProps {
    events: DelegationEvent[];
}

// ── Component ────────────────────────────────────────────────────────
export function AgentDebatePanel({ events }: AgentDebatePanelProps) {
    const [agents, setAgents] = useState<Record<string, AgentState>>({});
    const [totalAgents, setTotalAgents] = useState(0);
    const prevEventsLenRef = useRef(0);

    // Process events into agent states
    useEffect(() => {
        if (events.length === prevEventsLenRef.current) return;
        prevEventsLenRef.current = events.length;

        const agentMap: Record<string, AgentState> = { ...agents };

        for (const evt of events.slice(Object.keys(agents).length > 0 ? -5 : 0)) {
            const name = evt.specialist;
            if (!name && evt.type !== 'parallel_delegation_started' && evt.type !== 'parallel_delegation_complete') continue;

            if (evt.type === 'parallel_delegation_started') {
                setTotalAgents(evt.count || 0);
                // Pre-create agents from subtasks
                evt.subtasks?.forEach((s) => {
                    const spec = s.specialist || 'explorer';
                    const label = SPECIALIST_THEME[spec]?.label || spec;
                    if (!agentMap[label]) {
                        agentMap[label] = {
                            specialist: spec,
                            status: 'spawning',
                            goal: s.goal,
                            toolHistory: [],
                            spawnTime: Date.now(),
                        };
                    }
                });
                continue;
            }

            if (!name) continue;

            if (evt.type === 'delegation_started') {
                agentMap[name] = {
                    specialist: name,
                    status: 'thinking',
                    goal: evt.goal,
                    tools: evt.tools,
                    toolHistory: agentMap[name]?.toolHistory || [],
                    spawnTime: agentMap[name]?.spawnTime || Date.now(),
                };
            } else if (evt.type === 'delegation_progress') {
                if (!agentMap[name]) {
                    agentMap[name] = {
                        specialist: name,
                        status: 'thinking',
                        toolHistory: [],
                        spawnTime: Date.now(),
                    };
                }
                const agent = agentMap[name];
                agent.status = (evt.status as AgentState['status']) || 'thinking';

                if (evt.status === 'thinking') {
                    agent.lastThinking = evt.thinking;
                } else if (evt.status === 'tool_calling') {
                    agent.lastTool = evt.tool;
                    agent.lastToolArgs = evt.toolArgs;
                } else if (evt.status === 'tool_result') {
                    agent.lastToolResult = evt.toolResult;
                    if (evt.tool) {
                        agent.toolHistory = [
                            ...agent.toolHistory.slice(-4),
                            { tool: evt.tool, result: evt.toolResult?.slice(0, 100), timestamp: Date.now() },
                        ];
                    }
                } else if (evt.status === 'step_done') {
                    agent.lastContent = evt.content;
                }
            } else if (evt.type === 'delegation_complete') {
                if (agentMap[name]) {
                    agentMap[name].status = evt.status === 'complete' ? 'complete' : 'failed';
                    agentMap[name].result = evt.result;
                }
            }
        }

        setAgents(agentMap);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [events]);

    const agentList = useMemo(() => Object.values(agents), [agents]);
    const activeCount = agentList.filter((a) => !['complete', 'failed'].includes(a.status)).length;

    if (agentList.length === 0) return null;

    return (
        <div
            style={{
                background: 'rgba(0,0,0,0.2)',
                border: '1px solid rgba(0,243,255,0.15)',
                borderRadius: '8px',
                overflow: 'hidden',
            }}
        >
            {/* Panel Header */}
            <div
                style={{
                    padding: '10px 14px',
                    borderBottom: '1px solid rgba(0,243,255,0.1)',
                    display: 'flex',
                    alignItems: 'center',
                    gap: '8px',
                }}
            >
                <div
                    style={{
                        width: '8px',
                        height: '8px',
                        borderRadius: '50%',
                        background: activeCount > 0 ? '#00f3ff' : '#10b981',
                        boxShadow: activeCount > 0 ? '0 0 8px #00f3ff' : 'none',
                        animation: activeCount > 0 ? 'pulse 1.5s ease-in-out infinite' : 'none',
                    }}
                />
                <span style={{
                    fontFamily: 'var(--font-mono)',
                    fontSize: '0.75rem',
                    fontWeight: 'bold',
                    color: '#00f3ff',
                    letterSpacing: '0.08em',
                }}>
                    🗣️ AGENT DEBATE
                </span>
                <span style={{
                    marginLeft: 'auto',
                    fontFamily: 'var(--font-mono)',
                    fontSize: '0.65rem',
                    color: 'var(--text-dim)',
                    background: 'rgba(0,243,255,0.1)',
                    padding: '2px 8px',
                    borderRadius: '10px',
                    border: '1px solid rgba(0,243,255,0.2)',
                }}>
                    {activeCount > 0 ? `${activeCount} ACTIVE` : `${totalAgents || agentList.length} AGENTS`}
                </span>
            </div>

            {/* Agent Cards */}
            <div style={{ padding: '8px', display: 'flex', flexDirection: 'column', gap: '6px' }}>
                {agentList.map((agent) => (
                    <AgentCard key={agent.specialist} agent={agent} />
                ))}
            </div>
        </div>
    );
}

// ── Agent Card ───────────────────────────────────────────────────────
function AgentCard({ agent }: { agent: AgentState }) {
    const [isExpanded, setIsExpanded] = useState(false);
    const theme = getTheme(agent.specialist);
    const badgeColor = statusColor(agent.status);
    const badge = statusLabel(agent.status);
    const isActive = !['complete', 'failed'].includes(agent.status);

    return (
        <div
            style={{
                background: 'rgba(255,255,255,0.02)',
                border: `1px solid ${isActive ? `${theme.color}33` : 'rgba(255,255,255,0.05)'}`,
                borderLeft: `3px solid ${theme.color}`,
                borderRadius: '6px',
                padding: '10px 12px',
                animation: agent.status === 'spawning' ? 'slideIn 0.4s ease-out' : undefined,
                transition: 'border-color 0.3s ease, opacity 0.3s ease',
                opacity: agent.status === 'failed' ? 0.6 : 1,
                cursor: agent.result ? 'pointer' : 'default',
            }}
            onClick={() => agent.result && setIsExpanded((prev) => !prev)}
        >
            {/* Header: Emoji + Name + Badge */}
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                <span style={{ fontSize: '1.1rem' }}>{theme.emoji}</span>
                <span style={{
                    fontFamily: 'var(--font-mono)',
                    fontSize: '0.75rem',
                    fontWeight: 'bold',
                    color: theme.color,
                }}>
                    {agent.specialist}
                </span>
                <span style={{
                    marginLeft: 'auto',
                    fontFamily: 'var(--font-mono)',
                    fontSize: '0.6rem',
                    fontWeight: 'bold',
                    color: badgeColor,
                    background: `${badgeColor}18`,
                    border: `1px solid ${badgeColor}40`,
                    padding: '1px 8px',
                    borderRadius: '10px',
                    letterSpacing: '0.05em',
                }}>
                    {badge}
                </span>
            </div>

            {/* Goal (shown briefly at start) */}
            {agent.goal && agent.status === 'thinking' && !agent.lastThinking && (
                <div style={{
                    fontFamily: 'var(--font-mono)',
                    fontSize: '0.65rem',
                    color: 'var(--text-dim)',
                    marginTop: '6px',
                    paddingLeft: '26px',
                    opacity: 0.7,
                }}>
                    📋 {agent.goal.slice(0, 120)}
                </div>
            )}

            {/* Thought Bubble */}
            {agent.lastThinking && isActive && (
                <div style={{
                    marginTop: '6px',
                    paddingLeft: '26px',
                    fontFamily: 'var(--font-mono)',
                    fontSize: '0.65rem',
                    color: 'var(--text-secondary)',
                    fontStyle: 'italic',
                    display: 'flex',
                    gap: '6px',
                    alignItems: 'flex-start',
                }}>
                    <span style={{ color: '#00f3ff', flexShrink: 0 }}>💭</span>
                    <span>{agent.lastThinking.slice(0, 150)}{agent.lastThinking.length > 150 ? '…' : ''}</span>
                </div>
            )}

            {/* Active Tool Indicator */}
            {agent.status === 'tool_calling' && agent.lastTool && (
                <div style={{
                    marginTop: '6px',
                    paddingLeft: '26px',
                    fontFamily: 'var(--font-mono)',
                    fontSize: '0.65rem',
                    display: 'flex',
                    alignItems: 'center',
                    gap: '6px',
                }}>
                    <span style={{
                        color: '#f59e0b',
                        animation: 'pulse 1s ease-in-out infinite',
                    }}>⚡</span>
                    <span style={{ color: '#f59e0b', fontWeight: 'bold' }}>{agent.lastTool}</span>
                    {agent.lastToolArgs && (
                        <span style={{ color: 'var(--text-dim)', fontSize: '0.6rem' }}>
                            {agent.lastToolArgs.slice(0, 60)}
                        </span>
                    )}
                </div>
            )}

            {/* Tool Result Preview */}
            {agent.status === 'tool_result' && agent.lastToolResult && (
                <div style={{
                    marginTop: '6px',
                    paddingLeft: '26px',
                    fontFamily: 'var(--font-mono)',
                    fontSize: '0.6rem',
                    color: '#10b981',
                    maxHeight: '40px',
                    overflow: 'hidden',
                }}>
                    ✓ {agent.lastToolResult.slice(0, 120)}
                </div>
            )}

            {/* Tool History (compact) */}
            {agent.toolHistory.length > 0 && isActive && (
                <div style={{
                    marginTop: '4px',
                    paddingLeft: '26px',
                    display: 'flex',
                    gap: '4px',
                    flexWrap: 'wrap',
                }}>
                    {agent.toolHistory.slice(-3).map((t, i) => (
                        <span
                            key={i}
                            style={{
                                fontFamily: 'var(--font-mono)',
                                fontSize: '0.55rem',
                                color: 'var(--text-dim)',
                                background: 'rgba(255,255,255,0.04)',
                                padding: '1px 6px',
                                borderRadius: '8px',
                            }}
                        >
                            ✓ {t.tool}
                        </span>
                    ))}
                </div>
            )}

            {/* Complete Result Preview */}
            {agent.status === 'complete' && agent.result && (
                <div style={{
                    marginTop: '6px',
                    paddingLeft: '26px',
                    fontFamily: 'var(--font-mono)',
                    fontSize: '0.6rem',
                    color: 'var(--text-secondary)',
                    maxHeight: isExpanded ? '200px' : '40px',
                    overflow: 'hidden',
                    transition: 'max-height 0.3s ease',
                    lineHeight: '1.4',
                }}>
                    {agent.result.slice(0, isExpanded ? 800 : 120)}
                    {!isExpanded && agent.result.length > 120 && (
                        <span style={{ color: '#00f3ff', cursor: 'pointer' }}> ▸ more</span>
                    )}
                </div>
            )}

            {/* Failed indicator */}
            {agent.status === 'failed' && agent.result && (
                <div style={{
                    marginTop: '6px',
                    paddingLeft: '26px',
                    fontFamily: 'var(--font-mono)',
                    fontSize: '0.6rem',
                    color: '#ef4444',
                }}>
                    ⚠️ {agent.result.slice(0, 100)}
                </div>
            )}
        </div>
    );
}
