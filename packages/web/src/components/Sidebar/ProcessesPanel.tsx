import { useState, useEffect, useCallback } from 'react';
import { request } from '../../api/client';

interface Process {
    id: string;
    name: string;
    type: 'self_improve' | 'scheduled' | 'agent_task';
    status: 'running' | 'completed' | 'failed' | 'pending' | 'idle';
    cron?: string;
    last_run?: string;
    next_run?: string;
    run_count?: number;
    last_result?: string;
    branch?: string;
    error?: string;
}

interface ProcessesPanelProps {
    workspaceId: string;
    onClose: () => void;
}

export function ProcessesPanel({ workspaceId, onClose }: ProcessesPanelProps) {
    const [processes, setProcesses] = useState<Process[]>([]);
    const [loading, setLoading] = useState(true);
    const [runningCount, setRunningCount] = useState(0);

    const fetchProcesses = useCallback(async () => {
        try {
            const res = await request(`/workspaces/${workspaceId}/processes`) as { processes?: Process[]; running?: number };
            setProcesses(res.processes || []);
            setRunningCount(res.running || 0);
        } catch (err) {
            console.error('Failed to fetch processes:', err);
            // Show default self-improvement entry when API not ready
            setProcesses([{
                id: 'self-improve-default',
                name: 'Self-Improvement Cycle',
                type: 'self_improve',
                status: 'idle',
                cron: '0 */6 * * *',
                last_run: undefined,
                next_run: undefined,
                run_count: 0,
                last_result: 'Waiting for first run',
            }]);
        } finally {
            setLoading(false);
        }
    }, [workspaceId]);

    useEffect(() => {
        fetchProcesses();
        // Poll every 30 seconds for updates
        const interval = setInterval(fetchProcesses, 30_000);
        return () => clearInterval(interval);
    }, [fetchProcesses]);

    const statusIcon = (status: string) => {
        switch (status) {
            case 'running': return '⟳';
            case 'completed': return '✓';
            case 'failed': return '✗';
            case 'pending': return '◌';
            case 'idle': return '○';
            default: return '?';
        }
    };

    const statusColor = (status: string) => {
        switch (status) {
            case 'running': return 'var(--accent-cyan)';
            case 'completed': return 'var(--accent-green)';
            case 'failed': return 'var(--accent-error)';
            case 'pending': return 'var(--accent-purple)';
            case 'idle': return 'var(--text-dim)';
            default: return 'var(--text-secondary)';
        }
    };

    const typeLabel = (type: string) => {
        switch (type) {
            case 'self_improve': return '🔧 Self-Improve';
            case 'scheduled': return '⏰ Scheduled';
            case 'agent_task': return '🤖 Agent Task';
            default: return type;
        }
    };

    const formatTime = (iso?: string) => {
        if (!iso) return '—';
        const d = new Date(iso);
        const now = new Date();
        const diffMs = now.getTime() - d.getTime();
        const diffMin = Math.floor(diffMs / 60_000);
        const diffHr = Math.floor(diffMin / 60);

        if (diffMin < 1) return 'just now';
        if (diffMin < 60) return `${diffMin}m ago`;
        if (diffHr < 24) return `${diffHr}h ago`;
        return d.toLocaleDateString();
    };

    return (
        <div
            style={{
                position: 'fixed',
                inset: 0,
                zIndex: 50,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                background: 'rgba(0, 0, 0, 0.6)',
                backdropFilter: 'blur(8px)',
            }}
            onClick={onClose}
        >
            <div
                style={{
                    width: '640px',
                    maxHeight: '80vh',
                    background: 'var(--bg-panel)',
                    border: '1px solid var(--border-color)',
                    borderRadius: 'var(--radius-lg)',
                    display: 'flex',
                    flexDirection: 'column',
                    overflow: 'hidden',
                    fontFamily: 'var(--font-mono)',
                }}
                onClick={e => e.stopPropagation()}
            >
                {/* Header */}
                <div style={{
                    padding: '20px 24px',
                    borderBottom: '1px solid var(--border-color)',
                    display: 'flex',
                    justifyContent: 'space-between',
                    alignItems: 'center',
                    background: 'var(--bg-highlight)',
                }}>
                    <div>
                        <div style={{
                            fontSize: '0.7rem',
                            color: 'var(--text-dim)',
                            textTransform: 'uppercase',
                            letterSpacing: '0.1em',
                            marginBottom: '4px',
                        }}>
                            // BACKGROUND PROCESSES
                        </div>
                        <div style={{
                            fontSize: '1rem',
                            color: 'var(--text-primary)',
                            display: 'flex',
                            alignItems: 'center',
                            gap: '8px',
                        }}>
                            <span>System Monitor</span>
                            {runningCount > 0 && (
                                <span style={{
                                    background: 'rgba(0, 243, 255, 0.15)',
                                    color: 'var(--accent-cyan)',
                                    padding: '2px 8px',
                                    borderRadius: '10px',
                                    fontSize: '0.75rem',
                                }}>
                                    {runningCount} active
                                </span>
                            )}
                        </div>
                    </div>
                    <button
                        onClick={onClose}
                        style={{
                            background: 'transparent',
                            border: 'none',
                            color: 'var(--text-secondary)',
                            fontSize: '1.2rem',
                            cursor: 'pointer',
                        }}
                    >
                        ✕
                    </button>
                </div>

                {/* Process List */}
                <div style={{
                    flex: 1,
                    overflowY: 'auto',
                    padding: '16px',
                }}>
                    {loading ? (
                        <div style={{
                            textAlign: 'center',
                            padding: '40px',
                            color: 'var(--text-dim)',
                        }}>
                            Loading processes...
                        </div>
                    ) : processes.length === 0 ? (
                        <div style={{
                            textAlign: 'center',
                            padding: '40px',
                            color: 'var(--text-dim)',
                        }}>
                            <div style={{ fontSize: '2rem', marginBottom: '12px' }}>○</div>
                            <div>No background processes</div>
                            <div style={{ fontSize: '0.8rem', marginTop: '8px' }}>
                                Self-improvement cycles will appear here when scheduled.
                            </div>
                        </div>
                    ) : (
                        processes.map((proc) => (
                            <div
                                key={proc.id}
                                style={{
                                    padding: '16px',
                                    marginBottom: '8px',
                                    background: 'var(--bg-surface)',
                                    border: `1px solid ${proc.status === 'running' ? 'var(--accent-cyan)' : 'var(--border-color)'}`,
                                    borderRadius: 'var(--radius-sm)',
                                    transition: 'border-color 0.3s',
                                }}
                            >
                                {/* Process header */}
                                <div style={{
                                    display: 'flex',
                                    justifyContent: 'space-between',
                                    alignItems: 'center',
                                    marginBottom: '8px',
                                }}>
                                    <div style={{
                                        display: 'flex',
                                        alignItems: 'center',
                                        gap: '8px',
                                    }}>
                                        <span style={{
                                            color: statusColor(proc.status),
                                            fontSize: '1rem',
                                            animation: proc.status === 'running' ? 'spin 1s linear infinite' : 'none',
                                        }}>
                                            {statusIcon(proc.status)}
                                        </span>
                                        <span style={{
                                            color: 'var(--text-primary)',
                                            fontWeight: 600,
                                        }}>
                                            {proc.name}
                                        </span>
                                    </div>
                                    <span style={{
                                        fontSize: '0.75rem',
                                        color: 'var(--text-dim)',
                                        background: 'var(--bg-highlight)',
                                        padding: '2px 8px',
                                        borderRadius: '4px',
                                    }}>
                                        {typeLabel(proc.type)}
                                    </span>
                                </div>

                                {/* Process details */}
                                <div style={{
                                    display: 'grid',
                                    gridTemplateColumns: '1fr 1fr',
                                    gap: '6px',
                                    fontSize: '0.8rem',
                                    color: 'var(--text-secondary)',
                                }}>
                                    {proc.cron && (
                                        <div>
                                            <span style={{ color: 'var(--text-dim)' }}>schedule: </span>
                                            <span style={{ color: 'var(--accent-purple)' }}>{proc.cron}</span>
                                        </div>
                                    )}
                                    <div>
                                        <span style={{ color: 'var(--text-dim)' }}>runs: </span>
                                        {proc.run_count || 0}
                                    </div>
                                    <div>
                                        <span style={{ color: 'var(--text-dim)' }}>last: </span>
                                        {formatTime(proc.last_run)}
                                    </div>
                                    <div>
                                        <span style={{ color: 'var(--text-dim)' }}>next: </span>
                                        {formatTime(proc.next_run)}
                                    </div>
                                </div>

                                {/* Last result */}
                                {proc.last_result && (
                                    <div style={{
                                        marginTop: '8px',
                                        padding: '8px',
                                        background: 'var(--bg-terminal)',
                                        borderRadius: '4px',
                                        fontSize: '0.8rem',
                                        color: proc.status === 'failed' ? 'var(--accent-error)' : 'var(--accent-green)',
                                        fontFamily: 'var(--font-mono)',
                                    }}>
                                        {proc.last_result}
                                    </div>
                                )}

                                {/* Branch link */}
                                {proc.branch && (
                                    <div style={{
                                        marginTop: '6px',
                                        fontSize: '0.8rem',
                                    }}>
                                        <span style={{ color: 'var(--text-dim)' }}>branch: </span>
                                        <a
                                            href={`https://github.com/John-MiracleWorker/LibreBird/tree/${proc.branch}`}
                                            target="_blank"
                                            rel="noopener noreferrer"
                                            style={{ color: 'var(--accent-cyan)', textDecoration: 'none' }}
                                        >
                                            {proc.branch}
                                        </a>
                                    </div>
                                )}

                                {/* Error */}
                                {proc.error && (
                                    <div style={{
                                        marginTop: '6px',
                                        padding: '6px 8px',
                                        background: 'rgba(255, 85, 85, 0.1)',
                                        borderRadius: '4px',
                                        fontSize: '0.8rem',
                                        color: 'var(--accent-error)',
                                    }}>
                                        {proc.error}
                                    </div>
                                )}
                            </div>
                        ))
                    )}
                </div>

                {/* Footer */}
                <div style={{
                    padding: '12px 24px',
                    borderTop: '1px solid var(--border-color)',
                    background: 'var(--bg-highlight)',
                    display: 'flex',
                    justifyContent: 'space-between',
                    alignItems: 'center',
                    fontSize: '0.75rem',
                    color: 'var(--text-dim)',
                }}>
                    <span>Auto-refreshes every 30s</span>
                    <button
                        onClick={fetchProcesses}
                        style={{
                            background: 'transparent',
                            border: '1px solid var(--border-color)',
                            color: 'var(--text-secondary)',
                            padding: '4px 12px',
                            borderRadius: '4px',
                            cursor: 'pointer',
                            fontFamily: 'var(--font-mono)',
                            fontSize: '0.75rem',
                        }}
                    >
                        ↻ Refresh
                    </button>
                </div>
            </div>
        </div>
    );
}
