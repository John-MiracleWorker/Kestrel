import { useState, useEffect } from 'react';
import { workspaces, conversations, type Workspace, type Conversation } from '../../api/client';
import { ProcessesPanel } from './ProcessesPanel';

interface SidebarProps {
    currentWorkspace: Workspace | null;
    currentConversation: Conversation | null;
    onSelectWorkspace: (ws: Workspace) => void;
    onSelectConversation: (conv: Conversation) => void;
    onNewConversation: () => void;
    onOpenSettings: () => void;
    onOpenMoltbook: () => void;
    onOpenMemoryPalace: () => void;
    onOpenDocs: () => void;
    onOpenScreenShare: () => void;
    onOpenPRReview: () => void;
    onLogout: () => void;
}

export function Sidebar({
    currentWorkspace,
    currentConversation,
    onSelectWorkspace,
    onSelectConversation,
    onNewConversation,
    onOpenSettings,
    onOpenMoltbook,
    onOpenMemoryPalace,
    onOpenDocs,
    onOpenScreenShare,
    onOpenPRReview,
    onLogout,
}: SidebarProps) {
    const [workspaceList, setWorkspaceList] = useState<Workspace[]>([]);
    const [conversationList, setConversationList] = useState<Conversation[]>([]);
    const [showWorkspaceSwitcher, setShowWorkspaceSwitcher] = useState(false);
    const [searchQuery, setSearchQuery] = useState('');
    const [editingConversationId, setEditingConversationId] = useState<string | null>(null);
    const [editTitle, setEditTitle] = useState('');
    const [deleteConfirmationId, setDeleteConfirmationId] = useState<string | null>(null);
    const [showProcesses, setShowProcesses] = useState(false);

    // Load workspaces
    useEffect(() => {
        workspaces.list()
            .then((res) => {
                const list = res.workspaces || [];
                setWorkspaceList(list);

                // Auto-select if none selected OR if current selection is invalid
                const currentIsValid = currentWorkspace && list.find(w => w.id === currentWorkspace.id);

                if ((!currentWorkspace || !currentIsValid) && list.length > 0) {
                    onSelectWorkspace(list[0]);
                }
            })
            .catch(console.error);
    }, [currentWorkspace]);

    // Load conversations when workspace changes
    useEffect(() => {
        if (!currentWorkspace) return;
        conversations.list(currentWorkspace.id)
            .then((res) => setConversationList(res.conversations || []))
            .catch(console.error);
    }, [currentWorkspace]);

    // Listen for title updates
    useEffect(() => {
        const handleTitleChange = (e: Event) => {
            const customEvent = e as CustomEvent;
            setConversationList(prev => prev.map(c =>
                c.id === customEvent.detail.conversationId
                    ? { ...c, title: customEvent.detail.newTitle }
                    : c
            ));
        };
        window.addEventListener('conversation-title-changed', handleTitleChange);
        return () => window.removeEventListener('conversation-title-changed', handleTitleChange);
    }, []);

    const handleDeleteConversation = (e: React.MouseEvent, conversationId: string) => {
        e.stopPropagation();
        setDeleteConfirmationId(conversationId);
    };

    const confirmDelete = async () => {
        if (!currentWorkspace || !deleteConfirmationId) return;

        try {
            await conversations.delete(currentWorkspace.id, deleteConfirmationId);
            setConversationList(prev => prev.filter(c => c.id !== deleteConfirmationId));
            if (currentConversation?.id === deleteConfirmationId) {
                onNewConversation();
            }
        } catch (err) {
            console.error('Failed to delete conversation', err);
        } finally {
            setDeleteConfirmationId(null);
        }
    };

    const startEditing = (e: React.MouseEvent, conv: Conversation) => {
        e.stopPropagation();
        setEditingConversationId(conv.id);
        setEditTitle(conv.title || 'New conversation');
    };

    const saveTitle = async () => {
        if (!currentWorkspace || !editingConversationId) return;
        try {
            await conversations.rename(currentWorkspace.id, editingConversationId, editTitle);
            setConversationList(prev => prev.map(c =>
                c.id === editingConversationId ? { ...c, title: editTitle } : c
            ));
            setEditingConversationId(null);
        } catch (err) {
            console.error('Failed to rename conversation', err);
        }
    };

    const handleKeyDown = (e: React.KeyboardEvent) => {
        if (e.key === 'Enter') saveTitle();
        if (e.key === 'Escape') setEditingConversationId(null);
    };

    const filteredConversations = conversationList.filter(
        (c) => !searchQuery || c.title.toLowerCase().includes(searchQuery.toLowerCase())
    );

    return (
        <>
            <aside style={{
                display: 'flex',
                flexDirection: 'column',
                width: '280px',
                backgroundColor: 'var(--bg-panel)',
                borderRight: '1px solid var(--border-color)',
                height: '100%',
                fontFamily: 'var(--font-mono)',
                backgroundImage: 'linear-gradient(180deg, rgba(0, 243, 255, 0.02) 0%, transparent 30%)',
            }}>
                {/* Workspace header */}
                <div style={{
                    padding: '16px',
                    borderBottom: '1px solid var(--border-color)',
                    background: 'linear-gradient(180deg, rgba(0, 243, 255, 0.04), var(--bg-panel))',
                }}>
                    <div style={{ fontSize: '0.75rem', color: 'var(--text-secondary)', marginBottom: '4px' }}>CURRENT_WORKSPACE</div>
                    <button
                        className="terminal-border"
                        style={{
                            width: '100%',
                            display: 'flex',
                            alignItems: 'center',
                            justifyContent: 'space-between',
                            padding: '10px 12px',
                            background: 'var(--bg-surface)',
                            color: 'var(--accent-cyan)',
                            cursor: 'pointer',
                            borderRadius: 'var(--radius-md)',
                            transition: 'all 0.3s',
                            boxShadow: '0 0 0 0 rgba(0, 243, 255, 0)',
                        }}
                        onClick={() => setShowWorkspaceSwitcher(!showWorkspaceSwitcher)}
                        onMouseEnter={(e) => {
                            e.currentTarget.style.boxShadow = '0 0 15px rgba(0, 243, 255, 0.1)';
                            e.currentTarget.style.borderColor = 'rgba(0, 243, 255, 0.4)';
                        }}
                        onMouseLeave={(e) => {
                            e.currentTarget.style.boxShadow = '0 0 0 0 rgba(0, 243, 255, 0)';
                            e.currentTarget.style.borderColor = '';
                        }}
                    >
                        <span style={{ fontWeight: 600, textShadow: '0 0 10px rgba(0, 243, 255, 0.3)' }}>
                            ~/{currentWorkspace?.name || 'Start'}
                        </span>
                        <span style={{ fontSize: '0.8em', transition: 'transform 0.2s', transform: showWorkspaceSwitcher ? 'rotate(180deg)' : 'none' }}>▼</span>
                    </button>

                    {/* Workspace dropdown */}
                    {showWorkspaceSwitcher && (
                        <div style={{
                            position: 'absolute',
                            left: '16px',
                            width: '248px',
                            zIndex: 50,
                            marginTop: '4px',
                            background: 'rgba(10, 10, 10, 0.95)',
                            backdropFilter: 'blur(16px)',
                            border: '1px solid rgba(0, 243, 255, 0.3)',
                            borderRadius: 'var(--radius-md)',
                            boxShadow: '0 8px 32px rgba(0,0,0,0.6), 0 0 20px rgba(0, 243, 255, 0.08)',
                            animation: 'fadeIn 0.2s ease-out',
                        }}>
                            {workspaceList.map((ws) => (
                                <button
                                    key={ws.id}
                                    style={{
                                        width: '100%',
                                        textAlign: 'left',
                                        padding: '8px 12px',
                                        background: ws.id === currentWorkspace?.id ? 'var(--bg-highlight)' : 'transparent',
                                        color: ws.id === currentWorkspace?.id ? 'var(--accent-cyan)' : 'var(--text-primary)',
                                        borderBottom: '1px solid var(--border-color)',
                                        cursor: 'pointer',
                                    }}
                                    onClick={() => {
                                        onSelectWorkspace(ws);
                                        setShowWorkspaceSwitcher(false);
                                    }}
                                >
                                    {ws.name}
                                </button>
                            ))}
                        </div>
                    )}
                </div>

                {/* New conversation "Command" */}
                <div style={{ padding: '16px 16px 8px' }}>
                    <button
                        style={{
                            width: '100%',
                            padding: '10px 14px',
                            background: 'rgba(0, 255, 157, 0.03)',
                            border: '1px solid rgba(0, 255, 157, 0.15)',
                            color: 'var(--text-primary)',
                            display: 'flex',
                            alignItems: 'center',
                            justifyContent: 'center',
                            gap: '8px',
                            transition: 'all 0.3s',
                            cursor: 'pointer',
                            borderRadius: 'var(--radius-md)',
                        }}
                        onMouseEnter={(e) => {
                            e.currentTarget.style.borderColor = 'rgba(0, 255, 157, 0.4)';
                            e.currentTarget.style.color = 'var(--accent-green)';
                            e.currentTarget.style.background = 'rgba(0, 255, 157, 0.06)';
                            e.currentTarget.style.boxShadow = '0 0 15px rgba(0, 255, 157, 0.08)';
                        }}
                        onMouseLeave={(e) => {
                            e.currentTarget.style.borderColor = 'rgba(0, 255, 157, 0.15)';
                            e.currentTarget.style.color = 'var(--text-primary)';
                            e.currentTarget.style.background = 'rgba(0, 255, 157, 0.03)';
                            e.currentTarget.style.boxShadow = 'none';
                        }}
                        onClick={onNewConversation}
                    >
                        <span style={{ color: 'var(--accent-green)' }}>+</span> NEW SESSION
                    </button>
                </div>

                {/* Search */}
                <div style={{ padding: '0 16px 16px' }}>
                    <div style={{
                        display: 'flex',
                        alignItems: 'center',
                        borderBottom: '1px solid var(--border-color)',
                        paddingBottom: '4px'
                    }}>
                        <span style={{ color: 'var(--accent-purple)', marginRight: '8px' }}>find</span>
                        <input
                            type="text"
                            placeholder='-name "*query*"'
                            value={searchQuery}
                            onChange={(e) => setSearchQuery(e.target.value)}
                            style={{
                                background: 'transparent',
                                border: 'none',
                                color: 'var(--text-primary)',
                                outline: 'none',
                                width: '100%',
                                fontFamily: 'var(--font-mono)',
                                fontSize: '0.875rem'
                            }}
                        />
                    </div>
                </div>

                {/* Conversation list as "Process List" */}
                <div style={{
                    flex: 1,
                    overflowY: 'auto',
                    padding: '0 8px',
                }}>
                    <div style={{ padding: '0 8px 8px', fontSize: '0.7rem', color: 'var(--text-dim)', textTransform: 'uppercase' }}>
                        Active Processes
                    </div>
                    {filteredConversations.map((conv) => (
                        <div
                            key={conv.id}
                            style={{
                                display: 'flex',
                                alignItems: 'center',
                                padding: '8px 10px',
                                marginBottom: '2px',
                                cursor: 'pointer',
                                background: conv.id === currentConversation?.id ? 'rgba(0, 243, 255, 0.08)' : 'transparent',
                                borderLeft: conv.id === currentConversation?.id ? '2px solid var(--accent-cyan)' : '2px solid transparent',
                                borderRadius: '0 var(--radius-sm) var(--radius-sm) 0',
                                color: conv.id === currentConversation?.id ? 'var(--accent-cyan)' : 'var(--text-secondary)',
                                fontSize: '0.82rem',
                                transition: 'all 0.2s',
                                ...(conv.id === currentConversation?.id ? { boxShadow: 'inset 0 0 20px rgba(0, 243, 255, 0.03)' } : {}),
                            }}
                            onClick={() => onSelectConversation(conv)}
                            onMouseEnter={(e) => {
                                if (conv.id !== currentConversation?.id) {
                                    e.currentTarget.style.background = 'rgba(255, 255, 255, 0.03)';
                                    e.currentTarget.style.color = 'var(--text-primary)';
                                }
                            }}
                            onMouseLeave={(e) => {
                                if (conv.id !== currentConversation?.id) {
                                    e.currentTarget.style.background = 'transparent';
                                    e.currentTarget.style.color = 'var(--text-secondary)';
                                }
                            }}
                            className="group"
                        >
                            <span style={{ marginRight: '8px', opacity: 0.7 }}>
                                {conv.id === currentConversation?.id ? '>' : '#'}
                            </span>

                            {editingConversationId === conv.id ? (
                                <input
                                    autoFocus
                                    value={editTitle}
                                    onChange={(e) => setEditTitle(e.target.value)}
                                    onBlur={saveTitle}
                                    onKeyDown={handleKeyDown}
                                    onClick={(e) => e.stopPropagation()}
                                    style={{
                                        background: 'var(--bg-terminal)',
                                        border: 'none',
                                        color: 'var(--accent-cyan)',
                                        outline: 'none',
                                        fontFamily: 'var(--font-mono)',
                                        width: '100%'
                                    }}
                                />
                            ) : (
                                <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', display: 'flex', alignItems: 'center', gap: '6px' }}>
                                    {conv.title || 'untitled_process'}
                                    {conv.channel && conv.channel !== 'web' && (
                                        <span style={{
                                            fontSize: '0.65rem',
                                            padding: '1px 5px',
                                            borderRadius: '3px',
                                            background: conv.channel === 'telegram'
                                                ? 'rgba(0, 136, 204, 0.15)'
                                                : 'rgba(88, 101, 242, 0.15)',
                                            color: conv.channel === 'telegram'
                                                ? '#0088cc'
                                                : '#5865F2',
                                            border: `1px solid ${conv.channel === 'telegram'
                                                ? 'rgba(0, 136, 204, 0.25)'
                                                : 'rgba(88, 101, 242, 0.25)'}`,
                                            flexShrink: 0,
                                        }}>
                                            {conv.channel === 'telegram' ? '✈️' : conv.channel === 'discord' ? '💬' : conv.channel}
                                        </span>
                                    )}
                                </span>
                            )}

                            {/* Hover Actions */}
                            <div
                                style={{
                                    display: 'none',
                                    gap: '6px'
                                }}
                                className="group-hover:flex"
                            >
                                <button onClick={(e) => startEditing(e, conv)} style={{ color: 'var(--text-secondary)' }}>rn</button>
                                <button onClick={(e) => handleDeleteConversation(e, conv.id)} style={{ color: 'var(--accent-error)' }}>rm</button>
                            </div>
                        </div>
                    ))}
                </div>

                {/* Footer status bar style */}
                <div style={{
                    padding: '10px 12px',
                    borderTop: '1px solid var(--border-color)',
                    background: 'linear-gradient(180deg, var(--bg-panel), rgba(0, 243, 255, 0.02))',
                    fontSize: '0.72rem',
                    display: 'flex',
                    justifyContent: 'space-between',
                    alignItems: 'center',
                    color: 'var(--text-dim)',
                    gap: '4px',
                }}>
                    <button
                        onClick={onOpenSettings}
                        style={{
                            display: 'flex', alignItems: 'center', gap: '6px', cursor: 'pointer',
                            padding: '6px 10px', borderRadius: 'var(--radius-sm)',
                            transition: 'all 0.2s', fontSize: '0.72rem',
                        }}
                        onMouseEnter={e => {
                            e.currentTarget.style.background = 'rgba(0, 255, 157, 0.08)';
                            e.currentTarget.style.color = 'var(--accent-green)';
                        }}
                        onMouseLeave={e => {
                            e.currentTarget.style.background = 'transparent';
                            e.currentTarget.style.color = '';
                        }}
                    >
                        <span style={{ color: 'var(--accent-green)', fontSize: '0.5rem' }}>●</span> CONFIG
                    </button>
                    <div style={{ display: 'flex', gap: '2px' }}>
                        <button
                            onClick={() => setShowProcesses(true)}
                            title="Background Processes"
                            style={{
                                cursor: 'pointer', fontSize: '1rem',
                                background: 'transparent', border: 'none',
                                padding: '4px 8px', borderRadius: 'var(--radius-sm)',
                                transition: 'all 0.2s',
                            }}
                            onMouseEnter={e => {
                                e.currentTarget.style.background = 'rgba(0, 243, 255, 0.1)';
                                e.currentTarget.style.boxShadow = '0 0 10px rgba(0, 243, 255, 0.1)';
                            }}
                            onMouseLeave={e => {
                                e.currentTarget.style.background = 'transparent';
                                e.currentTarget.style.boxShadow = 'none';
                            }}
                        >⚙</button>
                        <button
                            onClick={onOpenMoltbook}
                            title="Moltbook Activity"
                            style={{
                                cursor: 'pointer', fontSize: '1rem',
                                background: 'transparent', border: 'none',
                                padding: '4px 8px', borderRadius: 'var(--radius-sm)',
                                transition: 'all 0.2s',
                            }}
                            onMouseEnter={e => {
                                e.currentTarget.style.background = 'rgba(189, 0, 255, 0.1)';
                                e.currentTarget.style.boxShadow = '0 0 10px rgba(189, 0, 255, 0.1)';
                            }}
                            onMouseLeave={e => {
                                e.currentTarget.style.background = 'transparent';
                                e.currentTarget.style.boxShadow = 'none';
                            }}
                        >🦞</button>
                        <button
                            onClick={onOpenMemoryPalace}
                            title="Memory Palace"
                            style={{
                                cursor: 'pointer', fontSize: '1rem',
                                background: 'transparent', border: 'none',
                                padding: '4px 8px', borderRadius: 'var(--radius-sm)',
                                transition: 'all 0.2s',
                            }}
                            onMouseEnter={e => {
                                e.currentTarget.style.background = 'rgba(168, 85, 247, 0.1)';
                                e.currentTarget.style.boxShadow = '0 0 10px rgba(168, 85, 247, 0.1)';
                            }}
                            onMouseLeave={e => {
                                e.currentTarget.style.background = 'transparent';
                                e.currentTarget.style.boxShadow = 'none';
                            }}
                        >🧠</button>
                        <button
                            onClick={onOpenDocs}
                            title="Auto-Documentation"
                            style={{
                                cursor: 'pointer', fontSize: '1rem',
                                background: 'transparent', border: 'none',
                                padding: '4px 8px', borderRadius: 'var(--radius-sm)',
                                transition: 'all 0.2s',
                            }}
                            onMouseEnter={e => {
                                e.currentTarget.style.background = 'rgba(16, 185, 129, 0.1)';
                                e.currentTarget.style.boxShadow = '0 0 10px rgba(16, 185, 129, 0.1)';
                            }}
                            onMouseLeave={e => {
                                e.currentTarget.style.background = 'transparent';
                                e.currentTarget.style.boxShadow = 'none';
                            }}
                        >📖</button>
                        <button
                            onClick={onOpenScreenShare}
                            title="Live Screen-Share Agent"
                            style={{
                                cursor: 'pointer', fontSize: '1rem',
                                background: 'transparent', border: 'none',
                                padding: '4px 8px', borderRadius: 'var(--radius-sm)',
                                transition: 'all 0.2s',
                            }}
                            onMouseEnter={e => {
                                e.currentTarget.style.background = 'rgba(245, 158, 11, 0.1)';
                                e.currentTarget.style.boxShadow = '0 0 10px rgba(245, 158, 11, 0.1)';
                            }}
                            onMouseLeave={e => {
                                e.currentTarget.style.background = 'transparent';
                                e.currentTarget.style.boxShadow = 'none';
                            }}
                        >📺</button>
                        <button
                            onClick={onOpenPRReview}
                            title="PR Reviews"
                            style={{
                                cursor: 'pointer', fontSize: '1rem',
                                background: 'transparent', border: 'none',
                                padding: '4px 8px', borderRadius: 'var(--radius-sm)',
                                transition: 'all 0.2s',
                            }}
                            onMouseEnter={e => {
                                e.currentTarget.style.background = 'rgba(59, 130, 246, 0.1)';
                                e.currentTarget.style.boxShadow = '0 0 10px rgba(59, 130, 246, 0.1)';
                            }}
                            onMouseLeave={e => {
                                e.currentTarget.style.background = 'transparent';
                                e.currentTarget.style.boxShadow = 'none';
                            }}
                        >🔍</button>
                    </div>
                    <button
                        onClick={onLogout}
                        style={{
                            cursor: 'pointer', fontSize: '0.65rem', padding: '4px 8px',
                            borderRadius: 'var(--radius-sm)', transition: 'all 0.2s',
                            color: 'var(--text-dim)', letterSpacing: '0.05em',
                        }}
                        onMouseEnter={e => {
                            e.currentTarget.style.color = 'var(--accent-error)';
                            e.currentTarget.style.background = 'rgba(255, 0, 85, 0.06)';
                        }}
                        onMouseLeave={e => {
                            e.currentTarget.style.color = 'var(--text-dim)';
                            e.currentTarget.style.background = 'transparent';
                        }}
                    >EXIT</button>
                </div>
            </aside>

            {/* Delete Confirmation Modal */}
            {deleteConfirmationId && (
                <div
                    style={{
                        position: 'fixed',
                        inset: 0,
                        zIndex: 50,
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'center',
                        background: 'rgba(0, 0, 0, 0.7)',
                        backdropFilter: 'blur(12px)',
                    }}
                    onClick={() => setDeleteConfirmationId(null)}
                >
                    <div
                        className="card animate-fade-in"
                        style={{ maxWidth: 400, width: '100%', margin: '0 var(--space-4)', padding: 'var(--space-6)' }}
                        onClick={e => e.stopPropagation()}
                    >
                        <h3 style={{ fontSize: '1.0625rem', fontWeight: 700, marginBottom: 'var(--space-2)' }}>
                            Delete Conversation
                        </h3>
                        <p style={{ color: 'var(--color-text-secondary)', marginBottom: 'var(--space-6)', fontSize: '0.875rem' }}>
                            Are you sure you want to delete this conversation? This action cannot be undone.
                        </p>
                        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 'var(--space-3)' }}>
                            <button
                                className="btn btn-ghost"
                                onClick={() => setDeleteConfirmationId(null)}
                            >
                                Cancel
                            </button>
                            <button
                                className="btn btn-danger"
                                onClick={confirmDelete}
                            >
                                Delete
                            </button>
                        </div>
                    </div>
                </div>
            )}

            {/* Processes Panel */}
            {showProcesses && currentWorkspace && (
                <ProcessesPanel
                    workspaceId={currentWorkspace.id}
                    onClose={() => setShowProcesses(false)}
                />
            )}

        </>
    );
}
