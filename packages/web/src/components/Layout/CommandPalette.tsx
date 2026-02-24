/**
 * CommandPalette – ⌘K / Ctrl+K spotlight overlay.
 * Fuzzy search across conversations, panels, and actions.
 */
import { useState, useEffect, useRef, useCallback, useMemo } from 'react';

interface CommandItem {
    id: string;
    label: string;
    category: string;
    icon: string;
    shortcut?: string;
    action: () => void;
}

interface CommandPaletteProps {
    isOpen: boolean;
    onClose: () => void;
    actions: CommandItem[];
}

function fuzzyMatch(text: string, query: string): boolean {
    const lower = text.toLowerCase();
    const q = query.toLowerCase();
    let qi = 0;
    for (let i = 0; i < lower.length && qi < q.length; i++) {
        if (lower[i] === q[qi]) qi++;
    }
    return qi === q.length;
}

export function CommandPalette({ isOpen, onClose, actions }: CommandPaletteProps) {
    const [query, setQuery] = useState('');
    const [selectedIndex, setSelectedIndex] = useState(0);
    const inputRef = useRef<HTMLInputElement>(null);

    const filtered = useMemo(() => {
        if (!query.trim()) return actions;
        return actions.filter(a => fuzzyMatch(a.label, query) || fuzzyMatch(a.category, query));
    }, [query, actions]);

    // Reset on open
    useEffect(() => {
        if (isOpen) {
            setQuery('');
            setSelectedIndex(0);
            setTimeout(() => inputRef.current?.focus(), 50);
        }
    }, [isOpen]);

    // Reset selection when filter changes
    useEffect(() => {
        setSelectedIndex(0);
    }, [filtered.length]);

    const executeItem = useCallback((item: CommandItem) => {
        item.action();
        onClose();
    }, [onClose]);

    const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
        switch (e.key) {
            case 'ArrowDown':
                e.preventDefault();
                setSelectedIndex(i => Math.min(i + 1, filtered.length - 1));
                break;
            case 'ArrowUp':
                e.preventDefault();
                setSelectedIndex(i => Math.max(i - 1, 0));
                break;
            case 'Enter':
                e.preventDefault();
                if (filtered[selectedIndex]) {
                    executeItem(filtered[selectedIndex]);
                }
                break;
            case 'Escape':
                e.preventDefault();
                onClose();
                break;
        }
    }, [filtered, selectedIndex, executeItem, onClose]);

    // Global shortcut listener
    useEffect(() => {
        function handleGlobal(e: KeyboardEvent) {
            if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
                e.preventDefault();
                if (isOpen) onClose();
                // Opening is handled by parent
            }
            if (e.key === 'Escape' && isOpen) {
                e.preventDefault();
                onClose();
            }
        }
        window.addEventListener('keydown', handleGlobal);
        return () => window.removeEventListener('keydown', handleGlobal);
    }, [isOpen, onClose]);

    if (!isOpen) return null;

    // Group by category
    const categories = [...new Set(filtered.map(a => a.category))];

    return (
        <div
            style={{
                position: 'fixed',
                inset: 0,
                zIndex: 9999,
                display: 'flex',
                alignItems: 'flex-start',
                justifyContent: 'center',
                paddingTop: '15vh',
                background: 'rgba(0, 0, 0, 0.6)',
                backdropFilter: 'blur(8px)',
            }}
            onClick={onClose}
        >
            <div
                style={{
                    width: '560px',
                    maxHeight: '450px',
                    background: 'rgba(10, 10, 10, 0.96)',
                    border: '1px solid rgba(0, 243, 255, 0.2)',
                    borderRadius: '12px',
                    overflow: 'hidden',
                    boxShadow: '0 24px 80px rgba(0, 0, 0, 0.8), 0 0 40px rgba(0, 243, 255, 0.06)',
                    animation: 'palette-in 0.15s ease-out',
                }}
                onClick={e => e.stopPropagation()}
            >
                {/* Search input */}
                <div
                    style={{
                        display: 'flex',
                        alignItems: 'center',
                        padding: '14px 18px',
                        borderBottom: '1px solid rgba(255,255,255,0.06)',
                        gap: '10px',
                    }}
                >
                    <span style={{ color: 'var(--accent-cyan)', fontSize: '1.1rem' }}>⌘</span>
                    <input
                        ref={inputRef}
                        value={query}
                        onChange={e => setQuery(e.target.value)}
                        onKeyDown={handleKeyDown}
                        placeholder="Type a command..."
                        style={{
                            flex: 1,
                            background: 'transparent',
                            border: 'none',
                            outline: 'none',
                            color: 'var(--text-primary)',
                            fontFamily: 'var(--font-mono)',
                            fontSize: '0.95rem',
                        }}
                    />
                    <kbd
                        style={{
                            fontSize: '0.65rem',
                            color: 'var(--text-dim)',
                            border: '1px solid var(--border-color)',
                            padding: '2px 6px',
                            borderRadius: '4px',
                            fontFamily: 'var(--font-mono)',
                        }}
                    >
                        ESC
                    </kbd>
                </div>

                {/* Results */}
                <div
                    style={{
                        maxHeight: '360px',
                        overflowY: 'auto',
                        padding: '6px 0',
                    }}
                >
                    {filtered.length === 0 && (
                        <div
                            style={{
                                padding: '24px',
                                textAlign: 'center',
                                color: 'var(--text-dim)',
                                fontFamily: 'var(--font-mono)',
                                fontSize: '0.8rem',
                            }}
                        >
                            No results for "{query}"
                        </div>
                    )}

                    {categories.map(cat => (
                        <div key={cat}>
                            <div
                                style={{
                                    padding: '8px 18px 4px',
                                    fontSize: '0.65rem',
                                    color: 'var(--text-dim)',
                                    fontFamily: 'var(--font-mono)',
                                    letterSpacing: '0.1em',
                                    textTransform: 'uppercase',
                                }}
                            >
                                {cat}
                            </div>
                            {filtered
                                .filter(a => a.category === cat)
                                .map(item => {
                                    const globalIdx = filtered.indexOf(item);
                                    const isSelected = globalIdx === selectedIndex;
                                    return (
                                        <div
                                            key={item.id}
                                            onClick={() => executeItem(item)}
                                            onMouseEnter={() => setSelectedIndex(globalIdx)}
                                            style={{
                                                display: 'flex',
                                                alignItems: 'center',
                                                gap: '10px',
                                                padding: '10px 18px',
                                                cursor: 'pointer',
                                                background: isSelected ? 'rgba(0, 243, 255, 0.08)' : 'transparent',
                                                borderLeft: isSelected ? '2px solid var(--accent-cyan)' : '2px solid transparent',
                                                transition: 'all 0.1s',
                                            }}
                                        >
                                            <span style={{ fontSize: '1.1rem', width: '24px', textAlign: 'center' }}>
                                                {item.icon}
                                            </span>
                                            <span
                                                style={{
                                                    flex: 1,
                                                    fontFamily: 'var(--font-mono)',
                                                    fontSize: '0.85rem',
                                                    color: isSelected ? 'var(--accent-cyan)' : 'var(--text-primary)',
                                                }}
                                            >
                                                {item.label}
                                            </span>
                                            {item.shortcut && (
                                                <kbd
                                                    style={{
                                                        fontSize: '0.6rem',
                                                        color: 'var(--text-dim)',
                                                        border: '1px solid var(--border-color)',
                                                        padding: '2px 6px',
                                                        borderRadius: '3px',
                                                        fontFamily: 'var(--font-mono)',
                                                    }}
                                                >
                                                    {item.shortcut}
                                                </kbd>
                                            )}
                                        </div>
                                    );
                                })}
                        </div>
                    ))}
                </div>
            </div>

            <style>{`
                @keyframes palette-in {
                    from { opacity: 0; transform: translateY(-12px) scale(0.98); }
                    to { opacity: 1; transform: translateY(0) scale(1); }
                }
            `}</style>
        </div>
    );
}
