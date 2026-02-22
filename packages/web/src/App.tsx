import { useState, useEffect, useCallback, useRef } from 'react';
import { useAuth } from './hooks/useAuth';
import { LoginPage } from './components/Auth/LoginPage';
import { Sidebar } from './components/Sidebar/Sidebar';
import { ChatView } from './components/Chat/ChatView';
import { SettingsPanel } from './components/Settings/SettingsPanel';
import { LiveCanvas } from './components/Layout/LiveCanvas';
import { MoltbookPanel } from './components/Moltbook/MoltbookPanel';
import { MemoryPalace } from './components/MemoryPalace/MemoryPalace';
import { DocsPanel } from './components/Docs/DocsPanel';
import { ScreenShare } from './components/ScreenShare/ScreenShare';
import { useChat } from './hooks/useChat';
import { conversations, type Workspace, type Conversation, type Message } from './api/client';

export default function App() {
    const { user, isLoading, isAuthenticated, logout } = useAuth();
    const [currentWorkspace, setCurrentWorkspace] = useState<Workspace | null>(null);
    const [currentConversation, setCurrentConversation] = useState<Conversation | null>(null);
    const [initialMessages, setInitialMessages] = useState<Message[]>([]);
    const [showSettings, setShowSettings] = useState(false);
    const [showMoltbook, setShowMoltbook] = useState(false);
    const [showMemoryPalace, setShowMemoryPalace] = useState(false);
    const [showDocs, setShowDocs] = useState(false);
    const [showScreenShare, setShowScreenShare] = useState(false);
    const autoCreateAttempted = useRef<string | null>(null);
    const [showCanvas, setShowCanvas] = useState(true); // Default to open for the look

    const { messages, streamingMessage, sendMessage, isConnected } = useChat(
        currentWorkspace?.id || null,
        currentConversation?.id || null,
        initialMessages,
    );

    // Load messages when conversation changes
    useEffect(() => {
        if (!currentWorkspace || !currentConversation) return;
        conversations
            .messages(currentWorkspace.id, currentConversation.id)
            .then((res: { messages: Message[] }) => setInitialMessages(res.messages || []))
            .catch(console.error);
    }, [currentWorkspace, currentConversation]);

    // Auto-create conversation if none exists (only attempt once per workspace)
    useEffect(() => {
        if (currentWorkspace && !currentConversation && autoCreateAttempted.current !== currentWorkspace.id) {
            autoCreateAttempted.current = currentWorkspace.id;
            conversations.create(currentWorkspace.id)
                .then((conv: Conversation) => {
                    setCurrentConversation(conv);
                    setInitialMessages([]);
                })
                .catch((err: Error) => console.error('Failed to auto-create conversation:', err));
        }
    }, [currentWorkspace, currentConversation]);

    const handleNewConversation = useCallback(async () => {
        if (!currentWorkspace) return;
        try {
            const conv = await conversations.create(currentWorkspace.id);
            setCurrentConversation(conv);
            setInitialMessages([]);
        } catch (err) {
            console.error('Failed to create conversation:', err);
        }
    }, [currentWorkspace]);

    // Loading state
    if (isLoading) {
        return (
            <div style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                height: '100vh',
                background: 'var(--bg-terminal)',
                color: 'var(--accent-cyan)',
                fontFamily: 'var(--font-mono)'
            }}>
                <div className="animate-flicker">INITIALIZING_SYSTEM...</div>
            </div>
        );
    }

    // Auth gate
    if (!isAuthenticated) {
        return <LoginPage />;
    }

    return (
        <div style={{ display: 'flex', height: '100vh', width: '100vw', overflow: 'hidden' }}>
            <Sidebar
                currentWorkspace={currentWorkspace}
                currentConversation={currentConversation}
                onSelectWorkspace={setCurrentWorkspace}
                onSelectConversation={setCurrentConversation}
                onNewConversation={handleNewConversation}
                onOpenSettings={() => setShowSettings(true)}
                onOpenMoltbook={() => setShowMoltbook(true)}
                onOpenMemoryPalace={() => setShowMemoryPalace(true)}
                onOpenDocs={() => setShowDocs(true)}
                onOpenScreenShare={() => setShowScreenShare(true)}
                onLogout={logout}
            />

            <div style={{ flex: 1, display: 'flex', flexDirection: 'column', position: 'relative' }}>
                <ChatView
                    workspaceId={currentWorkspace?.id || null}
                    messages={messages}
                    streamingMessage={streamingMessage}
                    onSendMessage={sendMessage}
                    isConnected={isConnected}
                    conversationTitle={currentConversation?.title}
                    onToggleCanvas={() => setShowCanvas(!showCanvas)}
                />
            </div>

            <LiveCanvas
                isVisible={showCanvas}
                activeTask={streamingMessage ? "PROCESSING_USER_QUERY" : "SYSTEM_READY"}
                isStreaming={!!streamingMessage}
                content={streamingMessage?.content}
                toolActivity={streamingMessage?.toolActivity}
                agentActivities={streamingMessage?.agentActivities}
            />

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
        </div>
    );
}
