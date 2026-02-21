/**
 * REST + WebSocket API client for the Kestrel gateway.
 */

const BASE_URL = '/api';

type RequestOptions = {
    method?: string;
    body?: unknown;
    headers?: Record<string, string>;
};

let accessToken: string | null = null;
let refreshToken: string | null = null;
let onAuthExpired: (() => void) | null = null;

export function setTokens(access: string, refresh: string) {
    accessToken = access;
    refreshToken = refresh;
    localStorage.removeItem('kestrel_access'); // Ensure it's not stored
    localStorage.setItem('kestrel_refresh', refresh);
}

export function loadTokens(): boolean {
    refreshToken = localStorage.getItem('kestrel_refresh');
    return !!refreshToken;
}

export function clearTokens() {
    accessToken = null;
    refreshToken = null;
    localStorage.removeItem('kestrel_access');
    localStorage.removeItem('kestrel_refresh');
}

export function setOnAuthExpired(callback: () => void) {
    onAuthExpired = callback;
}

let refreshPromise: Promise<boolean> | null = null;

async function tryRefresh(): Promise<boolean> {
    if (!refreshToken) return false;

    if (refreshPromise) return refreshPromise;

    refreshPromise = (async () => {
        try {
            const res = await fetch(`${BASE_URL}/auth/refresh`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ refreshToken }),
            });

            if (!res.ok) return false;

            const data = (await res.json()) as { accessToken: string; refreshToken: string };
            setTokens(data.accessToken, data.refreshToken);
            return true;
        } catch {
            return false;
        } finally {
            refreshPromise = null;
        }
    })();

    return refreshPromise;
}

export async function forceRefresh(): Promise<boolean> {
    return tryRefresh();
}

async function request<T = unknown>(url: string, options: RequestOptions = {}): Promise<T> {
    const headers: Record<string, string> = {
        'Content-Type': 'application/json',
        ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
        ...options.headers,
    };

    let res = await fetch(`${BASE_URL}${url}`, {
        method: options.method || 'GET',
        headers,
        body: options.body ? JSON.stringify(options.body) : undefined,
    });

    // Auto-refresh on 401
    if (res.status === 401) {
        if (refreshToken) {
            const refreshed = await tryRefresh();
            if (refreshed) {
                headers.Authorization = `Bearer ${accessToken}`;
                res = await fetch(`${BASE_URL}${url}`, {
                    method: options.method || 'GET',
                    headers,
                    body: options.body ? JSON.stringify(options.body) : undefined,
                });
            } else {
                onAuthExpired?.();
                throw new Error('Session expired');
            }
        } else {
            onAuthExpired?.();
            throw new Error('Session expired');
        }
    }

    if (!res.ok) {
        const error = (await res.json().catch(() => ({ error: res.statusText }))) as {
            error?: string;
        };
        throw new Error(error.error || res.statusText);
    }

    return res.json() as Promise<T>;
}

// ── File Upload ─────────────────────────────────────────────────────
export interface UploadedFile {
    id: string;
    filename: string;
    mimeType: string;
    size: number;
    url: string;
}

export async function uploadFiles(files: File[]): Promise<UploadedFile[]> {
    const formData = new FormData();
    files.forEach((f) => formData.append('file', f));

    const headers: Record<string, string> = {};
    if (accessToken) headers.Authorization = `Bearer ${accessToken}`;

    let res = await fetch(`${BASE_URL}/upload`, {
        method: 'POST',
        headers,
        body: formData,
    });

    // Auto-refresh on 401
    if (res.status === 401 && refreshToken) {
        const refreshed = await tryRefresh();
        if (refreshed) {
            headers.Authorization = `Bearer ${accessToken}`;
            res = await fetch(`${BASE_URL}/upload`, {
                method: 'POST',
                headers,
                body: formData,
            });
        }
    }

    if (!res.ok) {
        const err = (await res.json().catch(() => ({ error: res.statusText }))) as { error?: string };
        throw new Error(err.error || 'Upload failed');
    }

    const data = (await res.json()) as { files: UploadedFile[] };
    return data.files;
}

// ── Auth ────────────────────────────────────────────────────────────
export const auth = {
    register: (email: string, password: string, displayName?: string) =>
        request<{ accessToken: string; refreshToken: string; user: unknown }>('/auth/register', {
            method: 'POST',
            body: { email, password, displayName },
        }),

    login: (email: string, password: string) =>
        request<{
            accessToken: string;
            refreshToken: string;
            user: unknown;
            workspaces: unknown[];
        }>('/auth/login', {
            method: 'POST',
            body: { email, password },
        }),

    logout: () => request('/auth/logout', { method: 'POST', body: { refreshToken } }),

    me: () => request<{ id: string; email: string; displayName: string }>('/auth/me'),

    oauthProviders: () => request<{ providers: string[] }>('/auth/oauth/providers'),
};

// ── Workspaces ──────────────────────────────────────────────────────
export const workspaces = {
    list: () => request<{ workspaces: Workspace[] }>('/workspaces'),
    create: (name: string) => request<Workspace>('/workspaces', { method: 'POST', body: { name } }),
    get: (id: string) => request<Workspace>(`/workspaces/${id}`),
    update: (id: string, data: Partial<Workspace>) =>
        request<Workspace>(`/workspaces/${id}`, { method: 'PUT', body: data }),
    delete: (id: string) => request(`/workspaces/${id}`, { method: 'DELETE' }),
};

// ── Moltbook ────────────────────────────────────────────────────────
export interface MoltbookActivityItem {
    id: string;
    action: string;
    title: string;
    content: string;
    submolt: string;
    post_id: string;
    url: string;
    created_at: string;
}

export const moltbook = {
    getActivity: (workspaceId: string, limit = 20) =>
        request<{ activity: MoltbookActivityItem[] }>(
            `/workspaces/${workspaceId}/moltbook/activity?limit=${limit}`,
        ).then(res => res.activity),
};

// ── Capabilities ────────────────────────────────────────────────────

export interface CapabilityItem {
    name: string;
    description: string;
    status: 'active' | 'available' | 'disabled';
    category: string;
    icon: string;
    stats?: Record<string, string>;
}

export const capabilities = {
    get: (workspaceId: string) =>
        request<{ capabilities: CapabilityItem[] }>(
            `/workspaces/${workspaceId}/capabilities`,
        ).then(res => res.capabilities),
};

// ── Workflows ───────────────────────────────────────────────────────

export interface WorkflowItem {
    id: string;
    name: string;
    description: string;
    icon: string;
    category: string;
    goalTemplate: string;
    tags: string[];
}

export const workflows = {
    list: (category?: string) =>
        request<{ workflows: WorkflowItem[] }>(
            `/workflows${category ? `?category=${category}` : ''}`,
        ).then(res => res.workflows),
};

// ── Conversations ───────────────────────────────────────────────────
// ── Conversations ───────────────────────────────────────────────────
export const conversations = {
    list: (workspaceId: string) =>
        request<{ conversations: Conversation[] }>(`/workspaces/${workspaceId}/conversations`),
    create: (workspaceId: string) =>
        request<{ conversation: Conversation }>(`/workspaces/${workspaceId}/conversations`, {
            method: 'POST',
            body: {},
        }).then((res) => res.conversation),
    messages: (workspaceId: string, conversationId: string) =>
        request<{ messages: Message[] }>(
            `/workspaces/${workspaceId}/conversations/${conversationId}/messages`,
        ),
    delete: (workspaceId: string, conversationId: string) =>
        request<{ success: boolean }>(
            `/workspaces/${workspaceId}/conversations/${conversationId}`,
            { method: 'DELETE' },
        ),
    rename: (workspaceId: string, conversationId: string, title: string) =>
        request<{ conversation: Conversation }>(
            `/workspaces/${workspaceId}/conversations/${conversationId}`,
            {
                method: 'PATCH',
                body: { title },
            },
        ).then((res) => res.conversation),
    generateTitle: (workspaceId: string, conversationId: string) =>
        request<{ title: string }>(
            `/workspaces/${workspaceId}/conversations/${conversationId}/generate-title`,
            {
                method: 'POST',
                body: {},
            },
        ).then((res) => res.title),
};

// ── Providers ───────────────────────────────────────────────────────
export const providers = {
    catalog: () => request<{ providers: ProviderInfo[] }>('/providers'),
    list: (workspaceId: string) => request(`/workspaces/${workspaceId}/providers`),
    listModels: (workspaceId: string, provider: string, apiKey?: string) =>
        request<{ models: { id: string; name: string; context_window: string }[] }>(
            `/workspaces/${workspaceId}/providers/${provider}/models${apiKey ? `?apiKey=${encodeURIComponent(apiKey)}` : ''}`,
        ).then((res) => res.models),
    set: (workspaceId: string, provider: string, config: Record<string, unknown>) =>
        request(`/workspaces/${workspaceId}/providers/${provider}`, {
            method: 'PUT',
            body: config,
        }),
    delete: (workspaceId: string, provider: string) =>
        request(`/workspaces/${workspaceId}/providers/${provider}`, { method: 'DELETE' }),
};

// ── API Keys ────────────────────────────────────────────────────────
export const apiKeys = {
    list: () => request<{ keys: ApiKey[] }>('/api-keys'),
    create: (name: string, expiresInDays?: number) =>
        request<{ id: string; name: string; key: string; expiresAt: string }>('/api-keys', {
            method: 'POST',
            body: { name, expiresInDays },
        }),
    revoke: (id: string) => request(`/api-keys/${id}`, { method: 'DELETE' }),
};

// ── Tools ───────────────────────────────────────────────────────────
export const tools = {
    list: (workspaceId: string) =>
        request<{ tools: ToolInfo[] }>(`/workspaces/${workspaceId}/tools`),
};

export interface ToolInfo {
    name: string;
    description: string;
    category: string;
    riskLevel: string;
    enabled: boolean;
}

// ── Integrations ────────────────────────────────────────────────────
export const integrations = {
    status: (workspaceId: string) =>
        request<{ telegram: { connected: boolean; status: string } }>(`/workspaces/${workspaceId}/integrations/status`),
    connectTelegram: (workspaceId: string, token: string, enabled: boolean) =>
        request<{ success: boolean; status: string }>(
            `/workspaces/${workspaceId}/integrations/telegram`,
            { method: 'POST', body: { token, enabled } }
        ),
    disconnectTelegram: (workspaceId: string) =>
        request<{ success: boolean; status: string }>(
            `/workspaces/${workspaceId}/integrations/telegram`,
            { method: 'DELETE' }
        ),
};

// ── WebSocket ───────────────────────────────────────────────────────
export function createChatSocket(): WebSocket {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws`;
    const ws = new WebSocket(wsUrl);
    ws.addEventListener('open', () => {
        ws.send(JSON.stringify({ type: 'auth', token: accessToken }));
    });
    return ws;
}

// ── Types ───────────────────────────────────────────────────────────
export interface Workspace {
    id: string;
    name: string;
    role: string;
    createdAt: string;
    settings?: Record<string, any>;
}

export interface Conversation {
    id: string;
    title: string;
    createdAt: string;
    updatedAt: string;
}

export interface Message {
    id: string;
    role: 'user' | 'assistant' | 'system';
    content: string;
    createdAt: string;
}

export interface ProviderInfo {
    id: string;
    name: string;
    description: string;
    requiresApiKey: boolean;
    models: string[];
}

export interface ApiKey {
    id: string;
    name: string;
    createdAt: string;
    expiresAt: string;
}

// ── Agent Tasks ─────────────────────────────────────────────────────

export interface TaskEvent {
    type: string;
    taskId: string;
    stepId: string;
    content: string;
    toolName: string;
    toolArgs: string;
    toolResult: string;
    approvalId: string;
    progress: Record<string, string>;
}

export interface TaskSummary {
    id: string;
    goal: string;
    status: string;
    iterations: number;
    toolCalls: number;
    result: string;
    error: string;
    createdAt: string;
    completedAt: string;
}

export interface StartTaskOptions {
    goal: string;
    conversationId?: string;
    guardrails?: {
        maxIterations?: number;
        maxToolCalls?: number;
        maxTokens?: number;
        maxWallTimeSeconds?: number;
        autoApproveRisk?: string;
    };
}

export const tasks = {
    list: (workspaceId: string, status?: string) =>
        request<{ tasks: TaskSummary[] }>(
            `/workspaces/${workspaceId}/tasks${status ? `?status=${status}` : ''}`,
        ),

    /**
     * Start a task via SSE — returns an EventSource that emits TaskEvent objects.
     */
    start: (workspaceId: string, _options: StartTaskOptions): EventSource => {
        // We POST and receive SSE, so we use fetch + ReadableStream
        const url = `${BASE_URL}/workspaces/${workspaceId}/tasks`;
        const eventSource = new EventSource(url); // Fallback — actual impl in hook
        return eventSource;
    },

    approve: (taskId: string, approvalId: string, approved: boolean) =>
        request<{ success: boolean; error?: string }>(`/tasks/${taskId}/approve`, {
            method: 'POST',
            body: { approvalId, approved },
        }),

    cancel: (taskId: string) =>
        request<{ success: boolean }>(`/tasks/${taskId}/cancel`, {
            method: 'POST',
        }),
};
