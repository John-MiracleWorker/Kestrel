import { useState, useEffect, useRef } from 'react';

interface LiveCanvasProps {
    isVisible: boolean;
    activeTask?: string;
    isStreaming?: boolean;
    content?: string;
}

export function LiveCanvas({ isVisible, activeTask, isStreaming, content = '' }: LiveCanvasProps) {
    const [systemLoad, setSystemLoad] = useState<number[]>([]);
    const [stats, setStats] = useState({ speed: 0, tokenCount: 0 });
    const lastContentLength = useRef(0);
    const lastTime = useRef(Date.now());

    // Dynamic system load & stats based on real streaming data
    useEffect(() => {
        if (!isStreaming) {
            setStats({ speed: 0, tokenCount: content.length });
            lastContentLength.current = content.length;
            return;
        }

        const now = Date.now();
        const timeDiff = (now - lastTime.current) / 1000; // seconds

        if (timeDiff > 0.5) { // Update stats every 500ms
            const charDiff = content.length - lastContentLength.current;
            const speed = Math.round(charDiff / timeDiff); // chars per second

            setStats({ speed: Math.max(0, speed), tokenCount: content.length });

            // Map speed to "System Load" (0-100)
            // Assuming ~100 chars/s is max "load" visually
            const load = Math.min(100, Math.max(5, (speed / 100) * 80 + 10)); // Baseline 10%

            setSystemLoad(prev => {
                const next = [...prev, load];
                if (next.length > 30) next.shift();
                return next;
            });

            lastContentLength.current = content.length;
            lastTime.current = now;
        }
    }, [content, isStreaming]);

    // Reset graph when idle for a while or starting new
    useEffect(() => {
        if (!isStreaming && systemLoad.length > 0) {
            // Decay effect
            const interval = setInterval(() => {
                setSystemLoad(prev => {
                    if (prev.length === 0 || prev[prev.length - 1] < 5) {
                        clearInterval(interval);
                        return prev;
                    }
                    const next = [...prev];
                    next.push(Math.max(0, prev[prev.length - 1] * 0.8)); // Decay
                    if (next.length > 30) next.shift();
                    return next;
                });
            }, 100);
            return () => clearInterval(interval);
        }
    }, [isStreaming]);

    // Detect basic state from content
    const detectedAction = (() => {
        if (!isStreaming) return "SYSTEM_IDLE";
        if (content.includes('```')) return "GENERATING_CODE_ARTIFACT";
        if (content.length < 50) return "INITIALIZING_RESPONSE";
        return "STREAMING_TEXT_RESPONSE";
    })();

    if (!isVisible) return null;

    return (
        <div style={{
            width: '350px',
            borderLeft: '1px solid var(--border-color)',
            background: 'var(--bg-panel)',
            display: 'flex',
            flexDirection: 'column',
            fontFamily: 'var(--font-mono)',
            transition: 'width 0.3s ease-in-out',
            overflow: 'hidden'
        }}>
            {/* Header */}
            <div style={{
                padding: '12px',
                borderBottom: '1px solid var(--border-color)',
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'center',
                background: 'var(--bg-highlight)',
                height: '60px'
            }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                    <div style={{ width: 8, height: 8, borderRadius: '50%', background: isStreaming ? 'var(--accent-purple)' : 'var(--accent-cyan)', boxShadow: `0 0 8px ${isStreaming ? 'var(--accent-purple)' : 'var(--accent-cyan)'}`, transition: 'background 0.3s, box-shadow 0.3s' }}></div>
                    <span style={{ color: 'var(--text-primary)', fontSize: '0.8rem', fontWeight: 600, letterSpacing: '1px' }}>LIVE_HUD::v2.1</span>
                </div>
                <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end' }}>
                    <span style={{ fontSize: '0.7rem', color: 'var(--text-dim)' }}>{isStreaming ? 'ACTIVE' : 'STANDBY'}</span>
                </div>
            </div>

            <div style={{ flex: 1, padding: '16px', overflowY: 'auto' }}>

                {/* Active Task Panel */}
                <div className="terminal-border" style={{ padding: '12px', marginBottom: '16px', background: 'rgba(0,0,0,0.2)' }}>
                    <div style={{ fontSize: '0.7rem', color: 'var(--text-secondary)', marginBottom: '8px', textTransform: 'uppercase' }}>CURRENT_OPERATION</div>
                    <div style={{ color: isStreaming ? 'var(--accent-purple)' : 'var(--accent-green)', lineHeight: '1.4', transition: 'color 0.3s', fontWeight: 'bold' }}>
                        {detectedAction}
                    </div>
                    {isStreaming && (
                        <div style={{ marginTop: '8px', fontSize: '0.7rem', color: 'var(--text-dim)', borderTop: '1px solid var(--border-color)', paddingTop: '4px' }}>
                            SPEED: {stats.speed} chars/s | TOKENS: {Math.round(stats.tokenCount / 4)} (est)
                        </div>
                    )}
                </div>

                {/* System Graph Mockup */}
                <div className="terminal-border" style={{ padding: '12px', marginBottom: '16px' }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '8px' }}>
                        <span style={{ fontSize: '0.7rem', color: 'var(--text-secondary)' }}>GENERATION_THROUGHPUT</span>
                        <span style={{ fontSize: '0.7rem', color: isStreaming ? 'var(--accent-purple)' : 'var(--text-dim)' }}>{Math.round(systemLoad[systemLoad.length - 1] || 0)}%</span>
                    </div>
                    <div style={{ display: 'flex', alignItems: 'flex-end', height: '60px', gap: '2px' }}>
                        {systemLoad.map((load, i) => (
                            <div key={i} style={{
                                flex: 1,
                                background: isStreaming ? `rgba(168, 85, 247, ${Math.max(0.2, load / 100)})` : `rgba(0, 243, 255, ${Math.max(0.1, load / 100)})`,
                                height: `${Math.max(2, load)}%`,
                                minHeight: '2px',
                                transition: 'background 0.3s, height 0.2s'
                            }} />
                        ))}
                    </div>
                </div>

                {/* Generative UI Section Placeholder */}
                <div style={{
                    marginTop: '32px',
                    border: '1px dashed var(--text-dim)',
                    padding: '24px',
                    borderRadius: '4px',
                    textAlign: 'center',
                    background: 'rgba(0,0,0,0.1)'
                }}>
                    <span style={{ color: 'var(--text-dim)', fontSize: '0.8rem', display: 'block', marginBottom: '8px' }}>[ DATA_STREAM_PREVIEW ]</span>
                    <div style={{
                        fontFamily: 'var(--font-mono)',
                        fontSize: '0.6rem',
                        color: 'var(--text-primary)',
                        opacity: 0.7,
                        textAlign: 'left',
                        overflow: 'hidden',
                        height: '60px',
                        wordBreak: 'break-all'
                    }}>
                        {content.slice(-150) || "NO_DATA_STREAM"}
                    </div>
                </div>

            </div>
        </div>
    );
}
