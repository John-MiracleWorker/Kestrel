import { useState, useRef, useEffect, type FormEvent } from 'react';
import type { Message } from '../../api/client';
import { providers, uploadFiles } from '../../api/client';
import { RichContent } from './RichContent';
import { NotificationBell } from '../Layout/NotificationBell';

interface ChatViewProps {
    workspaceId: string | null;
    conversationId: string | null;
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
    } | null;
    onSendMessage: (content: string, provider?: string, model?: string, attachments?: Array<{ url: string; filename: string; mimeType: string; size: number }>) => void;
    isConnected: boolean;
    conversationTitle?: string;
    onToggleCanvas?: () => void;
}

export function ChatView({
    workspaceId,
    conversationId,
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
    const fileInputRef = useRef<HTMLInputElement>(null);
    const [pendingFiles, setPendingFiles] = useState<File[]>([]);
    const [isUploading, setIsUploading] = useState(false);

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
        providers
            .listModels(workspaceId, selectedProvider)
            .then((models) => {
                const modelIds = models.map((m) => m.id);
                setAvailableModels(modelIds);
                if (modelIds.length > 0) {
                    setSelectedModel(modelIds[0]);
                } else {
                    setSelectedModel('');
                }
            })
            .catch((err) => {
                console.error('Failed to fetch models:', err);
                setAvailableModels([]);
                setSelectedModel('');
            })
            .finally(() => setLoadingModels(false));
    }, [selectedProvider, workspaceId]);

    function handleSubmit(e: FormEvent) {
        e.preventDefault();
        const trimmed = input.trim();
        if (!trimmed && pendingFiles.length === 0) return;
        if (isUploading) return;

        if (pendingFiles.length > 0) {
            setIsUploading(true);
            uploadFiles(pendingFiles)
                .then((uploaded) => {
                    const attachments = uploaded.map((f) => ({
                        url: f.url,
                        filename: f.filename,
                        mimeType: f.mimeType,
                        size: f.size,
                    }));
                    onSendMessage(trimmed || 'Analyze these files', selectedProvider || undefined, selectedModel || undefined, attachments);
                    setInput('');
                    setPendingFiles([]);
                })
                .catch((err) => console.error('Upload failed:', err))
                .finally(() => setIsUploading(false));
        } else {
            onSendMessage(trimmed, selectedProvider || undefined, selectedModel || undefined);
            setInput('');
        }
    }

    function handleAttach() {
        fileInputRef.current?.click();
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

                    {/* Connection Status */}
                    <div
                        style={{
                            width: '10px',
                            height: '10px',
                            borderRadius: '50%',
                            background: isConnected ? 'var(--accent-green)' : 'var(--accent-error)',
                            boxShadow: `0 0 8px ${isConnected ? 'var(--accent-green)' : 'var(--accent-error)'}`,
                        }}
                        title={isConnected ? 'System Online' : 'System Offline'}
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
                        <MessageBubble
                            key={msg.id}
                            message={msg}
                            workspaceId={workspaceId}
                            conversationId={conversationId}
                        />
                    ))}

                    {streamingMessage && (
                        <MessageBubble
                            message={streamingMessage}
                            isStreaming
                            workspaceId={workspaceId}
                            conversationId={conversationId}
                            toolActivity={streamingMessage.toolActivity}
                            agentActivities={streamingMessage.agentActivities}
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
                <form
                    onSubmit={handleSubmit}
                    style={{
                        maxWidth: '900px',
                        margin: '0 auto',
                        position: 'relative',
                        display: 'flex',
                        gap: '12px',
                        alignItems: 'flex-end',
                    }}
                >
                    <span
                        style={{
                            color: 'var(--accent-cyan)',
                            paddingBottom: '12px',
                            fontWeight: 'bold',
                        }}
                    >
                        $
                    </span>
                    <div
                        style={{
                            flex: 1,
                            background: 'rgba(0,0,0,0.3)',
                            border: '1px solid var(--text-dim)',
                            borderRadius: '4px',
                            padding: '12px',
                            transition: 'border-color 0.2s',
                        }}
                        onFocus={(e) => (e.currentTarget.style.borderColor = 'var(--accent-cyan)')}
                        onBlur={(e) => (e.currentTarget.style.borderColor = 'var(--text-dim)')}
                        tabIndex={-1}
                        onDragOver={(e) => { e.preventDefault(); e.currentTarget.style.borderColor = 'var(--accent-purple)'; }}
                        onDragLeave={(e) => { e.currentTarget.style.borderColor = 'var(--text-dim)'; }}
                        onDrop={(e) => {
                            e.preventDefault();
                            e.currentTarget.style.borderColor = 'var(--text-dim)';
                            const files = Array.from(e.dataTransfer.files);
                            if (files.length) setPendingFiles((prev) => [...prev, ...files].slice(0, 5));
                        }}
                    >
                        {/* Pending file chips */}
                        {pendingFiles.length > 0 && (
                            <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px', marginBottom: '8px' }}>
                                {pendingFiles.map((f, i) => (
                                    <div key={i} style={{
                                        display: 'flex', alignItems: 'center', gap: '4px',
                                        background: 'rgba(0,243,255,0.1)', border: '1px solid var(--accent-cyan)',
                                        borderRadius: '4px', padding: '4px 8px', fontSize: '0.75rem',
                                        color: 'var(--accent-cyan)', fontFamily: 'var(--font-mono)',
                                    }}>
                                        <span>{f.type.startsWith('image/') ? '🖼️' : '📄'}</span>
                                        <span style={{ maxWidth: '120px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{f.name}</span>
                                        <span style={{ color: 'var(--text-dim)', fontSize: '0.65rem' }}>
                                            {(f.size / 1024).toFixed(0)}KB
                                        </span>
                                        <button
                                            type="button"
                                            onClick={() => removeFile(i)}
                                            style={{
                                                background: 'none', border: 'none', color: 'var(--text-dim)',
                                                cursor: 'pointer', padding: '0 2px', fontSize: '0.8rem',
                                            }}
                                        >✕</button>
                                    </div>
                                ))}
                            </div>
                        )}
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
                                lineHeight: '1.5',
                            }}
                        />
                    </div>
                    {/* Hidden file input */}
                    <input
                        ref={fileInputRef}
                        type="file"
                        multiple
                        accept="image/*,.pdf,.txt,.md,.py,.js,.ts,.tsx,.jsx,.json,.csv,.xml,.yaml,.yml,.html,.css,.sh,.sql,.java,.cpp,.c,.h,.go,.rs,.rb,.swift,.kt"
                        style={{ display: 'none' }}
                        onChange={handleFilesSelected}
                    />
                    {/* Attach button */}
                    <button
                        type="button"
                        onClick={handleAttach}
                        title="Attach files (images, code, PDFs)"
                        style={{
                            background: pendingFiles.length > 0 ? 'rgba(168,85,247,0.2)' : 'transparent',
                            color: pendingFiles.length > 0 ? 'var(--accent-purple)' : 'var(--text-dim)',
                            border: '1px solid ' + (pendingFiles.length > 0 ? 'var(--accent-purple)' : 'var(--text-dim)'),
                            padding: '10px 12px',
                            borderRadius: '4px',
                            cursor: 'pointer',
                            fontFamily: 'var(--font-mono)',
                            height: '46px',
                            fontSize: '1.1rem',
                            transition: 'all 0.2s',
                        }}
                    >📎</button>
                    <button
                        type="submit"
                        disabled={(!input.trim() && pendingFiles.length === 0) || !isConnected || isUploading}
                        style={{
                            background: (input.trim() || pendingFiles.length > 0) ? (isUploading ? 'var(--accent-purple)' : 'var(--accent-cyan)') : 'transparent',
                            color: (input.trim() || pendingFiles.length > 0) ? '#000' : 'var(--text-dim)',
                            border: '1px solid var(--accent-cyan)',
                            padding: '10px 20px',
                            borderRadius: '4px',
                            cursor: (input.trim() || pendingFiles.length > 0) ? 'pointer' : 'not-allowed',
                            fontWeight: 'bold',
                            fontFamily: 'var(--font-mono)',
                            height: '46px',
                            opacity: isConnected ? 1 : 0.5,
                        }}
                    >
                        {isUploading ? 'UPLOADING...' : 'SEND'}
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
        </div >
    );
}

/* ── KestrelProcessBar ────────────────────────────────────────────── */

type Activity = { activity_type: string;[key: string]: unknown };

interface PhaseData {
    key: string;
    icon: string;
    label: string;
    color: string;
    summary: string;
    items: Activity[];
}

const VOTE_COLORS: Record<string, string> = {
    approve: '#22c55e',
    reject: '#ef4444',
    conditional: '#f59e0b',
    abstain: '#6b7280',
};
const ROLE_ICONS: Record<string, string> = {
    architect: '🏗️',
    implementer: '⚙️',
    security: '🔒',
    devils_advocate: '😈',
    user_advocate: '👤',
};

function buildPhases(
    activities: Activity[],
    toolActivity?: { status: string; toolName?: string } | null,
): PhaseData[] {
    const phases: PhaseData[] = [];
    const byType = (prefix: string) =>
        activities.filter((a) => a?.activity_type?.startsWith(prefix));

    // Memory
    const memories = byType('memory_recalled');
    if (memories.length > 0) {
        const count = (memories[0].count as number) || 0;
        phases.push({
            key: 'memory',
            icon: '🧠',
            label: 'Memory',
            color: '#06b6d4',
            summary: `${count} recalled`,
            items: memories,
        });
    }

    // Lessons
    const lessons = byType('lessons_loaded');
    if (lessons.length > 0) {
        const count = (lessons[0].count as number) || 0;
        phases.push({
            key: 'lessons',
            icon: '📖',
            label: 'Lessons',
            color: '#8b5cf6',
            summary: `${count} loaded`,
            items: lessons,
        });
    }

    // Skills
    const skills = byType('skill_activated');
    if (skills.length > 0) {
        const count = (skills[0].count as number) || 0;
        phases.push({
            key: 'skills',
            icon: '🔧',
            label: 'Skills',
            color: '#f97316',
            summary: `${count} active`,
            items: skills,
        });
    }

    // Plan
    const plans = byType('plan_created');
    if (plans.length > 0) {
        const stepCount = (plans[0].step_count as number) || 0;
        phases.push({
            key: 'plan',
            icon: '📋',
            label: 'Plan',
            color: '#3b82f6',
            summary: `${stepCount} steps`,
            items: plans,
        });
    }

    // Tools (from toolActivity state)
    if (toolActivity) {
        const toolItems = activities.filter(
            (a) => a.activity_type === 'tool_calling' || a.activity_type === 'tool_result' || a.activity_type === 'calling' || a.activity_type === 'result',
        );
        const toolLabel =
            toolActivity.status === 'thinking'
                ? 'Reasoning'
                : toolActivity.status === 'planning'
                    ? 'Planning'
                    : (toolActivity.status === 'calling' || toolActivity.status === 'tool_calling')
                        ? toolActivity.toolName || 'Tool'
                        : (toolActivity.status === 'result' || toolActivity.status === 'tool_result')
                            ? `${toolActivity.toolName} ✓`
                            : 'Working';
        phases.push({
            key: 'tools',
            icon: '⚡',
            label: toolLabel,
            color: '#8b5cf6',
            summary: (toolActivity.status === 'result' || toolActivity.status === 'tool_result') ? 'done' : '…',
            items: toolItems,
        });
    }

    // Council
    const council = byType('council_');
    if (council.length > 0) {
        const verdict = council.find((a) => a.activity_type === 'council_verdict');
        const consensus = verdict
            ? (verdict.has_consensus as boolean)
                ? 'consensus'
                : 'divided'
            : '…';
        phases.push({
            key: 'council',
            icon: '🤔',
            label: 'Council',
            color: '#f59e0b',
            summary: consensus,
            items: council,
        });
    }

    // Delegation
    const delegation = byType('delegation_');
    if (delegation.length > 0) {
        const done = delegation.find((a) => a.activity_type === 'delegation_complete');
        phases.push({
            key: 'delegation',
            icon: '🔀',
            label: 'Delegation',
            color: '#3b82f6',
            summary: done ? String(done.specialist) : '…',
            items: delegation,
        });
    }

    // Reflection
    const reflection = byType('reflection_');
    if (reflection.length > 0) {
        const verdict = reflection.find((a) => a.activity_type === 'reflection_verdict');
        const conf = verdict ? `${((verdict.confidence as number) * 100).toFixed(0)}%` : '…';
        phases.push({
            key: 'reflection',
            icon: '🔍',
            label: 'Reflection',
            color: '#a855f7',
            summary: conf,
            items: reflection,
        });
    }

    // Evidence
    const evidence = byType('evidence_summary');
    if (evidence.length > 0) {
        const count = (evidence[0].decision_count as number) || 0;
        phases.push({
            key: 'evidence',
            icon: '📎',
            label: 'Evidence',
            color: '#14b8a6',
            summary: `${count} decisions`,
            items: evidence,
        });
    }

    // Confidence (from reflection verdict)
    const reflVerdict = activities.find((a) => a.activity_type === 'reflection_verdict');
    if (reflVerdict) {
        const conf = ((reflVerdict.confidence as number) || 0) * 100;
        phases.push({
            key: 'confidence',
            icon: '🎯',
            label: 'Confidence',
            color: conf >= 80 ? '#22c55e' : conf >= 50 ? '#f59e0b' : '#ef4444',
            summary: `${conf.toFixed(0)}%`,
            items: [reflVerdict],
        });
    }

    // Tokens
    const tokens = byType('token_usage');
    if (tokens.length > 0) {
        const total = (tokens[0].total_tokens as number) || 0;
        const display = total >= 1000 ? `${(total / 1000).toFixed(1)}k` : String(total);
        phases.push({
            key: 'tokens',
            icon: '💰',
            label: 'Tokens',
            color: '#6b7280',
            summary: display,
            items: tokens,
        });
    }

    return phases;
}

function KestrelProcessBar({
    activities,
    toolActivity,
}: {
    activities: Activity[];
    toolActivity?: {
        status: string;
        toolName?: string;
        toolArgs?: string;
        toolResult?: string;
        thinking?: string;
    } | null;
}) {
    const phases = buildPhases(activities, toolActivity);

    if (phases.length === 0 && !toolActivity) return null;

    // Determine current active status label
    const currentLabel = toolActivity
        ? toolActivity.status === 'thinking'
            ? '🧠 Reasoning…'
            : toolActivity.status === 'planning'
                ? '📋 Planning…'
                : (toolActivity.status === 'calling' || toolActivity.status === 'tool_calling')
                    ? `⚡ Using ${toolActivity.toolName || 'tool'}…`
                    : (toolActivity.status === 'result' || toolActivity.status === 'tool_result')
                        ? `✅ ${toolActivity.toolName || 'Tool'} complete`
                        : '🔄 Working…'
        : '🔄 Processing…';

    const isActive = toolActivity?.status === 'calling' || toolActivity?.status === 'thinking' || toolActivity?.status === 'planning';
    const isDone = toolActivity?.status === 'result';
    const accentColor = isActive ? '#a855f7' : isDone ? '#10b981' : '#00f3ff';

    return (
        <div style={{
            marginBottom: '12px',
            borderRadius: '8px',
            overflow: 'hidden',
            border: `1px solid ${accentColor}44`,
            background: `linear-gradient(135deg, ${accentColor}08, ${accentColor}15)`,
            animation: 'processbar-in 0.3s ease-out',
        }}>
            {/* Animated progress line */}
            {isActive && (
                <div style={{
                    height: '2px',
                    background: `linear-gradient(90deg, transparent, ${accentColor}, transparent)`,
                    animation: 'progress-slide 1.5s ease-in-out infinite',
                }} />
            )}

            {/* Main status */}
            <div style={{
                padding: '10px 14px',
                display: 'flex',
                alignItems: 'center',
                gap: '10px',
            }}>
                {/* Pulsing dot */}
                {isActive && (
                    <div style={{
                        width: '8px',
                        height: '8px',
                        borderRadius: '50%',
                        background: accentColor,
                        boxShadow: `0 0 8px ${accentColor}`,
                        animation: 'pulse-dot 1.2s ease-in-out infinite',
                        flexShrink: 0,
                    }} />
                )}
                <span style={{
                    fontFamily: 'var(--font-mono)',
                    fontSize: '0.82rem',
                    fontWeight: 600,
                    color: accentColor,
                    letterSpacing: '0.02em',
                }}>
                    {currentLabel}
                </span>
                {toolActivity?.toolArgs && isActive && (
                    <span style={{
                        fontFamily: 'var(--font-mono)',
                        fontSize: '0.7rem',
                        color: 'var(--text-dim)',
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                        whiteSpace: 'nowrap',
                        maxWidth: '200px',
                    }}>
                        {toolActivity.toolArgs.slice(0, 60)}
                    </span>
                )}
            </div>

            {/* Phase pills (completed phases) */}
            {phases.length > 1 && (
                <div style={{
                    padding: '0 14px 10px',
                    display: 'flex',
                    flexWrap: 'wrap',
                    gap: '6px',
                }}>
                    {phases.map((phase) => (
                        <span
                            key={phase.key}
                            style={{
                                display: 'inline-flex',
                                alignItems: 'center',
                                gap: '4px',
                                padding: '2px 10px',
                                borderRadius: '12px',
                                background: `${phase.color}20`,
                                border: `1px solid ${phase.color}40`,
                                fontFamily: 'var(--font-mono)',
                                fontSize: '0.68rem',
                                color: phase.color,
                                fontWeight: 500,
                            }}
                        >
                            {phase.icon} {phase.label}
                        </span>
                    ))}
                </div>
            )}

            {/* Thinking preview */}
            {toolActivity?.thinking && (
                <div style={{
                    padding: '0 14px 10px',
                    fontFamily: 'var(--font-mono)',
                    fontSize: '0.7rem',
                    color: 'var(--text-dim)',
                    lineHeight: 1.4,
                    maxHeight: '40px',
                    overflow: 'hidden',
                    opacity: 0.7,
                }}>
                    💭 {toolActivity.thinking.slice(0, 150)}
                </div>
            )}

            <style>{`
                @keyframes processbar-in {
                    from { opacity: 0; transform: translateY(-4px); }
                    to { opacity: 1; transform: translateY(0); }
                }
                @keyframes progress-slide {
                    0% { transform: translateX(-100%); }
                    100% { transform: translateX(100%); }
                }
                @keyframes pulse-dot {
                    0%, 100% { opacity: 1; transform: scale(1); }
                    50% { opacity: 0.4; transform: scale(0.8); }
                }
            `}</style>
        </div>
    );

}

function PhaseDetail({ item, phaseKey }: { item: Activity; phaseKey: string }) {
    const dim = { color: 'var(--text-dim)' };

    // Memory — show entities and preview
    if (phaseKey === 'memory') {
        return (
            <div style={dim}>
                Queried: {String((item.entities as string[])?.join(', ') || '—')}
                <br />
                {String((item.preview as string)?.substring(0, 150) || '')}
            </div>
        );
    }

    // Plan — show numbered steps
    if (phaseKey === 'plan' && item.steps) {
        return (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '2px' }}>
                {(item.steps as Array<{ index: number; description: string }>).map((step, i) => (
                    <div key={i} style={{ ...dim, display: 'flex', gap: '6px' }}>
                        <span style={{ color: '#3b82f6', minWidth: '16px' }}>
                            {step.index + 1}.
                        </span>
                        <span>{step.description}</span>
                    </div>
                ))}
            </div>
        );
    }

    // Council opinions — show role, vote, analysis
    if (item.activity_type === 'council_opinion') {
        const voteColor = VOTE_COLORS[item.vote as string] || '#888';
        return (
            <div
                style={{
                    display: 'flex',
                    gap: '8px',
                    padding: '2px 0',
                    borderBottom: '1px solid rgba(255,255,255,0.05)',
                }}
            >
                <span style={{ minWidth: '24px' }}>{ROLE_ICONS[item.role as string] || '•'}</span>
                <span
                    style={{
                        color: voteColor,
                        fontWeight: 600,
                        minWidth: '90px',
                        textTransform: 'uppercase',
                    }}
                >
                    {String(item.vote)}
                </span>
                <span style={dim}>
                    {String((item.analysis as string)?.substring(0, 120) || '')}
                </span>
            </div>
        );
    }

    // Council verdict
    if (item.activity_type === 'council_verdict') {
        return (
            <div
                style={{
                    color: (item.has_consensus as boolean) ? '#22c55e' : '#ef4444',
                    fontWeight: 600,
                    marginTop: '4px',
                }}
            >
                {(item.has_consensus as boolean) ? '✓ CONSENSUS REACHED' : '⚠ NO CONSENSUS'}
                {item.requires_user_review ? ' — User review required' : ''}
            </div>
        );
    }

    // Reflection critique
    if (item.activity_type === 'reflection_critique') {
        const sevColor =
            item.severity === 'critical'
                ? '#ef4444'
                : item.severity === 'high'
                    ? '#f59e0b'
                    : '#6b7280';
        return (
            <div style={{ display: 'flex', gap: '8px', padding: '2px 0' }}>
                <span
                    style={{
                        color: sevColor,
                        fontWeight: 600,
                        textTransform: 'uppercase',
                        fontSize: '0.68rem',
                        minWidth: '55px',
                    }}
                >
                    {String(item.severity)}
                </span>
                <span style={dim}>
                    {String((item.description as string)?.substring(0, 150) || '')}
                </span>
            </div>
        );
    }

    // Evidence decisions
    if (phaseKey === 'evidence' && item.decisions) {
        return (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '2px' }}>
                {(item.decisions as Array<{ type: string; description: string }>).map((d, i) => (
                    <div key={i} style={{ ...dim, display: 'flex', gap: '6px' }}>
                        <span style={{ color: '#14b8a6', minWidth: '80px' }}>{d.type}</span>
                        <span>{d.description}</span>
                    </div>
                ))}
            </div>
        );
    }

    // Token usage
    if (phaseKey === 'tokens') {
        return (
            <div style={dim}>
                Total: {String(item.total_tokens)} tokens · {String(item.iterations)} iterations ·{' '}
                {String(item.tool_calls)} tool calls
            </div>
        );
    }

    // Generic — show preview or message
    const text = String(item.preview || item.message || item.description || '');
    if (text) return <div style={dim}>{text.substring(0, 200)}</div>;
    return null;
}

/* ── Feedback Buttons ─────────────────────────────────────────────── */

function FeedbackButtons({
    messageId,
    workspaceId,
    conversationId,
}: {
    messageId: string;
    workspaceId: string | null;
    conversationId: string | null;
}) {
    const [selected, setSelected] = useState<'up' | 'down' | null>(null);
    const [feedbackStatus, setFeedbackStatus] = useState<'idle' | 'success' | 'error'>('idle');
    const [idWarning, setIdWarning] = useState('');

    const statusTitle =
        feedbackStatus === 'success'
            ? 'Feedback submitted'
            : feedbackStatus === 'error'
                ? 'Feedback failed to submit'
                : undefined;

    const submitFeedback = async (rating: 1 | -1) => {
        const next = rating === 1 ? 'up' : 'down';
        if (selected === next) return; // Already selected
        if (!workspaceId || !conversationId) {
            setIdWarning('Feedback unavailable: missing conversation context.');
            setFeedbackStatus('error');
            return;
        }

        setIdWarning('');
        setSelected(next);
        setFeedbackStatus('idle');
        try {
            // Best effort — don't block UI
            const token = localStorage.getItem('kestrel_refresh');
            if (token) {
                const response = await fetch(`/api/workspaces/${workspaceId}/feedback`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        conversationId,
                        messageId,
                        rating,
                        comment: '',
                    }),
                });

                if (!response.ok) {
                    setSelected((current) => (current === next ? null : current));
                    setFeedbackStatus('error');
                    return;
                }

                setFeedbackStatus('success');
                return;
            }

            setSelected((current) => (current === next ? null : current));
            setFeedbackStatus('error');
        } catch {
            setSelected((current) => (current === next ? null : current));
            setFeedbackStatus('error');
        }
    };

    return (
        <div style={{ marginTop: '4px', marginLeft: '3px' }}>
            <div style={{ display: 'flex', gap: '4px' }}>
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
                    title={statusTitle || 'Good response'}
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
                    title={statusTitle || 'Bad response'}
                >
                    👎
                </button>
            </div>
            {idWarning && (
                <div
                    style={{
                        marginTop: '4px',
                        fontSize: '0.68rem',
                        color: 'var(--accent-error)',
                        fontFamily: 'var(--font-mono)',
                    }}
                >
                    {idWarning}
                </div>
            )}
        </div>
    );
}

/* ── Message Bubble ───────────────────────────────────────────────── */

function MessageBubble({
    message,
    workspaceId,
    conversationId,
    isStreaming = false,
    toolActivity,
    agentActivities = [],
}: {
    message: { role: string; content: string };
    workspaceId: string | null;
    conversationId: string | null;
    isStreaming?: boolean;
    toolActivity?: {
        status: string;
        toolName?: string;
        toolArgs?: string;
        toolResult?: string;
        thinking?: string;
    } | null;
    agentActivities?: Array<{ activity_type: string;[key: string]: unknown }>;
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
                {/* KestrelProcessBar — unified tool + agent activity display */}
                {isStreaming && (toolActivity || agentActivities.length > 0) && (
                    <KestrelProcessBar activities={agentActivities} toolActivity={toolActivity} />
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
                <FeedbackButtons
                    messageId={(message as { id?: string }).id || ''}
                    workspaceId={workspaceId}
                    conversationId={conversationId}
                />
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
