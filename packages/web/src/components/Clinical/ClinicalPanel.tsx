import { useState, useEffect } from 'react';

export interface Referral {
    id: string;
    patientName: string;
    specialty: string;
    location: string;
    status: 'Pending' | 'Scheduled' | 'Completed';
    date: string;
    notes?: string;
}

export interface PriorAuth {
    id: string;
    patientName: string;
    procedure: string;
    orderNumber: string;
    status: 'Pending' | 'Approved' | 'Denied';
    date: string;
    notes?: string;
}

interface ClinicalPanelProps {
    isVisible: boolean;
    onClose: () => void;
}

export function ClinicalPanel({ isVisible, onClose }: ClinicalPanelProps) {
    const [activeTab, setActiveTab] = useState<'referrals' | 'auths'>('referrals');

    // State
    const [referrals, setReferrals] = useState<Referral[]>(() => {
        const saved = localStorage.getItem('kestrel_referrals');
        return saved ? JSON.parse(saved) : [];
    });
    const [auths, setAuths] = useState<PriorAuth[]>(() => {
        const saved = localStorage.getItem('kestrel_auths');
        return saved ? JSON.parse(saved) : [];
    });

    // Form State
    const [isAdding, setIsAdding] = useState(false);
    const [newReferral, setNewReferral] = useState<Partial<Referral>>({ status: 'Pending', date: new Date().toISOString().split('T')[0] });
    const [newAuth, setNewAuth] = useState<Partial<PriorAuth>>({ status: 'Pending', date: new Date().toISOString().split('T')[0] });

    // Sync to local storage
    useEffect(() => {
        localStorage.setItem('kestrel_referrals', JSON.stringify(referrals));
    }, [referrals]);
    useEffect(() => {
        localStorage.setItem('kestrel_auths', JSON.stringify(auths));
    }, [auths]);

    if (!isVisible) return null;

    const handleAddReferral = (e: React.FormEvent) => {
        e.preventDefault();
        setReferrals([{ ...newReferral, id: Math.random().toString(36).substr(2, 9) } as Referral, ...referrals]);
        setIsAdding(false);
        setNewReferral({ status: 'Pending', date: new Date().toISOString().split('T')[0] });
    };

    const handleAddAuth = (e: React.FormEvent) => {
        e.preventDefault();
        setAuths([{ ...newAuth, id: Math.random().toString(36).substr(2, 9) } as PriorAuth, ...auths]);
        setIsAdding(false);
        setNewAuth({ status: 'Pending', date: new Date().toISOString().split('T')[0] });
    };

    const deleteReferral = (id: string) => setReferrals(referrals.filter(r => r.id !== id));
    const deleteAuth = (id: string) => setAuths(auths.filter(a => a.id !== id));

    const updateReferralStatus = (id: string, status: Referral['status']) => {
        setReferrals(referrals.map(r => r.id === id ? { ...r, status } : r));
    };

    const updateAuthStatus = (id: string, status: PriorAuth['status']) => {
        setAuths(auths.map(a => a.id === id ? { ...a, status } : a));
    };

    return (
        <div style={{
            position: 'absolute',
            top: 0, right: 0, bottom: 0,
            width: '600px',
            backgroundColor: 'rgba(10, 10, 12, 0.95)',
            backdropFilter: 'blur(16px)',
            borderLeft: '1px solid rgba(0, 243, 255, 0.2)',
            boxShadow: '-10px 0 40px rgba(0,0,0,0.5), 0 0 20px rgba(0, 243, 255, 0.05)',
            display: 'flex',
            flexDirection: 'column',
            zIndex: 100,
            transform: isVisible ? 'translateX(0)' : 'translateX(100%)',
            transition: 'transform 0.3s cubic-bezier(0.16, 1, 0.3, 1)',
            fontFamily: 'var(--font-mono)',
        }}>
            {/* Header */}
            <div style={{
                padding: '16px 20px',
                borderBottom: '1px solid rgba(255, 255, 255, 0.1)',
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'center',
                background: 'linear-gradient(180deg, rgba(0, 243, 255, 0.05), transparent)'
            }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                    <span style={{ fontSize: '1.2rem' }}>🏥</span>
                    <h2 style={{ fontSize: '1.1rem', fontWeight: 600, color: 'var(--accent-cyan)', margin: 0 }}>Clinical Tracking</h2>
                </div>
                <button onClick={onClose} style={{
                    background: 'transparent', border: 'none', color: 'var(--text-secondary)',
                    cursor: 'pointer', fontSize: '1.2rem', padding: '4px 8px'
                }}>×</button>
            </div>

            {/* Tabs */}
            <div style={{ display: 'flex', borderBottom: '1px solid rgba(255, 255, 255, 0.1)', padding: '0 20px' }}>
                <button
                    onClick={() => setActiveTab('referrals')}
                    style={{
                        padding: '12px 16px',
                        background: 'transparent',
                        border: 'none',
                        borderBottom: activeTab === 'referrals' ? '2px solid var(--accent-cyan)' : '2px solid transparent',
                        color: activeTab === 'referrals' ? 'var(--accent-cyan)' : 'var(--text-secondary)',
                        cursor: 'pointer',
                        fontWeight: activeTab === 'referrals' ? 600 : 400,
                    }}
                >
                    Referrals ({referrals.length})
                </button>
                <button
                    onClick={() => setActiveTab('auths')}
                    style={{
                        padding: '12px 16px',
                        background: 'transparent',
                        border: 'none',
                        borderBottom: activeTab === 'auths' ? '2px solid var(--accent-purple)' : '2px solid transparent',
                        color: activeTab === 'auths' ? 'var(--accent-purple)' : 'var(--text-secondary)',
                        cursor: 'pointer',
                        fontWeight: activeTab === 'auths' ? 600 : 400,
                    }}
                >
                    Prior Auths ({auths.length})
                </button>
            </div>

            {/* Content Area */}
            <div style={{ flex: 1, overflowY: 'auto', padding: '20px' }}>

                {/* Add New Header */}
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
                    <h3 style={{ fontSize: '0.9rem', color: 'var(--text-primary)', margin: 0 }}>
                        {activeTab === 'referrals' ? 'Patient Referrals' : 'Prior Authorizations'}
                    </h3>
                    <button
                        onClick={() => setIsAdding(!isAdding)}
                        style={{
                            background: 'rgba(0, 243, 255, 0.1)', border: '1px solid rgba(0, 243, 255, 0.3)',
                            color: 'var(--accent-cyan)', padding: '4px 12px', borderRadius: '4px', cursor: 'pointer',
                            fontSize: '0.8rem'
                        }}
                    >
                        {isAdding ? 'Cancel' : '+ Record New'}
                    </button>
                </div>

                {/* Add Forms */}
                {isAdding && activeTab === 'referrals' && (
                    <form onSubmit={handleAddReferral} style={{ background: 'rgba(255,255,255,0.02)', padding: '16px', borderRadius: '8px', border: '1px solid rgba(255,255,255,0.1)', marginBottom: '20px' }}>
                        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px', marginBottom: '12px' }}>
                            <input required placeholder="Patient Name" value={newReferral.patientName || ''} onChange={e => setNewReferral({ ...newReferral, patientName: e.target.value })} style={inputStyle} />
                            <input required placeholder="Specialty (e.g., Dentistry)" value={newReferral.specialty || ''} onChange={e => setNewReferral({ ...newReferral, specialty: e.target.value })} style={inputStyle} />
                            <input required placeholder="Location" value={newReferral.location || ''} onChange={e => setNewReferral({ ...newReferral, location: e.target.value })} style={inputStyle} />
                            <input type="date" value={newReferral.date || ''} onChange={e => setNewReferral({ ...newReferral, date: e.target.value })} style={inputStyle} />
                        </div>
                        <input placeholder="Notes" value={newReferral.notes || ''} onChange={e => setNewReferral({ ...newReferral, notes: e.target.value })} style={{ ...inputStyle, width: '100%', marginBottom: '12px' }} />
                        <button type="submit" style={submitBtnStyle}>Save Referral</button>
                    </form>
                )}

                {isAdding && activeTab === 'auths' && (
                    <form onSubmit={handleAddAuth} style={{ background: 'rgba(255,255,255,0.02)', padding: '16px', borderRadius: '8px', border: '1px solid rgba(255,255,255,0.1)', marginBottom: '20px' }}>
                        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px', marginBottom: '12px' }}>
                            <input required placeholder="Patient Name" value={newAuth.patientName || ''} onChange={e => setNewAuth({ ...newAuth, patientName: e.target.value })} style={inputStyle} />
                            <input required placeholder="Procedure (e.g., MRI)" value={newAuth.procedure || ''} onChange={e => setNewAuth({ ...newAuth, procedure: e.target.value })} style={inputStyle} />
                            <input placeholder="Order Number" value={newAuth.orderNumber || ''} onChange={e => setNewAuth({ ...newAuth, orderNumber: e.target.value })} style={inputStyle} />
                            <input type="date" value={newAuth.date || ''} onChange={e => setNewAuth({ ...newAuth, date: e.target.value })} style={inputStyle} />
                        </div>
                        <input placeholder="Notes" value={newAuth.notes || ''} onChange={e => setNewAuth({ ...newAuth, notes: e.target.value })} style={{ ...inputStyle, width: '100%', marginBottom: '12px' }} />
                        <button type="submit" style={submitBtnStyle}>Save Prior Auth</button>
                    </form>
                )}

                {/* Lists */}
                <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
                    {activeTab === 'referrals' && referrals.length === 0 && <div style={emptyStyle}>No active referrals.</div>}
                    {activeTab === 'referrals' && referrals.map(r => (
                        <div key={r.id} style={cardStyle}>
                            <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '8px' }}>
                                <span style={{ fontWeight: 600, color: 'var(--text-primary)' }}>{r.patientName}</span>
                                <span style={{ fontSize: '0.8rem', color: 'var(--text-dim)' }}>{r.date}</span>
                            </div>
                            <div style={{ fontSize: '0.85rem', color: 'var(--text-secondary)', marginBottom: '12px' }}>
                                <div><span style={{ color: 'var(--accent-cyan)' }}>Specialty:</span> {r.specialty}</div>
                                <div><span style={{ color: 'var(--accent-cyan)' }}>Location:</span> {r.location}</div>
                            </div>
                            {r.notes && <div style={{ fontSize: '0.8rem', color: 'var(--text-dim)', fontStyle: 'italic', marginBottom: '12px' }}>"{r.notes}"</div>}
                            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                                <select
                                    value={r.status}
                                    onChange={(e) => updateReferralStatus(r.id, e.target.value as any)}
                                    style={selectStyle(r.status === 'Completed' ? 'var(--accent-green)' : r.status === 'Scheduled' ? 'var(--accent-cyan)' : 'var(--accent-orange)')}
                                >
                                    <option value="Pending">Pending</option>
                                    <option value="Scheduled">Scheduled</option>
                                    <option value="Completed">Completed</option>
                                </select>
                                <button onClick={() => deleteReferral(r.id)} style={{ background: 'transparent', border: 'none', color: 'var(--accent-error)', cursor: 'pointer', fontSize: '0.8rem' }}>Delete</button>
                            </div>
                        </div>
                    ))}

                    {activeTab === 'auths' && auths.length === 0 && <div style={emptyStyle}>No active prior authorizations.</div>}
                    {activeTab === 'auths' && auths.map(a => (
                        <div key={a.id} style={cardStyle}>
                            <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '8px' }}>
                                <span style={{ fontWeight: 600, color: 'var(--text-primary)' }}>{a.patientName}</span>
                                <span style={{ fontSize: '0.8rem', color: 'var(--text-dim)' }}>{a.date}</span>
                            </div>
                            <div style={{ fontSize: '0.85rem', color: 'var(--text-secondary)', marginBottom: '12px' }}>
                                <div><span style={{ color: 'var(--accent-purple)' }}>Procedure:</span> {a.procedure}</div>
                                <div><span style={{ color: 'var(--accent-purple)' }}>Order #:</span> {a.orderNumber || 'N/A'}</div>
                            </div>
                            {a.notes && <div style={{ fontSize: '0.8rem', color: 'var(--text-dim)', fontStyle: 'italic', marginBottom: '12px' }}>"{a.notes}"</div>}
                            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                                <select
                                    value={a.status}
                                    onChange={(e) => updateAuthStatus(a.id, e.target.value as any)}
                                    style={selectStyle(a.status === 'Approved' ? 'var(--accent-green)' : a.status === 'Denied' ? 'var(--accent-error)' : 'var(--accent-orange)')}
                                >
                                    <option value="Pending">Pending</option>
                                    <option value="Approved">Approved</option>
                                    <option value="Denied">Denied</option>
                                </select>
                                <button onClick={() => deleteAuth(a.id)} style={{ background: 'transparent', border: 'none', color: 'var(--accent-error)', cursor: 'pointer', fontSize: '0.8rem' }}>Delete</button>
                            </div>
                        </div>
                    ))}
                </div>
            </div>
        </div>
    );
}

const inputStyle = {
    background: 'rgba(0,0,0,0.2)',
    border: '1px solid rgba(255,255,255,0.1)',
    color: 'var(--text-primary)',
    padding: '8px',
    borderRadius: '4px',
    fontFamily: 'var(--font-mono)',
    fontSize: '0.85rem',
    outline: 'none',
};

const submitBtnStyle = {
    background: 'var(--accent-cyan)',
    color: '#000',
    border: 'none',
    padding: '8px 16px',
    borderRadius: '4px',
    cursor: 'pointer',
    fontWeight: 600,
    fontSize: '0.85rem',
    width: '100%',
};

const emptyStyle = {
    textAlign: 'center' as const,
    padding: '40px 20px',
    color: 'var(--text-dim)',
    fontSize: '0.9rem',
    border: '1px dashed rgba(255,255,255,0.1)',
    borderRadius: '8px',
};

const cardStyle = {
    padding: '16px',
    background: 'rgba(255,255,255,0.03)',
    border: '1px solid rgba(255,255,255,0.08)',
    borderRadius: '8px',
};

const selectStyle = (color: string) => ({
    background: 'transparent',
    border: `1px solid ${color}`,
    color: color,
    padding: '4px 8px',
    borderRadius: '4px',
    fontSize: '0.8rem',
    cursor: 'pointer',
    outline: 'none',
});
