import React from 'react';
import './ApprovalDialog.css';

interface ApprovalDialogProps {
    toolName: string;
    toolArgs: string;
    reason: string;
    onApprove: () => void;
    onDeny: () => void;
}

/**
 * Human-in-the-loop approval dialog for high-risk agent actions.
 */
export function ApprovalDialog({
    toolName,
    toolArgs,
    reason,
    onApprove,
    onDeny,
}: ApprovalDialogProps) {
    let parsedArgs: Record<string, unknown> = {};
    try {
        parsedArgs = JSON.parse(toolArgs);
    } catch {
        // Keep empty
    }

    return (
        <div className="approval-dialog">
            <div className="approval-dialog__overlay" />
            <div className="approval-dialog__card">
                <div className="approval-dialog__header">
                    <span className="approval-dialog__icon">⚠️</span>
                    <h3>Approval Required</h3>
                </div>

                <p className="approval-dialog__reason">{reason}</p>

                <div className="approval-dialog__tool">
                    <span className="approval-dialog__tool-label">Tool:</span>
                    <code>{toolName}</code>
                </div>

                {Object.keys(parsedArgs).length > 0 && (
                    <div className="approval-dialog__args">
                        <span className="approval-dialog__args-label">Arguments:</span>
                        <pre>{JSON.stringify(parsedArgs, null, 2)}</pre>
                    </div>
                )}

                <div className="approval-dialog__actions">
                    <button className="approval-dialog__deny" onClick={onDeny}>
                        ✕ Deny
                    </button>
                    <button className="approval-dialog__approve" onClick={onApprove}>
                        ✓ Approve
                    </button>
                </div>
            </div>
        </div>
    );
}

export default ApprovalDialog;
