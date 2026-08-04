import type {
  FlockDigest,
  FlockEvidenceKind,
  FlockPrivacyClass,
  FlockRiskLevel,
  FlockRunStatus,
  FlockScopeQualificationState,
  FlockTerminalRunStatus,
  FlockUsdText,
} from "../types";

/** Owner-entered corpus item (client shape; camelCase). */
export type QualificationCorpusItemInput = Readonly<{
  itemId: string;
  taskFamily: string;
  risk: FlockRiskLevel;
  capabilities: string[];
  taskContractDigest: FlockDigest;
  acceptancePlanDigest: FlockDigest;
  evidenceKind: FlockEvidenceKind;
  actionable?: boolean;
  exclusionReasons?: string[];
}>;

export type QualificationThresholdsInput = Readonly<{
  minExamplesPerScope?: number;
  minExamplesPerTarget?: number;
  confidenceThreshold?: number;
  utilityMargin?: number;
  costCoverageThreshold?: number;
  decayHalfLifeDays?: number;
  maxGuardrailViolations?: number;
  replayRuns?: number;
  replaySuccessesRequired?: number;
}>;

export type PreviewQualificationInput = Readonly<{
  projectId: string;
  taskFamilies: string[];
  corpus: QualificationCorpusItemInput[];
  policyId?: string;
  policyRevision?: number;
  /** Decimal text cap ("37.25"); forwarded verbatim, never parsed as float. */
  maximumSpendUsd?: FlockUsdText;
  defaultPrivacyClass?: FlockPrivacyClass;
  projectAuthority?: Record<string, unknown>;
  learnedConfig?: Record<string, unknown>;
}>;

export type QualificationScopeInput = Readonly<{
  projectId: string;
  taskFamily: string;
  risk: FlockRiskLevel;
  capabilityKey: string;
  policyId: string;
  policyRevision: number;
  targetIds: string[];
  targetInventoryDigest: FlockDigest;
  priceDigest: FlockDigest;
  learnedConfigDigest: FlockDigest;
  projectAuthorityDigest: FlockDigest;
}>;

export type CreateQualificationInput = Readonly<{
  scope: QualificationScopeInput;
  corpus: QualificationCorpusItemInput[];
  thresholds?: QualificationThresholdsInput;
  targetSnapshot: Record<string, unknown>;
  priceSnapshot: Record<string, unknown>;
  policyPayload: Record<string, unknown>;
  learnedPayload: Record<string, unknown>;
  projectAuthority: Record<string, unknown>;
  build?: Record<string, unknown>;
  maximumSpendUsd?: FlockUsdText;
  effectiveStopCapUsd?: FlockUsdText;
  attemptCeilingUsd?: FlockUsdText;
}>;

export type QualificationLifecycleInput = Readonly<{
  runId: string;
  expectedRevision: number;
}>;

export type LowerQualificationCapInput = Readonly<{
  runId: string;
  /** New cap as decimal text; may only lower the effective stop cap. */
  maximumSpendUsd: FlockUsdText;
  expectedRevision: number;
}>;

// --- server payload contracts (snake_case mirrors the wire) -------------------

export type QualificationScopePayload = Readonly<{
  project_id: string;
  task_family: string;
  risk: FlockRiskLevel;
  capability_key: string;
  policy_id: string;
  policy_revision: number;
  target_ids: string[];
  target_inventory_digest: FlockDigest;
  price_digest: FlockDigest;
  learned_config_digest: FlockDigest;
  project_authority_digest: FlockDigest;
}>;

export type QualificationPreviewBudget = Readonly<{
  maximum_spend_micros: number;
  maximum_spend_usd: FlockUsdText;
  estimated_reserved_cost_range_micros: [number, number];
}>;

export type QualificationPreview = Readonly<{
  schema: "kestrel.flock.qualification_preview.v1";
  created_at: string;
  scopes: QualificationScopePayload[];
  excluded_scopes: Record<string, string[]>;
  target_snapshot_digest: FlockDigest;
  target_ids: string[];
  excluded_targets: Record<string, string[]>;
  start_blockers: Record<string, string[]>;
  warnings: Record<string, string[]>;
  matrix_size: number;
  estimated_reserved_cost_range: [number, number];
  policy_digest: FlockDigest;
  corpus_digest: FlockDigest;
  project_authority_digest: FlockDigest;
  target_inventory_digest: FlockDigest;
  learned_config_digest: FlockDigest;
  budget: QualificationPreviewBudget;
  preview_digest: FlockDigest;
}>;

export type QualificationRunCaps = Readonly<{
  max_spend_micros: number;
  max_spend_usd: FlockUsdText;
  effective_stop_cap_micros: number;
  effective_stop_cap_usd: FlockUsdText;
  attempt_ceiling_micros: number;
  attempt_ceiling_usd: FlockUsdText;
}>;

export type QualificationRunSpend = Readonly<{
  actual_spend_micros: number;
  actual_spend_usd: FlockUsdText;
  unresolved_reserve_micros: number;
  inflight_reserve_micros: number;
}>;

export type QualificationRun = Readonly<{
  run_id: string;
  status: FlockRunStatus;
  revision: number;
  owner_principal: string;
  scope_digest: FlockDigest;
  corpus_digest: FlockDigest;
  target_digest: FlockDigest;
  price_digest: FlockDigest;
  policy_digest: FlockDigest;
  learned_digest: FlockDigest;
  project_authority_digest: FlockDigest;
  thresholds_digest: FlockDigest;
  build_digest: FlockDigest;
  caps: QualificationRunCaps;
  spend: QualificationRunSpend;
  blockers: string[];
  created_at: string;
  updated_at: string;
  started_at: string | null;
  finished_at: string | null;
  terminal_reason: string | null;
}>;

/**
 * Per-scope outcome from the terminal receipt.  ``reasons`` preserves every
 * abstention/suspension reason code verbatim; ``state`` (never the run
 * status) is the only qualification signal.
 */
export type ScopeQualificationResult = Readonly<{
  scope_digest: FlockDigest;
  state: FlockScopeQualificationState;
  qualified: boolean;
  static_target_id: string;
  selected_target_id: string | null;
  total_support: number;
  selected_target_support: number;
  confidence: number;
  static_utility: number | null;
  learned_utility: number | null;
  utility_delta: number;
  cost_coverage: number;
  estimated_savings_usd: number | null;
  estimated_regret_usd: number | null;
  guardrail_violations: number;
  evaluated_target_ids: string[];
  reasons: string[];
  router_state: Record<string, unknown>;
  thresholds_digest: FlockDigest;
}>;

export type QualificationReceiptPayload = Readonly<{
  schema: string;
  status: FlockTerminalRunStatus;
  terminal_reason: string;
  qualifying: boolean;
  scopes: ScopeQualificationResult[];
}> &
  Readonly<Record<string, unknown>>;

export type QualificationReceipt = Readonly<{
  receipt_id: string;
  run_id: string;
  receipt_type: "run_terminal";
  payload_digest: FlockDigest;
  payload: QualificationReceiptPayload;
  created_at: string;
}>;

export type QualificationEventType =
  | "run_completed"
  | "run_failed"
  | "run_cancelled"
  | "budget_projection_overrun";

export type QualificationEvent = Readonly<{
  sequence: string;
  event_type: QualificationEventType;
  payload: Readonly<Record<string, unknown>>;
  created_at: string;
}>;

export type QualificationEventStreamOptions = Readonly<{
  afterSequence?: string;
  signal: AbortSignal;
  onEvent: (event: QualificationEvent) => void;
}>;

/**
 * Owner actions offered for a run.  There is intentionally no ``raise_cap``:
 * the effective stop cap can only move down after the run starts.
 */
export type QualificationAction =
  | "start"
  | "pause"
  | "resume"
  | "cancel"
  | "lower_cap";
