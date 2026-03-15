import type {
    ApprovalAuditItem,
    TaskCheckpointItem,
    TaskDetail,
    TaskTimelineItem,
} from '../../api/client';

import { compactId, eventLabel, panelStyle, parseJsonArray } from './OperationsShared';

type OperationsDetailSectionProps = {
    detailLoading: boolean;
    taskDetail: TaskDetail | null;
    timeline: TaskTimelineItem[];
    checkpoints: TaskCheckpointItem[];
    taskApprovals: ApprovalAuditItem[];
};

export function OperationsDetailSection({
    detailLoading,
    taskDetail,
    timeline,
    checkpoints,
    taskApprovals,
}: OperationsDetailSectionProps) {
    return (
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
                                {taskDetail.execution.riskClass && (
                                    <span>Risk {taskDetail.execution.riskClass}</span>
                                )}
                                {taskDetail.session.channel && (
                                    <span>Channel {taskDetail.session.channel}</span>
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
                            {(taskDetail.session.sessionId ||
                                taskDetail.session.externalConversationId ||
                                taskDetail.session.externalThreadId) && (
                                <div
                                    style={{
                                        marginTop: '10px',
                                        padding: '10px',
                                        borderRadius: 'var(--radius-md)',
                                        background: 'rgba(0, 243, 255, 0.04)',
                                        display: 'grid',
                                        gap: '4px',
                                        fontSize: '0.76rem',
                                        color: 'var(--text-secondary)',
                                    }}
                                >
                                    {taskDetail.session.sessionId && (
                                        <div>Session {compactId(taskDetail.session.sessionId)}</div>
                                    )}
                                    {taskDetail.session.externalConversationId && (
                                        <div>
                                            External conversation{' '}
                                            {taskDetail.session.externalConversationId}
                                        </div>
                                    )}
                                    {taskDetail.session.externalThreadId && (
                                        <div>
                                            External thread {taskDetail.session.externalThreadId}
                                        </div>
                                    )}
                                    {taskDetail.session.returnRouteJson &&
                                        taskDetail.session.returnRouteJson !== '{}' && (
                                            <div>
                                                Return route{' '}
                                                {compactId(taskDetail.session.returnRouteJson, 18)}
                                            </div>
                                        )}
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
                                    <div
                                        style={{
                                            display: 'flex',
                                            gap: '8px',
                                            flexWrap: 'wrap',
                                            marginTop: '6px',
                                            fontSize: '0.74rem',
                                            color: 'var(--text-secondary)',
                                        }}
                                    >
                                        {event.toolName && <span>Tool {event.toolName}</span>}
                                        {event.receiptId && (
                                            <span>Receipt {compactId(event.receiptId, 6)}</span>
                                        )}
                                        {event.journalEventId && (
                                            <span>
                                                Journal {compactId(event.journalEventId, 6)}
                                            </span>
                                        )}
                                        {parseJsonArray(event.verifierEvidenceIdsJson).length >
                                            0 && (
                                            <span>
                                                Evidence{' '}
                                                {
                                                    parseJsonArray(event.verifierEvidenceIdsJson)
                                                        .length
                                                }
                                            </span>
                                        )}
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
                            <div style={{ color: 'var(--text-dim)' }}>No checkpoints recorded.</div>
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
                                {checkpoint.journalEventId && (
                                    <div
                                        style={{
                                            fontSize: '0.72rem',
                                            color: 'var(--text-dim)',
                                            marginTop: '4px',
                                        }}
                                    >
                                        Journal {compactId(checkpoint.journalEventId, 6)}
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
    );
}
