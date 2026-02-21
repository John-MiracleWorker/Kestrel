import { useState, useRef, useEffect, type FormEvent } from 'react';
import type { Message } from '../../api/client';
import { providers } from '../../api/client';

const PROVIDER_MODELS: Record<string, string[]> = {
    openai: ['gpt-5-mini', 'gpt-5', 'gpt-5.1', 'gpt-5.2', 'gpt-5-nano'],
    anthropic: ['claude-haiku-4-5', 'claude-sonnet-4-5', 'claude-opus-4-6'],
    google: ['gemini-2.0-flash', 'gemini-2.0-pro', 'gemini-1.5-flash', 'gemini-1.5-pro'],
    local: ['auto'],
};

interface ChatViewProps {
    workspaceId: string | null;
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
    onToggleCanvas?: () => void;
}

export function ChatView({
    workspaceId,
    messages,
    streamingMessage,
    onSendMessage,
    isConnected,
    conversationTitle,
    onToggleCanvas,
}: ChatViewProps) {
    const [input, setInput] = useState('');
    const [selectedProvider, setSelectedProvider] = useState('');
    const [selectedModel, setSelectedModel] = useState('');
    const [availableModels, setAvailableModels] = useState<string[]>([]);
    const [loadingModels, setLoadingModels] = useState(false);

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

    // Fetch models when provider changes
    useEffect(() => {
        if (!selectedProvider || !workspaceId) {
            setAvailableModels([]);
            return;
        }

        setLoadingModels(true);
        providers.listModels(workspaceId, selectedProvider)
            .then(models => {
                const modelIds = models.map(m => m.id);
                setAvailableModels(modelIds);
                if (modelIds.length > 0) {
                    setSelectedModel(modelIds[0]);
                } else {
                    setSelectedModel('');
                }
            })
            .catch(err => {
                console.error('Failed to fetch models:', err);
                setAvailableModels([]);
                setSelectedModel('');
            })
            .finally(() => setLoadingModels(false));
    }, [selectedProvider, workspaceId]);

    function handleSubmit(e: FormEvent) {
        e.preventDefault();
        const trimmed = input.trim();
        if (!trimmed) return;
        onSendMessage(trimmed, selectedProvider || undefined, selectedModel || undefined);
        setInput('');
    }

    function handleProviderChange(provider: string) {
        setSelectedProvider(provider);
        setSelectedModel('');
    }

    function handleKeyDown(e: React.KeyboardEvent) {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            handleSubmit(e);
        }
    }

    return (
        <div style={{
            display: 'flex',
            flexDirection: 'column',
            height: '100%',
            background: 'var(--bg-terminal)',
            fontFamily: 'var(--font-mono)',
            color: 'var(--text-primary)',
        }}>
            {/* Header */}
            <header style={{
                height: '60px',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                padding: '0 24px',
                borderBottom: '1px solid var(--border-color)',
                background: 'var(--bg-panel)',
            }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
                    <span style={{ color: 'var(--accent-green)', fontWeight: 'bold' }}>&gt;</span>
                    <h2 style={{ fontSize: '0.9rem', fontWeight: 600, margin: 0 }}>
                        {conversationTitle || 'NEW_SESSION'}
                    </h2>
                </div>

                <div style={{ display: 'flex', alignItems: 'center', gap: '16px' }}>

                    {/* Model Selector */}
                    <div style={{ display: 'flex', gap: '8px' }}>
                        <select
                            value={selectedProvider}
                            onChange={(e) => handleProviderChange(e.target.value)}
                            className="terminal-select"
                        >
                            <option value="">SYSTEM_DEFAULT</option>
                            <option value="google">GOOGLE</option>
                            <option value="openai">OPENAI</option>
                            <option value="anthropic">ANTHROPIC</option>
                            <option value="local">LOCAL_LLM</option>
                        </select>
                        {selectedProvider && (
                            <select
                                value={selectedModel}
                                onChange={(e) => setSelectedModel(e.target.value)}
                                className="terminal-select"
                                disabled={loadingModels}
                            >
                                {loadingModels ? (
                                    <option value="">LOADING...</option>
                                ) : availableModels.length > 0 ? (
                                    availableModels.map((m) => (
                                        <option key={m} value={m}>{m.toUpperCase()}</option>
                                    ))
                                ) : (
                                    <option value="">NO_MODELS</option>
                                )}
                            </select>
                        )}
                    </div>

                    {/* Connection Status */}
                    <div style={{
                        width: '10px',
                        height: '10px',
                        borderRadius: '50%',
                        background: isConnected ? 'var(--accent-green)' : 'var(--accent-error)',
                        boxShadow: `0 0 8px ${isConnected ? 'var(--accent-green)' : 'var(--accent-error)'}`
                    }} title={isConnected ? "System Online" : "System Offline"} />

                    {/* Toggle Canvas */}
                    <button
                        onClick={onToggleCanvas}
                        style={{
                            background: 'transparent',
                            border: '1px solid var(--accent-cyan)',
                            color: 'var(--accent-cyan)',
                            padding: '4px 12px',
                            fontSize: '0.75rem',
                            cursor: 'pointer',
                            borderRadius: '2px'
                        }}
                    >
                        Toggle HUD
                    </button>
                </div>
            </header>

            {/* Messages Area */}
            <div style={{
                flex: 1,
                overflowY: 'auto',
                padding: '24px',
                scrollBehavior: 'smooth'
            }}>
                {messages.length === 0 && !streamingMessage && (
                    <div style={{
                        display: 'flex',
                        flexDirection: 'column',
                        alignItems: 'center',
                        justifyContent: 'center',
                        height: '100%',
                        color: 'var(--text-dim)',
                        opacity: 0.5
                    }}>
                        <div style={{ fontSize: '4rem', marginBottom: '16px' }}>_</div>
                        <p>READY FOR INPUT</p>
                    </div>
                )}

                <div style={{
                    maxWidth: '900px',
                    margin: '0 auto',
                    display: 'flex',
                    flexDirection: 'column',
                    gap: '24px',
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

            {/* Input Area */}
            <div style={{
                padding: '24px',
                borderTop: '1px solid var(--border-color)',
                background: 'var(--bg-panel)'
            }}>
                <form
                    onSubmit={handleSubmit}
                    style={{
                        maxWidth: '900px',
                        margin: '0 auto',
                        position: 'relative',
                        display: 'flex',
                        gap: '12px',
                        alignItems: 'flex-end'
                    }}
                >
                    <span style={{ color: 'var(--accent-cyan)', paddingBottom: '12px', fontWeight: 'bold' }}>$</span>
                    <div style={{
                        flex: 1,
                        background: 'rgba(0,0,0,0.3)',
                        border: '1px solid var(--text-dim)',
                        borderRadius: '4px',
                        padding: '12px',
                        transition: 'border-color 0.2s'
                    }}
                        onFocus={(e) => e.currentTarget.style.borderColor = 'var(--accent-cyan)'}
                        onBlur={(e) => e.currentTarget.style.borderColor = 'var(--text-dim)'}
                        tabIndex={-1}
                    >
                        <textarea
                            ref={inputRef}
                            value={input}
                            onChange={(e) => setInput(e.target.value)}
                            onKeyDown={handleKeyDown}
                            placeholder="Execute command or enter message..."
                            rows={1}
                            style={{
                                width: '100%',
                                border: 'none',
                                background: 'transparent',
                                resize: 'none',
                                maxHeight: '200px',
                                outline: 'none',
                                color: 'var(--text-primary)',
                                fontFamily: 'var(--font-mono)',
                                lineHeight: '1.5'
                            }}
                        />
                    </div>
                    <button
                        type="submit"
                        disabled={!input.trim() || !isConnected}
                        style={{
                            background: input.trim() ? 'var(--accent-cyan)' : 'transparent',
                            color: input.trim() ? '#000' : 'var(--text-dim)',
                            border: '1px solid var(--accent-cyan)',
                            padding: '10px 20px',
                            borderRadius: '4px',
                            cursor: input.trim() ? 'pointer' : 'not-allowed',
                            fontWeight: 'bold',
                            fontFamily: 'var(--font-mono)',
                            height: '46px',
                            opacity: isConnected ? 1 : 0.5
                        }}
                    >
                        SEND
                    </button>
                </form>
            </div>

            <style>{`
                .terminal-select {
                    background: transparent;
                    color: var(--accent-cyan);
                    border: 1px solid var(--text-dim);
                    padding: 4px 8px;
                    font-family: var(--font-mono);
                    font-size: 0.75rem;
                    outline: none;
                    cursor: pointer;
                }
                .terminal-select:hover {
                    border-color: var(--accent-cyan);
                }
                .terminal-select option {
                    background: var(--bg-panel);
                    color: var(--text-primary);
                }
            `}</style>
        </div>
    );
}

function MessageBubble({
    message,
    isStreaming = false,
}: {
    message: { role: string; content: string };
    isStreaming?: boolean;
}) {
    const isUser = message.role === 'user';

    return (
        <div style={{
            display: 'flex',
            flexDirection: 'column',
            alignItems: isUser ? 'flex-end' : 'flex-start',
            maxWidth: '100%'
        }}>
            <div style={{
                fontSize: '0.75rem',
                color: isUser ? 'var(--accent-cyan)' : 'var(--accent-purple)',
                marginBottom: '4px',
                fontFamily: 'var(--font-mono)',
                fontWeight: 600
            }}>
                {isUser ? 'USER_INPUT' : 'SYSTEM_RESPONSE'}
            </div>

            <div style={{
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
                boxShadow: isUser ? '0 0 10px rgba(0, 243, 255, 0.05)' : 'none'
            }}>
                {message.content}
                {isStreaming && (
                    <span style={{
                        display: 'inline-block',
                        width: '8px',
                        height: '14px',
                        background: 'var(--accent-cyan)',
                        marginLeft: '4px',
                        animation: 'blink 1s step-end infinite'
                    }} />
                )}
            </div>
            <style>{`
                @keyframes blink { 50% { opacity: 0; } }
            `}</style>
        </div>
    );
}
