import type { ApprovalAuditItem, OperatorTaskItem, TaskDetail } from '../../api/client';

import {
    compactId,
    panelStyle,
    parseJsonArray,
    receiptTone,
    verdictTone,
} from './OperationsShared';

type StatusCard = {
    label: string;
    value: number;
    tone: string;
};

type OperationsOverviewSectionProps = {
    statusCards: StatusCard[];
    loading: boolean;
    tasks: OperatorTaskItem[];
    selectedTaskId: string | null;
    onSelectTask: (taskId: string) => void;
    taskDetail: TaskDetail | null;
    pendingApprovals: ApprovalAuditItem[];
    operatorIssues: string[];
};

export function OperationsOverviewSection({
    statusCards,
    loading,
    tasks,
    selectedTaskId,
    onSelectTask,
    taskDetail,
    pendingApprovals,
    operatorIssues,
}: OperationsOverviewSectionProps) {
    return (
        <>
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
                                onClick={() => onSelectTask(task.summary.id)}
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
                                    {task.sessionChannel && (
                                        <span>Session: {task.sessionChannel}</span>
                                    )}
                                    {task.latestReceiptId && (
                                        <span>Receipt: {compactId(task.latestReceiptId, 6)}</span>
                                    )}
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
                            Receipts
                        </div>
                        <div
                            style={{
                                display: 'grid',
                                gap: '8px',
                                maxHeight: '180px',
                                overflowY: 'auto',
                            }}
                        >
                            {(taskDetail?.receipts || []).length === 0 && (
                                <div style={{ color: 'var(--text-dim)' }}>
                                    No action receipts recorded.
                                </div>
                            )}
                            {(taskDetail?.receipts || []).map((receipt) => (
                                <div
                                    key={receipt.receiptId}
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
                                        <div style={{ fontWeight: 600 }}>
                                            {receipt.toolName || 'Action receipt'}
                                        </div>
                                        <div
                                            style={{
                                                color: receiptTone(receipt.failureClass),
                                                fontSize: '0.74rem',
                                            }}
                                        >
                                            {receipt.failureClass || 'none'}
                                        </div>
                                    </div>
                                    <div
                                        style={{
                                            fontSize: '0.76rem',
                                            color: 'var(--text-secondary)',
                                            marginTop: '4px',
                                        }}
                                    >
                                        {receipt.runtimeClass || 'runtime unknown'} · exit{' '}
                                        {receipt.exitCode}
                                    </div>
                                    <div
                                        style={{
                                            fontSize: '0.76rem',
                                            color: 'var(--text-secondary)',
                                            marginTop: '4px',
                                        }}
                                    >
                                        {parseJsonArray(receipt.artifactManifestJson).length}{' '}
                                        artifacts
                                        {receipt.createdAt
                                            ? ` · ${new Date(receipt.createdAt).toLocaleString()}`
                                            : ''}
                                    </div>
                                    {receipt.logsPointer && (
                                        <div
                                            style={{
                                                fontSize: '0.72rem',
                                                color: 'var(--text-dim)',
                                                marginTop: '4px',
                                            }}
                                        >
                                            {compactId(receipt.logsPointer, 18)}
                                        </div>
                                    )}
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
                            Evidence Ledger
                        </div>
                        <div
                            style={{
                                display: 'grid',
                                gap: '8px',
                                maxHeight: '180px',
                                overflowY: 'auto',
                            }}
                        >
                            {(taskDetail?.verifierEvidence || []).length === 0 && (
                                <div style={{ color: 'var(--text-dim)' }}>
                                    No verifier evidence recorded.
                                </div>
                            )}
                            {(taskDetail?.verifierEvidence || []).map((evidence) => (
                                <div
                                    key={evidence.id}
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
                                        <div
                                            style={{
                                                fontWeight: 600,
                                                color: 'var(--text-primary)',
                                            }}
                                        >
                                            {evidence.claimText || 'Verifier claim'}
                                        </div>
                                        <div
                                            style={{
                                                color: verdictTone(evidence.verdict),
                                                fontSize: '0.74rem',
                                            }}
                                        >
                                            {evidence.verdict || 'unknown'}
                                        </div>
                                    </div>
                                    <div
                                        style={{
                                            fontSize: '0.76rem',
                                            color: 'var(--text-secondary)',
                                            marginTop: '4px',
                                        }}
                                    >
                                        Confidence {(evidence.confidence * 100).toFixed(0)}% ·{' '}
                                        {parseJsonArray(evidence.supportingReceiptIdsJson).length}{' '}
                                        receipt refs
                                    </div>
                                    <div
                                        style={{
                                            fontSize: '0.76rem',
                                            color: 'var(--text-secondary)',
                                            marginTop: '4px',
                                        }}
                                    >
                                        {evidence.rationale}
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
        </>
    );
}
