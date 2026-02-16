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

    // Load workspaces
    useEffect(() => {
        workspaces.list()
            .then((res) => setWorkspaceList(res.workspaces || []))
            .catch(console.error);
    }, []);

    // Load conversations when workspace changes
    useEffect(() => {
        if (!currentWorkspace) return;
        conversations.list(currentWorkspace.id)
            .then((res) => setConversationList(res.conversations || []))
            .catch(console.error);
    }, [currentWorkspace]);

    const filteredConversations = conversationList.filter(
        (c) => !searchQuery || c.title.toLowerCase().includes(searchQuery.toLowerCase())
    );

    return (
        <aside className="sidebar">
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
                    <button
                        key={conv.id}
                        className="btn btn-ghost animate-slide-in"
                        style={{
                            width: '100%',
                            justifyContent: 'flex-start',
                            padding: 'var(--space-2) var(--space-3)',
                            marginBottom: '2px',
                            borderRadius: 'var(--radius-sm)',
                            background: conv.id === currentConversation?.id
                                ? 'var(--color-bg-hover)' : undefined,
                            borderLeft: conv.id === currentConversation?.id
                                ? '2px solid var(--color-brand)' : '2px solid transparent',
                        }}
                        onClick={() => onSelectConversation(conv)}
                    >
                        <span style={{
                            overflow: 'hidden',
                            textOverflow: 'ellipsis',
                            whiteSpace: 'nowrap',
                            fontSize: '0.875rem',
                        }}>
                            {conv.title || 'New conversation'}
                        </span>
                    </button>
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
    );
}
