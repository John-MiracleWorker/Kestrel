import { useState, useEffect, useCallback, useRef } from 'react';
import { createChatSocket, conversations, forceRefresh, type Message } from '../api/client';
import { useAuth } from './useAuth';

interface AgentActivity {
    activity_type: string;
    [key: string]: unknown;
}

interface ToolActivity {
    status: string; // 'thinking' | 'planning' | 'tool_calling' | 'tool_result' | 'agent_activity'
    toolName?: string;
    toolArgs?: string;
    toolResult?: string;
    thinking?: string;
    activity?: AgentActivity;
}

export interface DelegationEvent {
    type: string; // delegation_started, delegation_progress, council_started, council_opinion, etc.
    specialist: string;
    status?: string; // thinking, tool_calling, tool_result, step_done, complete, failed, approve, reject
    goal?: string;
    tools?: string[];
    tool?: string;
    toolArgs?: string;
    toolResult?: string;
    thinking?: string;
    content?: string;
    result?: string;
    count?: number;
    subtasks?: Array<{ goal: string; specialist: string }>;
    // Council-specific fields
    roles?: string[];
    confidence?: number;
    concerns?: string[];
    suggestions?: string[];
    timestamp: number;
}

export interface RoutingInfo {
    provider: string;
    model: string;
    wasEscalated: boolean;
    complexity: number;
}

interface StreamingMessage {
    id: string;
    role: 'assistant';
    content: string;
    isStreaming: boolean;
    toolActivity?: ToolActivity | null;
    agentActivities?: AgentActivity[];
    delegationEvents?: DelegationEvent[];
    routingInfo?: RoutingInfo | null;
}

interface UseChatReturn {
    messages: Message[];
    streamingMessage: StreamingMessage | null;
    sendMessage: (
        content: string,
        provider?: string,
        model?: string,
        attachments?: Array<{ url: string; filename: string; mimeType: string; size: number }>,
    ) => void;
    isConnected: boolean;
}

function generateId(): string {
    if (typeof crypto !== 'undefined' && crypto.randomUUID) {
        return crypto.randomUUID();
    }
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (c) {
        const r = (Math.random() * 16) | 0;
        const v = c === 'x' ? r : (r & 0x3) | 0x8;
        return v.toString(16);
    });
}

const RECONNECT_DELAYS = [1000, 2000, 4000, 8000, 15000]; // exponential backoff

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
    // Backup ref: never cleared except inside the done handler.
    // Prevents message loss when contentRef is cleared by conversation
    // changes or sendMessage during active streaming.
    const lastDoneContentRef = useRef('');
    const activitiesRef = useRef<AgentActivity[]>([]);
    const delegationEventsRef = useRef<DelegationEvent[]>([]);
    const routingInfoRef = useRef<RoutingInfo | null>(null);
    const reconnectAttemptRef = useRef(0);
    const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    const isMountedRef = useRef(true);
    const { isAuthenticated } = useAuth();

    const connectSocket = useCallback(() => {
        if (!workspaceId || !conversationId || !isMountedRef.current) return;

        const ws = createChatSocket();
        wsRef.current = ws;

        ws.onopen = () => {
            if (!isMountedRef.current || wsRef.current !== ws) return;
            setIsConnected(true);
            reconnectAttemptRef.current = 0;
        };

        ws.onmessage = (event) => {
            if (!isMountedRef.current || wsRef.current !== ws) return;
            const data = JSON.parse(event.data as string) as {
                type: 'token' | 'done' | 'error' | 'thinking' | 'tool_activity' | 'routing_info';
                content?: string;
                messageId?: string;
                error?: string;
                status?: string;
                toolName?: string;
                toolArgs?: string;
                toolResult?: string;
                thinking?: string;
                provider?: string;
                model?: string;
                wasEscalated?: boolean;
                complexity?: number;
                delegationType?: string;
                delegation?: string;
                activity?: string;
            };

            switch (data.type) {
                case 'thinking':
                    // Show thinking indicator before first token arrives
                    if (!contentRef.current) {
                        setStreamingMessage({
                            id: data.messageId || 'streaming',
                            role: 'assistant',
                            content: '',
                            isStreaming: true,
                        });
                    }
                    break;

                case 'routing_info':
                    // Store routing metadata for display on messages
                    routingInfoRef.current = {
                        provider: data.provider || '',
                        model: data.model || '',
                        wasEscalated: data.wasEscalated || false,
                        complexity: data.complexity || 0,
                    };
                    setStreamingMessage((prev) => ({
                        id: prev?.id || data.messageId || 'streaming',
                        role: 'assistant',
                        content: prev?.content || contentRef.current || '',
                        isStreaming: true,
                        toolActivity: prev?.toolActivity ?? null,
                        agentActivities: prev?.agentActivities,
                        routingInfo: routingInfoRef.current,
                    }));
                    break;

                case 'tool_activity': {
                    // Check if this is a delegation event (for AgentDebatePanel)
                    const isDelegation = data.status === 'delegation' && data.delegationType;
                    if (isDelegation) {
                        try {
                            const delegationData = JSON.parse(data.delegation || '{}');
                            // Map council event fields to delegation format
                            // Council uses: role, vote, analysis, concerns
                            // Coordinator uses: specialist, status, thinking, tool
                            const specialist =
                                delegationData.specialist || delegationData.role || '';
                            const thinking =
                                delegationData.thinking ||
                                delegationData.analysis ||
                                delegationData.message ||
                                '';
                            const status =
                                delegationData.status ||
                                delegationData.vote ||
                                delegationData.consensus ||
                                '';
                            const evt: DelegationEvent = {
                                type: data.delegationType!,
                                specialist,
                                status,
                                goal: delegationData.goal || delegationData.topic || '',
                                tools: delegationData.tools,
                                tool: delegationData.tool,
                                toolArgs: delegationData.tool_args,
                                toolResult: delegationData.tool_result,
                                thinking,
                                content: delegationData.content,
                                result: delegationData.result,
                                count: delegationData.count || delegationData.member_count,
                                subtasks: delegationData.subtasks,
                                // Council-specific fields
                                roles: delegationData.roles,
                                confidence: delegationData.confidence,
                                concerns: delegationData.concerns,
                                suggestions: delegationData.suggestions,
                                timestamp: Date.now(),
                            };
                            delegationEventsRef.current = [...delegationEventsRef.current, evt];
                        } catch {
                            /* ignore parse errors */
                        }
                        setStreamingMessage((prev) => ({
                            id: prev?.id || data.messageId || 'streaming',
                            role: 'assistant',
                            content: prev?.content || contentRef.current || '',
                            isStreaming: true,
                            toolActivity: prev?.toolActivity ?? null,
                            agentActivities: [...activitiesRef.current],
                            delegationEvents: [...delegationEventsRef.current],
                            routingInfo: prev?.routingInfo ?? routingInfoRef.current,
                        }));
                        break;
                    }

                    // Check if this is an agent_activity event (council/coordinator/reflection)
                    const isAgentActivity = data.status === 'agent_activity';
                    if (isAgentActivity) {
                        try {
                            const activityData = JSON.parse(
                                String(data.activity || '{}'),
                            ) as AgentActivity;
                            activitiesRef.current = [...activitiesRef.current, activityData];
                        } catch {
                            /* ignore parse errors */
                        }
                    }
                    setStreamingMessage((prev) => ({
                        id: prev?.id || data.messageId || 'streaming',
                        role: 'assistant',
                        content: prev?.content || contentRef.current || '',
                        isStreaming: true,
                        toolActivity: isAgentActivity
                            ? (prev?.toolActivity ?? null)
                            : {
                                  status: data.status || 'thinking',
                                  toolName: data.toolName,
                                  toolArgs: data.toolArgs,
                                  toolResult: data.toolResult,
                                  thinking: data.thinking,
                              },
                        agentActivities: [...activitiesRef.current],
                        delegationEvents: [...delegationEventsRef.current],
                        routingInfo: prev?.routingInfo ?? routingInfoRef.current,
                    }));
                    break;
                }

                case 'token':
                    contentRef.current += data.content;
                    lastDoneContentRef.current += data.content;
                    setStreamingMessage((prev) => ({
                        id: prev?.id || data.messageId || 'streaming',
                        role: 'assistant',
                        content: contentRef.current,
                        isStreaming: true,
                        // Keep last toolActivity visible instead of clearing
                        toolActivity: prev?.toolActivity ?? null,
                        agentActivities: prev?.agentActivities,
                        routingInfo: prev?.routingInfo ?? routingInfoRef.current,
                    }));
                    break;

                case 'done': {
                    // Use lastDoneContentRef as the authoritative source —
                    // it's never cleared except here, so it survives
                    // contentRef resets from conversation changes or sendMessage.
                    const finalContent = lastDoneContentRef.current || contentRef.current;

                    if (finalContent) {
                        const finalMessage: Message = {
                            id: data.messageId || generateId(),
                            role: 'assistant',
                            content: finalContent,
                            createdAt: new Date().toISOString(),
                            routingInfo: routingInfoRef.current || undefined,
                        };
                        setMessages((prev) => [...prev, finalMessage]);

                        // Auto-generate title after first assistant reply
                        if (workspaceId && conversationId && !titleGeneratedRef.current) {
                            titleGeneratedRef.current = true;
                            conversations
                                .generateTitle(workspaceId, conversationId)
                                .then((newTitle) => {
                                    window.dispatchEvent(
                                        new CustomEvent('conversation-title-changed', {
                                            detail: { conversationId, newTitle },
                                        }),
                                    );
                                })
                                .catch((err) => console.error('Auto-title failed:', err));
                        }
                    }
                    setStreamingMessage(null);
                    contentRef.current = '';
                    lastDoneContentRef.current = '';
                    activitiesRef.current = [];
                    delegationEventsRef.current = [];
                    routingInfoRef.current = null;
                    break;
                }

                case 'error': {
                    console.error('Chat error:', JSON.stringify(data));
                    const errorMessage: Message = {
                        id: data.messageId || generateId(),
                        role: 'assistant',
                        content: `[Error: ${data.error || 'Unknown error'}]`,
                        createdAt: new Date().toISOString(),
                    };
                    setMessages((prev) => [...prev, errorMessage]);
                    setStreamingMessage(null);
                    contentRef.current = '';
                    break;
                }
            }
        };

        const handleDisconnect = async (event: CloseEvent) => {
            if (!isMountedRef.current || wsRef.current !== ws) return;
            setIsConnected(false);

            // If disconnected due to an invalid/expired token, force a refresh
            if (event.code === 4001 || event.code === 4008) {
                const refreshed = await forceRefresh();
                if (!refreshed) return; // Completely dead session; client.ts handles HTTP logout
            }

            // Schedule reconnect with backoff
            const attempt = reconnectAttemptRef.current;
            const delay = RECONNECT_DELAYS[Math.min(attempt, RECONNECT_DELAYS.length - 1)];
            reconnectAttemptRef.current = attempt + 1;
            reconnectTimerRef.current = setTimeout(connectSocket, delay);
        };

        ws.onclose = handleDisconnect;
        ws.onerror = () => setIsConnected(false);
    }, [workspaceId, conversationId]);

    useEffect(() => {
        if (!workspaceId || !conversationId || !isAuthenticated) return;

        isMountedRef.current = true;
        reconnectAttemptRef.current = 0;
        connectSocket();

        return () => {
            isMountedRef.current = false;
            if (reconnectTimerRef.current) {
                clearTimeout(reconnectTimerRef.current);
                reconnectTimerRef.current = null;
            }
            wsRef.current?.close();
            wsRef.current = null;
        };
    }, [workspaceId, conversationId, connectSocket, isAuthenticated]);

    // Automatic title generation trigger — fires after first assistant reply
    const titleGeneratedRef = useRef(false);
    useEffect(() => {
        if (
            workspaceId &&
            conversationId &&
            !titleGeneratedRef.current &&
            messages.length >= 2 &&
            messages.some((m) => m.role === 'user') &&
            messages.some((m) => m.role === 'assistant')
        ) {
            titleGeneratedRef.current = true;
            conversations
                .generateTitle(workspaceId, conversationId)
                .then((newTitle) => {
                    const event = new CustomEvent('conversation-title-changed', {
                        detail: { conversationId, newTitle },
                    });
                    window.dispatchEvent(event);
                })
                .catch((err) => console.error('Failed to auto-generate title:', err));
        }
    }, [messages, workspaceId, conversationId]);

    // Reset title gen flag when conversation changes
    useEffect(() => {
        titleGeneratedRef.current = false;
    }, [conversationId]);

    // Clear ALL state immediately when conversation changes so nothing bleeds between threads
    const prevConversationIdRef = useRef<string | null>(conversationId);
    useEffect(() => {
        if (conversationId !== prevConversationIdRef.current) {
            prevConversationIdRef.current = conversationId;
            setMessages([]);
            setStreamingMessage(null);
            contentRef.current = '';
            activitiesRef.current = [];
            delegationEventsRef.current = [];
            routingInfoRef.current = null;
        }
    }, [conversationId]);

    // Hydrate messages when the async fetch in App.tsx completes
    // (initialMessages gets a new array identity each time setInitialMessages is called)
    const prevInitialMessagesRef = useRef<Message[]>(initialMessages);
    useEffect(() => {
        if (initialMessages !== prevInitialMessagesRef.current) {
            prevInitialMessagesRef.current = initialMessages;
            setMessages(initialMessages);
        }
    }, [initialMessages]);

    const sendMessage = useCallback(
        (
            content: string,
            provider?: string,
            model?: string,
            attachments?: Array<{ url: string; filename: string; mimeType: string; size: number }>,
        ) => {
            if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
                console.warn('WebSocket not ready, message dropped');
                return;
            }

            const userMessage: Message = {
                id: generateId(),
                role: 'user',
                content,
                createdAt: new Date().toISOString(),
            };

            setMessages((prev) => [...prev, userMessage]);
            // Only reset content if we're not mid-stream; otherwise the
            // done handler would find an empty buffer and drop the response.
            if (!streamingMessage) {
                contentRef.current = '';
            }

            wsRef.current.send(
                JSON.stringify({
                    type: 'chat',
                    workspaceId,
                    conversationId,
                    content,
                    ...(provider ? { provider } : {}),
                    ...(model ? { model } : {}),
                    ...(attachments?.length ? { attachments } : {}),
                }),
            );
        },
        [workspaceId, conversationId],
    );

    return { messages, streamingMessage, sendMessage, isConnected };
}
