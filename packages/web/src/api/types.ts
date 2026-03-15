export interface UploadedFile {
    id: string;
    filename: string;
    mimeType: string;
    size: number;
    url: string;
}

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
    channel?: string;
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

export interface WorkflowItem {
    id: string;
    name: string;
    description: string;
    icon: string;
    category: string;
    goalTemplate: string;
    tags: string[];
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

export interface ToolInfo {
    name: string;
    description: string;
    category: string;
    riskLevel: string;
    enabled: boolean;
}

export interface WorkspaceWebhookConfig {
    enabled: boolean;
    endpointUrl: string;
    secret: string;
    selectedEvents: string[];
    maxRetries: number;
    timeoutMs: number;
}

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
