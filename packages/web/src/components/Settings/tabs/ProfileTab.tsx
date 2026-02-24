import { S } from '../constants';

interface ProfileTabProps {
    displayName: string;
    setDisplayName: (name: string) => void;
    userEmail?: string;
}

export function ProfileTab({ displayName, setDisplayName, userEmail }: ProfileTabProps) {
    return (
        <div>
            <div style={S.sectionTitle}>// PROFILE</div>
            <div style={S.field}>
                <label style={S.label}>Display Name</label>
                <input style={S.input} value={displayName} onChange={e => setDisplayName(e.target.value)}
                    placeholder="Your name"
                    onFocus={e => { e.target.style.borderColor = '#00f3ff'; }}
                    onBlur={e => { e.target.style.borderColor = '#333'; }} />
            </div>
            <div style={S.field}>
                <label style={S.label}>Email</label>
                <input style={{ ...S.input, opacity: 0.5, cursor: 'not-allowed' }} defaultValue={userEmail} disabled />
                <div style={{ fontSize: '0.65rem', color: '#444', marginTop: '4px' }}>Email cannot be changed</div>
            </div>
            <div style={{ ...S.sectionTitle, marginTop: '32px' }}>// SESSION</div>
            <button style={{ ...S.btnGhost, color: '#ff0055', borderColor: 'rgba(255,0,85,0.3)' }}>Sign Out</button>
        </div>
    );
}
