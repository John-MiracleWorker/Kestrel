import { useEffect, useMemo, useRef, useState, type CSSProperties } from 'react';
import {
    capabilities,
    getAccessToken,
    integrations,
    operations,
    type ApprovalAuditItem,
    type CapabilityItem,
    type OperatorTaskItem,
    type RuntimeProfile,
    type TaskArtifactItem,
    type TaskCheckpointItem,
    type TaskDetail,
    type TaskTimelineItem,
} from '../../api/client';

interface OperationsPageProps {
    workspaceId: string;
}

const ACTIVE_TASK_STATUSES = new Set([
    'planning',
    'executing',
    'observing',
    'reflecting',
    'verifying',
    'waiting_approval',
    'paused',
]);

function panelStyle(extra: CSSProperties = {}): CSSProperties {
    return {
        background: 'rgba(7, 12, 18, 0.92)',
        border: '1px solid rgba(0, 243, 255, 0.14)',
        borderRadius: 'var(--radius-lg)',
        padding: '16px',
        boxShadow: '0 18px 40px rgba(0, 0, 0, 0.28)',
        ...extra,
    };
}

function statValue(tasks: OperatorTaskItem[], status: string) {
    if (status === 'orphaned') {
        return tasks.filter((task) => task.orphaned).length;
    }
    return tasks.filter((task) => task.summary.status === status).length;
}

function eventLabel(event: TaskTimelineItem): string {
    return String(event.type || '')
        .replace(/_/g, ' ')
        .toLowerCase();
}

export function OperationsPage({ workspaceId }: OperationsPageProps) {
    const [tasks, setTasks] = useState<OperatorTaskItem[]>([]);
    const [pendingApprovals, setPendingApprovals] = useState<ApprovalAuditItem[]>([]);
    const [runtimeProfile, setRuntimeProfile] = useState<RuntimeProfile | null>(null);
    const [integrationStatus, setIntegrationStatus] = useState<any>(null);
    const [capabilityItems, setCapabilityItems] = useState<CapabilityItem[]>([]);
    const [artifacts, setArtifacts] = useState<TaskArtifactItem[]>([]);
    const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
    const [taskDetail, setTaskDetail] = useState<TaskDetail | null>(null);
    const [timeline, setTimeline] = useState<TaskTimelineItem[]>([]);
    const [checkpoints, setCheckpoints] = useState<TaskCheckpointItem[]>([]);
    const [taskArtifacts, setTaskArtifacts] = useState<TaskArtifactItem[]>([]);
    const [taskApprovals, setTaskApprovals] = useState<ApprovalAuditItem[]>([]);
    const [loading, setLoading] = useState(false);
    const [detailLoading, setDetailLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const refreshTimeoutRef = useRef<number | null>(null);

    const refreshOverview = async (preserveSelection = true) => {
        if (!workspaceId) return;
        setLoading(true);
        setError(null);
        try {
            const [
                nextTasks,
                nextApprovals,
                nextProfile,
                nextIntegrations,
                nextCapabilities,
                nextArtifacts,
            ] = await Promise.all([
                operations.listTasks(workspaceId),
                operations.listApprovals(workspaceId, { status: 'pending' }),
                operations.getRuntimeProfile(workspaceId),
                integrations.status(workspaceId),
                capabilities.get(workspaceId),
                operations.listArtifacts(workspaceId),
            ]);

            setTasks(nextTasks);
            setPendingApprovals(nextApprovals);
            setRuntimeProfile(nextProfile);
            setIntegrationStatus(nextIntegrations);
            setCapabilityItems(nextCapabilities);
            setArtifacts(nextArtifacts);

            if (!preserveSelection) {
                setSelectedTaskId(nextTasks[0]?.summary.id || null);
            } else if (!selectedTaskId && nextTasks.length > 0) {
                setSelectedTaskId(nextTasks[0].summary.id);
            } else if (
                selectedTaskId &&
                nextTasks.length > 0 &&
                !nextTasks.some((task) => task.summary.id === selectedTaskId)
            ) {
                setSelectedTaskId(nextTasks[0].summary.id);
            }
        } catch (err) {
            setError(err instanceof Error ? err.message : 'Failed to load operations data');
        } finally {
            setLoading(false);
        }
    };

    const refreshTaskDetail = async (taskId: string | null) => {
        if (!workspaceId || !taskId) {
            setTaskDetail(null);
            setTimeline([]);
            setCheckpoints([]);
            setTaskArtifacts([]);
            setTaskApprovals([]);
            return;
        }

        setDetailLoading(true);
        try {
            const [detail, nextTimeline, nextCheckpoints, nextArtifacts, approvals] =
                await Promise.all([
                    operations.getTaskDetail(workspaceId, taskId),
                    operations.listTimeline(workspaceId, taskId),
                    operations.listCheckpoints(workspaceId, taskId),
                    operations.listArtifacts(workspaceId, taskId),
                    operations.listApprovals(workspaceId, { taskId }),
                ]);
            setTaskDetail(detail);
            setTimeline(nextTimeline);
            setCheckpoints(nextCheckpoints);
            setTaskArtifacts(nextArtifacts);
            setTaskApprovals(approvals);
        } catch (err) {
            setError(err instanceof Error ? err.message : 'Failed to load task details');
        } finally {
            setDetailLoading(false);
        }
    };

    useEffect(() => {
        void refreshOverview(false);
    }, [workspaceId]);

    useEffect(() => {
        void refreshTaskDetail(selectedTaskId);
    }, [workspaceId, selectedTaskId]);

    useEffect(() => {
        if (!workspaceId || !selectedTaskId) return;
        const selectedTask = tasks.find((task) => task.summary.id === selectedTaskId);
        if (!selectedTask || !ACTIVE_TASK_STATUSES.has(selectedTask.summary.status)) return;

        const token = getAccessToken();
        const controller = new AbortController();

        const scheduleRefresh = () => {
            if (refreshTimeoutRef.current) {
                window.clearTimeout(refreshTimeoutRef.current);
            }
            refreshTimeoutRef.current = window.setTimeout(() => {
                void refreshOverview();
                void refreshTaskDetail(selectedTaskId);
            }, 500);
        };

        void fetch(`/api/workspaces/${workspaceId}/tasks/${selectedTaskId}/events`, {
            headers: token ? { Authorization: `Bearer ${token}` } : {},
            signal: controller.signal,
        })
            .then(async (res) => {
                if (!res.ok || !res.body) return;
                const reader = res.body.getReader();
                const decoder = new TextDecoder();
                let buffer = '';

                while (true) {
                    const { done, value } = await reader.read();
                    if (done) break;

                    buffer += decoder.decode(value, { stream: true });
                    const lines = buffer.split('\n');
                    buffer = lines.pop() || '';
                    for (const line of lines) {
                        if (line.startsWith('data: ')) {
                            scheduleRefresh();
                        }
                    }
                }
            })
            .catch(() => {
                // Best-effort live refresh only.
            });

        return () => {
            controller.abort();
            if (refreshTimeoutRef.current) {
                window.clearTimeout(refreshTimeoutRef.current);
                refreshTimeoutRef.current = null;
            }
        };
    }, [workspaceId, selectedTaskId, tasks]);

    const statusCards = useMemo(
        () => [
            {
                label: 'Queued',
                value: statValue(tasks, 'planning') + statValue(tasks, 'paused'),
                tone: '#00f3ff',
            },
            {
                label: 'Running',
                value:
                    statValue(tasks, 'executing') +
                    statValue(tasks, 'observing') +
                    statValue(tasks, 'reflecting') +
                    statValue(tasks, 'verifying'),
                tone: '#00ff9d',
            },
            { label: 'Approval', value: pendingApprovals.length, tone: '#f59e0b' },
            { label: 'Failed', value: statValue(tasks, 'failed'), tone: '#ff5a7a' },
            { label: 'Orphaned', value: statValue(tasks, 'orphaned'), tone: '#ff9f43' },
        ],
        [tasks, pendingApprovals],
    );

    const operatorIssues = useMemo(() => {
        const issues: string[] = [];
        if (tasks.some((task) => task.orphaned)) issues.push('Orphaned task leases detected');
        if (tasks.some((task) => task.stale && !task.orphaned))
            issues.push('Stale queued or paused tasks need review');
        if (integrationStatus) {
            for (const [name, integration] of Object.entries(integrationStatus)) {
                if ((integration as { connected?: boolean }).connected === false) {
                    issues.push(`${name} integration is disconnected`);
                }
            }
        }
        if (runtimeProfile?.nativeEnabled && runtimeProfile.hostMounts.length === 0) {
            issues.push(
                'Native execution is enabled without visible host mount metadata for this role',
            );
        }
        return issues;
    }, [integrationStatus, runtimeProfile, tasks]);

    return (
        <div
            style={{
                flex: 1,
                overflowY: 'auto',
                padding: '24px',
                background:
                    'radial-gradient(circle at top right, rgba(0, 243, 255, 0.08), transparent 30%), linear-gradient(180deg, rgba(6, 12, 16, 0.98), rgba(6, 8, 12, 1))',
            }}
        >
            <div
                style={{
                    display: 'flex',
                    justifyContent: 'space-between',
                    alignItems: 'flex-start',
                    gap: '16px',
                    marginBottom: '20px',
                }}
            >
                <div>
                    <div
                        style={{
                            fontSize: '0.78rem',
                            letterSpacing: '0.14em',
                            textTransform: 'uppercase',
                            color: 'var(--text-dim)',
                            marginBottom: '6px',
                        }}
                    >
                        Workspace Operations
                    </div>
                    <h1 style={{ margin: 0, fontSize: '1.7rem', color: 'var(--text-primary)' }}>
                        Control Plane
                    </h1>
                    <div
                        style={{
                            marginTop: '6px',
                            color: 'var(--text-secondary)',
                            maxWidth: '760px',
                            lineHeight: 1.5,
                        }}
                    >
                        Task queue, approvals, checkpoints, artifacts, runtime profile, and
                        integration health are now sourced from Brain-owned read contracts.
                    </div>
                </div>
                <button
                    onClick={() => {
                        void refreshOverview();
                        void refreshTaskDetail(selectedTaskId);
                    }}
                    style={{
                        padding: '10px 14px',
                        borderRadius: 'var(--radius-md)',
                        border: '1px solid rgba(0, 243, 255, 0.2)',
                        background: 'rgba(0, 243, 255, 0.06)',
                        color: 'var(--accent-cyan)',
                        cursor: 'pointer',
                        fontFamily: 'var(--font-mono)',
                    }}
                >
                    Refresh
                </button>
            </div>

            {error && (
                <div
                    style={{
                        ...panelStyle({
                            marginBottom: '16px',
                            borderColor: 'rgba(255, 0, 85, 0.3)',
                        }),
                        color: '#ff8ca5',
                    }}
                >
                    {error}
                </div>
            )}

            <div
                style={{
                    display: 'grid',
                    gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))',
                    gap: '12px',
                    marginBottom: '20px',
                }}
            >
                {statusCards.map((card) => (
                    <div key={card.label} style={panelStyle()}>
                        <div
                            style={{
                                fontSize: '0.72rem',
                                textTransform: 'uppercase',
                                letterSpacing: '0.12em',
                                color: 'var(--text-dim)',
                            }}
                        >
                            {card.label}
                        </div>
                        <div
                            style={{
                                marginTop: '10px',
                                fontSize: '1.9rem',
                                fontWeight: 700,
                                color: card.tone,
                            }}
                        >
                            {card.value}
                        </div>
                    </div>
                ))}
            </div>

            <div
                style={{
                    display: 'grid',
                    gridTemplateColumns: '1.2fr 0.8fr',
                    gap: '16px',
                    marginBottom: '16px',
                }}
            >
                <div style={panelStyle()}>
                    <div
                        style={{
                            display: 'flex',
                            justifyContent: 'space-between',
                            marginBottom: '12px',
                        }}
                    >
                        <div>
                            <div
                                style={{
                                    fontSize: '1rem',
                                    fontWeight: 700,
                                    color: 'var(--text-primary)',
                                }}
                            >
                                Task Board
                            </div>
                            <div
                                style={{
                                    fontSize: '0.78rem',
                                    color: 'var(--text-secondary)',
                                    marginTop: '4px',
                                }}
                            >
                                Live queue state with stale and orphan detection.
                            </div>
                        </div>
                        <div style={{ fontSize: '0.78rem', color: 'var(--text-dim)' }}>
                            {loading ? 'Refreshing…' : `${tasks.length} tasks`}
                        </div>
                    </div>
                    <div
                        style={{
                            display: 'grid',
                            gap: '8px',
                            maxHeight: '360px',
                            overflowY: 'auto',
                        }}
                    >
                        {tasks.length === 0 && (
                            <div style={{ color: 'var(--text-dim)', padding: '12px 0' }}>
                                No operator-visible tasks yet.
                            </div>
                        )}
                        {tasks.map((task) => (
                            <button
                                key={task.summary.id}
                                onClick={() => setSelectedTaskId(task.summary.id)}
                                style={{
                                    textAlign: 'left',
                                    padding: '12px',
                                    borderRadius: 'var(--radius-md)',
                                    border:
                                        selectedTaskId === task.summary.id
                                            ? '1px solid rgba(0, 243, 255, 0.4)'
                                            : '1px solid rgba(255, 255, 255, 0.06)',
                                    background:
                                        selectedTaskId === task.summary.id
                                            ? 'rgba(0, 243, 255, 0.08)'
                                            : 'rgba(255, 255, 255, 0.02)',
                                    color: 'var(--text-primary)',
                                    cursor: 'pointer',
                                }}
                            >
                                <div
                                    style={{
                                        display: 'flex',
                                        justifyContent: 'space-between',
                                        gap: '12px',
                                    }}
                                >
                                    <div
                                        style={{
                                            fontWeight: 600,
                                            overflow: 'hidden',
                                            textOverflow: 'ellipsis',
                                            whiteSpace: 'nowrap',
                                        }}
                                    >
                                        {task.summary.goal}
                                    </div>
                                    <div
                                        style={{
                                            color: task.orphaned
                                                ? '#ff9f43'
                                                : task.stale
                                                  ? '#f59e0b'
                                                  : 'var(--accent-cyan)',
                                            fontSize: '0.78rem',
                                            flexShrink: 0,
                                        }}
                                    >
                                        {task.summary.status}
                                    </div>
                                </div>
                                <div
                                    style={{
                                        display: 'flex',
                                        gap: '10px',
                                        flexWrap: 'wrap',
                                        marginTop: '8px',
                                        fontSize: '0.74rem',
                                        color: 'var(--text-secondary)',
                                    }}
                                >
                                    <span>
                                        {task.currentStep && task.totalSteps
                                            ? `Step ${task.currentStep}/${task.totalSteps}`
                                            : 'No plan yet'}
                                    </span>
                                    <span>{task.pendingApprovalCount} approvals</span>
                                    {task.queueStatus && <span>Queue: {task.queueStatus}</span>}
                                    {task.orphaned && (
                                        <span style={{ color: '#ff9f43' }}>lease expired</span>
                                    )}
                                </div>
                            </button>
                        ))}
                    </div>
                </div>

                <div style={{ display: 'grid', gap: '16px' }}>
                    <div style={panelStyle()}>
                        <div
                            style={{
                                fontSize: '1rem',
                                fontWeight: 700,
                                color: 'var(--text-primary)',
                                marginBottom: '8px',
                            }}
                        >
                            Approval Queue
                        </div>
                        <div
                            style={{
                                display: 'grid',
                                gap: '8px',
                                maxHeight: '160px',
                                overflowY: 'auto',
                            }}
                        >
                            {pendingApprovals.length === 0 && (
                                <div style={{ color: 'var(--text-dim)' }}>
                                    No pending approvals.
                                </div>
                            )}
                            {pendingApprovals.map((approval) => (
                                <div
                                    key={approval.approvalId}
                                    style={{
                                        padding: '10px',
                                        borderRadius: 'var(--radius-md)',
                                        background: 'rgba(245, 158, 11, 0.07)',
                                    }}
                                >
                                    <div style={{ fontWeight: 600 }}>{approval.toolName}</div>
                                    <div
                                        style={{
                                            fontSize: '0.78rem',
                                            color: 'var(--text-secondary)',
                                            marginTop: '4px',
                                        }}
                                    >
                                        {approval.reason || 'Approval requested'}
                                    </div>
                                </div>
                            ))}
                        </div>
                    </div>

                    <div style={panelStyle()}>
                        <div
                            style={{
                                fontSize: '1rem',
                                fontWeight: 700,
                                color: 'var(--text-primary)',
                                marginBottom: '8px',
                            }}
                        >
                            Operator Health
                        </div>
                        <div style={{ display: 'grid', gap: '8px' }}>
                            {operatorIssues.length === 0 && (
                                <div style={{ color: 'var(--accent-green)' }}>
                                    No active operator alerts.
                                </div>
                            )}
                            {operatorIssues.map((issue) => (
                                <div key={issue} style={{ color: '#ffb36a', fontSize: '0.84rem' }}>
                                    {issue}
                                </div>
                            ))}
                        </div>
                    </div>
                </div>
            </div>

            <div
                style={{
                    display: 'grid',
                    gridTemplateColumns: '1.1fr 0.9fr',
                    gap: '16px',
                    marginBottom: '16px',
                }}
            >
                <div style={panelStyle()}>
                    <div
                        style={{
                            display: 'flex',
                            justifyContent: 'space-between',
                            gap: '12px',
                            marginBottom: '12px',
                        }}
                    >
                        <div>
                            <div
                                style={{
                                    fontSize: '1rem',
                                    fontWeight: 700,
                                    color: 'var(--text-primary)',
                                }}
                            >
                                Task Detail
                            </div>
                            <div
                                style={{
                                    fontSize: '0.78rem',
                                    color: 'var(--text-secondary)',
                                    marginTop: '4px',
                                }}
                            >
                                Timeline, execution trace, approvals, and recovery hints.
                            </div>
                        </div>
                        <div style={{ fontSize: '0.78rem', color: 'var(--text-dim)' }}>
                            {detailLoading ? 'Loading…' : taskDetail?.status || 'No task selected'}
                        </div>
                    </div>

                    {!taskDetail && (
                        <div style={{ color: 'var(--text-dim)' }}>Select a task to inspect.</div>
                    )}
                    {taskDetail && (
                        <>
                            <div
                                style={{
                                    padding: '12px',
                                    borderRadius: 'var(--radius-md)',
                                    background: 'rgba(255, 255, 255, 0.02)',
                                    marginBottom: '12px',
                                }}
                            >
                                <div style={{ fontWeight: 700, color: 'var(--text-primary)' }}>
                                    {taskDetail.goal}
                                </div>
                                <div
                                    style={{
                                        display: 'flex',
                                        gap: '12px',
                                        flexWrap: 'wrap',
                                        marginTop: '8px',
                                        fontSize: '0.78rem',
                                        color: 'var(--text-secondary)',
                                    }}
                                >
                                    <span>
                                        {taskDetail.currentStep && taskDetail.totalSteps
                                            ? `Step ${taskDetail.currentStep}/${taskDetail.totalSteps}`
                                            : 'No step progress'}
                                    </span>
                                    <span>Iterations {taskDetail.iterations}</span>
                                    <span>Tool calls {taskDetail.toolCalls}</span>
                                    {taskDetail.execution.runtimeClass && (
                                        <span>Runtime {taskDetail.execution.runtimeClass}</span>
                                    )}
                                    {taskDetail.execution.fallbackSummary && (
                                        <span>Fallback {taskDetail.execution.fallbackSummary}</span>
                                    )}
                                </div>
                                {taskDetail.recoveryHints.length > 0 && (
                                    <div style={{ marginTop: '10px', display: 'grid', gap: '6px' }}>
                                        {taskDetail.recoveryHints.map((hint) => (
                                            <div
                                                key={hint.code}
                                                style={{ color: '#ffd08a', fontSize: '0.8rem' }}
                                            >
                                                {hint.title}: {hint.description}
                                            </div>
                                        ))}
                                    </div>
                                )}
                            </div>

                            <div
                                style={{
                                    display: 'grid',
                                    gap: '8px',
                                    maxHeight: '340px',
                                    overflowY: 'auto',
                                }}
                            >
                                {timeline.map((event, index) => (
                                    <div
                                        key={`${event.createdAt}-${index}`}
                                        style={{
                                            borderLeft: '2px solid rgba(0, 243, 255, 0.25)',
                                            paddingLeft: '10px',
                                        }}
                                    >
                                        <div
                                            style={{
                                                display: 'flex',
                                                justifyContent: 'space-between',
                                                gap: '12px',
                                                fontSize: '0.76rem',
                                                color: 'var(--text-dim)',
                                            }}
                                        >
                                            <span>{eventLabel(event)}</span>
                                            <span>
                                                {event.createdAt
                                                    ? new Date(event.createdAt).toLocaleString()
                                                    : ''}
                                            </span>
                                        </div>
                                        <div
                                            style={{
                                                marginTop: '4px',
                                                color: 'var(--text-primary)',
                                                fontSize: '0.88rem',
                                            }}
                                        >
                                            {event.content ||
                                                event.toolResult ||
                                                event.toolName ||
                                                'Task event'}
                                        </div>
                                    </div>
                                ))}
                            </div>
                        </>
                    )}
                </div>

                <div style={{ display: 'grid', gap: '16px' }}>
                    <div style={panelStyle()}>
                        <div
                            style={{
                                fontSize: '1rem',
                                fontWeight: 700,
                                color: 'var(--text-primary)',
                                marginBottom: '8px',
                            }}
                        >
                            Checkpoints
                        </div>
                        <div
                            style={{
                                display: 'grid',
                                gap: '8px',
                                maxHeight: '180px',
                                overflowY: 'auto',
                            }}
                        >
                            {checkpoints.length === 0 && (
                                <div style={{ color: 'var(--text-dim)' }}>
                                    No checkpoints recorded.
                                </div>
                            )}
                            {checkpoints.map((checkpoint) => (
                                <div
                                    key={checkpoint.id}
                                    style={{
                                        padding: '10px',
                                        borderRadius: 'var(--radius-md)',
                                        background: 'rgba(255, 255, 255, 0.03)',
                                    }}
                                >
                                    <div style={{ fontWeight: 600 }}>{checkpoint.label}</div>
                                    <div
                                        style={{
                                            fontSize: '0.76rem',
                                            color: 'var(--text-secondary)',
                                            marginTop: '4px',
                                        }}
                                    >
                                        Step {checkpoint.stepIndex} ·{' '}
                                        {checkpoint.createdAt
                                            ? new Date(checkpoint.createdAt).toLocaleString()
                                            : ''}
                                    </div>
                                </div>
                            ))}
                        </div>
                    </div>

                    <div style={panelStyle()}>
                        <div
                            style={{
                                fontSize: '1rem',
                                fontWeight: 700,
                                color: 'var(--text-primary)',
                                marginBottom: '8px',
                            }}
                        >
                            Approval Audit
                        </div>
                        <div
                            style={{
                                display: 'grid',
                                gap: '8px',
                                maxHeight: '180px',
                                overflowY: 'auto',
                            }}
                        >
                            {taskApprovals.length === 0 && (
                                <div style={{ color: 'var(--text-dim)' }}>
                                    No approvals for this task.
                                </div>
                            )}
                            {taskApprovals.map((approval) => (
                                <div
                                    key={approval.approvalId}
                                    style={{
                                        padding: '10px',
                                        borderRadius: 'var(--radius-md)',
                                        background: 'rgba(255, 255, 255, 0.03)',
                                    }}
                                >
                                    <div style={{ fontWeight: 600 }}>{approval.toolName}</div>
                                    <div
                                        style={{
                                            fontSize: '0.76rem',
                                            color: 'var(--text-secondary)',
                                            marginTop: '4px',
                                        }}
                                    >
                                        {approval.status} · {approval.riskLevel}
                                    </div>
                                </div>
                            ))}
                        </div>
                    </div>
                </div>
            </div>

            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: '16px' }}>
                <div style={panelStyle()}>
                    <div
                        style={{
                            fontSize: '1rem',
                            fontWeight: 700,
                            color: 'var(--text-primary)',
                            marginBottom: '8px',
                        }}
                    >
                        Artifact Explorer
                    </div>
                    <div
                        style={{
                            display: 'grid',
                            gap: '8px',
                            maxHeight: '280px',
                            overflowY: 'auto',
                        }}
                    >
                        {(taskArtifacts.length > 0 ? taskArtifacts : artifacts).map((artifact) => (
                            <div
                                key={artifact.id}
                                style={{
                                    padding: '10px',
                                    borderRadius: 'var(--radius-md)',
                                    background: 'rgba(255, 255, 255, 0.03)',
                                }}
                            >
                                <div
                                    style={{
                                        display: 'flex',
                                        justifyContent: 'space-between',
                                        gap: '8px',
                                    }}
                                >
                                    <div style={{ fontWeight: 600 }}>{artifact.title}</div>
                                    <div style={{ fontSize: '0.72rem', color: 'var(--text-dim)' }}>
                                        v{artifact.version}
                                    </div>
                                </div>
                                <div
                                    style={{
                                        fontSize: '0.78rem',
                                        color: 'var(--text-secondary)',
                                        marginTop: '4px',
                                    }}
                                >
                                    {artifact.componentType} ·{' '}
                                    {artifact.updatedAt
                                        ? new Date(artifact.updatedAt).toLocaleString()
                                        : ''}
                                </div>
                            </div>
                        ))}
                        {artifacts.length === 0 && (
                            <div style={{ color: 'var(--text-dim)' }}>No artifacts available.</div>
                        )}
                    </div>
                </div>

                <div style={panelStyle()}>
                    <div
                        style={{
                            fontSize: '1rem',
                            fontWeight: 700,
                            color: 'var(--text-primary)',
                            marginBottom: '8px',
                        }}
                    >
                        Runtime Profile
                    </div>
                    {!runtimeProfile && (
                        <div style={{ color: 'var(--text-dim)' }}>Runtime profile unavailable.</div>
                    )}
                    {runtimeProfile && (
                        <div
                            style={{
                                display: 'grid',
                                gap: '8px',
                                fontSize: '0.84rem',
                                color: 'var(--text-secondary)',
                            }}
                        >
                            <div>
                                Mode:{' '}
                                <span style={{ color: 'var(--text-primary)' }}>
                                    {runtimeProfile.runtimeMode}
                                </span>
                            </div>
                            <div>
                                Policy:{' '}
                                <span style={{ color: 'var(--text-primary)' }}>
                                    {runtimeProfile.policyName} v{runtimeProfile.policyVersion}
                                </span>
                            </div>
                            <div>
                                Docker:{' '}
                                <span
                                    style={{
                                        color: runtimeProfile.dockerEnabled
                                            ? 'var(--accent-green)'
                                            : '#ff8ca5',
                                    }}
                                >
                                    {runtimeProfile.dockerEnabled ? 'enabled' : 'disabled'}
                                </span>
                            </div>
                            <div>
                                Native:{' '}
                                <span
                                    style={{
                                        color: runtimeProfile.nativeEnabled
                                            ? '#ffb36a'
                                            : 'var(--text-dim)',
                                    }}
                                >
                                    {runtimeProfile.nativeEnabled ? 'enabled' : 'disabled'}
                                </span>
                            </div>
                            <div>
                                Fallback visible:{' '}
                                <span style={{ color: 'var(--text-primary)' }}>
                                    {runtimeProfile.hybridFallbackVisible ? 'yes' : 'no'}
                                </span>
                            </div>
                            {runtimeProfile.hostMounts.length > 0 && (
                                <div>
                                    Mounts:
                                    {runtimeProfile.hostMounts.map((mount) => (
                                        <div
                                            key={mount.path}
                                            style={{
                                                marginTop: '4px',
                                                color: 'var(--text-primary)',
                                            }}
                                        >
                                            {mount.path} ({mount.mode})
                                        </div>
                                    ))}
                                </div>
                            )}
                        </div>
                    )}
                </div>

                <div style={panelStyle()}>
                    <div
                        style={{
                            fontSize: '1rem',
                            fontWeight: 700,
                            color: 'var(--text-primary)',
                            marginBottom: '8px',
                        }}
                    >
                        Channel And Capability Health
                    </div>
                    <div style={{ display: 'grid', gap: '8px', fontSize: '0.84rem' }}>
                        {integrationStatus &&
                            Object.entries(integrationStatus).map(([name, value]) => (
                                <div
                                    key={name}
                                    style={{
                                        color: (value as any).connected
                                            ? 'var(--accent-green)'
                                            : '#ff8ca5',
                                    }}
                                >
                                    {name}:{' '}
                                    {(value as any).status ||
                                        ((value as any).connected ? 'connected' : 'disconnected')}
                                </div>
                            ))}
                        {capabilityItems.slice(0, 5).map((item) => (
                            <div key={item.name} style={{ color: 'var(--text-secondary)' }}>
                                {item.name}:{' '}
                                <span style={{ color: 'var(--text-primary)' }}>{item.status}</span>
                            </div>
                        ))}
                    </div>
                </div>
            </div>
        </div>
    );
}

export default OperationsPage;
