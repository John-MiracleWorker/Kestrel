import { useState, useCallback, useRef } from 'react';
import { tasks, type TaskEvent, type StartTaskOptions } from '../api/client';

export type AgentStatus = 'idle' | 'running' | 'waiting_approval' | 'complete' | 'failed';

interface PendingApproval {
    approvalId: string;
    toolName: string;
    toolArgs: string;
    content: string;
}

interface UseAgentReturn {
    status: AgentStatus;
    events: TaskEvent[];
    taskId: string | null;
    thinking: string;
    pendingApproval: PendingApproval | null;
    progress: Record<string, string>;
    error: string | null;
    startTask: (workspaceId: string, options: StartTaskOptions) => void;
    approve: (approved: boolean) => Promise<void>;
    cancel: () => Promise<void>;
}

/**
 * React hook for controlling autonomous agent tasks via SSE.
 */
export function useAgent(): UseAgentReturn {
    const [status, setStatus] = useState<AgentStatus>('idle');
    const [events, setEvents] = useState<TaskEvent[]>([]);
    const [taskId, setTaskId] = useState<string | null>(null);
    const [thinking, setThinking] = useState('');
    const [pendingApproval, setPendingApproval] = useState<PendingApproval | null>(null);
    const [progress, setProgress] = useState<Record<string, string>>({});
    const [error, setError] = useState<string | null>(null);
    const abortRef = useRef<AbortController | null>(null);

    const startTask = useCallback((workspaceId: string, options: StartTaskOptions) => {
        // Reset state
        setStatus('running');
        setEvents([]);
        setTaskId(null);
        setThinking('');
        setPendingApproval(null);
        setProgress({});
        setError(null);

        const abort = new AbortController();
        abortRef.current = abort;

        // POST with SSE response
        const token = localStorage.getItem('kestrel_access');
        fetch(`/api/workspaces/${workspaceId}/tasks`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                ...(token ? { Authorization: `Bearer ${token}` } : {}),
            },
            body: JSON.stringify(options),
            signal: abort.signal,
        })
            .then(async (res) => {
                if (!res.ok) {
                    const err = await res.json().catch(() => ({ error: 'Task failed' }));
                    setError(err.error || 'Task failed');
                    setStatus('failed');
                    return;
                }

                const reader = res.body?.getReader();
                if (!reader) return;

                const decoder = new TextDecoder();
                let buffer = '';

                while (true) {
                    const { done, value } = await reader.read();
                    if (done) break;

                    buffer += decoder.decode(value, { stream: true });
                    const lines = buffer.split('\n');
                    buffer = lines.pop() || '';

                    for (const line of lines) {
                        if (line.startsWith('data: ')) {
                            try {
                                const event: TaskEvent = JSON.parse(line.slice(6));
                                handleEvent(event);
                            } catch {
                                // Skip invalid JSON
                            }
                        }
                        if (line.startsWith('event: done')) {
                            setStatus('complete');
                        }
                        if (line.startsWith('event: error')) {
                            setStatus('failed');
                        }
                    }
                }
            })
            .catch((err) => {
                if (err.name !== 'AbortError') {
                    setError(err.message);
                    setStatus('failed');
                }
            });
    }, []);

    function handleEvent(event: TaskEvent) {
        setEvents((prev) => [...prev, event]);
        if (event.taskId) setTaskId(event.taskId);
        if (event.progress) setProgress(event.progress);

        switch (event.type) {
            case 'THINKING':
            case '6': // enum value
                setThinking(event.content);
                break;

            case 'APPROVAL_NEEDED':
            case '5':
                setStatus('waiting_approval');
                setPendingApproval({
                    approvalId: event.approvalId,
                    toolName: event.toolName,
                    toolArgs: event.toolArgs,
                    content: event.content,
                });
                break;

            case 'TASK_COMPLETE':
            case '7':
                setStatus('complete');
                break;

            case 'TASK_FAILED':
            case '8':
                setStatus('failed');
                setError(event.content);
                break;
        }
    }

    const approve = useCallback(
        async (approved: boolean) => {
            if (!taskId || !pendingApproval) return;
            await tasks.approve(taskId, pendingApproval.approvalId, approved);
            setPendingApproval(null);
            setStatus('running');
        },
        [taskId, pendingApproval],
    );

    const cancel = useCallback(async () => {
        abortRef.current?.abort();
        if (taskId) {
            await tasks.cancel(taskId);
        }
        setStatus('idle');
    }, [taskId]);

    return {
        status,
        events,
        taskId,
        thinking,
        pendingApproval,
        progress,
        error,
        startTask,
        approve,
        cancel,
    };
}
