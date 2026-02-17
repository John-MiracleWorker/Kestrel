import { useState, useEffect, useCallback } from 'react';
import { useAuth } from './hooks/useAuth';
import { LoginPage } from './components/Auth/LoginPage';
import { Sidebar } from './components/Sidebar/Sidebar';
import { ChatView } from './components/Chat/ChatView';
import { SettingsPanel } from './components/Settings/SettingsPanel';
import { useChat } from './hooks/useChat';
import { conversations, type Workspace, type Conversation, type Message } from './api/client';

export default function App() {
    const { user, isLoading, isAuthenticated, logout } = useAuth();
    const [currentWorkspace, setCurrentWorkspace] = useState<Workspace | null>(null);
    const [currentConversation, setCurrentConversation] = useState<Conversation | null>(null);
    const [initialMessages, setInitialMessages] = useState<Message[]>([]);
    const [showSettings, setShowSettings] = useState(false);

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
            .then((res) => setInitialMessages(res.messages || []))
            .catch(console.error);
    }, [currentWorkspace, currentConversation]);

    // Auto-create conversation if none exists
    useEffect(() => {
        if (currentWorkspace && !currentConversation) {
            conversations.create(currentWorkspace.id)
                .then(conv => {
                    setCurrentConversation(conv);
                    setInitialMessages([]);
                })
                .catch(err => console.error('Failed to auto-create conversation:', err));
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
                background: 'var(--color-bg)',
            }}>
                <div style={{
                    width: 48,
                    height: 48,
                    background: 'linear-gradient(135deg, var(--color-brand), #a855f7)',
                    borderRadius: 'var(--radius-md)',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    fontSize: '1.5rem',
                    boxShadow: 'var(--shadow-glow)',
                    animation: 'pulse 2s infinite',
                }}>
                    🪶
                </div>
            </div>
        );
    }

    // Auth gate
    if (!isAuthenticated) {
        return <LoginPage />;
    }

    return (
        <div className="app-layout">
            <Sidebar
                currentWorkspace={currentWorkspace}
                currentConversation={currentConversation}
                onSelectWorkspace={setCurrentWorkspace}
                onSelectConversation={setCurrentConversation}
                onNewConversation={handleNewConversation}
                onOpenSettings={() => setShowSettings(true)}
                onLogout={logout}
            />
            <ChatView
                messages={messages}
                streamingMessage={streamingMessage}
                onSendMessage={sendMessage}
                isConnected={isConnected}
                conversationTitle={currentConversation?.title}
            />
            {showSettings && (
                <SettingsPanel
                    onClose={() => setShowSettings(false)}
                    userEmail={user?.email}
                    userDisplayName={user?.displayName}
                    workspaceId={currentWorkspace?.id || ''}
                />
            )}
        </div>
    );
}
