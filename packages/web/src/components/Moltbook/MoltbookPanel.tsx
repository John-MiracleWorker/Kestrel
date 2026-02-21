import { useState, useEffect } from 'react';
import { moltbook, type MoltbookActivityItem } from '../../api/client';

interface MoltbookPanelProps {
    workspaceId: string;
    isVisible: boolean;
    onClose: () => void;
}

const ACTION_META: Record<string, { emoji: string; label: string; color: string }> = {
    register: { emoji: '🦞', label: 'Registered', color: '#a855f7' },
    post: { emoji: '📝', label: 'Posted', color: '#3b82f6' },
    comment: { emoji: '💬', label: 'Commented', color: '#22d3ee' },
    upvote: { emoji: '👍', label: 'Upvoted', color: '#f59e0b' },
    feed: { emoji: '📰', label: 'Browsed Feed', color: '#6b7280' },
    search: { emoji: '🔍', label: 'Searched', color: '#6b7280' },
    profile: { emoji: '👤', label: 'Checked Profile', color: '#6b7280' },
    status: { emoji: '📊', label: 'Checked Status', color: '#6b7280' },
    submolts: { emoji: '🏘️', label: 'Listed Submolts', color: '#6b7280' },
};

function timeAgo(iso: string): string {
    const diff = Date.now() - new Date(iso).getTime();
    const min = Math.floor(diff / 60000);
    if (min < 1) return 'just now';
    if (min < 60) return `${min}m ago`;
    const hr = Math.floor(min / 60);
    if (hr < 24) return `${hr}h ago`;
    const d = Math.floor(hr / 24);
    return `${d}d ago`;
}

export function MoltbookPanel({ workspaceId, isVisible, onClose }: MoltbookPanelProps) {
    const [activity, setActivity] = useState<MoltbookActivityItem[]>([]);
    const [loading, setLoading] = useState(true);

    useEffect(() => {
        if (!isVisible || !workspaceId) return;

        let cancelled = false;

        const fetchActivity = async () => {
            try {
                const items = await moltbook.getActivity(workspaceId, 30);
                if (!cancelled) {
                    setActivity(items);
                    setLoading(false);
                }
            } catch {
                if (!cancelled) setLoading(false);
            }
        };

        fetchActivity();
        // Poll every 30 seconds
        const interval = setInterval(fetchActivity, 30_000);

        return () => {
            cancelled = true;
            clearInterval(interval);
        };
    }, [isVisible, workspaceId]);

    if (!isVisible) return null;

    return (
        <div style={{
            position: 'fixed',
            top: 0,
            right: 0,
            width: '380px',
            height: '100vh',
            background: 'var(--bg-panel, #0a0a0f)',
            borderLeft: '1px solid var(--border-color, #1a1a2e)',
            display: 'flex',
            flexDirection: 'column',
            zIndex: 1000,
            fontFamily: 'var(--font-mono, monospace)',
            boxShadow: '-4px 0 20px rgba(0, 0, 0, 0.4)',
        }}>
            {/* Header */}
            <div style={{
                padding: '16px 20px',
                borderBottom: '1px solid var(--border-color, #1a1a2e)',
                background: 'linear-gradient(135deg, rgba(168, 85, 247, 0.08), rgba(59, 130, 246, 0.08))',
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'center',
            }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                    <span style={{ fontSize: '1.3rem' }}>🦞</span>
                    <div>
                        <div style={{
                            color: 'var(--text-primary, #e0e0ff)',
                            fontSize: '0.85rem',
                            fontWeight: 700,
                            letterSpacing: '1.5px',
                        }}>MOLTBOOK</div>
                        <div style={{
                            color: 'var(--text-dim, #555)',
                            fontSize: '0.65rem',
                            letterSpacing: '0.5px',
                            marginTop: '2px',
                        }}>KESTREL_ACTIVITY_FEED</div>
                    </div>
                </div>
                <button
                    onClick={onClose}
                    style={{
                        background: 'transparent',
                        border: '1px solid var(--border-color, #333)',
                        color: 'var(--text-secondary, #888)',
                        cursor: 'pointer',
                        padding: '4px 10px',
                        fontSize: '0.7rem',
                        borderRadius: '4px',
                        letterSpacing: '1px',
                        fontFamily: 'inherit',
                    }}
                >CLOSE</button>
            </div>

            {/* Activity Stream */}
            <div style={{
                flex: 1,
                overflowY: 'auto',
                padding: '12px',
            }}>
                {loading ? (
                    <div style={{
                        color: 'var(--text-dim, #555)',
                        textAlign: 'center',
                        padding: '40px 20px',
                        fontSize: '0.75rem',
                        letterSpacing: '1px',
                    }}>
                        <div style={{ marginBottom: '8px', fontSize: '1.5rem' }}>🦞</div>
                        LOADING_ACTIVITY...
                    </div>
                ) : activity.length === 0 ? (
                    <div style={{
                        textAlign: 'center',
                        padding: '40px 20px',
                    }}>
                        <div style={{ fontSize: '2rem', marginBottom: '16px' }}>🦞</div>
                        <div style={{
                            color: 'var(--text-secondary, #888)',
                            fontSize: '0.8rem',
                            marginBottom: '8px',
                        }}>No Moltbook activity yet</div>
                        <div style={{
                            color: 'var(--text-dim, #555)',
                            fontSize: '0.7rem',
                            lineHeight: 1.6,
                        }}>
                            Kestrel will start posting here once it<br />
                            registers and engages with Moltbook.
                        </div>
                    </div>
                ) : (
                    activity.map((item) => {
                        const meta = ACTION_META[item.action] || { emoji: '🔵', label: item.action, color: '#6b7280' };
                        const isInteresting = ['post', 'comment', 'register', 'upvote'].includes(item.action);
                        return (
                            <div
                                key={item.id}
                                style={{
                                    padding: '12px 14px',
                                    marginBottom: '8px',
                                    borderRadius: '6px',
                                    background: isInteresting
                                        ? 'rgba(168, 85, 247, 0.06)'
                                        : 'rgba(255, 255, 255, 0.02)',
                                    border: `1px solid ${isInteresting ? 'rgba(168, 85, 247, 0.15)' : 'var(--border-color, #1a1a2e)'}`,
                                    transition: 'background 0.2s, border-color 0.2s',
                                }}
                                onMouseEnter={e => {
                                    (e.currentTarget as HTMLDivElement).style.background = 'rgba(168, 85, 247, 0.1)';
                                    (e.currentTarget as HTMLDivElement).style.borderColor = 'rgba(168, 85, 247, 0.25)';
                                }}
                                onMouseLeave={e => {
                                    (e.currentTarget as HTMLDivElement).style.background = isInteresting
                                        ? 'rgba(168, 85, 247, 0.06)'
                                        : 'rgba(255, 255, 255, 0.02)';
                                    (e.currentTarget as HTMLDivElement).style.borderColor = isInteresting
                                        ? 'rgba(168, 85, 247, 0.15)'
                                        : 'var(--border-color, #1a1a2e)';
                                }}
                            >
                                {/* Action header */}
                                <div style={{
                                    display: 'flex',
                                    justifyContent: 'space-between',
                                    alignItems: 'center',
                                    marginBottom: '6px',
                                }}>
                                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                                        <span>{meta.emoji}</span>
                                        <span style={{
                                            fontSize: '0.7rem',
                                            fontWeight: 600,
                                            color: meta.color,
                                            letterSpacing: '0.5px',
                                            textTransform: 'uppercase',
                                        }}>{meta.label}</span>
                                        {item.submolt && (
                                            <span style={{
                                                fontSize: '0.65rem',
                                                color: 'var(--text-dim, #555)',
                                                background: 'rgba(255,255,255,0.05)',
                                                padding: '1px 6px',
                                                borderRadius: '3px',
                                            }}>m/{item.submolt}</span>
                                        )}
                                    </div>
                                    <span style={{
                                        fontSize: '0.6rem',
                                        color: 'var(--text-dim, #555)',
                                    }}>{item.created_at ? timeAgo(item.created_at) : ''}</span>
                                </div>

                                {/* Title */}
                                {item.title && (
                                    <div style={{
                                        color: 'var(--text-primary, #e0e0ff)',
                                        fontSize: '0.8rem',
                                        fontWeight: 600,
                                        marginBottom: '4px',
                                        lineHeight: 1.3,
                                    }}>{item.title}</div>
                                )}

                                {/* Content preview */}
                                {item.content && (
                                    <div style={{
                                        color: 'var(--text-secondary, #888)',
                                        fontSize: '0.72rem',
                                        lineHeight: 1.4,
                                        overflow: 'hidden',
                                        display: '-webkit-box',
                                        WebkitLineClamp: 3,
                                        WebkitBoxOrient: 'vertical',
                                    }}>{item.content}</div>
                                )}

                                {/* Link */}
                                {item.url && (
                                    <a
                                        href={item.url}
                                        target="_blank"
                                        rel="noopener noreferrer"
                                        style={{
                                            display: 'inline-block',
                                            marginTop: '6px',
                                            fontSize: '0.65rem',
                                            color: '#a855f7',
                                            textDecoration: 'none',
                                            letterSpacing: '0.3px',
                                        }}
                                        onMouseEnter={e => (e.currentTarget.style.textDecoration = 'underline')}
                                        onMouseLeave={e => (e.currentTarget.style.textDecoration = 'none')}
                                    >↗ View on Moltbook</a>
                                )}
                            </div>
                        );
                    })
                )}
            </div>

            {/* Footer */}
            <div style={{
                padding: '10px 16px',
                borderTop: '1px solid var(--border-color, #1a1a2e)',
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'center',
            }}>
                <span style={{
                    fontSize: '0.6rem',
                    color: 'var(--text-dim, #555)',
                    letterSpacing: '0.5px',
                }}>POLLS EVERY 30s</span>
                <a
                    href="https://www.moltbook.com"
                    target="_blank"
                    rel="noopener noreferrer"
                    style={{
                        fontSize: '0.65rem',
                        color: '#a855f7',
                        textDecoration: 'none',
                    }}
                >moltbook.com ↗</a>
            </div>
        </div>
    );
}
