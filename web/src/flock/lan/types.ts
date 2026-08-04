export type LanScanStatus =
  | "draft"
  | "running"
  | "cancelling"
  | "cancelled"
  | "completed"
  | "failed"
  | "interrupted";

export type LanClientScanStatus = LanScanStatus | "unknown";
export type LanEventSequence = string;
export type LanObservationSource = "mdns" | "active" | "manual";
export type LanMdnsStatus = "available" | "unavailable" | "timed_out";
export type LanCancelReason = "owner_cancelled" | "shutdown_cancelled";
export type LanApiShape = "ollama_compatible" | "openai_compatible";
export type LanReachability = "not_attempted" | "unreachable" | "reachable";
export type LanTransportSecurity = "plain_http";
export type LanFailureCategory =
  | "cancelled"
  | "scan_deadline_exceeded"
  | "interface_drift"
  | "interface_pinning_unavailable"
  | "tcp_timeout"
  | "tcp_refused"
  | "tcp_unreachable"
  | "tcp_error"
  | "http_timeout"
  | "http_protocol_rejected"
  | "redirect_rejected"
  | "response_too_large"
  | "unsupported_content_encoding"
  | "http_status_rejected"
  | "catalog_not_found"
  | "catalog_invalid"
  | "catalog_empty"
  | "generation_request_failed"
  | "generation_response_invalid";

export type LanCapabilityName =
  | "generation"
  | "streaming"
  | "structured_output"
  | "tools"
  | "vision";

export type LanGenerationCapabilityEvidence =
  | Readonly<{
      capability: "generation";
      supported: true;
      provenance: "observed";
      status: "observed_pass";
    }>
  | Readonly<{
      capability: "generation";
      supported: null;
      provenance: "observed";
      status: "observed_failure";
    }>
  | Readonly<{
      capability: "generation";
      supported: null;
      provenance: "not_run";
      status: "not_run";
    }>;

export type LanNotRunCapabilityEvidence<
  Capability extends Exclude<LanCapabilityName, "generation">,
> = Readonly<{
  capability: Capability;
  supported: null;
  provenance: "not_run";
  status: "not_run";
}>;

export type LanCapabilityEvidenceTuple = readonly [
  LanGenerationCapabilityEvidence,
  LanNotRunCapabilityEvidence<"streaming">,
  LanNotRunCapabilityEvidence<"structured_output">,
  LanNotRunCapabilityEvidence<"tools">,
  LanNotRunCapabilityEvidence<"vision">,
];

type LanDurableObservationEvidenceBase = Readonly<{
  schema: "kestrel.lan.durable-observation.v1";
  endpoint_binding_digest: string;
  observation_digest: string;
  reachability: LanReachability;
  transport_security: LanTransportSecurity | null;
  api_shape: LanApiShape | null;
  catalog_complete: boolean;
  catalog_truncated: boolean;
  model_ids: readonly string[];
  capability_route: "/api/generate" | "/v1/chat/completions" | null;
  selected_model_id: string | null;
  capabilities: LanCapabilityEvidenceTuple;
  failure_category: LanFailureCategory | null;
}>;

export type LanDurableObservationEvidence =
  | (LanDurableObservationEvidenceBase &
      Readonly<{
        observation_source?: never;
        endpoint_kind?: never;
      }>)
  | (LanDurableObservationEvidenceBase &
      Readonly<{
        observation_source: "manual";
        endpoint_kind: "manual";
      }>);

export type LanLegacyObservationEvidence = Readonly<{
  service?: string;
  service_version?: string;
  model_count?: number;
  model_ids?: string[];
  capabilities?: string[];
  metadata?: Readonly<{
    display_name?: string;
    vendor?: string;
    product?: string;
    description?: string;
  }>;
}>;

export type LanObservationPublicEvidence =
  | LanDurableObservationEvidence
  | LanLegacyObservationEvidence;

export type LanInterface = Readonly<{
  interface_id: string;
  display_name: string;
  addresses: string[];
}>;

export type LanAutomaticScanLimits = Readonly<{
  known_model_service_ports: number[];
  max_active_hosts: number;
  max_scan_concurrency: number;
  tcp_connect_timeout_seconds: number;
  http_probe_timeout_seconds: number;
  total_scan_deadline_seconds: number;
  max_probe_response_bytes: number;
  max_discovered_models: number;
  mdns_window_seconds: number;
}>;

export type LanManualScanLimits = Readonly<{
  mode: "manual";
  exact_port: number;
  max_active_hosts: 1;
  max_scan_concurrency: 1;
  tcp_connect_timeout_seconds: number;
  http_probe_timeout_seconds: number;
  total_scan_deadline_seconds: number;
  max_probe_response_bytes: number;
  max_discovered_models: number;
  mdns_enabled: false;
}>;

export type LanScanLimits = LanAutomaticScanLimits | LanManualScanLimits;

export type LanScopePreview = Readonly<{
  interface_id: string;
  network: string;
  limits: LanAutomaticScanLimits;
  active_host_count: number;
  passive_or_manual_only: boolean;
  port_count: number;
  mdns_status: LanMdnsStatus;
  server_version: string;
  contract_version: string;
  preview_digest: string;
  issued_at: string;
  expires_at: string;
}>;

export type LanManualPreview = Readonly<{
  schema: "kestrel.lan.manual-preview.v1";
  interface_id: string;
  port: number;
  resolved_addresses: string[];
  preview_digest: string;
  issued_at: string;
  expires_at: string;
  server_version: string;
  contract_version: string;
  requires_confirmation: true;
}>;

export type LanScan = Readonly<{
  scan_id: string;
  status: LanScanStatus;
  revision: number;
  confirmed_interface_id: string;
  network: string;
  limits: LanScanLimits;
  limits_digest: string;
  preview_digest: string;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  finished_at: string | null;
  cancel_reason: LanCancelReason | null;
  terminal_reason:
    | "scan_complete"
    | "owner_cancelled"
    | "shutdown_cancelled"
    | "worker_error"
    | "deadline_expired"
    | "startup_interrupted"
    | "worker_interrupted"
    | null;
  candidate_count: number | null;
  error_count: number | null;
  timeout_count: number | null;
  terminal_receipt_digest: string | null;
}>;

type LanObservationBase = Readonly<{
  scan_id: string;
  endpoint_id: string;
  source: LanObservationSource;
  interface_id: string;
  address: string;
  port: number;
  tls_enabled: boolean;
  certificate_sha256: string | null;
  freshness_timestamp: string;
  created_at: string;
}>;

export type LanObservation =
  | (LanObservationBase &
      Readonly<{
        evidence_kind: "durable";
        api_shape: LanApiShape | null;
        catalog_digest: string;
        capability_digest: string;
        public_payload: LanDurableObservationEvidence;
        error_category: LanFailureCategory | null;
      }>)
  | (LanObservationBase &
      Readonly<{
        evidence_kind: "legacy";
        api_shape: string | null;
        catalog_digest: string | null;
        capability_digest: string | null;
        public_payload: LanLegacyObservationEvidence;
        error_category: string | null;
      }>);

export type LanObservationCursor = string;

export type LanScanPageOptions = Readonly<{
  signal?: AbortSignal;
  cursor?: LanObservationCursor;
}>;

export type LanScanDetail = LanScan &
  Readonly<{
    observations: LanObservation[];
    observation_total_count: number;
    observations_truncated: boolean;
    observation_next_cursor: LanObservationCursor | null;
  }>;

export type LanScanProgressPayload = Readonly<{
  planned_count: number;
  admitted_count: number;
  completed_count: number;
  persisted_observation_count: number;
  error_category_counts: Readonly<
    Partial<Record<LanFailureCategory, number>>
  >;
  timeout_count: number;
  mdns_status: LanMdnsStatus;
}>;

export type LanAutomaticScanStartedPayload = Readonly<{
  interface_id: string;
  network: string;
  limits: LanAutomaticScanLimits;
  active_host_count: number;
  passive_or_manual_only: boolean;
  port_count: number;
  mdns_status: LanMdnsStatus;
  server_version: string;
  contract_version: string;
  preview_digest: string;
  expires_at: string;
}>;

export type LanManualScanStartedPayload = Readonly<{
  mode: "manual";
  endpoint_kind: "manual";
  observation_source: "manual";
  interface_id: string;
  network: string;
  limits: LanManualScanLimits;
  active_host_count: 1;
  passive_or_manual_only: true;
  port_count: 1;
  exact_port: number;
  mdns_status: "unavailable";
  server_version: string;
  contract_version: string;
  preview_digest: string;
  expires_at: string;
  confirmed: true;
  privacy_acknowledged: true;
}>;

export type LanScanEventType =
  | "scan_started"
  | "scan_progress"
  | "scan_cancel_requested"
  | "scan_completed"
  | "scan_cancelled"
  | "scan_failed"
  | "scan_interrupted";

type LanEvent<EventType extends LanScanEventType, Payload> = Readonly<{
  scan_id: string;
  sequence: LanEventSequence;
  event_type: EventType;
  payload: Payload;
  created_at: string;
}>;

export type LanScanEvent =
  | LanEvent<
      "scan_started",
      LanAutomaticScanStartedPayload | LanManualScanStartedPayload
    >
  | LanEvent<"scan_progress", LanScanProgressPayload>
  | LanEvent<
      "scan_cancel_requested",
      Readonly<{ reason: LanCancelReason }>
    >
  | LanEvent<
      "scan_completed",
      Readonly<{
        status: "completed";
        terminal_reason: "scan_complete";
        cancel_reason: null;
      }>
    >
  | LanEvent<
      "scan_cancelled",
      Readonly<{
        status: "cancelled";
        terminal_reason: LanCancelReason;
        cancel_reason: LanCancelReason;
      }>
    >
  | LanEvent<
      "scan_failed",
      Readonly<
        | {
            status: "failed";
            terminal_reason: "worker_error";
            cancel_reason: LanCancelReason | null;
          }
        | {
            status: "failed";
            terminal_reason: "deadline_expired";
            cancel_reason: null;
          }
      >
    >
  | LanEvent<
      "scan_interrupted",
      Readonly<{
        status: "interrupted";
        terminal_reason: "startup_interrupted" | "worker_interrupted";
        cancel_reason: LanCancelReason | null;
      }>
    >;

export type PreviewLanScopeInput = Readonly<{
  interfaceId: string;
  network: string;
}>;

export type PreviewManualLanProbeInput = Readonly<{
  interfaceId: string;
  host: string;
  port: number;
}>;

export type ConfirmManualLanProbeInput = Readonly<{
  expectedRevision: 0;
  previewDigest: string;
  selectedAddress: string;
  confirmed: true;
  privacyAcknowledged: true;
}>;

export type CreateLanScanInput = Readonly<{
  previewDigest: string;
  expectedRevision: 0;
  confirmed: true;
}>;

export type StartLanScanInput = Readonly<{
  scanId: string;
  expectedRevision: number;
  previewDigest: string;
  confirmed: true;
}>;

export type CancelLanScanInput = Readonly<{
  scanId: string;
  expectedRevision: number;
}>;

export type LanScanEventStreamOptions = Readonly<{
  afterSequence: LanEventSequence;
  signal: AbortSignal;
  onEvent: (event: LanScanEvent) => void;
}>;

export type {
  LanExpectedRevision,
  LanImportAuthority,
  LanImportConfirmation,
  LanImportConfirmationResult,
  LanImportPreview,
  LanImportResult,
  LanImportSelector,
  LanImportSelectorProjection,
  LanReplacementConfirmation,
  LanStaleReason,
  LanTargetReviewAuthority,
  LanTargetReviewConfirmation,
  LanTargetReviewConfirmationResult,
  LanTargetReviewOptions,
  LanTargetReviewOptionsProjection,
  LanTargetReviewPreview,
  LanTargetReviewResult,
} from "../../routing/types";
