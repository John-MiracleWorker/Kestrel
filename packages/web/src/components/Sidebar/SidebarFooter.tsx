type SidebarFooterProps = {
    onOpenSettings: () => void;
    onShowProcesses: () => void;
    onOpenMoltbook: () => void;
    onOpenMemoryPalace: () => void;
    onOpenDocs: () => void;
    onOpenScreenShare: () => void;
    onOpenPRReview: () => void;
    onLogout: () => void;
};

export function SidebarFooter({
    onOpenSettings,
    onShowProcesses,
    onOpenMoltbook,
    onOpenMemoryPalace,
    onOpenDocs,
    onOpenScreenShare,
    onOpenPRReview,
    onLogout,
}: SidebarFooterProps) {
    return (
        <div
            style={{
                padding: '10px 12px',
                borderTop: '1px solid var(--border-color)',
                background: 'linear-gradient(180deg, var(--bg-panel), rgba(0, 243, 255, 0.02))',
                fontSize: '0.72rem',
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'center',
                color: 'var(--text-dim)',
                gap: '4px',
            }}
        >
            <button
                onClick={onOpenSettings}
                style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: '6px',
                    cursor: 'pointer',
                    padding: '6px 10px',
                    borderRadius: 'var(--radius-sm)',
                    transition: 'all 0.2s',
                    fontSize: '0.72rem',
                }}
                onMouseEnter={(e) => {
                    e.currentTarget.style.background = 'rgba(0, 255, 157, 0.08)';
                    e.currentTarget.style.color = 'var(--accent-green)';
                }}
                onMouseLeave={(e) => {
                    e.currentTarget.style.background = 'transparent';
                    e.currentTarget.style.color = '';
                }}
            >
                <span style={{ color: 'var(--accent-green)', fontSize: '0.5rem' }}>●</span> CONFIG
            </button>
            <div style={{ display: 'flex', gap: '2px' }}>
                <button
                    onClick={onShowProcesses}
                    title="Background Processes"
                    style={{
                        cursor: 'pointer',
                        fontSize: '1rem',
                        background: 'transparent',
                        border: 'none',
                        padding: '4px 8px',
                        borderRadius: 'var(--radius-sm)',
                        transition: 'all 0.2s',
                    }}
                    onMouseEnter={(e) => {
                        e.currentTarget.style.background = 'rgba(0, 243, 255, 0.1)';
                        e.currentTarget.style.boxShadow = '0 0 10px rgba(0, 243, 255, 0.1)';
                    }}
                    onMouseLeave={(e) => {
                        e.currentTarget.style.background = 'transparent';
                        e.currentTarget.style.boxShadow = 'none';
                    }}
                >
                    ⚙
                </button>
                <button
                    onClick={onOpenMoltbook}
                    title="Moltbook Activity"
                    style={{
                        cursor: 'pointer',
                        fontSize: '1rem',
                        background: 'transparent',
                        border: 'none',
                        padding: '4px 8px',
                        borderRadius: 'var(--radius-sm)',
                        transition: 'all 0.2s',
                    }}
                    onMouseEnter={(e) => {
                        e.currentTarget.style.background = 'rgba(189, 0, 255, 0.1)';
                        e.currentTarget.style.boxShadow = '0 0 10px rgba(189, 0, 255, 0.1)';
                    }}
                    onMouseLeave={(e) => {
                        e.currentTarget.style.background = 'transparent';
                        e.currentTarget.style.boxShadow = 'none';
                    }}
                >
                    🦞
                </button>
                <button
                    onClick={onOpenMemoryPalace}
                    title="Memory Palace"
                    style={{
                        cursor: 'pointer',
                        fontSize: '1rem',
                        background: 'transparent',
                        border: 'none',
                        padding: '4px 8px',
                        borderRadius: 'var(--radius-sm)',
                        transition: 'all 0.2s',
                    }}
                    onMouseEnter={(e) => {
                        e.currentTarget.style.background = 'rgba(168, 85, 247, 0.1)';
                        e.currentTarget.style.boxShadow = '0 0 10px rgba(168, 85, 247, 0.1)';
                    }}
                    onMouseLeave={(e) => {
                        e.currentTarget.style.background = 'transparent';
                        e.currentTarget.style.boxShadow = 'none';
                    }}
                >
                    🧠
                </button>
                <button
                    onClick={onOpenDocs}
                    title="Auto-Documentation"
                    style={{
                        cursor: 'pointer',
                        fontSize: '1rem',
                        background: 'transparent',
                        border: 'none',
                        padding: '4px 8px',
                        borderRadius: 'var(--radius-sm)',
                        transition: 'all 0.2s',
                    }}
                    onMouseEnter={(e) => {
                        e.currentTarget.style.background = 'rgba(16, 185, 129, 0.1)';
                        e.currentTarget.style.boxShadow = '0 0 10px rgba(16, 185, 129, 0.1)';
                    }}
                    onMouseLeave={(e) => {
                        e.currentTarget.style.background = 'transparent';
                        e.currentTarget.style.boxShadow = 'none';
                    }}
                >
                    📖
                </button>
                <button
                    onClick={onOpenScreenShare}
                    title="Live Screen-Share Agent"
                    style={{
                        cursor: 'pointer',
                        fontSize: '1rem',
                        background: 'transparent',
                        border: 'none',
                        padding: '4px 8px',
                        borderRadius: 'var(--radius-sm)',
                        transition: 'all 0.2s',
                    }}
                    onMouseEnter={(e) => {
                        e.currentTarget.style.background = 'rgba(245, 158, 11, 0.1)';
                        e.currentTarget.style.boxShadow = '0 0 10px rgba(245, 158, 11, 0.1)';
                    }}
                    onMouseLeave={(e) => {
                        e.currentTarget.style.background = 'transparent';
                        e.currentTarget.style.boxShadow = 'none';
                    }}
                >
                    📺
                </button>
                <button
                    onClick={onOpenPRReview}
                    title="PR Reviews"
                    style={{
                        cursor: 'pointer',
                        fontSize: '1rem',
                        background: 'transparent',
                        border: 'none',
                        padding: '4px 8px',
                        borderRadius: 'var(--radius-sm)',
                        transition: 'all 0.2s',
                    }}
                    onMouseEnter={(e) => {
                        e.currentTarget.style.background = 'rgba(59, 130, 246, 0.1)';
                        e.currentTarget.style.boxShadow = '0 0 10px rgba(59, 130, 246, 0.1)';
                    }}
                    onMouseLeave={(e) => {
                        e.currentTarget.style.background = 'transparent';
                        e.currentTarget.style.boxShadow = 'none';
                    }}
                >
                    🔍
                </button>
            </div>
            <button
                onClick={onLogout}
                style={{
                    cursor: 'pointer',
                    fontSize: '0.65rem',
                    padding: '4px 8px',
                    borderRadius: 'var(--radius-sm)',
                    transition: 'all 0.2s',
                    color: 'var(--text-dim)',
                    letterSpacing: '0.05em',
                }}
                onMouseEnter={(e) => {
                    e.currentTarget.style.color = 'var(--accent-error)';
                    e.currentTarget.style.background = 'rgba(255, 0, 85, 0.06)';
                }}
                onMouseLeave={(e) => {
                    e.currentTarget.style.color = 'var(--text-dim)';
                    e.currentTarget.style.background = 'transparent';
                }}
            >
                EXIT
            </button>
        </div>
    );
}
