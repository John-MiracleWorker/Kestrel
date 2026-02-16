import { useState, useEffect, useCallback, useRef } from 'react';
import { createChatSocket, type Message } from '../api/client';

interface StreamingMessage {
    id: string;
    role: 'assistant';
    content: string;
    isStreaming: boolean;
}

interface UseChatReturn {
    messages: Message[];
    streamingMessage: StreamingMessage | null;
    sendMessage: (content: string) => void;
    isConnected: boolean;
}

export function useChat(
    workspaceId: string | null,
    conversationId: string | null,
    initialMessages: Message[] = [],
): UseChatReturn {
    const [messages, setMessages] = useState<Message[]>(initialMessages);
    const [streamingMessage, setStreamingMessage] = useState<StreamingMessage | null>(null);
    const [isConnected, setIsConnected] = useState(false);
    const wsRef = useRef<WebSocket | null>(null);
    const contentRef = useRef('');

    useEffect(() => {
        if (!workspaceId || !conversationId) return;

        const ws = createChatSocket();
        wsRef.current = ws;

        ws.onopen = () => {
            setIsConnected(true);
            // Join conversation
            ws.send(JSON.stringify({
                type: 'join',
                workspaceId,
                conversationId,
            }));
        };

        ws.onmessage = (event) => {
            const data = JSON.parse(event.data);

            switch (data.type) {
                case 'content_delta':
                    contentRef.current += data.content;
                    setStreamingMessage({
                        id: data.messageId || 'streaming',
                        role: 'assistant',
                        content: contentRef.current,
                        isStreaming: true,
                    });
                    break;

                case 'done':
                    if (contentRef.current) {
                        const finalMessage: Message = {
                            id: data.messageId || crypto.randomUUID(),
                            role: 'assistant',
                            content: contentRef.current,
                            createdAt: new Date().toISOString(),
                        };
                        setMessages(prev => [...prev, finalMessage]);
                    }
                    setStreamingMessage(null);
                    contentRef.current = '';
                    break;

                case 'error':
                    console.error('Chat error:', data.message);
                    setStreamingMessage(null);
                    contentRef.current = '';
                    break;
            }
        };

        ws.onclose = () => setIsConnected(false);
        ws.onerror = () => setIsConnected(false);

        return () => {
            ws.close();
            wsRef.current = null;
        };
    }, [workspaceId, conversationId]);

    // Update messages when initialMessages change
    useEffect(() => {
        setMessages(initialMessages);
    }, [initialMessages]);

    const sendMessage = useCallback((content: string) => {
        if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;

        const userMessage: Message = {
            id: crypto.randomUUID(),
            role: 'user',
            content,
            createdAt: new Date().toISOString(),
        };

        setMessages(prev => [...prev, userMessage]);
        contentRef.current = '';

        wsRef.current.send(JSON.stringify({
            type: 'chat',
            workspaceId,
            conversationId,
            content,
        }));
    }, [workspaceId, conversationId]);

    return { messages, streamingMessage, sendMessage, isConnected };
}
