import { useEffect, useMemo, useRef, useState } from 'react';
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
import { OperationsDetailSection } from './OperationsDetailSection';
import { OperationsMetaSection } from './OperationsMetaSection';
import { OperationsOverviewSection } from './OperationsOverviewSection';
import { panelStyle, statValue } from './OperationsShared';

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

            <OperationsOverviewSection
                statusCards={statusCards}
                loading={loading}
                tasks={tasks}
                selectedTaskId={selectedTaskId}
                onSelectTask={setSelectedTaskId}
                taskDetail={taskDetail}
                pendingApprovals={pendingApprovals}
                operatorIssues={operatorIssues}
            />

            <OperationsDetailSection
                detailLoading={detailLoading}
                taskDetail={taskDetail}
                timeline={timeline}
                checkpoints={checkpoints}
                taskApprovals={taskApprovals}
            />

            <OperationsMetaSection
                taskArtifacts={taskArtifacts}
                artifacts={artifacts}
                runtimeProfile={runtimeProfile}
                integrationStatus={integrationStatus}
                capabilityItems={capabilityItems}
            />
        </div>
    );
}

export default OperationsPage;
