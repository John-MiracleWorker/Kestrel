/**
 * REST + WebSocket API client for the Kestrel gateway.
 */

import { isTauri } from '@tauri-apps/api/core';

const GATEWAY_ORIGIN = 'http://localhost:8741';
const BASE_URL = isTauri() ? `${GATEWAY_ORIGIN}/api` : '/api';

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

export function getAccessToken(): string | null {
    return accessToken;
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

export async function request<T = unknown>(url: string, options: RequestOptions = {}): Promise<T> {
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
        const err = (await res.json().catch(() => ({ error: res.statusText }))) as {
            error?: string;
        };
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
        ).then((res) => res.activity),
};

// ── Capabilities ────────────────────────────────────────────────────

export interface CapabilityItem {
    id: string;
    name: string;
    description: string;
    status: 'active' | 'available' | 'disabled';
    category: string;
    icon: string;
    stats?: Record<string, string>;
    installed?: boolean;
    enabled?: boolean;
    requires_mcp?: string[];
}

export const capabilities = {
    get: (workspaceId: string) =>
        request<{ capabilities: CapabilityItem[] }>(`/workspaces/${workspaceId}/capabilities`).then(
            (res) => res.capabilities,
        ),
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
        ).then((res) => res.workflows),
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
    list: (workspaceId: string) =>
        request<{ keys: ApiKey[] }>(`/workspaces/${workspaceId}/api-keys`),
    create: (
        workspaceId: string,
        name: string,
        options: { expiresInDays?: number; role?: ApiKey['role'] } = {},
    ) =>
        request<{
            id: string;
            name: string;
            role: ApiKey['role'];
            workspaceId: string;
            key: string;
            expiresAt: string;
        }>(`/workspaces/${workspaceId}/api-keys`, {
            method: 'POST',
            body: {
                name,
                expiresInDays: options.expiresInDays,
                role: options.role || 'member',
            },
        }),
    revoke: (workspaceId: string, id: string) =>
        request(`/workspaces/${workspaceId}/api-keys/${id}`, { method: 'DELETE' }),
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
        request<{
            telegram: {
                connected: boolean;
                status: string;
                botId?: number;
                botUsername?: string;
                tokenConfigured?: boolean;
            };
            discord: { connected: boolean; status: string };
            whatsapp: { connected: boolean; status: string };
        }>(`/workspaces/${workspaceId}/integrations/status`),
    connectTelegram: (workspaceId: string, token: string, enabled: boolean) =>
        request<{ success: boolean; status: string; botId?: number; botUsername?: string }>(
            `/workspaces/${workspaceId}/integrations/telegram`,
            { method: 'POST', body: { token, enabled } },
        ),
    disconnectTelegram: (workspaceId: string) =>
        request<{ success: boolean; status: string }>(
            `/workspaces/${workspaceId}/integrations/telegram`,
            { method: 'DELETE' },
        ),
};

export interface WorkspaceWebhookConfig {
    enabled: boolean;
    endpointUrl: string;
    secret: string;
    selectedEvents: string[];
    maxRetries: number;
    timeoutMs: number;
}

export const webhooks = {
    getConfig: (workspaceId: string) =>
        request<{ webhook: WorkspaceWebhookConfig; supportedEvents: string[] }>(
            `/workspaces/${workspaceId}/webhooks/config`,
        ),
    saveConfig: (workspaceId: string, webhook: WorkspaceWebhookConfig) =>
        request<{ success: boolean; webhook: WorkspaceWebhookConfig }>(
            `/workspaces/${workspaceId}/webhooks/config`,
            { method: 'PUT', body: webhook },
        ),
    testConnection: (workspaceId: string) =>
        request<{
            success: boolean;
            delivery: { success: boolean; statusCode?: number; error?: string; attempt: number };
        }>(`/workspaces/${workspaceId}/webhooks/test`, { method: 'POST' }),
};

// ── WebSocket ───────────────────────────────────────────────────────
export function createChatSocket(): WebSocket {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = isTauri() ? 'ws://localhost:8741/ws' : `${protocol}//${window.location.host}/ws`;
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
    channel?: string; // "web", "telegram", "discord", etc.
}

export interface Message {
    id: string;
    role: 'user' | 'assistant' | 'system';
    content: string;
    createdAt: string;
    routingInfo?: {
        provider: string;
        model: string;
        wasEscalated: boolean;
        complexity: number;
    };
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
    role: 'admin' | 'member' | 'guest';
    workspaceId: string;
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
    metadata?: Record<string, unknown>;
    metrics?: Record<string, unknown>;
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

export interface OperatorTaskItem {
    summary: TaskSummary;
    pendingApprovalCount: number;
    stale: boolean;
    orphaned: boolean;
    currentStep: string;
    totalSteps: string;
    leaseExpiresAt: string;
    queueStatus: string;
    conversationId: string;
    sessionChannel: string;
    externalConversationId: string;
    latestReceiptId: string;
}

export interface ExecutionTraceSummary {
    runtimeClass: string;
    riskClass: string;
    fallbackSummary: string;
    recentTools: string[];
    lastEventAt: string;
}

export interface TaskArtifactRef {
    id: string;
    title: string;
    componentType: string;
    version: number;
    updatedAt: string;
    dataSource: string;
}

export interface RecoveryHint {
    code: string;
    title: string;
    description: string;
}

export interface ReceiptSummary {
    receiptId: string;
    toolName: string;
    stepId: string;
    runtimeClass: string;
    riskClass: string;
    failureClass: string;
    logsPointer: string;
    exitCode: number;
    auditSummary: string;
    artifactManifestJson: string;
    createdAt: string;
}

export interface VerifierEvidenceReference {
    id: string;
    claimText: string;
    verdict: string;
    confidence: number;
    rationale: string;
    supportingReceiptIdsJson: string;
    artifactRefsJson: string;
    createdAt: string;
}

export interface SessionProvenance {
    sessionId: string;
    channel: string;
    externalConversationId: string;
    externalThreadId: string;
    returnRouteJson: string;
    metadataJson: string;
}

export interface TaskDetail {
    id: string;
    goal: string;
    status: string;
    iterations: number;
    toolCalls: number;
    result: string;
    error: string;
    createdAt: string;
    completedAt: string;
    workspaceId: string;
    userId: string;
    conversationId: string;
    currentStep: string;
    totalSteps: string;
    pendingApprovalId: string;
    pendingApprovalTool: string;
    lastCheckpointId: string;
    lastCheckpointLabel: string;
    lastCheckpointAt: string;
    execution: ExecutionTraceSummary;
    artifactRefs: TaskArtifactRef[];
    stale: boolean;
    orphaned: boolean;
    recoveryHints: RecoveryHint[];
    receipts: ReceiptSummary[];
    verifierEvidence: VerifierEvidenceReference[];
    session: SessionProvenance;
}

export interface TaskTimelineItem {
    type: string;
    taskId: string;
    stepId: string;
    content: string;
    toolName: string;
    toolArgs: string;
    toolResult: string;
    approvalId: string;
    progress: Record<string, string>;
    eventMetadataJson: string;
    metricsJson: string;
    createdAt: string;
    journalEventId: string;
    receiptId: string;
    verifierEvidenceIdsJson: string;
}

export interface TaskCheckpointItem {
    id: string;
    stepIndex: number;
    label: string;
    createdAt: string;
    journalEventId: string;
}

export interface TaskArtifactItem {
    id: string;
    title: string;
    description: string;
    componentType: string;
    version: number;
    updatedAt: string;
    createdBy: string;
    dataSource: string;
}

export interface ApprovalAuditItem {
    approvalId: string;
    taskId: string;
    stepId: string;
    toolName: string;
    reason: string;
    riskLevel: string;
    status: string;
    decidedBy: string;
    decidedAt: string;
    createdAt: string;
    toolArgsJson: string;
    capabilityGrantsJson: string;
    receiptId: string;
}

export interface RuntimeProfile {
    runtimeMode: string;
    policyName: string;
    policyVersion: string;
    dockerEnabled: boolean;
    nativeEnabled: boolean;
    hybridFallbackVisible: boolean;
    hostMounts: Array<{ path: string; mode: string }>;
    subsystems: Array<{ name: string; status: string; detail: string }>;
    providerRoutes: Array<{ provider: string; model: string; isDefault: boolean; source: string }>;
    runtimeCapabilities: Record<string, string>;
}

function mapTaskSummary(raw: any): TaskSummary {
    return {
        id: raw?.id || '',
        goal: raw?.goal || '',
        status: raw?.status || '',
        iterations: Number(raw?.iterations || 0),
        toolCalls: Number(raw?.tool_calls ?? raw?.toolCalls ?? 0),
        result: raw?.result || '',
        error: raw?.error || '',
        createdAt: raw?.created_at ?? raw?.createdAt ?? '',
        completedAt: raw?.completed_at ?? raw?.completedAt ?? '',
    };
}

function mapOperatorTaskItem(raw: any): OperatorTaskItem {
    return {
        summary: mapTaskSummary(raw?.summary || {}),
        pendingApprovalCount: Number(raw?.pending_approval_count ?? raw?.pendingApprovalCount ?? 0),
        stale: Boolean(raw?.stale),
        orphaned: Boolean(raw?.orphaned),
        currentStep: raw?.current_step ?? raw?.currentStep ?? '',
        totalSteps: raw?.total_steps ?? raw?.totalSteps ?? '',
        leaseExpiresAt: raw?.lease_expires_at ?? raw?.leaseExpiresAt ?? '',
        queueStatus: raw?.queue_status ?? raw?.queueStatus ?? '',
        conversationId: raw?.conversation_id ?? raw?.conversationId ?? '',
        sessionChannel: raw?.session_channel ?? raw?.sessionChannel ?? '',
        externalConversationId: raw?.external_conversation_id ?? raw?.externalConversationId ?? '',
        latestReceiptId: raw?.latest_receipt_id ?? raw?.latestReceiptId ?? '',
    };
}

function mapTaskDetail(raw: any): TaskDetail {
    return {
        id: raw?.id || '',
        goal: raw?.goal || '',
        status: raw?.status || '',
        iterations: Number(raw?.iterations || 0),
        toolCalls: Number(raw?.tool_calls ?? raw?.toolCalls ?? 0),
        result: raw?.result || '',
        error: raw?.error || '',
        createdAt: raw?.created_at ?? raw?.createdAt ?? '',
        completedAt: raw?.completed_at ?? raw?.completedAt ?? '',
        workspaceId: raw?.workspace_id ?? raw?.workspaceId ?? '',
        userId: raw?.user_id ?? raw?.userId ?? '',
        conversationId: raw?.conversation_id ?? raw?.conversationId ?? '',
        currentStep: raw?.current_step ?? raw?.currentStep ?? '',
        totalSteps: raw?.total_steps ?? raw?.totalSteps ?? '',
        pendingApprovalId: raw?.pending_approval_id ?? raw?.pendingApprovalId ?? '',
        pendingApprovalTool: raw?.pending_approval_tool ?? raw?.pendingApprovalTool ?? '',
        lastCheckpointId: raw?.last_checkpoint_id ?? raw?.lastCheckpointId ?? '',
        lastCheckpointLabel: raw?.last_checkpoint_label ?? raw?.lastCheckpointLabel ?? '',
        lastCheckpointAt: raw?.last_checkpoint_at ?? raw?.lastCheckpointAt ?? '',
        execution: {
            runtimeClass: raw?.execution?.runtime_class ?? raw?.execution?.runtimeClass ?? '',
            riskClass: raw?.execution?.risk_class ?? raw?.execution?.riskClass ?? '',
            fallbackSummary:
                raw?.execution?.fallback_summary ?? raw?.execution?.fallbackSummary ?? '',
            recentTools: raw?.execution?.recent_tools ?? raw?.execution?.recentTools ?? [],
            lastEventAt: raw?.execution?.last_event_at ?? raw?.execution?.lastEventAt ?? '',
        },
        artifactRefs: (raw?.artifact_refs ?? raw?.artifactRefs ?? []).map((artifact: any) => ({
            id: artifact?.id || '',
            title: artifact?.title || '',
            componentType: artifact?.component_type ?? artifact?.componentType ?? '',
            version: Number(artifact?.version || 0),
            updatedAt: artifact?.updated_at ?? artifact?.updatedAt ?? '',
            dataSource: artifact?.data_source ?? artifact?.dataSource ?? '',
        })),
        stale: Boolean(raw?.stale),
        orphaned: Boolean(raw?.orphaned),
        recoveryHints: (raw?.recovery_hints ?? raw?.recoveryHints ?? []).map((hint: any) => ({
            code: hint?.code || '',
            title: hint?.title || '',
            description: hint?.description || '',
        })),
        receipts: (raw?.receipts || []).map((receipt: any) => ({
            receiptId: receipt?.receipt_id ?? receipt?.receiptId ?? '',
            toolName: receipt?.tool_name ?? receipt?.toolName ?? '',
            stepId: receipt?.step_id ?? receipt?.stepId ?? '',
            runtimeClass: receipt?.runtime_class ?? receipt?.runtimeClass ?? '',
            riskClass: receipt?.risk_class ?? receipt?.riskClass ?? '',
            failureClass: receipt?.failure_class ?? receipt?.failureClass ?? '',
            logsPointer: receipt?.logs_pointer ?? receipt?.logsPointer ?? '',
            exitCode: Number(receipt?.exit_code ?? receipt?.exitCode ?? 0),
            auditSummary: receipt?.audit_summary ?? receipt?.auditSummary ?? '',
            artifactManifestJson:
                receipt?.artifact_manifest_json ?? receipt?.artifactManifestJson ?? '[]',
            createdAt: receipt?.created_at ?? receipt?.createdAt ?? '',
        })),
        verifierEvidence: (raw?.verifier_evidence ?? raw?.verifierEvidence ?? []).map(
            (evidence: any) => ({
                id: evidence?.id || '',
                claimText: evidence?.claim_text ?? evidence?.claimText ?? '',
                verdict: evidence?.verdict || '',
                confidence: Number(evidence?.confidence ?? 0),
                rationale: evidence?.rationale || '',
                supportingReceiptIdsJson:
                    evidence?.supporting_receipt_ids_json ??
                    evidence?.supportingReceiptIdsJson ??
                    '[]',
                artifactRefsJson:
                    evidence?.artifact_refs_json ?? evidence?.artifactRefsJson ?? '[]',
                createdAt: evidence?.created_at ?? evidence?.createdAt ?? '',
            }),
        ),
        session: {
            sessionId: raw?.session?.session_id ?? raw?.session?.sessionId ?? '',
            channel: raw?.session?.channel ?? '',
            externalConversationId:
                raw?.session?.external_conversation_id ??
                raw?.session?.externalConversationId ??
                '',
            externalThreadId:
                raw?.session?.external_thread_id ?? raw?.session?.externalThreadId ?? '',
            returnRouteJson:
                raw?.session?.return_route_json ?? raw?.session?.returnRouteJson ?? '{}',
            metadataJson: raw?.session?.metadata_json ?? raw?.session?.metadataJson ?? '{}',
        },
    };
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
        ).then((res) => (res.tasks || []).map(mapTaskSummary)),

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

export const operations = {
    listTasks: (workspaceId: string, status?: string) =>
        request<{ tasks: OperatorTaskItem[] }>(
            `/workspaces/${workspaceId}/operations/tasks${status ? `?status=${encodeURIComponent(status)}` : ''}`,
        ).then((res) => (res.tasks || []).map(mapOperatorTaskItem)),
    getTaskDetail: (workspaceId: string, taskId: string) =>
        request<{ task: TaskDetail }>(`/workspaces/${workspaceId}/operations/tasks/${taskId}`).then(
            (res) => mapTaskDetail(res.task),
        ),
    listTimeline: (workspaceId: string, taskId: string) =>
        request<{ events: TaskTimelineItem[] }>(
            `/workspaces/${workspaceId}/operations/tasks/${taskId}/timeline`,
        ).then((res) =>
            (res.events || []).map((event: any) => ({
                type: event?.type || '',
                taskId: event?.task_id ?? event?.taskId ?? '',
                stepId: event?.step_id ?? event?.stepId ?? '',
                content: event?.content || '',
                toolName: event?.tool_name ?? event?.toolName ?? '',
                toolArgs: event?.tool_args ?? event?.toolArgs ?? '',
                toolResult: event?.tool_result ?? event?.toolResult ?? '',
                approvalId: event?.approval_id ?? event?.approvalId ?? '',
                progress: event?.progress || {},
                eventMetadataJson: event?.event_metadata_json ?? event?.eventMetadataJson ?? '',
                metricsJson: event?.metrics_json ?? event?.metricsJson ?? '',
                createdAt: event?.created_at ?? event?.createdAt ?? '',
                journalEventId: event?.journal_event_id ?? event?.journalEventId ?? '',
                receiptId: event?.receipt_id ?? event?.receiptId ?? '',
                verifierEvidenceIdsJson:
                    event?.verifier_evidence_ids_json ?? event?.verifierEvidenceIdsJson ?? '[]',
            })),
        ),
    listCheckpoints: (workspaceId: string, taskId: string) =>
        request<{ checkpoints: TaskCheckpointItem[] }>(
            `/workspaces/${workspaceId}/operations/tasks/${taskId}/checkpoints`,
        ).then((res) =>
            (res.checkpoints || []).map((checkpoint: any) => ({
                id: checkpoint?.id || '',
                stepIndex: Number(checkpoint?.step_index ?? checkpoint?.stepIndex ?? 0),
                label: checkpoint?.label || '',
                createdAt: checkpoint?.created_at ?? checkpoint?.createdAt ?? '',
                journalEventId: checkpoint?.journal_event_id ?? checkpoint?.journalEventId ?? '',
            })),
        ),
    listArtifacts: (workspaceId: string, taskId?: string) =>
        request<{ artifacts: TaskArtifactItem[] }>(
            taskId
                ? `/workspaces/${workspaceId}/operations/tasks/${taskId}/artifacts`
                : `/workspaces/${workspaceId}/operations/artifacts`,
        ).then((res) =>
            (res.artifacts || []).map((artifact: any) => ({
                id: artifact?.id || '',
                title: artifact?.title || '',
                description: artifact?.description || '',
                componentType: artifact?.component_type ?? artifact?.componentType ?? '',
                version: Number(artifact?.version || 0),
                updatedAt: artifact?.updated_at ?? artifact?.updatedAt ?? '',
                createdBy: artifact?.created_by ?? artifact?.createdBy ?? '',
                dataSource: artifact?.data_source ?? artifact?.dataSource ?? '',
            })),
        ),
    listApprovals: (workspaceId: string, options: { taskId?: string; status?: string } = {}) => {
        const search = new URLSearchParams();
        if (options.taskId) search.set('taskId', options.taskId);
        if (options.status) search.set('status', options.status);
        const suffix = search.toString() ? `?${search.toString()}` : '';
        return request<{ approvals: ApprovalAuditItem[] }>(
            `/workspaces/${workspaceId}/operations/approvals${suffix}`,
        ).then((res) =>
            (res.approvals || []).map((approval: any) => ({
                approvalId: approval?.approval_id ?? approval?.approvalId ?? '',
                taskId: approval?.task_id ?? approval?.taskId ?? '',
                stepId: approval?.step_id ?? approval?.stepId ?? '',
                toolName: approval?.tool_name ?? approval?.toolName ?? '',
                reason: approval?.reason || '',
                riskLevel: approval?.risk_level ?? approval?.riskLevel ?? '',
                status: approval?.status || '',
                decidedBy: approval?.decided_by ?? approval?.decidedBy ?? '',
                decidedAt: approval?.decided_at ?? approval?.decidedAt ?? '',
                createdAt: approval?.created_at ?? approval?.createdAt ?? '',
                toolArgsJson: approval?.tool_args_json ?? approval?.toolArgsJson ?? '',
                capabilityGrantsJson:
                    approval?.capability_grants_json ?? approval?.capabilityGrantsJson ?? '[]',
                receiptId: approval?.receipt_id ?? approval?.receiptId ?? '',
            })),
        );
    },
    getRuntimeProfile: (workspaceId: string) =>
        request<{ profile: any }>(`/workspaces/${workspaceId}/operations/runtime-profile`).then(
            (res) => ({
                runtimeMode: res.profile?.runtime_mode ?? res.profile?.runtimeMode ?? '',
                policyName: res.profile?.policy_name ?? res.profile?.policyName ?? '',
                policyVersion: res.profile?.policy_version ?? res.profile?.policyVersion ?? '',
                dockerEnabled: Boolean(res.profile?.docker_enabled ?? res.profile?.dockerEnabled),
                nativeEnabled: Boolean(res.profile?.native_enabled ?? res.profile?.nativeEnabled),
                hybridFallbackVisible: Boolean(
                    res.profile?.hybrid_fallback_visible ?? res.profile?.hybridFallbackVisible,
                ),
                hostMounts: (res.profile?.host_mounts ?? res.profile?.hostMounts ?? []).map(
                    (mount: any) => ({
                        path: mount?.path || '',
                        mode: mount?.mode || '',
                    }),
                ),
                subsystems: (res.profile?.subsystems || []).map((subsystem: any) => ({
                    name: subsystem?.name || '',
                    status: subsystem?.status || '',
                    detail: subsystem?.detail || '',
                })),
                providerRoutes: (
                    res.profile?.provider_routes ??
                    res.profile?.providerRoutes ??
                    []
                ).map((route: any) => ({
                    provider: route?.provider || '',
                    model: route?.model || '',
                    isDefault: Boolean(route?.is_default ?? route?.isDefault),
                    source: route?.source || '',
                })),
                runtimeCapabilities:
                    res.profile?.runtime_capabilities ?? res.profile?.runtimeCapabilities ?? {},
            }),
        ),
};

// ── UI Artifacts ────────────────────────────────────────────────────

export interface UIArtifactItem {
    id: string;
    title: string;
    description: string;
    component_type: 'react' | 'html' | 'markdown';
    component_code: string;
    props_schema: Record<string, unknown>;
    data_source: string;
    version: number;
    created_at: string;
    updated_at: string;
}

export const uiArtifacts = {
    list: (workspaceId: string) =>
        request<{ artifacts: UIArtifactItem[] }>(`/workspaces/${workspaceId}/ui-artifacts`).then(
            (res) => res.artifacts,
        ),

    get: (workspaceId: string, artifactId: string) =>
        request<UIArtifactItem>(`/workspaces/${workspaceId}/ui-artifacts/${artifactId}`),

    update: (workspaceId: string, artifactId: string, instruction: string) =>
        request<UIArtifactItem>(`/workspaces/${workspaceId}/ui-artifacts/${artifactId}`, {
            method: 'PUT',
            body: { instruction },
        }),
};
