import type {
    ApprovalAuditItem,
    OperatorTaskItem,
    RuntimeProfile,
    TaskArtifactItem,
    TaskCheckpointItem,
    TaskDetail,
    TaskSummary,
    TaskTimelineItem,
} from './types';

export function mapTaskSummary(raw: any): TaskSummary {
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

export function mapOperatorTaskItem(raw: any): OperatorTaskItem {
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

export function mapTaskDetail(raw: any): TaskDetail {
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

export function mapTaskTimelineItem(event: any): TaskTimelineItem {
    return {
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
    };
}

export function mapTaskCheckpointItem(checkpoint: any): TaskCheckpointItem {
    return {
        id: checkpoint?.id || '',
        stepIndex: Number(checkpoint?.step_index ?? checkpoint?.stepIndex ?? 0),
        label: checkpoint?.label || '',
        createdAt: checkpoint?.created_at ?? checkpoint?.createdAt ?? '',
        journalEventId: checkpoint?.journal_event_id ?? checkpoint?.journalEventId ?? '',
    };
}

export function mapTaskArtifactItem(artifact: any): TaskArtifactItem {
    return {
        id: artifact?.id || '',
        title: artifact?.title || '',
        description: artifact?.description || '',
        componentType: artifact?.component_type ?? artifact?.componentType ?? '',
        version: Number(artifact?.version || 0),
        updatedAt: artifact?.updated_at ?? artifact?.updatedAt ?? '',
        createdBy: artifact?.created_by ?? artifact?.createdBy ?? '',
        dataSource: artifact?.data_source ?? artifact?.dataSource ?? '',
    };
}

export function mapApprovalAuditItem(approval: any): ApprovalAuditItem {
    return {
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
    };
}

export function mapRuntimeProfile(raw: any): RuntimeProfile {
    return {
        runtimeMode: raw?.runtime_mode ?? raw?.runtimeMode ?? '',
        policyName: raw?.policy_name ?? raw?.policyName ?? '',
        policyVersion: raw?.policy_version ?? raw?.policyVersion ?? '',
        dockerEnabled: Boolean(raw?.docker_enabled ?? raw?.dockerEnabled),
        nativeEnabled: Boolean(raw?.native_enabled ?? raw?.nativeEnabled),
        hybridFallbackVisible: Boolean(raw?.hybrid_fallback_visible ?? raw?.hybridFallbackVisible),
        hostMounts: (raw?.host_mounts ?? raw?.hostMounts ?? []).map((mount: any) => ({
            path: mount?.path || '',
            mode: mount?.mode || '',
        })),
        subsystems: (raw?.subsystems || []).map((subsystem: any) => ({
            name: subsystem?.name || '',
            status: subsystem?.status || '',
            detail: subsystem?.detail || '',
        })),
        providerRoutes: (raw?.provider_routes ?? raw?.providerRoutes ?? []).map((route: any) => ({
            provider: route?.provider || '',
            model: route?.model || '',
            isDefault: Boolean(route?.is_default ?? route?.isDefault),
            source: route?.source || '',
        })),
        runtimeCapabilities: raw?.runtime_capabilities ?? raw?.runtimeCapabilities ?? {},
    };
}
