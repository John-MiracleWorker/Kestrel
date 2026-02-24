export const KEY_MASKS = ['***', '••••••••••••'];
export const isKeyMasked = (key: string) => KEY_MASKS.includes(key);
export const DISPLAY_MASK = '••••••••••••';

export const PROVIDER_META: Record<string, { name: string; icon: string; requiresKey: boolean }> = {
    local: { name: 'Local (llama.cpp)', icon: '⚡', requiresKey: false },
    openai: { name: 'OpenAI', icon: '◈', requiresKey: true },
    anthropic: { name: 'Anthropic', icon: '◆', requiresKey: true },
    google: { name: 'Google Gemini', icon: '◉', requiresKey: true },
};

export const RISK_COLORS: Record<string, string> = {
    low: '#00ff9d',
    medium: '#00f3ff',
    high: '#ff9d00',
    critical: '#ff0055',
};

export const CATEGORY_ICONS: Record<string, string> = {
    code: '⟨/⟩',
    web: '◎',
    file: '▤',
    memory: '⬡',
    data: '▦',
    control: '⊕',
    skill: '✦',
    general: '○',
};

export const WEBHOOK_EVENTS = ['task.started', 'task.completed', 'task.failed', 'message.created'];

export const S = {
    backdrop: {
        position: 'fixed' as const, inset: 0, zIndex: 9999,
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        background: 'rgba(0,0,0,0.7)', backdropFilter: 'blur(12px)',
        WebkitBackdropFilter: 'blur(12px)',
    },
    panel: {
        display: 'flex', width: '100%', maxWidth: 880, height: '85vh',
        background: '#0a0a0a', border: '1px solid rgba(255,255,255,0.06)',
        borderRadius: '12px', overflow: 'hidden',
        fontFamily: "'JetBrains Mono', monospace",
        animation: 'scaleIn 0.2s ease-out',
        boxShadow: '0 20px 60px rgba(0,0,0,0.6), 0 0 30px rgba(0, 243, 255, 0.03)',
    },
    nav: {
        width: 200, minWidth: 200, borderRight: '1px solid rgba(255,255,255,0.04)',
        display: 'flex', flexDirection: 'column' as const,
        background: '#080808', padding: '16px 0',
        backgroundImage: 'linear-gradient(180deg, rgba(0, 243, 255, 0.02) 0%, transparent 40%)',
    },
    navHeader: {
        padding: '0 16px 12px', fontSize: '0.7rem', fontWeight: 600,
        color: '#00f3ff', letterSpacing: '0.1em', textTransform: 'uppercase' as const,
        borderBottom: '1px solid rgba(0, 243, 255, 0.1)', marginBottom: '4px',
        textShadow: '0 0 10px rgba(0, 243, 255, 0.3)',
    },
    navSection: {
        padding: '8px 16px 4px', fontSize: '0.6rem', fontWeight: 600,
        color: '#333', letterSpacing: '0.1em', textTransform: 'uppercase' as const,
    },
    navItem: (active: boolean) => ({
        display: 'flex', alignItems: 'center', gap: '10px',
        padding: '9px 16px', cursor: 'pointer', fontSize: '0.78rem',
        color: active ? '#00f3ff' : '#888', fontWeight: active ? 600 : 400,
        background: active ? 'rgba(0,243,255,0.06)' : 'transparent',
        borderLeft: `2px solid ${active ? '#00f3ff' : 'transparent'}`,
        transition: 'all 0.15s', fontFamily: "'JetBrains Mono', monospace",
        border: 'none', borderRight: 'none', borderTop: 'none', borderBottom: 'none',
        borderLeftWidth: '2px', borderLeftStyle: 'solid' as const,
        borderLeftColor: active ? '#00f3ff' : 'transparent',
        textAlign: 'left' as const, width: '100%',
    }),
    content: {
        flex: 1, overflow: 'auto', padding: '24px 28px',
    },
    sectionTitle: {
        fontSize: '0.7rem', fontWeight: 600, color: '#555',
        letterSpacing: '0.08em', textTransform: 'uppercase' as const,
        marginBottom: '16px', paddingBottom: '8px',
        borderBottom: '1px solid #1a1a1a',
    },
    label: {
        fontSize: '0.75rem', fontWeight: 500, color: '#888',
        marginBottom: '6px', display: 'block',
    },
    input: {
        width: '100%', padding: '10px 12px', fontSize: '0.8rem',
        fontFamily: "'JetBrains Mono', monospace",
        background: '#111', border: '1px solid rgba(255,255,255,0.06)', borderRadius: '8px',
        color: '#e0e0e0', outline: 'none', transition: 'border-color 0.3s, box-shadow 0.3s',
    },
    textarea: {
        width: '100%', padding: '12px', fontSize: '0.8rem', minHeight: 160,
        fontFamily: "'JetBrains Mono', monospace",
        background: '#111', border: '1px solid #333', borderRadius: '4px',
        color: '#e0e0e0', outline: 'none', resize: 'vertical' as const,
        lineHeight: 1.5, transition: 'border-color 0.2s',
    },
    select: {
        width: '100%', padding: '10px 12px', fontSize: '0.8rem',
        fontFamily: "'JetBrains Mono', monospace",
        background: '#111', border: '1px solid #333', borderRadius: '4px',
        color: '#e0e0e0', outline: 'none', cursor: 'pointer',
        appearance: 'none' as const,
        backgroundImage: `url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%23888' stroke-width='2'%3E%3Cpath d='M6 9l6 6 6-6'/%3E%3C/svg%3E")`,
        backgroundRepeat: 'no-repeat', backgroundPosition: 'right 12px center',
    },
    field: {
        marginBottom: '20px',
    },
    providerCard: (active: boolean) => ({
        display: 'flex', alignItems: 'center', gap: '12px',
        padding: '12px 14px', cursor: 'pointer',
        background: active ? 'rgba(0,243,255,0.06)' : '#111',
        border: `1px solid ${active ? '#00f3ff' : '#222'}`,
        borderRadius: '4px', transition: 'all 0.15s',
    }),
    slider: {
        width: '100%', height: '4px', appearance: 'none' as const,
        background: '#333', borderRadius: '2px', outline: 'none',
        cursor: 'pointer',
    },
    toggle: (on: boolean) => ({
        width: 40, height: 22, borderRadius: '11px', position: 'relative' as const,
        background: on ? '#00f3ff' : '#333', cursor: 'pointer',
        transition: 'background 0.2s', border: 'none', padding: 0,
        display: 'inline-flex', alignItems: 'center', flexShrink: 0,
    }),
    toggleDot: (on: boolean) => ({
        width: 16, height: 16, borderRadius: '50%', background: '#0a0a0a',
        position: 'absolute' as const, top: 3,
        left: on ? 21 : 3, transition: 'left 0.2s',
    }),
    btnPrimary: {
        padding: '10px 20px', fontSize: '0.8rem', fontWeight: 600,
        fontFamily: "'JetBrains Mono', monospace",
        background: '#00f3ff', color: '#000', border: 'none',
        borderRadius: '4px', cursor: 'pointer', transition: 'opacity 0.15s',
    },
    btnGhost: {
        padding: '10px 20px', fontSize: '0.8rem',
        fontFamily: "'JetBrains Mono', monospace",
        background: 'transparent', color: '#888',
        border: '1px solid #333', borderRadius: '4px',
        cursor: 'pointer', transition: 'all 0.15s',
    },
    badge: {
        fontSize: '0.65rem', padding: '2px 8px', borderRadius: '3px',
        background: '#00f3ff', color: '#000', fontWeight: 700,
        letterSpacing: '0.05em',
    },
    successBox: {
        padding: '10px 14px', marginBottom: '16px', borderRadius: '4px',
        background: 'rgba(0,255,157,0.06)', border: '1px solid rgba(0,255,157,0.3)',
        color: '#00ff9d', fontSize: '0.8rem',
    },
    closeBtn: {
        position: 'absolute' as const, top: 12, right: 12,
        background: 'none', border: 'none', color: '#555', cursor: 'pointer',
        padding: '4px', fontSize: '1rem', lineHeight: 1,
    },
    toolCard: {
        display: 'flex', alignItems: 'center', gap: '12px',
        padding: '12px 14px', background: '#111', border: '1px solid #1a1a1a',
        borderRadius: '4px', marginBottom: '8px',
    },
};
