import { useState, useEffect } from 'react';
import { workspaces, conversations, type Workspace, type Conversation } from '../../api/client';

interface SidebarProps {
    currentWorkspace: Workspace | null;
    currentConversation: Conversation | null;
    onSelectWorkspace: (ws: Workspace) => void;
    onSelectConversation: (conv: Conversation) => void;
    onNewConversation: () => void;
    onOpenSettings: () => void;
    onLogout: () => void;
}

export function Sidebar({
    currentWorkspace,
    currentConversation,
    onSelectWorkspace,
    onSelectConversation,
    onNewConversation,
    onOpenSettings,
    onLogout,
}: SidebarProps) {
    const [workspaceList, setWorkspaceList] = useState<Workspace[]>([]);
    const [conversationList, setConversationList] = useState<Conversation[]>([]);
    const [showWorkspaceSwitcher, setShowWorkspaceSwitcher] = useState(false);
    const [searchQuery, setSearchQuery] = useState('');
    const [editingConversationId, setEditingConversationId] = useState<string | null>(null);
    const [editTitle, setEditTitle] = useState('');
    const [deleteConfirmationId, setDeleteConfirmationId] = useState<string | null>(null);

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
            <aside className="card" style={{
                display: 'flex',
                flexDirection: 'column',
                width: '260px',
                backgroundColor: 'var(--color-bg-secondary)',
                borderRight: '1px solid var(--color-border)',
                height: '100%',
            }}>
                {/* Workspace header */}
                <div style={{
                    padding: 'var(--space-4)',
                    borderBottom: '1px solid var(--color-border)',
                }}>
                    <button
                        className="btn btn-ghost"
                        style={{
                            width: '100%',
                            justifyContent: 'space-between',
                            padding: 'var(--space-2) var(--space-3)',
                        }}
                        onClick={() => setShowWorkspaceSwitcher(!showWorkspaceSwitcher)}
                    >
                        <span style={{
                            display: 'flex',
                            alignItems: 'center',
                            gap: 'var(--space-2)',
                            fontWeight: 600,
                        }}>
                            <span style={{
                                width: 28,
                                height: 28,
                                background: 'linear-gradient(135deg, var(--color-brand), #a855f7)',
                                borderRadius: 'var(--radius-sm)',
                                display: 'flex',
                                alignItems: 'center',
                                justifyContent: 'center',
                                fontSize: '0.75rem',
                            }}>
                                {currentWorkspace?.name?.[0]?.toUpperCase() || 'K'}
                            </span>
                            {currentWorkspace?.name || 'Select Workspace'}
                        </span>
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" style={{ opacity: 0.5 }}>
                            <path d="M7 10l5 5 5-5z" />
                        </svg>
                    </button>

                    {/* Workspace dropdown */}
                    {showWorkspaceSwitcher && (
                        <div className="card animate-fade-in" style={{
                            position: 'absolute',
                            left: 'var(--space-2)',
                            right: 'var(--space-2)',
                            zIndex: 50,
                            marginTop: 'var(--space-2)',
                            padding: 'var(--space-2)',
                        }}>
                            {workspaceList.map((ws) => (
                                <button
                                    key={ws.id}
                                    className="btn btn-ghost"
                                    style={{
                                        width: '100%',
                                        justifyContent: 'flex-start',
                                        padding: 'var(--space-2) var(--space-3)',
                                        background: ws.id === currentWorkspace?.id
                                            ? 'var(--color-bg-hover)' : undefined,
                                    }}
                                    onClick={() => {
                                        onSelectWorkspace(ws);
                                        setShowWorkspaceSwitcher(false);
                                    }}
                                >
                                    {ws.name}
                                    <span className="badge" style={{ marginLeft: 'auto' }}>
                                        {ws.role}
                                    </span>
                                </button>
                            ))}
                        </div>
                    )}
                </div>

                {/* New conversation button */}
                <div style={{ padding: 'var(--space-3) var(--space-4)' }}>
                    <button
                        className="btn btn-primary"
                        style={{ width: '100%' }}
                        onClick={onNewConversation}
                    >
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                            <path d="M12 5v14M5 12h14" />
                        </svg>
                        New Chat
                    </button>
                </div>

                {/* Search */}
                <div style={{ padding: '0 var(--space-4) var(--space-3)' }}>
                    <input
                        className="input"
                        type="text"
                        placeholder="Search conversations..."
                        value={searchQuery}
                        onChange={(e) => setSearchQuery(e.target.value)}
                        style={{ fontSize: '0.8125rem' }}
                    />
                </div>

                {/* Conversation list */}
                <div style={{
                    flex: 1,
                    overflowY: 'auto',
                    padding: '0 var(--space-2)',
                }}>
                    {filteredConversations.map((conv) => (
                        <div
                            key={conv.id}
                            className="group"
                            style={{
                                position: 'relative',
                                marginBottom: '2px',
                            }}
                        >
                            {editingConversationId === conv.id ? (
                                <div style={{ padding: 'var(--space-2) var(--space-3)' }}>
                                    <input
                                        autoFocus
                                        className="input"
                                        value={editTitle}
                                        onChange={(e) => setEditTitle(e.target.value)}
                                        onBlur={saveTitle}
                                        onKeyDown={handleKeyDown}
                                        style={{
                                            width: '100%',
                                            fontSize: '0.875rem',
                                            padding: '4px 8px',
                                            height: 'auto',
                                        }}
                                    />
                                </div>
                            ) : (
                                <div
                                    role="button"
                                    tabIndex={0}
                                    className="btn btn-ghost animate-slide-in"
                                    style={{
                                        width: '100%',
                                        justifyContent: 'flex-start',
                                        padding: 'var(--space-2) var(--space-3)',
                                        paddingRight: 'var(--space-8)', // Make room for actions
                                        borderRadius: 'var(--radius-sm)',
                                        background: conv.id === currentConversation?.id
                                            ? 'var(--color-bg-hover)' : undefined,
                                        borderLeft: conv.id === currentConversation?.id
                                            ? '2px solid var(--color-brand)' : '2px solid transparent',
                                        cursor: 'pointer',
                                        position: 'relative',
                                        textAlign: 'left'
                                    }}
                                    onClick={() => onSelectConversation(conv)}
                                    onKeyDown={(e) => {
                                        if (e.key === 'Enter' || e.key === ' ') {
                                            onSelectConversation(conv);
                                        }
                                    }}
                                >
                                    <span style={{
                                        display: 'block',
                                        overflow: 'hidden',
                                        textOverflow: 'ellipsis',
                                        whiteSpace: 'nowrap',
                                        fontSize: '0.875rem',
                                        width: '100%'
                                    }}>
                                        {conv.title || 'New conversation'}
                                    </span>

                                    {/* Actions (visible on hover) */}
                                    <div
                                        className="opacity-0 group-hover:opacity-100 transition-opacity"
                                        style={{
                                            position: 'absolute',
                                            right: 'var(--space-2)',
                                            top: '50%',
                                            transform: 'translateY(-50%)',
                                            display: 'flex',
                                            gap: '4px',
                                            zIndex: 10,
                                            background: conv.id === currentConversation?.id
                                                ? 'var(--color-bg-hover)'
                                                : 'var(--color-bg-secondary)',
                                        }}
                                        onClick={(e) => e.stopPropagation()}
                                    >
                                        <button
                                            type="button"
                                            style={{
                                                padding: 4,
                                                cursor: 'pointer',
                                                opacity: 0.6,
                                                background: 'transparent',
                                                border: 'none',
                                                display: 'flex',
                                                alignItems: 'center'
                                            }}
                                            onClick={(e) => startEditing(e, conv)}
                                            title="Rename"
                                        >
                                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                                                <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" />
                                                <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z" />
                                            </svg>
                                        </button>
                                        <button
                                            type="button"
                                            style={{
                                                padding: 4,
                                                cursor: 'pointer',
                                                opacity: 0.6,
                                                color: 'var(--color-danger)',
                                                background: 'transparent',
                                                border: 'none',
                                                display: 'flex',
                                                alignItems: 'center'
                                            }}
                                            onClick={(e) => handleDeleteConversation(e, conv.id)}
                                            title="Delete"
                                        >
                                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                                                <polyline points="3 6 5 6 21 6" />
                                                <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
                                            </svg>
                                        </button>
                                    </div>
                                </div>
                            )}
                        </div>
                    ))}

                    {filteredConversations.length === 0 && (
                        <p style={{
                            textAlign: 'center',
                            color: 'var(--color-text-tertiary)',
                            fontSize: '0.8125rem',
                            padding: 'var(--space-6)',
                        }}>
                            {searchQuery ? 'No matches found' : 'No conversations yet'}
                        </p>
                    )}
                </div>

                {/* Bottom actions */}
                <div style={{
                    padding: 'var(--space-3) var(--space-4)',
                    borderTop: '1px solid var(--color-border)',
                    display: 'flex',
                    gap: 'var(--space-2)',
                }}>
                    <button className="btn btn-ghost" style={{ flex: 1 }} onClick={onOpenSettings}>
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                            <circle cx="12" cy="12" r="3" />
                            <path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 010 2.83 2 2 0 01-2.83 0l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-2 2 2 2 0 01-2-2v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 01-2.83 0 2 2 0 010-2.83l.06-.06A1.65 1.65 0 004.68 15a1.65 1.65 0 00-1.51-1H3a2 2 0 01-2-2 2 2 0 012-2h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 010-2.83 2 2 0 012.83 0l.06.06A1.65 1.65 0 009 4.68a1.65 1.65 0 001-1.51V3a2 2 0 012-2 2 2 0 012 2v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 012.83 0 2 2 0 010 2.83l-.06.06A1.65 1.65 0 0019.32 9a1.65 1.65 0 001.51 1H21a2 2 0 012 2 2 2 0 01-2 2h-.09a1.65 1.65 0 00-1.51 1z" />
                        </svg>
                        Settings
                    </button>
                    <button className="btn btn-ghost" onClick={onLogout}>
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                            <path d="M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4" />
                            <polyline points="16,17 21,12 16,7" />
                            <line x1="21" y1="12" x2="9" y2="12" />
                        </svg>
                    </button>
                </div>
            </aside>

            {/* Delete Confirmation Modal */}
            {
                deleteConfirmationId && (
                    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50" onClick={() => setDeleteConfirmationId(null)}>
                        <div className="card animate-fade-in p-6 max-w-sm w-full mx-4" onClick={e => e.stopPropagation()}>
                            <h3 className="text-lg font-bold mb-2">Delete Conversation</h3>
                            <p className="text-gray-400 mb-6">Are you sure you want to delete this conversation? This action cannot be undone.</p>
                            <div className="flex justify-end gap-3">
                                <button
                                    className="btn btn-ghost"
                                    onClick={() => setDeleteConfirmationId(null)}
                                >
                                    Cancel
                                </button>
                                <button
                                    className="btn btn-primary bg-red-600 hover:bg-red-700 border-none"
                                    onClick={confirmDelete}
                                >
                                    Delete
                                </button>
                            </div>
                        </div>
                    </div>
                )
            }
        </>
    );
}
