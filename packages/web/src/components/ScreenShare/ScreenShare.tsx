import { useState, useRef, useEffect, useCallback } from 'react';
import { createPortal } from 'react-dom';

/* ── Types ─────────────────────────────────────────────────────── */
interface Suggestion {
    id: string;
    type: 'warning' | 'info' | 'suggestion';
    title: string;
    description: string;
    lineRef?: string;
    timestamp: number;
}

interface ScreenShareProps {
    workspaceId: string;
    isVisible: boolean;
    onClose: () => void;
}

const TYPE_COLORS: Record<string, string> = {
    warning: '#f59e0b',
    info: '#00f3ff',
    suggestion: '#10b981',
};

const TYPE_ICONS: Record<string, string> = {
    warning: '⚠',
    info: '💡',
    suggestion: '✨',
};

/* ── Styles ────────────────────────────────────────────────────── */
const S = {
    panel: {
        position: 'fixed' as const, bottom: '20px', right: '20px',
        width: '380px', maxHeight: '520px',
        background: 'rgba(10,10,10,0.95)', backdropFilter: 'blur(12px)',
        border: '1px solid #222', borderRadius: '8px',
        fontFamily: 'JetBrains Mono, monospace', zIndex: 9999,
        display: 'flex', flexDirection: 'column' as const,
        boxShadow: '0 8px 32px rgba(0,0,0,0.5)',
        overflow: 'hidden',
    },
    header: {
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        padding: '10px 14px', borderBottom: '1px solid #222',
        cursor: 'grab', userSelect: 'none' as const,
    },
    title: {
        fontSize: '0.7rem', fontWeight: 700, color: '#00f3ff',
        letterSpacing: '0.06em', textTransform: 'uppercase' as const,
    },
    statusDot: (active: boolean) => ({
        width: '6px', height: '6px', borderRadius: '50%',
        background: active ? '#10b981' : '#555',
        display: 'inline-block', marginRight: '6px',
        boxShadow: active ? '0 0 6px #10b981' : 'none',
    }),
    headerBtns: {
        display: 'flex', gap: '6px',
    },
    iconBtn: {
        background: 'none', border: 'none', color: '#555',
        fontSize: '0.8rem', cursor: 'pointer', padding: '2px 4px',
    },
    body: {
        flex: 1, overflowY: 'auto' as const, padding: '8px 14px',
    },
    suggestion: (type: string) => ({
        padding: '10px 12px', marginBottom: '8px',
        background: '#111', borderRadius: '4px',
        borderLeft: `3px solid ${TYPE_COLORS[type] || '#555'}`,
    }),
    suggTitle: {
        fontSize: '0.75rem', color: '#e0e0e0', marginBottom: '4px',
        display: 'flex', alignItems: 'center', gap: '6px',
    },
    suggDesc: {
        fontSize: '0.7rem', color: '#888', lineHeight: 1.45,
    },
    actionBtn: {
        background: 'none', border: '1px solid #333', borderRadius: '3px',
        color: '#00f3ff', fontSize: '0.6rem', padding: '3px 8px',
        cursor: 'pointer', marginTop: '8px', float: 'right' as const,
    },
    statusBar: {
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        padding: '8px 14px', borderTop: '1px solid #222',
        background: '#080808', fontSize: '0.6rem', color: '#555',
    },
    resolutionPill: (active: boolean) => ({
        padding: '2px 6px', borderRadius: '3px', fontSize: '0.55rem',
        border: `1px solid ${active ? '#a855f7' : '#333'}`,
        color: active ? '#a855f7' : '#555',
        background: active ? 'rgba(168,85,247,0.1)' : 'transparent',
        cursor: 'pointer', marginLeft: '4px',
    }),
    pauseBtn: {
        background: 'none', border: '1px solid #333', borderRadius: '3px',
        color: '#f59e0b', fontSize: '0.6rem', padding: '2px 8px',
        cursor: 'pointer',
    },
    emptyState: {
        padding: '40px 20px', textAlign: 'center' as const,
        color: '#444', fontSize: '0.7rem',
    },
};

/* ── Mock Suggestions ─────────────────────────────────────────── */
const MOCK_SUGGESTIONS: Suggestion[] = [
    {
        id: '1', type: 'warning',
        title: 'Potential security issue detected',
        description: 'The JWT expiry on line 23 is set to 24h. Industry standard is 1h for session tokens.',
        lineRef: 'auth.ts:23',
        timestamp: Date.now() - 30000,
    },
    {
        id: '2', type: 'suggestion',
        title: 'Consider using bcrypt',
        description: 'SHA256 is being used for password hashing on line 45. bcrypt is more resistant to brute-force attacks.',
        lineRef: 'auth.ts:45',
        timestamp: Date.now() - 15000,
    },
    {
        id: '3', type: 'info',
        title: 'Missing error handling',
        description: 'The async function at line 78 doesn\'t have a try-catch block. Unhandled promise rejections could crash the process.',
        lineRef: 'server.py:78',
        timestamp: Date.now() - 5000,
    },
];

/* ── Component ────────────────────────────────────────────────── */
export function ScreenShare({ workspaceId, isVisible, onClose }: ScreenShareProps) {
    const [isWatching, setIsWatching] = useState(false);
    const [isPaused, setIsPaused] = useState(false);
    const [suggestions, setSuggestions] = useState<Suggestion[]>([]);
    const [frameCount, setFrameCount] = useState(0);
    const [resolution, setResolution] = useState<'720p' | '1080p' | '4k'>('1080p');
    const [captureInterval, setCaptureInterval] = useState(5000);
    const [showSettings, setShowSettings] = useState(false);
    const streamRef = useRef<MediaStream | null>(null);
    const intervalRef = useRef<NodeJS.Timeout | null>(null);
    const [position, setPosition] = useState({ x: 0, y: 0 });
    const dragRef = useRef({ isDragging: false, startX: 0, startY: 0 });

    const startCapture = async () => {
        try {
            const stream = await navigator.mediaDevices.getDisplayMedia({
                video: { width: { ideal: resolution === '4k' ? 3840 : resolution === '1080p' ? 1920 : 1280 } }
            });
            streamRef.current = stream;
            setIsWatching(true);
            setFrameCount(0);

            // Mock: add suggestions over time
            intervalRef.current = setInterval(() => {
                setFrameCount(prev => prev + 1);
                // Simulate AI suggestions appearing
                if (Math.random() > 0.7) {
                    const mock = MOCK_SUGGESTIONS[Math.floor(Math.random() * MOCK_SUGGESTIONS.length)];
                    setSuggestions(prev => {
                        if (prev.length >= 5) return prev;
                        return [{ ...mock, id: `${mock.id}-${Date.now()}`, timestamp: Date.now() }, ...prev];
                    });
                }
            }, captureInterval);

            // Listen for stream end
            stream.getVideoTracks()[0]?.addEventListener('ended', () => {
                stopCapture();
            });
        } catch (err) {
            console.warn('Screen capture cancelled:', err);
        }
    };

    const stopCapture = () => {
        streamRef.current?.getTracks().forEach(t => t.stop());
        streamRef.current = null;
        if (intervalRef.current) clearInterval(intervalRef.current);
        setIsWatching(false);
        setIsPaused(false);
    };

    const togglePause = () => {
        if (isPaused && intervalRef.current) {
            // Resume
            intervalRef.current = setInterval(() => {
                setFrameCount(prev => prev + 1);
            }, captureInterval);
        } else if (intervalRef.current) {
            clearInterval(intervalRef.current);
        }
        setIsPaused(!isPaused);
    };

    const dismissSuggestion = (id: string) => {
        setSuggestions(prev => prev.filter(s => s.id !== id));
    };

    // Cleanup
    useEffect(() => {
        return () => {
            streamRef.current?.getTracks().forEach(t => t.stop());
            if (intervalRef.current) clearInterval(intervalRef.current);
        };
    }, []);

    // Drag handlers
    const handleDragStart = (e: React.MouseEvent) => {
        dragRef.current = { isDragging: true, startX: e.clientX - position.x, startY: e.clientY - position.y };
    };

    const handleDragMove = useCallback((e: MouseEvent) => {
        if (dragRef.current.isDragging) {
            setPosition({ x: e.clientX - dragRef.current.startX, y: e.clientY - dragRef.current.startY });
        }
    }, []);

    const handleDragEnd = useCallback(() => {
        dragRef.current.isDragging = false;
    }, []);

    useEffect(() => {
        window.addEventListener('mousemove', handleDragMove);
        window.addEventListener('mouseup', handleDragEnd);
        return () => {
            window.removeEventListener('mousemove', handleDragMove);
            window.removeEventListener('mouseup', handleDragEnd);
        };
    }, [handleDragMove, handleDragEnd]);

    if (!isVisible) return null;

    const panelStyle = {
        ...S.panel,
        transform: `translate(${position.x}px, ${position.y}px)`,
    };

    return createPortal(
        <div style={panelStyle}>
            {/* Header */}
            <div style={S.header} onMouseDown={handleDragStart}>
                <div style={{ display: 'flex', alignItems: 'center' }}>
                    <span style={S.statusDot(isWatching && !isPaused)} />
                    <span style={S.title}>
                        Kestrel Vision {isWatching ? (isPaused ? '· PAUSED' : '· WATCHING') : ''}
                    </span>
                </div>
                <div style={S.headerBtns}>
                    <button style={S.iconBtn} onClick={() => setShowSettings(!showSettings)} title="Settings">⚙</button>
                    <button style={S.iconBtn} onClick={() => { stopCapture(); onClose(); }} title="Close">✕</button>
                </div>
            </div>

            {/* Settings Popover */}
            {showSettings && (
                <div style={{ padding: '10px 14px', borderBottom: '1px solid #222', background: '#0d0d0d' }}>
                    <div style={{ fontSize: '0.65rem', color: '#555', marginBottom: '6px', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
                        Capture Interval
                    </div>
                    <div style={{ display: 'flex', gap: '4px', marginBottom: '10px' }}>
                        {[3000, 5000, 10000, 30000].map(ms => (
                            <button key={ms} style={S.resolutionPill(captureInterval === ms)}
                                onClick={() => setCaptureInterval(ms)}>
                                {ms / 1000}s
                            </button>
                        ))}
                    </div>
                    <div style={{ fontSize: '0.65rem', color: '#555', marginBottom: '6px', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
                        Resolution
                    </div>
                    <div style={{ display: 'flex', gap: '4px' }}>
                        {(['720p', '1080p', '4k'] as const).map(r => (
                            <button key={r} style={S.resolutionPill(resolution === r)}
                                onClick={() => setResolution(r)}>
                                {r}
                            </button>
                        ))}
                    </div>
                </div>
            )}

            {/* Body */}
            <div style={S.body}>
                {!isWatching ? (
                    <div style={S.emptyState}>
                        <div style={{ fontSize: '1.5rem', marginBottom: '12px' }}>👁</div>
                        <div style={{ marginBottom: '12px' }}>
                            Share your screen and Kestrel will analyze it in real-time, providing contextual suggestions.
                        </div>
                        <button style={{
                            ...S.actionBtn, float: 'none' as any,
                            fontSize: '0.7rem', padding: '6px 16px',
                            background: 'rgba(0,243,255,0.1)',
                            border: '1px solid rgba(0,243,255,0.3)',
                        }} onClick={startCapture}>
                            Start Watching
                        </button>
                    </div>
                ) : suggestions.length === 0 ? (
                    <div style={S.emptyState}>
                        <div style={{ marginBottom: '8px' }}>Analyzing screen...</div>
                        <div style={{ color: '#333' }}>Suggestions will appear here.</div>
                    </div>
                ) : suggestions.map(s => (
                    <div key={s.id} style={S.suggestion(s.type)}>
                        <div style={S.suggTitle}>
                            <span>{TYPE_ICONS[s.type]}</span>
                            <span>{s.title}</span>
                        </div>
                        <div style={S.suggDesc}>{s.description}</div>
                        {s.lineRef && (
                            <div style={{ fontSize: '0.6rem', color: '#555', marginTop: '4px' }}>
                                📍 {s.lineRef}
                            </div>
                        )}
                        <button style={S.actionBtn} onClick={() => dismissSuggestion(s.id)}>
                            Dismiss
                        </button>
                        <div style={{ clear: 'both' }} />
                    </div>
                ))}
            </div>

            {/* Status Bar */}
            <div style={S.statusBar}>
                <div>
                    ⏱ {captureInterval / 1000}s · Frame {frameCount}
                </div>
                <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                    {isWatching && (
                        <button style={S.pauseBtn} onClick={togglePause}>
                            {isPaused ? '▶ Resume' : '⏸ Pause'}
                        </button>
                    )}
                </div>
            </div>
        </div>,
        document.body
    );
}
