import { useState, useRef, useEffect, useCallback } from 'react';
import { createPortal } from 'react-dom';
import { request } from '../../api/client';

/* ── Types ─────────────────────────────────────────────────────── */
interface Suggestion {
    id: string;
    type: 'warning' | 'info' | 'suggestion';
    title: string;
    description: string;
    lineRef?: string | null;
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
        animation: 'fadeIn 0.3s ease-out',
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
    analyzing: {
        padding: '12px 14px', borderBottom: '1px solid #222',
        background: 'rgba(0,243,255,0.03)', fontSize: '0.65rem',
        color: '#00f3ff', display: 'flex', alignItems: 'center', gap: '8px',
    },
    error: {
        padding: '10px 12px', marginBottom: '8px',
        background: '#111', borderRadius: '4px',
        borderLeft: '3px solid #ef4444',
        fontSize: '0.7rem', color: '#f87171',
    },
};

/* ── Component ────────────────────────────────────────────────── */
export function ScreenShare({ workspaceId, isVisible, onClose }: ScreenShareProps) {
    const [isWatching, setIsWatching] = useState(false);
    const [isPaused, setIsPaused] = useState(false);
    const [isAnalyzing, setIsAnalyzing] = useState(false);
    const [suggestions, setSuggestions] = useState<Suggestion[]>([]);
    const [screenContext, setScreenContext] = useState('');
    const [frameCount, setFrameCount] = useState(0);
    const [captureInterval, setCaptureInterval] = useState(10000);
    const [showSettings, setShowSettings] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const streamRef = useRef<MediaStream | null>(null);
    const videoRef = useRef<HTMLVideoElement | null>(null);
    const canvasRef = useRef<HTMLCanvasElement | null>(null);
    const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
    const [position, setPosition] = useState({ x: 0, y: 0 });
    const dragRef = useRef({ isDragging: false, startX: 0, startY: 0 });

    /** Capture a single frame from the video stream as base64 PNG */
    const captureFrame = useCallback((): string | null => {
        const video = videoRef.current;
        const canvas = canvasRef.current;
        if (!video || !canvas || video.readyState < 2) return null;

        canvas.width = video.videoWidth;
        canvas.height = video.videoHeight;
        const ctx = canvas.getContext('2d');
        if (!ctx) return null;

        ctx.drawImage(video, 0, 0);
        // Get as JPEG for smaller payload (~60-70% smaller than PNG)
        const dataUrl = canvas.toDataURL('image/jpeg', 0.8);
        // Strip the data:image/jpeg;base64, prefix
        return dataUrl.split(',')[1] || null;
    }, []);

    /** Send a frame to the vision API */
    const analyzeFrame = useCallback(async () => {
        if (isPaused || isAnalyzing) return;

        const imageBase64 = captureFrame();
        if (!imageBase64) return;

        setIsAnalyzing(true);
        setError(null);
        setFrameCount(prev => prev + 1);

        try {
            const result = await request<{
                suggestions: Suggestion[];
                context: string;
            }>(`/workspaces/${workspaceId}/vision/analyze`, {
                method: 'POST',
                body: {
                    image: imageBase64,
                    mimeType: 'image/jpeg',
                },
            });

            if (result.suggestions?.length > 0) {
                setSuggestions(prev => {
                    // Deduplicate by title
                    const existingTitles = new Set(prev.map(s => s.title));
                    const newSuggs = result.suggestions.filter(s => !existingTitles.has(s.title));
                    // Keep max 10 suggestions
                    return [...newSuggs, ...prev].slice(0, 10);
                });
            }
            if (result.context) {
                setScreenContext(result.context);
            }
        } catch (err: any) {
            console.error('Vision analysis error:', err);
            setError(err.message || 'Analysis failed');
        } finally {
            setIsAnalyzing(false);
        }
    }, [workspaceId, isPaused, isAnalyzing, captureFrame]);

    const startCapture = async () => {
        try {
            const stream = await navigator.mediaDevices.getDisplayMedia({
                video: { width: { ideal: 1920 }, height: { ideal: 1080 } },
            });

            streamRef.current = stream;

            // Create hidden video element to receive the stream
            const video = document.createElement('video');
            video.srcObject = stream;
            video.muted = true;
            video.playsInline = true;
            await video.play();
            videoRef.current = video;

            // Create offscreen canvas for frame capture
            const canvas = document.createElement('canvas');
            canvasRef.current = canvas;

            setIsWatching(true);
            setFrameCount(0);
            setSuggestions([]);
            setError(null);

            // Start periodic analysis
            // Small delay for the first frame to ensure video is ready
            setTimeout(() => analyzeFrame(), 2000);
            intervalRef.current = setInterval(() => analyzeFrame(), captureInterval);

            // Listen for stream end (user clicks "Stop sharing")
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
        if (videoRef.current) {
            videoRef.current.pause();
            videoRef.current.srcObject = null;
            videoRef.current = null;
        }
        canvasRef.current = null;
        if (intervalRef.current) clearInterval(intervalRef.current);
        intervalRef.current = null;
        setIsWatching(false);
        setIsPaused(false);
        setIsAnalyzing(false);
    };

    const togglePause = () => {
        if (isPaused) {
            // Resume — restart interval
            intervalRef.current = setInterval(() => analyzeFrame(), captureInterval);
        } else {
            if (intervalRef.current) clearInterval(intervalRef.current);
            intervalRef.current = null;
        }
        setIsPaused(!isPaused);
    };

    const dismissSuggestion = (id: string) => {
        setSuggestions(prev => prev.filter(s => s.id !== id));
    };

    // Cleanup on unmount
    useEffect(() => {
        return () => {
            streamRef.current?.getTracks().forEach(t => t.stop());
            if (intervalRef.current) clearInterval(intervalRef.current);
        };
    }, []);

    // Reset interval when capture interval changes
    useEffect(() => {
        if (isWatching && !isPaused && intervalRef.current) {
            clearInterval(intervalRef.current);
            intervalRef.current = setInterval(() => analyzeFrame(), captureInterval);
        }
    }, [captureInterval, isWatching, isPaused, analyzeFrame]);

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
                        Kestrel Vision {isWatching ? (isPaused ? '· PAUSED' : '· LIVE') : ''}
                    </span>
                </div>
                <div style={S.headerBtns}>
                    <button style={S.iconBtn} onClick={() => setShowSettings(!showSettings)} title="Settings">⚙</button>
                    <button style={S.iconBtn} onClick={() => { stopCapture(); onClose(); }} title="Close">✕</button>
                </div>
            </div>

            {/* Analyzing indicator */}
            {isAnalyzing && (
                <div style={S.analyzing}>
                    <span style={{ animation: 'pulse 1s infinite' }}>◉</span>
                    Analyzing frame...
                </div>
            )}

            {/* Settings */}
            {showSettings && (
                <div style={{ padding: '10px 14px', borderBottom: '1px solid #222', background: '#0d0d0d' }}>
                    <div style={{ fontSize: '0.65rem', color: '#555', marginBottom: '6px', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
                        Capture Interval
                    </div>
                    <div style={{ display: 'flex', gap: '4px', marginBottom: '10px' }}>
                        {[5000, 10000, 20000, 30000].map(ms => (
                            <button key={ms} style={S.resolutionPill(captureInterval === ms)}
                                onClick={() => setCaptureInterval(ms)}>
                                {ms / 1000}s
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
                            Share your screen and Kestrel will analyze it in real-time using AI vision, providing contextual code suggestions.
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
                ) : suggestions.length === 0 && !error ? (
                    <div style={S.emptyState}>
                        <div style={{ marginBottom: '8px' }}>
                            {isAnalyzing ? 'Analyzing your screen...' : 'Watching for code issues...'}
                        </div>
                        <div style={{ color: '#333' }}>AI suggestions will appear here.</div>
                        {screenContext && (
                            <div style={{ color: '#444', marginTop: '8px', fontSize: '0.6rem', fontStyle: 'italic' }}>
                                Sees: {screenContext}
                            </div>
                        )}
                    </div>
                ) : (
                    <>
                        {error && (
                            <div style={S.error}>
                                ✗ {error}
                            </div>
                        )}
                        {screenContext && (
                            <div style={{ fontSize: '0.6rem', color: '#444', marginBottom: '8px', fontStyle: 'italic' }}>
                                Sees: {screenContext}
                            </div>
                        )}
                        {suggestions.map(s => (
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
                    </>
                )}
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
