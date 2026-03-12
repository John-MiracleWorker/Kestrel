import React, { useState, useRef, useEffect } from 'react';
import { useAgent, type AgentStatus } from '../../hooks/useAgent';
import { type TaskEvent } from '../../api/client';
import { ApprovalDialog } from './ApprovalDialog';
import { PlanView } from './PlanView';
import './TaskPanel.css';

interface TaskPanelProps {
    workspaceId: string;
    conversationId?: string;
}

const STATUS_LABELS: Record<AgentStatus, string> = {
    idle: 'Ready',
    running: 'Executing…',
    waiting_approval: 'Waiting for Approval',
    complete: 'Complete',
    failed: 'Failed',
};

const STATUS_COLORS: Record<AgentStatus, string> = {
    idle: 'var(--agent-neutral)',
    running: 'var(--agent-active)',
    waiting_approval: 'var(--agent-warning)',
    complete: 'var(--agent-success)',
    failed: 'var(--agent-error)',
};

const EVENT_ICONS: Record<string, string> = {
    PLAN_CREATED: '📋',
    STEP_STARTED: '▶️',
    TOOL_CALLED: '🔧',
    TOOL_RESULT: '📄',
    STEP_COMPLETE: '✅',
    APPROVAL_NEEDED: '⚠️',
    THINKING: '💭',
    TASK_COMPLETE: '🎉',
    TASK_FAILED: '❌',
    TASK_PAUSED: '⏸️',
};

/**
 * Real-time agent task execution panel.
 * Shows streaming events, thinking, and approval dialogs.
 */
export function TaskPanel({ workspaceId, conversationId }: TaskPanelProps) {
    const {
        status,
        events,
        thinking,
        pendingApproval,
        progress,
        error,
        startTask,
        approve,
        cancel,
    } = useAgent();

    const [goal, setGoal] = useState('');
    const eventsEndRef = useRef<HTMLDivElement>(null);
    const [showPlan, setShowPlan] = useState(false);

    // Auto-scroll to bottom on new events
    useEffect(() => {
        eventsEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    }, [events]);

    const handleStart = () => {
        if (!goal.trim() || status === 'running') return;
        startTask(workspaceId, { goal: goal.trim(), conversationId });
    };

    const handleKeyDown = (e: React.KeyboardEvent) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            handleStart();
        }
    };

    const planEvent = events.find((e) => e.type === 'PLAN_CREATED' || e.type === '0');

    return (
        <div className="task-panel">
            {/* ── Header ─────────────────────────────────── */}
            <div className="task-panel__header">
                <div className="task-panel__title">
                    <span className="task-panel__icon">🤖</span>
                    <span>Agent</span>
                </div>
                <div className="task-panel__status" style={{ color: STATUS_COLORS[status] }}>
                    <span className={`task-panel__dot ${status}`} />
                    {STATUS_LABELS[status]}
                </div>
            </div>

            {/* ── Progress Bar ────────────────────────────── */}
            {status === 'running' && progress.current_step && (
                <div className="task-panel__progress">
                    <div className="task-panel__progress-label">
                        Step {progress.current_step} of {progress.total_steps || '?'}
                    </div>
                    <div className="task-panel__progress-bar">
                        <div
                            className="task-panel__progress-fill"
                            style={{
                                width: `${
                                    ((parseInt(progress.current_step) || 0) /
                                        (parseInt(progress.total_steps) || 1)) *
                                    100
                                }%`,
                            }}
                        />
                    </div>
                </div>
            )}

            {/* ── Thinking ────────────────────────────────── */}
            {thinking && (
                <div className="task-panel__thinking">
                    <span className="task-panel__thinking-icon">💭</span>
                    <span className="task-panel__thinking-text">{thinking}</span>
                </div>
            )}

            {/* ── Plan Toggle ─────────────────────────────── */}
            {planEvent && (
                <button className="task-panel__plan-toggle" onClick={() => setShowPlan(!showPlan)}>
                    📋 {showPlan ? 'Hide Plan' : 'View Plan'}
                </button>
            )}
            {showPlan && planEvent && <PlanView planJson={planEvent.content} progress={progress} />}

            {/* ── Event Stream ────────────────────────────── */}
            <div className="task-panel__events">
                {events
                    .filter((e) => e.type !== 'THINKING' && e.type !== '6')
                    .map((event, i) => (
                        <EventRow key={i} event={event} />
                    ))}
                <div ref={eventsEndRef} />
            </div>

            {/* ── Approval Dialog ─────────────────────────── */}
            {pendingApproval && (
                <ApprovalDialog
                    toolName={pendingApproval.toolName}
                    toolArgs={pendingApproval.toolArgs}
                    reason={pendingApproval.content}
                    onApprove={() => approve(true)}
                    onDeny={() => approve(false)}
                />
            )}

            {/* ── Error ───────────────────────────────────── */}
            {error && (
                <div className="task-panel__error">
                    <span>❌</span> {error}
                </div>
            )}

            {/* ── Input / Controls ─────────────────────────── */}
            <div className="task-panel__controls">
                {status === 'idle' || status === 'complete' || status === 'failed' ? (
                    <div className="task-panel__input-row">
                        <input
                            type="text"
                            className="task-panel__input"
                            placeholder="Describe a task for the agent…"
                            value={goal}
                            onChange={(e) => setGoal(e.target.value)}
                            onKeyDown={handleKeyDown}
                        />
                        <button
                            className="task-panel__start-btn"
                            onClick={handleStart}
                            disabled={!goal.trim()}
                        >
                            ▶ Start
                        </button>
                    </div>
                ) : (
                    <button className="task-panel__cancel-btn" onClick={cancel}>
                        ⏹ Cancel
                    </button>
                )}
            </div>
        </div>
    );
}

function getExecutionMetadata(event: TaskEvent): Record<string, unknown> | null {
    const raw = event.metadata?.execution;
    return raw && typeof raw === 'object' && !Array.isArray(raw)
        ? (raw as Record<string, unknown>)
        : null;
}

function formatExecutionSummary(event: TaskEvent): string {
    const execution = getExecutionMetadata(event);
    if (!execution) {
        return event.metadata?.cached === true ? 'cached result' : '';
    }

    const runtimeClass =
        typeof execution.runtime_class === 'string'
            ? execution.runtime_class.replace(/_/g, ' ')
            : '';
    const riskClass =
        typeof execution.risk_class === 'string' ? execution.risk_class.replace(/_/g, ' ') : '';
    const fallbackUsed = execution.fallback_used === true || execution.fallback_used === 'true';
    const fallbackFrom =
        typeof execution.fallback_from === 'string'
            ? execution.fallback_from.replace(/_/g, ' ')
            : '';
    const fallbackTo =
        typeof execution.fallback_to === 'string' ? execution.fallback_to.replace(/_/g, ' ') : '';

    const parts: string[] = [];
    if (runtimeClass) parts.push(`runtime: ${runtimeClass}`);
    if (riskClass) parts.push(`risk: ${riskClass}`);
    if (fallbackUsed && (fallbackFrom || fallbackTo)) {
        parts.push(`fallback: ${fallbackFrom || 'unknown'} -> ${fallbackTo || 'unknown'}`);
    }
    if (event.metadata?.cached === true) {
        parts.push('cached');
    }
    return parts.join(' | ');
}

/** Single event row in the stream. */
function EventRow({ event }: { event: TaskEvent }) {
    const icon = EVENT_ICONS[event.type] || '•';
    let label = event.type.replace(/_/g, ' ').toLowerCase();
    let detail = event.content;

    if (event.toolName) {
        label = event.toolName;
        if (event.toolArgs) {
            try {
                const args = JSON.parse(event.toolArgs);
                detail = Object.entries(args)
                    .map(([k, v]) => `${k}: ${JSON.stringify(v)}`)
                    .join(', ');
            } catch {
                detail = event.toolArgs;
            }
        }
        if (event.toolResult) {
            detail =
                event.toolResult.length > 200
                    ? event.toolResult.slice(0, 200) + '…'
                    : event.toolResult;
        }
    }

    const executionSummary = formatExecutionSummary(event);
    if (executionSummary) {
        detail = detail ? `${detail} | ${executionSummary}` : executionSummary;
    }

    return (
        <div className={`event-row event-row--${event.type.toLowerCase()}`}>
            <span className="event-row__icon">{icon}</span>
            <span className="event-row__label">{label}</span>
            {detail && <span className="event-row__detail">{detail}</span>}
        </div>
    );
}

export default TaskPanel;
