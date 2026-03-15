type SidebarDeleteDialogProps = {
    onCancel: () => void;
    onConfirm: () => void;
};

export function SidebarDeleteDialog({ onCancel, onConfirm }: SidebarDeleteDialogProps) {
    return (
        <div
            style={{
                position: 'fixed',
                inset: 0,
                zIndex: 50,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                background: 'rgba(0, 0, 0, 0.7)',
                backdropFilter: 'blur(12px)',
            }}
            onClick={onCancel}
        >
            <div
                className="card animate-fade-in"
                style={{
                    maxWidth: 400,
                    width: '100%',
                    margin: '0 var(--space-4)',
                    padding: 'var(--space-6)',
                }}
                onClick={(e) => e.stopPropagation()}
            >
                <h3
                    style={{
                        fontSize: '1.0625rem',
                        fontWeight: 700,
                        marginBottom: 'var(--space-2)',
                    }}
                >
                    Delete Conversation
                </h3>
                <p
                    style={{
                        color: 'var(--color-text-secondary)',
                        marginBottom: 'var(--space-6)',
                        fontSize: '0.875rem',
                    }}
                >
                    Are you sure you want to delete this conversation? This action cannot be undone.
                </p>
                <div
                    style={{
                        display: 'flex',
                        justifyContent: 'flex-end',
                        gap: 'var(--space-3)',
                    }}
                >
                    <button className="btn btn-ghost" onClick={onCancel}>
                        Cancel
                    </button>
                    <button className="btn btn-danger" onClick={onConfirm}>
                        Delete
                    </button>
                </div>
            </div>
        </div>
    );
}
