import { useState, useRef, useEffect, type FormEvent } from 'react';
import type { Message } from '../../api/client';

const PROVIDER_MODELS: Record<string, string[]> = {
    openai: ['gpt-5-mini', 'gpt-5', 'gpt-5.1', 'gpt-5.2', 'gpt-5-nano'],
    anthropic: ['claude-haiku-4-5', 'claude-sonnet-4-5', 'claude-opus-4-6'],
    google: ['gemini-3-flash', 'gemini-3-pro', 'gemini-2.5-flash', 'gemini-2.5-pro'],
    local: ['auto'],
};

interface ChatViewProps {
    messages: Message[];
    streamingMessage: {
        id: string;
        role: 'assistant';
        content: string;
        isStreaming: boolean;
    } | null;
    onSendMessage: (content: string, provider?: string, model?: string) => void;
    isConnected: boolean;
    conversationTitle?: string;
}

export function ChatView({
    messages,
    streamingMessage,
    onSendMessage,
    isConnected,
    conversationTitle,
}: ChatViewProps) {
    const [input, setInput] = useState('');
    const [selectedProvider, setSelectedProvider] = useState('');
    const [selectedModel, setSelectedModel] = useState('');
    const messagesEndRef = useRef<HTMLDivElement>(null);
    const inputRef = useRef<HTMLTextAreaElement>(null);

    // Auto-scroll on new messages
    useEffect(() => {
        messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    }, [messages, streamingMessage]);

    // Auto-resize textarea
    useEffect(() => {
        if (inputRef.current) {
            inputRef.current.style.height = 'auto';
            inputRef.current.style.height = Math.min(inputRef.current.scrollHeight, 200) + 'px';
        }
    }, [input]);

    function handleSubmit(e: FormEvent) {
        e.preventDefault();
        const trimmed = input.trim();
        if (!trimmed) return;
        onSendMessage(trimmed, selectedProvider || undefined, selectedModel || undefined);
        setInput('');
    }

    function handleProviderChange(provider: string) {
        setSelectedProvider(provider);
        // Reset model when provider changes; default to first model for that provider
        setSelectedModel(provider ? (PROVIDER_MODELS[provider]?.[0] ?? '') : '');
    }

    function handleKeyDown(e: React.KeyboardEvent) {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            handleSubmit(e);
        }
    }

    return (
        <div className="main-content">
            {/* Header */}
            <header style={{
                height: 'var(--header-height)',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                padding: '0 var(--space-6)',
                borderBottom: '1px solid var(--color-border)',
                background: 'var(--color-bg-elevated)',
            }}>
                <h2 style={{ fontSize: '0.9375rem', fontWeight: 600 }}>
                    {conversationTitle || 'New Conversation'}
                </h2>
                <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-3)' }}>
                    {/* Provider selector */}
                    <select
                        value={selectedProvider}
                        onChange={(e) => handleProviderChange(e.target.value)}
                        style={{
                            background: 'var(--color-bg-surface)',
                            border: '1px solid var(--color-border)',
                            borderRadius: 'var(--radius-sm)',
                            color: 'var(--color-text)',
                            fontSize: '0.8125rem',
                            padding: '2px var(--space-2)',
                            cursor: 'pointer',
                        }}
                    >
                        <option value="">Workspace Default</option>
                        <option value="google">Google</option>
                        <option value="openai">OpenAI</option>
                        <option value="anthropic">Anthropic</option>
                        <option value="local">Local</option>
                    </select>
                    {/* Model selector — only shown when a provider is picked */}
                    {selectedProvider && PROVIDER_MODELS[selectedProvider] && (
                        <select
                            value={selectedModel}
                            onChange={(e) => setSelectedModel(e.target.value)}
                            style={{
                                background: 'var(--color-bg-surface)',
                                border: '1px solid var(--color-border)',
                                borderRadius: 'var(--radius-sm)',
                                color: 'var(--color-text)',
                                fontSize: '0.8125rem',
                                padding: '2px var(--space-2)',
                                cursor: 'pointer',
                            }}
                        >
                            {PROVIDER_MODELS[selectedProvider].map((m) => (
                                <option key={m} value={m}>{m}</option>
                            ))}
                        </select>
                    )}
                    <span className={`badge ${isConnected ? 'badge-success' : 'badge-warning'}`}>
                        {isConnected ? '● Connected' : '○ Disconnected'}
                    </span>
                </div>
            </header>

            {/* Messages */}
            <div style={{
                flex: 1,
                overflowY: 'auto',
                padding: 'var(--space-6)',
            }}>
                {messages.length === 0 && !streamingMessage && (
                    <div style={{
                        display: 'flex',
                        flexDirection: 'column',
                        alignItems: 'center',
                        justifyContent: 'center',
                        height: '100%',
                        gap: 'var(--space-4)',
                        color: 'var(--color-text-tertiary)',
                    }}>
                        <div style={{
                            width: 64,
                            height: 64,
                            background: 'linear-gradient(135deg, var(--color-brand), #a855f7)',
                            borderRadius: 'var(--radius-lg)',
                            display: 'flex',
                            alignItems: 'center',
                            justifyContent: 'center',
                            fontSize: '2rem',
                            boxShadow: 'var(--shadow-glow)',
                        }}>
                            🪶
                        </div>
                        <p style={{ fontSize: '1.125rem', fontWeight: 500 }}>How can I help you today?</p>
                        <p style={{ fontSize: '0.875rem' }}>Start a conversation with Kestrel</p>
                    </div>
                )}

                <div style={{
                    maxWidth: 768,
                    margin: '0 auto',
                    display: 'flex',
                    flexDirection: 'column',
                    gap: 'var(--space-4)',
                }}>
                    {messages.map((msg) => (
                        <MessageBubble key={msg.id} message={msg} />
                    ))}

                    {streamingMessage && (
                        <MessageBubble
                            message={streamingMessage}
                            isStreaming
                        />
                    )}

                    <div ref={messagesEndRef} />
                </div>
            </div>

            {/* Input */}
            <div style={{
                padding: 'var(--space-4) var(--space-6) var(--space-6)',
                borderTop: '1px solid var(--color-border-subtle)',
            }}>
                <form
                    onSubmit={handleSubmit}
                    style={{
                        maxWidth: 768,
                        margin: '0 auto',
                        position: 'relative',
                    }}
                >
                    <div style={{
                        background: 'var(--color-bg-surface)',
                        border: '1px solid var(--color-border)',
                        borderRadius: 'var(--radius-lg)',
                        display: 'flex',
                        alignItems: 'flex-end',
                        padding: 'var(--space-2)',
                        transition: 'border-color var(--transition-fast), box-shadow var(--transition-fast)',
                    }}>
                        <textarea
                            ref={inputRef}
                            className="input"
                            value={input}
                            onChange={(e) => setInput(e.target.value)}
                            onKeyDown={handleKeyDown}
                            placeholder="Message Kestrel..."
                            rows={1}
                            style={{
                                border: 'none',
                                background: 'transparent',
                                resize: 'none',
                                flex: 1,
                                maxHeight: 200,
                                outline: 'none',
                                boxShadow: 'none',
                            }}
                        />
                        <button
                            className="btn btn-primary"
                            type="submit"
                            disabled={!input.trim() || !isConnected}
                            style={{
                                borderRadius: 'var(--radius-md)',
                                padding: 'var(--space-2)',
                                minWidth: 36,
                                height: 36,
                                opacity: input.trim() ? 1 : 0.5,
                            }}
                        >
                            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                                <line x1="22" y1="2" x2="11" y2="13" />
                                <polygon points="22,2 15,22 11,13 2,9" />
                            </svg>
                        </button>
                    </div>
                </form>
            </div>
        </div>
    );
}

// ── Message Bubble ──────────────────────────────────────────────────

function MessageBubble({
    message,
    isStreaming = false,
}: {
    message: { role: string; content: string };
    isStreaming?: boolean;
}) {
    const isUser = message.role === 'user';

    return (
        <div
            className="animate-fade-in"
            style={{
                display: 'flex',
                justifyContent: isUser ? 'flex-end' : 'flex-start',
                gap: 'var(--space-3)',
            }}
        >
            {!isUser && (
                <div style={{
                    width: 32,
                    height: 32,
                    borderRadius: 'var(--radius-sm)',
                    background: 'linear-gradient(135deg, var(--color-brand), #a855f7)',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    fontSize: '0.875rem',
                    flexShrink: 0,
                }}>
                    🪶
                </div>
            )}

            <div style={{
                maxWidth: '70%',
                padding: 'var(--space-3) var(--space-4)',
                borderRadius: isUser
                    ? 'var(--radius-lg) var(--radius-lg) var(--radius-sm) var(--radius-lg)'
                    : 'var(--radius-lg) var(--radius-lg) var(--radius-lg) var(--radius-sm)',
                background: isUser
                    ? 'var(--color-brand)'
                    : 'var(--color-bg-surface)',
                border: isUser ? 'none' : '1px solid var(--color-border)',
                color: isUser ? '#fff' : 'var(--color-text)',
                fontSize: '0.9375rem',
                lineHeight: 1.6,
                whiteSpace: 'pre-wrap',
                wordBreak: 'break-word',
            }}>
                {message.content}
                {isStreaming && (
                    <span style={{
                        display: 'inline-block',
                        width: 6,
                        height: 16,
                        background: 'var(--color-brand)',
                        borderRadius: 1,
                        marginLeft: 2,
                        animation: 'pulse 1s infinite',
                        verticalAlign: 'text-bottom',
                    }} />
                )}
            </div>

            {isUser && (
                <div style={{
                    width: 32,
                    height: 32,
                    borderRadius: 'var(--radius-full)',
                    background: 'var(--color-bg-hover)',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    fontSize: '0.75rem',
                    fontWeight: 600,
                    flexShrink: 0,
                    color: 'var(--color-text-secondary)',
                }}>
                    U
                </div>
            )}
        </div>
    );
}
