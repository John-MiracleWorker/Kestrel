import { S } from '../constants';

export function GeneralTab() {
    return (
        <div>
            <div style={S.sectionTitle}>// WORKSPACE</div>
            <div style={S.field}>
                <label style={S.label}>Workspace Name</label>
                <input style={S.input} defaultValue="John-MiracleWorker's Workspace" placeholder="Workspace name"
                    onFocus={e => { e.target.style.borderColor = '#00f3ff'; }}
                    onBlur={e => { e.target.style.borderColor = '#333'; }} />
            </div>
            <div style={S.field}>
                <label style={S.label}>Description</label>
                <textarea style={{ ...S.textarea, minHeight: 80 }} placeholder="What is this workspace for?"
                    onFocus={e => { e.target.style.borderColor = '#00f3ff'; }}
                    onBlur={e => { e.target.style.borderColor = '#333'; }} />
            </div>
            <div style={{ ...S.sectionTitle, marginTop: '32px' }}>// DANGER ZONE</div>
            <div style={{
                padding: '16px', border: '1px solid rgba(255,0,85,0.2)',
                borderRadius: '4px', background: 'rgba(255,0,85,0.03)',
            }}>
                <div style={{ fontSize: '0.8rem', fontWeight: 500, color: '#ff0055', marginBottom: '6px' }}>Delete Workspace</div>
                <div style={{ fontSize: '0.7rem', color: '#666', marginBottom: '12px' }}>
                    This will permanently delete the workspace and all conversations.
                </div>
                <button style={{ ...S.btnGhost, color: '#ff0055', borderColor: 'rgba(255,0,85,0.3)', fontSize: '0.75rem' }}>
                    Delete Workspace
                </button>
            </div>
        </div>
    );
}
