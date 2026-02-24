/**
 * InputComposer – Rich input with /slash commands and enhanced UX.
 */
import { useState, useRef, useEffect, useCallback, type FormEvent } from 'react';

interface SlashCommand {
    command: string;
    label: string;
    icon: string;
    description: string;
    action?: () => void;
}

const SLASH_COMMANDS: SlashCommand[] = [
    { command: '/workflow', label: 'Run Workflow', icon: '🔄', description: 'Launch an automated workflow' },
    { command: '/memory', label: 'Memory Palace', icon: '🧠', description: 'Search the memory graph' },
    { command: '/docs', label: 'Documentation', icon: '📖', description: 'Open auto-documentation' },
    { command: '/canvas', label: 'Toggle Canvas', icon: '📺', description: 'Show/hide the live HUD' },
    { command: '/settings', label: 'Settings', icon: '⚙️', description: 'Open settings panel' },
    { command: '/clear', label: 'New Session', icon: '✨', description: 'Start a fresh conversation' },
    { command: '/pr', label: 'PR Review', icon: '🔍', description: 'Open PR review panel' },
    { command: '/screen', label: 'Screen Share', icon: '🖥️', description: 'Start screen sharing' },
];

interface InputComposerProps {
    value: string;
    onChange: (val: string) => void;
    onSubmit: (e: FormEvent) => void;
    onKeyDown: (e: React.KeyboardEvent) => void;
    onAttach: () => void;
    onSlashCommand?: (command: string) => void;
    pendingFiles: File[];
    onRemoveFile: (index: number) => void;
    isUploading: boolean;
    disabled?: boolean;
}

export function InputComposer({
    value,
    onChange,
    onSubmit,
    onKeyDown,
    onAttach,
    onSlashCommand,
    pendingFiles,
    onRemoveFile,
    isUploading,
    disabled,
}: InputComposerProps) {
    const [showSlash, setShowSlash] = useState(false);
    const [slashQuery, setSlashQuery] = useState('');
    const [slashIndex, setSlashIndex] = useState(0);
    const inputRef = useRef<HTMLTextAreaElement>(null);
    const slashRef = useRef<HTMLDivElement>(null);

    // Filter slash commands
    const filteredCommands = slashQuery
        ? SLASH_COMMANDS.filter(c =>
            c.command.includes(slashQuery.toLowerCase()) ||
            c.label.toLowerCase().includes(slashQuery.toLowerCase())
        )
        : SLASH_COMMANDS;

    // Detect /slash
    useEffect(() => {
        if (value.startsWith('/')) {
            setShowSlash(true);
            setSlashQuery(value);
            setSlashIndex(0);
        } else {
            setShowSlash(false);
            setSlashQuery('');
        }
    }, [value]);

    // Reset index on filter change
    useEffect(() => {
        setSlashIndex(0);
    }, [filteredCommands.length]);

    const executeSlash = useCallback((cmd: SlashCommand) => {
        setShowSlash(false);
        onChange('');
        if (onSlashCommand) {
            onSlashCommand(cmd.command);
        }
    }, [onChange, onSlashCommand]);

    const handleComposerKeyDown = useCallback((e: React.KeyboardEvent) => {
        if (showSlash && filteredCommands.length > 0) {
            if (e.key === 'ArrowDown') {
                e.preventDefault();
                setSlashIndex(i => Math.min(i + 1, filteredCommands.length - 1));
                return;
            }
            if (e.key === 'ArrowUp') {
                e.preventDefault();
                setSlashIndex(i => Math.max(i - 1, 0));
                return;
            }
            if (e.key === 'Enter' || e.key === 'Tab') {
                e.preventDefault();
                executeSlash(filteredCommands[slashIndex]);
                return;
            }
            if (e.key === 'Escape') {
                e.preventDefault();
                setShowSlash(false);
                return;
            }
        }
        // Default key handling (Enter to submit, etc.)
        onKeyDown(e);
    }, [showSlash, filteredCommands, slashIndex, executeSlash, onKeyDown]);

    // Auto-resize textarea
    useEffect(() => {
        if (inputRef.current) {
            inputRef.current.style.height = 'auto';
            inputRef.current.style.height = Math.min(inputRef.current.scrollHeight, 200) + 'px';
        }
    }, [value]);

    return (
        <div style={{ position: 'relative' }}>
            {/* Slash command popup */}
            {showSlash && filteredCommands.length > 0 && (
                <div
                    ref={slashRef}
                    style={{
                        position: 'absolute',
                        bottom: '100%',
                        left: '0',
                        right: '0',
                        marginBottom: '6px',
                        background: 'rgba(10, 10, 10, 0.96)',
                        border: '1px solid rgba(0, 243, 255, 0.2)',
                        borderRadius: '8px',
                        overflow: 'hidden',
                        boxShadow: '0 -8px 32px rgba(0, 0, 0, 0.6), 0 0 20px rgba(0, 243, 255, 0.04)',
                        animation: 'slash-in 0.12s ease-out',
                        maxHeight: '260px',
                        overflowY: 'auto',
                        zIndex: 100,
                    }}
                >
                    <div
                        style={{
                            padding: '6px 12px',
                            fontSize: '0.6rem',
                            color: 'var(--text-dim)',
                            fontFamily: 'var(--font-mono)',
                            letterSpacing: '0.1em',
                            borderBottom: '1px solid rgba(255,255,255,0.04)',
                        }}
                    >
                        COMMANDS
                    </div>
                    {filteredCommands.map((cmd, i) => (
                        <div
                            key={cmd.command}
                            onClick={() => executeSlash(cmd)}
                            onMouseEnter={() => setSlashIndex(i)}
                            style={{
                                display: 'flex',
                                alignItems: 'center',
                                gap: '10px',
                                padding: '8px 12px',
                                cursor: 'pointer',
                                background: i === slashIndex ? 'rgba(0, 243, 255, 0.08)' : 'transparent',
                                borderLeft: i === slashIndex ? '2px solid var(--accent-cyan)' : '2px solid transparent',
                                transition: 'all 0.1s',
                            }}
                        >
                            <span style={{ fontSize: '1rem', width: '24px', textAlign: 'center' }}>
                                {cmd.icon}
                            </span>
                            <div style={{ flex: 1 }}>
                                <div
                                    style={{
                                        fontFamily: 'var(--font-mono)',
                                        fontSize: '0.8rem',
                                        color: i === slashIndex ? 'var(--accent-cyan)' : 'var(--text-primary)',
                                    }}
                                >
                                    {cmd.command}
                                </div>
                                <div
                                    style={{
                                        fontFamily: 'var(--font-mono)',
                                        fontSize: '0.65rem',
                                        color: 'var(--text-dim)',
                                    }}
                                >
                                    {cmd.description}
                                </div>
                            </div>
                        </div>
                    ))}
                </div>
            )}

            <form
                onSubmit={onSubmit}
                style={{
                    display: 'flex',
                    flexDirection: 'column',
                    alignItems: 'stretch',
                    gap: '10px',
                }}
            >
                <div
                    style={{
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
                            transition: 'border-color 0.2s, box-shadow 0.2s',
                        }}
                        onFocus={(e) => {
                            e.currentTarget.style.borderColor = 'var(--accent-cyan)';
                            e.currentTarget.style.boxShadow = '0 0 12px rgba(0, 243, 255, 0.1)';
                        }}
                        onBlur={(e) => {
                            e.currentTarget.style.borderColor = 'var(--text-dim)';
                            e.currentTarget.style.boxShadow = 'none';
                        }}
                        tabIndex={-1}
                        onDragOver={(e) => {
                            e.preventDefault();
                            e.currentTarget.style.borderColor = 'var(--accent-purple)';
                        }}
                        onDragLeave={(e) => {
                            e.currentTarget.style.borderColor = 'var(--text-dim)';
                        }}
                        onDrop={(e) => {
                            e.preventDefault();
                            e.currentTarget.style.borderColor = 'var(--text-dim)';
                            // Parent handles files via the hidden input
                        }}
                    >
                        {/* Pending file chips */}
                        {pendingFiles.length > 0 && (
                            <div
                                style={{
                                    display: 'flex',
                                    flexWrap: 'wrap',
                                    gap: '6px',
                                    marginBottom: '8px',
                                }}
                            >
                                {pendingFiles.map((f, i) => (
                                    <div
                                        key={i}
                                        style={{
                                            display: 'flex',
                                            alignItems: 'center',
                                            gap: '4px',
                                            background: 'rgba(0,243,255,0.1)',
                                            border: '1px solid var(--accent-cyan)',
                                            borderRadius: '4px',
                                            padding: '4px 8px',
                                            fontSize: '0.75rem',
                                            color: 'var(--accent-cyan)',
                                            fontFamily: 'var(--font-mono)',
                                        }}
                                    >
                                        <span>{f.type.startsWith('image/') ? '🖼️' : '📄'}</span>
                                        <span
                                            style={{
                                                maxWidth: '120px',
                                                overflow: 'hidden',
                                                textOverflow: 'ellipsis',
                                                whiteSpace: 'nowrap',
                                            }}
                                        >
                                            {f.name}
                                        </span>
                                        <span
                                            style={{ color: 'var(--text-dim)', fontSize: '0.65rem' }}
                                        >
                                            {(f.size / 1024).toFixed(0)}KB
                                        </span>
                                        <button
                                            type="button"
                                            onClick={() => onRemoveFile(i)}
                                            style={{
                                                background: 'none',
                                                border: 'none',
                                                color: 'var(--text-dim)',
                                                cursor: 'pointer',
                                                padding: '0 2px',
                                                fontSize: '0.8rem',
                                            }}
                                        >
                                            ✕
                                        </button>
                                    </div>
                                ))}
                            </div>
                        )}
                        <textarea
                            ref={inputRef}
                            value={value}
                            onChange={(e) => onChange(e.target.value)}
                            onKeyDown={handleComposerKeyDown}
                            placeholder="Type a message or / for commands..."
                            rows={1}
                            disabled={disabled}
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
                    {/* Attach button */}
                    <button
                        type="button"
                        onClick={onAttach}
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
                    >
                        📎
                    </button>
                    {/* Submit */}
                    <button
                        type="submit"
                        disabled={isUploading}
                        style={{
                            background:
                                value.trim() || pendingFiles.length > 0
                                    ? isUploading
                                        ? 'var(--accent-purple)'
                                        : 'var(--accent-cyan)'
                                    : 'transparent',
                            color:
                                value.trim() || pendingFiles.length > 0
                                    ? '#000'
                                    : 'var(--text-dim)',
                            border: '1px solid var(--accent-cyan)',
                            padding: '10px 20px',
                            borderRadius: '4px',
                            cursor: isUploading ? 'not-allowed' : 'pointer',
                            fontWeight: 'bold',
                            fontFamily: 'var(--font-mono)',
                            height: '46px',
                            opacity: isUploading ? 0.7 : 1,
                        }}
                    >
                        {isUploading ? 'UPLOADING...' : 'SEND'}
                    </button>
                </div>
                {/* Slash hint */}
                <div
                    style={{
                        display: 'flex',
                        justifyContent: 'space-between',
                        fontFamily: 'var(--font-mono)',
                        fontSize: '0.6rem',
                        color: 'var(--text-dim)',
                        padding: '0 4px',
                    }}
                >
                    <span>
                        <kbd style={{ border: '1px solid var(--border-color)', padding: '1px 4px', borderRadius: '2px' }}>/</kbd>
                        {' '}commands
                        {'  ·  '}
                        <kbd style={{ border: '1px solid var(--border-color)', padding: '1px 4px', borderRadius: '2px' }}>⌘K</kbd>
                        {' '}palette
                    </span>
                    <span>
                        <kbd style={{ border: '1px solid var(--border-color)', padding: '1px 4px', borderRadius: '2px' }}>Shift+Enter</kbd>
                        {' '}new line
                    </span>
                </div>
            </form>

            <style>{`
                @keyframes slash-in {
                    from { opacity: 0; transform: translateY(4px); }
                    to { opacity: 1; transform: translateY(0); }
                }
            `}</style>
        </div>
    );
}
