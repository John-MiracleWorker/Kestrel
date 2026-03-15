import { useState, useEffect, useCallback, useRef, useMemo } from 'react';
import { Navigate, Route, Routes, matchPath, useLocation, useNavigate } from 'react-router-dom';
import { useAuth } from './hooks/useAuth';
import { LoginPage } from './components/Auth/LoginPage';
import { Sidebar } from './components/Sidebar/Sidebar';
import { ChatView } from './components/Chat/ChatView';
import { SettingsPanel } from './components/Settings/SettingsPanel';
import { LiveCanvas } from './components/Layout/LiveCanvas';
import { CommandPalette } from './components/Layout/CommandPalette';
import { MoltbookPanel } from './components/Moltbook/MoltbookPanel';
import { MemoryPalace } from './components/MemoryPalace/MemoryPalace';
import { DocsPanel } from './components/Docs/DocsPanel';
import { ScreenShare } from './components/ScreenShare/ScreenShare';
import { PRReview } from './components/PRReview/PRReview';
import { OperationsPage } from './components/Operations/OperationsPage';
import { useChat } from './hooks/useChat';
import {
    conversations,
    workspaces,
    type Workspace,
    type Conversation,
    type Message,
} from './api/client';

export default function App() {
    const { user, isLoading, isAuthenticated, logout } = useAuth();
    const location = useLocation();
    const navigate = useNavigate();

    const [workspaceList, setWorkspaceList] = useState<Workspace[]>([]);
    const [currentWorkspace, setCurrentWorkspace] = useState<Workspace | null>(null);
    const [currentConversation, setCurrentConversation] = useState<Conversation | null>(null);
    const [initialMessages, setInitialMessages] = useState<Message[]>([]);
    const [sidebarOpen, setSidebarOpen] = useState(false);
    const [showSettings, setShowSettings] = useState(false);
    const [showMoltbook, setShowMoltbook] = useState(false);
    const [showMemoryPalace, setShowMemoryPalace] = useState(false);
    const [showDocs, setShowDocs] = useState(false);
    const [showScreenShare, setShowScreenShare] = useState(false);
    const [showPRReview, setShowPRReview] = useState(false);
    const [showCommandPalette, setShowCommandPalette] = useState(false);
    const [showCanvas, setShowCanvas] = useState(true);
    const autoCreateAttempted = useRef<string | null>(null);

    const routeWorkspaceMatch =
        matchPath('/workspaces/:workspaceId/operations', location.pathname) ||
        matchPath('/workspaces/:workspaceId', location.pathname);
    const routeWorkspaceId = routeWorkspaceMatch?.params.workspaceId || null;
    const activeSurface: 'chat' | 'operations' = location.pathname.endsWith('/operations')
        ? 'operations'
        : 'chat';

    const {
        messages,
        streamingMessage,
        sendMessage,
        isConnected,
        pendingApproval,
        handleApproval,
    } = useChat(currentWorkspace?.id || null, currentConversation?.id || null, initialMessages);

    useEffect(() => {
        if (!isAuthenticated) return;
        workspaces
            .list()
            .then((res) => setWorkspaceList(res.workspaces || []))
            .catch(() => setWorkspaceList([]));
    }, [isAuthenticated]);

    useEffect(() => {
        if (!workspaceList.length) {
            setCurrentWorkspace(null);
            return;
        }

        const routedWorkspace = routeWorkspaceId
            ? workspaceList.find((workspace) => workspace.id === routeWorkspaceId) || null
            : null;
        const nextWorkspace = routedWorkspace || currentWorkspace || workspaceList[0];

        if (nextWorkspace && nextWorkspace.id !== currentWorkspace?.id) {
            setCurrentWorkspace(nextWorkspace);
            setCurrentConversation(null);
            setInitialMessages([]);
        }

        if (!routeWorkspaceId && nextWorkspace) {
            navigate(`/workspaces/${nextWorkspace.id}`, { replace: true });
        }

        if (routeWorkspaceId && !routedWorkspace && workspaceList[0]) {
            navigate(`/workspaces/${workspaceList[0].id}`, { replace: true });
        }
    }, [workspaceList, routeWorkspaceId, currentWorkspace, navigate]);

    useEffect(() => {
        if (!currentWorkspace || !currentConversation || activeSurface !== 'chat') return;
        conversations
            .messages(currentWorkspace.id, currentConversation.id)
            .then((res: { messages: Message[] }) => setInitialMessages(res.messages || []))
            .catch(console.error);
    }, [currentWorkspace, currentConversation, activeSurface]);

    useEffect(() => {
        if (activeSurface !== 'chat') return;
        if (
            currentWorkspace &&
            !currentConversation &&
            autoCreateAttempted.current !== currentWorkspace.id
        ) {
            autoCreateAttempted.current = currentWorkspace.id;
            conversations
                .create(currentWorkspace.id)
                .then((conv: Conversation) => {
                    setCurrentConversation(conv);
                    setInitialMessages([]);
                })
                .catch((err: Error) => console.error('Failed to auto-create conversation:', err));
        }
    }, [currentWorkspace, currentConversation, activeSurface]);

    const handleNewConversation = useCallback(async () => {
        if (!currentWorkspace) return;
        try {
            const conv = await conversations.create(currentWorkspace.id);
            setCurrentConversation(conv);
            setInitialMessages([]);
            navigate(`/workspaces/${currentWorkspace.id}`);
        } catch (err) {
            console.error('Failed to create conversation:', err);
        }
    }, [currentWorkspace, navigate]);

    const handleSelectWorkspace = useCallback(
        (workspace: Workspace) => {
            setCurrentWorkspace(workspace);
            setCurrentConversation(null);
            setInitialMessages([]);
            autoCreateAttempted.current = null;
            navigate(
                `/workspaces/${workspace.id}${activeSurface === 'operations' ? '/operations' : ''}`,
            );
        },
        [activeSurface, navigate],
    );

    const handleSelectConversation = useCallback(
        (conversation: Conversation) => {
            setCurrentConversation(conversation);
            if (currentWorkspace) {
                navigate(`/workspaces/${currentWorkspace.id}`);
            }
        },
        [currentWorkspace, navigate],
    );

    const handleOpenOperations = useCallback(() => {
        if (currentWorkspace) {
            navigate(`/workspaces/${currentWorkspace.id}/operations`);
        }
    }, [currentWorkspace, navigate]);

    useEffect(() => {
        const onNewConv = () => handleNewConversation();
        const onSettings = () => setShowSettings(true);
        const onCmdK = (e: KeyboardEvent) => {
            if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
                e.preventDefault();
                setShowCommandPalette((prev) => !prev);
            }
        };
        window.addEventListener('new-conversation', onNewConv);
        window.addEventListener('open-settings', onSettings);
        window.addEventListener('keydown', onCmdK);
        return () => {
            window.removeEventListener('new-conversation', onNewConv);
            window.removeEventListener('open-settings', onSettings);
            window.removeEventListener('keydown', onCmdK);
        };
    }, [handleNewConversation]);

    const paletteActions = useMemo(
        () => [
            {
                id: 'new-chat',
                label: 'New Conversation',
                category: 'Navigation',
                icon: '✨',
                shortcut: '⌘N',
                action: handleNewConversation,
            },
            {
                id: 'operations',
                label: 'Open Operations',
                category: 'Navigation',
                icon: '◇',
                shortcut: '⌘O',
                action: handleOpenOperations,
            },
            {
                id: 'toggle-canvas',
                label: 'Toggle Live HUD',
                category: 'Navigation',
                icon: '📺',
                shortcut: '⌘\\',
                action: () => setShowCanvas((c) => !c),
            },
            {
                id: 'settings',
                label: 'Open Settings',
                category: 'Navigation',
                icon: '⚙️',
                shortcut: '⌘,',
                action: () => setShowSettings(true),
            },
            {
                id: 'memory',
                label: 'Memory Palace',
                category: 'Panels',
                icon: '🧠',
                action: () => setShowMemoryPalace(true),
            },
            {
                id: 'moltbook',
                label: 'Moltbook',
                category: 'Panels',
                icon: '📘',
                action: () => setShowMoltbook(true),
            },
            {
                id: 'docs',
                label: 'Documentation',
                category: 'Panels',
                icon: '📖',
                action: () => setShowDocs(true),
            },
            {
                id: 'screen',
                label: 'Screen Share',
                category: 'Panels',
                icon: '🖥️',
                action: () => setShowScreenShare(true),
            },
            {
                id: 'pr-review',
                label: 'PR Review',
                category: 'Panels',
                icon: '🔍',
                action: () => setShowPRReview(true),
            },
        ],
        [handleNewConversation, handleOpenOperations],
    );

    if (isLoading) {
        return (
            <div
                style={{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    height: '100vh',
                    background: 'var(--bg-terminal)',
                    color: 'var(--accent-cyan)',
                    fontFamily: 'var(--font-mono)',
                }}
            >
                <div className="animate-flicker">INITIALIZING_SYSTEM...</div>
            </div>
        );
    }

    if (!isAuthenticated) {
        return <LoginPage />;
    }

    return (
        <div style={{ display: 'flex', height: '100vh', width: '100vw', overflow: 'hidden' }}>
            {/* Responsive sidebar toggle */}
            <button
                className="sidebar-toggle"
                onClick={() => setSidebarOpen(!sidebarOpen)}
                aria-label="Toggle sidebar"
            >
                {sidebarOpen ? '✕' : '☰'}
            </button>

            {/* Backdrop for mobile sidebar */}
            <div
                className={`sidebar-backdrop ${sidebarOpen ? 'sidebar-backdrop-visible' : ''}`}
                onClick={() => setSidebarOpen(false)}
            />

            <div className={`sidebar-responsive ${sidebarOpen ? 'sidebar-open' : ''}`}>
                <Sidebar
                    currentWorkspace={currentWorkspace}
                    currentConversation={currentConversation}
                    activeSurface={activeSurface}
                    onSelectWorkspace={(ws) => {
                        handleSelectWorkspace(ws);
                        setSidebarOpen(false);
                    }}
                    onSelectConversation={(conv) => {
                        handleSelectConversation(conv);
                        setSidebarOpen(false);
                    }}
                    onNewConversation={() => {
                        handleNewConversation();
                        setSidebarOpen(false);
                    }}
                    onOpenOperations={() => {
                        handleOpenOperations();
                        setSidebarOpen(false);
                    }}
                    onOpenSettings={() => setShowSettings(true)}
                    onOpenMoltbook={() => setShowMoltbook(true)}
                    onOpenMemoryPalace={() => setShowMemoryPalace(true)}
                    onOpenDocs={() => setShowDocs(true)}
                    onOpenScreenShare={() => setShowScreenShare(true)}
                    onOpenPRReview={() => setShowPRReview(true)}
                    onLogout={logout}
                />
            </div>

            <div
                style={{
                    flex: 1,
                    display: 'flex',
                    flexDirection: 'column',
                    position: 'relative',
                    minWidth: 0,
                }}
            >
                <Routes>
                    <Route
                        path="/workspaces/:workspaceId/operations"
                        element={<OperationsPage workspaceId={currentWorkspace?.id || ''} />}
                    />
                    <Route
                        path="/workspaces/:workspaceId"
                        element={
                            <ChatView
                                workspaceId={currentWorkspace?.id || null}
                                messages={messages}
                                streamingMessage={streamingMessage}
                                onSendMessage={sendMessage}
                                isConnected={isConnected}
                                conversationTitle={currentConversation?.title}
                                onToggleCanvas={() => setShowCanvas(!showCanvas)}
                                pendingApproval={pendingApproval}
                                onApproval={handleApproval}
                            />
                        }
                    />
                    <Route
                        path="*"
                        element={
                            currentWorkspace ? (
                                <Navigate to={`/workspaces/${currentWorkspace.id}`} replace />
                            ) : (
                                <div style={{ padding: '24px', color: 'var(--text-dim)' }}>
                                    Loading workspace…
                                </div>
                            )
                        }
                    />
                </Routes>
            </div>

            <div className="live-canvas-responsive">
                <LiveCanvas
                    isVisible={showCanvas && activeSurface === 'chat'}
                    activeTask={streamingMessage ? 'PROCESSING_USER_QUERY' : 'SYSTEM_READY'}
                    isStreaming={!!streamingMessage}
                    content={streamingMessage?.content}
                    toolActivity={streamingMessage?.toolActivity}
                    agentActivities={streamingMessage?.agentActivities}
                    delegationEvents={streamingMessage?.delegationEvents}
                />
            </div>

            {showSettings && (
                <SettingsPanel
                    onClose={() => setShowSettings(false)}
                    userEmail={user?.email}
                    userDisplayName={user?.displayName}
                    workspaceId={currentWorkspace?.id || ''}
                />
            )}

            <MoltbookPanel
                workspaceId={currentWorkspace?.id || ''}
                isVisible={showMoltbook}
                onClose={() => setShowMoltbook(false)}
            />

            <MemoryPalace
                workspaceId={currentWorkspace?.id || ''}
                isVisible={showMemoryPalace}
                onClose={() => setShowMemoryPalace(false)}
            />

            <DocsPanel
                workspaceId={currentWorkspace?.id || ''}
                isVisible={showDocs}
                onClose={() => setShowDocs(false)}
            />

            <ScreenShare
                workspaceId={currentWorkspace?.id || ''}
                isVisible={showScreenShare}
                onClose={() => setShowScreenShare(false)}
            />

            <PRReview
                workspaceId={currentWorkspace?.id || ''}
                isVisible={showPRReview}
                onClose={() => setShowPRReview(false)}
            />

            <CommandPalette
                isOpen={showCommandPalette}
                onClose={() => setShowCommandPalette(false)}
                actions={paletteActions}
            />
        </div>
    );
}
