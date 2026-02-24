import { useState } from 'react';
import { RichContent } from './RichContent';
import { ActivityTimeline } from '../Agent/ActivityTimeline';
import { KestrelProcessBar } from './KestrelProcessBar';

/* ── Feedback Buttons ─────────────────────────────────────────────── */

export function FeedbackButtons({ messageId }: { messageId: string }) {
    const [selected, setSelected] = useState<'up' | 'down' | null>(null);

    const submitFeedback = async (rating: 1 | -1) => {
        const next = rating === 1 ? 'up' : 'down';
        if (selected === next) return; // Already selected
        setSelected(next);
        try {
            // Best effort — don't block UI
            const token = localStorage.getItem('kestrel_refresh');
            if (token) {
                await fetch('/api/workspaces/default/feedback', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        conversationId: '00000000-0000-0000-0000-000000000000',
                        messageId,
                        rating,
                        comment: '',
                    }),
                });
            }
        } catch {
            /* silent */
        }
    };

    return (
        <div style={{ display: 'flex', gap: '4px', marginTop: '4px', marginLeft: '3px' }}>
            <button
                onClick={() => submitFeedback(1)}
                style={{
                    background: selected === 'up' ? 'rgba(16,185,129,0.15)' : 'transparent',
                    border: 'none',
                    color: selected === 'up' ? '#10b981' : 'var(--text-dim)',
                    cursor: 'pointer',
                    padding: '2px 6px',
                    borderRadius: '4px',
                    fontSize: '0.8rem',
                    transition: 'all 0.2s',
                    opacity: selected && selected !== 'up' ? 0.3 : 0.6,
                }}
                title="Good response"
            >
                👍
            </button>
            <button
                onClick={() => submitFeedback(-1)}
                style={{
                    background: selected === 'down' ? 'rgba(239,68,68,0.15)' : 'transparent',
                    border: 'none',
                    color: selected === 'down' ? '#ef4444' : 'var(--text-dim)',
                    cursor: 'pointer',
                    padding: '2px 6px',
                    borderRadius: '4px',
                    fontSize: '0.8rem',
                    transition: 'all 0.2s',
                    opacity: selected && selected !== 'down' ? 0.3 : 0.6,
                }}
                title="Bad response"
            >
                👎
            </button>
        </div>
    );
}

/* ── Message Bubble ───────────────────────────────────────────────── */

export function MessageBubble({
    message,
    isStreaming = false,
    toolActivity,
    agentActivities = [],
    routingInfo,
}: {
    message: { role: string; content: string };
    isStreaming?: boolean;
    toolActivity?: {
        status: string;
        toolName?: string;
        toolArgs?: string;
        toolResult?: string;
        thinking?: string;
    } | null;
    agentActivities?: Array<{ activity_type: string;[key: string]: unknown }>;
    routingInfo?: { provider: string; model: string; wasEscalated: boolean; complexity: number } | null;
}) {
    const isUser = message.role === 'user';

    return (
        <div
            style={{
                display: 'flex',
                flexDirection: 'column',
                alignItems: isUser ? 'flex-end' : 'flex-start',
                maxWidth: '100%',
            }}
        >
            <div
                style={{
                    fontSize: '0.75rem',
                    color: isUser ? 'var(--accent-cyan)' : 'var(--accent-purple)',
                    marginBottom: '4px',
                    fontFamily: 'var(--font-mono)',
                    fontWeight: 600,
                }}
            >
                {isUser ? 'USER_INPUT' : 'SYSTEM_RESPONSE'}
                {/* Routing badge for assistant messages */}
                {!isUser && routingInfo && (
                    <span
                        style={{
                            marginLeft: '10px',
                            fontSize: '0.6rem',
                            padding: '2px 6px',
                            borderRadius: '3px',
                            background: routingInfo.wasEscalated
                                ? 'rgba(249, 115, 22, 0.15)'
                                : 'rgba(0, 243, 255, 0.1)',
                            border: `1px solid ${routingInfo.wasEscalated ? 'rgba(249, 115, 22, 0.4)' : 'rgba(0, 243, 255, 0.3)'}`,
                            color: routingInfo.wasEscalated ? '#f97316' : 'var(--accent-cyan)',
                            fontWeight: 400,
                            letterSpacing: '0.05em',
                        }}
                        title={`Provider: ${routingInfo.provider} | Model: ${routingInfo.model} | Complexity: ${routingInfo.complexity.toFixed(1)}`}
                    >
                        {routingInfo.wasEscalated ? '☁️ ' : '⚡ '}
                        {routingInfo.model.split('/').pop()}
                    </span>
                )}
            </div>

            <div
                style={{
                    maxWidth: '85%',
                    padding: '12px 16px',
                    background: isUser ? 'rgba(0, 243, 255, 0.1)' : 'transparent',
                    border: isUser ? '1px solid var(--accent-cyan)' : 'none',
                    borderLeft: !isUser ? '3px solid var(--accent-purple)' : undefined,
                    color: 'var(--text-primary)',
                    fontFamily: isUser ? 'var(--font-mono)' : 'var(--font-sans)',
                    lineHeight: 1.6,
                    fontSize: '0.95rem',
                    whiteSpace: 'pre-wrap',
                    position: 'relative',
                    boxShadow: isUser ? '0 0 10px rgba(0, 243, 255, 0.05)' : 'none',
                }}
            >
                {!message.content &&
                    isStreaming &&
                    !toolActivity &&
                    agentActivities.length === 0 && (
                        <span
                            style={{
                                fontFamily: 'var(--font-mono)',
                                fontSize: '0.85rem',
                                color: 'var(--accent-purple)',
                                opacity: 0.8,
                            }}
                        >
                            <span className="thinking-dots">thinking</span>
                        </span>
                    )}
                {/* Activity Timeline — unified tool + agent activity display */}
                {isStreaming && (toolActivity || agentActivities.length > 0) && (
                    <>
                        <ActivityTimeline toolActivity={toolActivity} agentActivities={agentActivities} />
                        <KestrelProcessBar activities={agentActivities} toolActivity={toolActivity} />
                    </>
                )}
                {isUser ? message.content : <RichContent content={message.content} />}
                {isStreaming && message.content && (
                    <span
                        style={{
                            display: 'inline-block',
                            width: '8px',
                            height: '14px',
                            background: 'var(--accent-cyan)',
                            marginLeft: '4px',
                            animation: 'blink 1s step-end infinite',
                        }}
                    />
                )}
            </div>
            {/* Feedback buttons for assistant messages */}
            {!isUser && message.content && !isStreaming && (
                <FeedbackButtons messageId={(message as { id?: string }).id || ''} />
            )}
            <style>{`
                @keyframes blink { 50% { opacity: 0; } }
                @keyframes thinking-pulse {
                    0%, 100% { opacity: 0.4; }
                    50% { opacity: 1; }
                }
                .thinking-dots::after {
                    content: '...';
                    animation: thinking-pulse 1.5s ease-in-out infinite;
                }
                @keyframes agent-pulse {
                    0%, 100% { opacity: 0.4; transform: scale(1); }
                    50% { opacity: 1; transform: scale(1.3); }
                }
            `}</style>
        </div>
    );
}
