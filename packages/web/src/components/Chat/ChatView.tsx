import { useState, useRef, useEffect, useCallback, type FormEvent } from 'react';
import type { Message } from '../../api/client';
import { providers, uploadFiles } from '../../api/client';
import { RichContent } from './RichContent';
import { NotificationBell } from '../Layout/NotificationBell';
import { StatusOrb } from '../Layout/StatusOrb';
import { InputComposer } from './InputComposer';
import { MessageBubble } from './MessageBubble';

import type { RoutingInfo } from '../../hooks/useChat';

interface ChatViewProps {
    workspaceId: string | null;
    messages: Message[];
    streamingMessage: {
        id: string;
        role: 'assistant';
        content: string;
        isStreaming: boolean;
        toolActivity?: {
            status: string;
            toolName?: string;
            toolArgs?: string;
            toolResult?: string;
            thinking?: string;
        } | null;
        agentActivities?: Array<{ activity_type: string;[key: string]: unknown }>;
        routingInfo?: RoutingInfo | null;
    } | null;
    onSendMessage: (
        content: string,
        provider?: string,
        model?: string,
        attachments?: Array<{ url: string; filename: string; mimeType: string; size: number }>,
    ) => void;
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
    const [modelError, setModelError] = useState('');

    const messagesEndRef = useRef<HTMLDivElement>(null);
    const inputRef = useRef<HTMLTextAreaElement>(null);
    const fileInputRef = useRef<HTMLInputElement>(null);
    const [pendingFiles, setPendingFiles] = useState<File[]>([]);
    const [isUploading, setIsUploading] = useState(false);
    const [uploadError, setUploadError] = useState('');

    function loadModels(provider: string) {
        if (!workspaceId || !provider) return;

        setLoadingModels(true);
        setModelError('');
        providers
            .listModels(workspaceId, provider)
            .then((models) => {
                const modelIds = models.map((m) => m.id);
                setAvailableModels(modelIds);
                if (modelIds.length > 0) {
                    setSelectedModel(modelIds[0]);
                } else {
                    setSelectedModel('');
                }
                setModelError('');
            })
            .catch((err) => {
                console.error('Failed to fetch models:', err);
                setAvailableModels([]);
                setSelectedModel('');
                setModelError('Could not load models for this provider. Please try again.');
            })
            .finally(() => setLoadingModels(false));
    }

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
            setModelError('');
            return;
        }

        loadModels(selectedProvider);
    }, [selectedProvider, workspaceId]);

    function submitWithPendingFiles(trimmed: string) {
        setIsUploading(true);
        uploadFiles(pendingFiles)
            .then((uploaded) => {
                const attachments = uploaded.map((f) => ({
                    url: f.url,
                    filename: f.filename,
                    mimeType: f.mimeType,
                    size: f.size,
                }));
                onSendMessage(
                    trimmed || 'Analyze these files',
                    selectedProvider || undefined,
                    selectedModel || undefined,
                    attachments,
                );
                setInput('');
                setPendingFiles([]);
                setUploadError('');
            })
            .catch((err) => {
                console.error('Upload failed:', err);
                setUploadError('Upload failed. Please check your files and try again.');
            })
            .finally(() => setIsUploading(false));
    }

    function handleSubmit(e: FormEvent) {
        e.preventDefault();
        const trimmed = input.trim();
        if (!trimmed && pendingFiles.length === 0) return;
        if (isUploading) return;

        if (pendingFiles.length > 0) {
            submitWithPendingFiles(trimmed);
        } else {
            onSendMessage(trimmed, selectedProvider || undefined, selectedModel || undefined);
            setInput('');
        }
    }

    function handleAttach() {
        fileInputRef.current?.click();
    }

    function handleSlashCommand(command: string) {
        switch (command) {
            case '/canvas':
                onToggleCanvas?.();
                break;
            case '/clear':
                // Dispatch a new-conversation event for App.tsx to handle
                window.dispatchEvent(new CustomEvent('new-conversation'));
                break;
            case '/settings':
                window.dispatchEvent(new CustomEvent('open-settings'));
                break;
            default:
                // For unhandled commands, send as a message with /command prefix
                onSendMessage(command);
                break;
        }
    }

    function handleFilesSelected(e: React.ChangeEvent<HTMLInputElement>) {
        const files = Array.from(e.target.files || []);
        setPendingFiles((prev) => [...prev, ...files].slice(0, 5)); // Max 5
        if (fileInputRef.current) fileInputRef.current.value = '';
    }

    function removeFile(index: number) {
        setPendingFiles((prev) => prev.filter((_, i) => i !== index));
    }

    function handleProviderChange(provider: string) {
        setSelectedProvider(provider);
        setSelectedModel('');
        setModelError('');
        setUploadError('');
    }

    function handleRetryUpload() {
        if (isUploading || pendingFiles.length === 0) return;
        submitWithPendingFiles(input.trim());
    }

    function handleRetryModels() {
        if (!selectedProvider || !workspaceId) return;
        loadModels(selectedProvider);
    }

    function handleKeyDown(e: React.KeyboardEvent) {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            handleSubmit(e);
        }
    }

    return (
        <div
            style={{
                display: 'flex',
                flexDirection: 'column',
                height: '100%',
                background: 'var(--bg-terminal)',
                fontFamily: 'var(--font-mono)',
                color: 'var(--text-primary)',
            }}
        >
            {/* Header */}
            <header
                style={{
                    height: '60px',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                    padding: '0 24px',
                    borderBottom: '1px solid var(--border-color)',
                    background: 'var(--bg-panel)',
                }}
            >
                <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
                    <span style={{ color: 'var(--accent-green)', fontWeight: 'bold' }}>&gt;</span>
                    <h2 style={{ fontSize: '0.9rem', fontWeight: 600, margin: 0 }}>
                        {conversationTitle || 'NEW_SESSION'}
                    </h2>
                </div>

                <div style={{ display: 'flex', alignItems: 'center', gap: '16px' }}>
                    {/* Model Selector */}
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                        {modelError && (
                            <div
                                style={{
                                    border: '1px solid var(--accent-error)',
                                    background: 'rgba(239,68,68,0.12)',
                                    color: 'var(--text-primary)',
                                    padding: '6px 8px',
                                    borderRadius: '4px',
                                    fontSize: '0.7rem',
                                    display: 'flex',
                                    alignItems: 'center',
                                    gap: '8px',
                                }}
                            >
                                <span style={{ flex: 1 }}>{modelError}</span>
                                <button
                                    type="button"
                                    onClick={handleRetryModels}
                                    style={{
                                        background: 'transparent',
                                        border: '1px solid var(--accent-error)',
                                        color: 'var(--accent-error)',
                                        fontSize: '0.65rem',
                                        cursor: 'pointer',
                                        padding: '2px 6px',
                                    }}
                                >
                                    Retry models
                                </button>
                                <button
                                    type="button"
                                    onClick={() => setModelError('')}
                                    style={{
                                        background: 'none',
                                        border: 'none',
                                        color: 'var(--text-dim)',
                                        cursor: 'pointer',
                                        fontSize: '0.85rem',
                                    }}
                                >
                                    ✕
                                </button>
                            </div>
                        )}
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
                                            <option key={m} value={m}>
                                                {m.toUpperCase()}
                                            </option>
                                        ))
                                    ) : (
                                        <option value="">NO_MODELS</option>
                                    )}
                                </select>
                            )}
                        </div>
                    </div>

                    {/* Connection Status */}
                    <StatusOrb
                        isConnected={isConnected}
                        isStreaming={!!streamingMessage?.isStreaming}
                        toolStatus={streamingMessage?.toolActivity?.status}
                        wasEscalated={streamingMessage?.routingInfo?.wasEscalated}
                    />

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
                            borderRadius: '2px',
                        }}
                    >
                        Toggle HUD
                    </button>

                    {/* Notification Bell */}
                    <NotificationBell />
                </div>
            </header>

            {/* Messages Area */}
            <div
                style={{
                    flex: 1,
                    overflowY: 'auto',
                    padding: '24px',
                    scrollBehavior: 'smooth',
                }}
            >
                {messages.length === 0 && !streamingMessage && (
                    <div
                        style={{
                            display: 'flex',
                            flexDirection: 'column',
                            alignItems: 'center',
                            justifyContent: 'center',
                            height: '100%',
                            color: 'var(--text-dim)',
                            opacity: 0.5,
                        }}
                    >
                        <div style={{ fontSize: '4rem', marginBottom: '16px' }}>_</div>
                        <p>READY FOR INPUT</p>
                    </div>
                )}

                <div
                    style={{
                        maxWidth: '900px',
                        margin: '0 auto',
                        display: 'flex',
                        flexDirection: 'column',
                        gap: '24px',
                    }}
                >
                    {messages.map((msg) => (
                        <MessageBubble key={msg.id} message={msg} routingInfo={msg.routingInfo} />
                    ))}

                    {streamingMessage && (
                        <MessageBubble
                            message={streamingMessage}
                            isStreaming
                            toolActivity={streamingMessage.toolActivity}
                            agentActivities={streamingMessage.agentActivities}
                            routingInfo={streamingMessage.routingInfo}
                        />
                    )}

                    <div ref={messagesEndRef} />
                </div>
            </div>

            {/* Input Area */}
            <div
                style={{
                    padding: '24px',
                    borderTop: '1px solid var(--border-color)',
                    background: 'var(--bg-panel)',
                }}
            >
                <div style={{ maxWidth: '900px', margin: '0 auto' }}>
                    {uploadError && (
                        <div
                            style={{
                                border: '1px solid var(--accent-error)',
                                background: 'rgba(239,68,68,0.12)',
                                color: 'var(--text-primary)',
                                padding: '8px 10px',
                                borderRadius: '4px',
                                fontSize: '0.75rem',
                                display: 'flex',
                                alignItems: 'center',
                                gap: '8px',
                                marginBottom: '10px',
                            }}
                        >
                            <span style={{ flex: 1 }}>{uploadError}</span>
                            <button
                                type="button"
                                onClick={handleRetryUpload}
                                disabled={pendingFiles.length === 0 || isUploading}
                                style={{
                                    background: 'transparent',
                                    border: '1px solid var(--accent-error)',
                                    color: 'var(--accent-error)',
                                    fontSize: '0.7rem',
                                    cursor:
                                        pendingFiles.length > 0 && !isUploading
                                            ? 'pointer'
                                            : 'not-allowed',
                                    padding: '2px 8px',
                                    opacity: pendingFiles.length > 0 && !isUploading ? 1 : 0.5,
                                }}
                            >
                                Retry upload
                            </button>
                            <button
                                type="button"
                                onClick={() => setUploadError('')}
                                style={{
                                    background: 'none',
                                    border: 'none',
                                    color: 'var(--text-dim)',
                                    cursor: 'pointer',
                                    fontSize: '0.85rem',
                                }}
                            >
                                ✕
                            </button>
                        </div>
                    )}
                    {/* Hidden file input */}
                    <input
                        ref={fileInputRef}
                        type="file"
                        multiple
                        accept="image/*,.pdf,.txt,.md,.py,.js,.ts,.tsx,.jsx,.json,.csv,.xml,.yaml,.yml,.html,.css,.sh,.sql,.java,.cpp,.c,.h,.go,.rs,.rb,.swift,.kt"
                        style={{ display: 'none' }}
                        onChange={handleFilesSelected}
                    />
                    <InputComposer
                        value={input}
                        onChange={setInput}
                        onSubmit={handleSubmit}
                        onKeyDown={handleKeyDown}
                        onAttach={handleAttach}
                        onSlashCommand={handleSlashCommand}
                        pendingFiles={pendingFiles}
                        onRemoveFile={removeFile}
                        isUploading={isUploading}
                    />
                </div>
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

