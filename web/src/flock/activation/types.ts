import type {
  FlockDigest,
  FlockGrantStatus,
  FlockRiskLevel,
  FlockTransitionType,
} from "../types";
import type { QualificationScopePayload } from "../qualification/types";

export type ActivationBindingsInput = Readonly<{
  projectAuthority: Record<string, unknown>;
  targetSnapshot: Record<string, unknown>;
  priceSnapshot: Record<string, unknown>;
  policyPayload: Record<string, unknown>;
  learnedPayload: Record<string, unknown>;
}>;

export type PreviewActivationInput = Readonly<{
  receiptId: string;
  scopeDigests: string[];
}>;

export type CreateActivationInput = Readonly<{
  receiptId: string;
  scopeDigests: string[];
  expectedReceiptDigest: FlockDigest;
  expectedRunRevision: number;
  bindings: ActivationBindingsInput;
}>;

export type RevokeActivationInput = Readonly<{
  grantId: string;
  expectedRevision: number;
  reason?: string;
}>;

export type ListActivationsOptions = Readonly<{
  receiptId?: string;
  signal?: AbortSignal;
}>;

// --- server payload contracts (snake_case mirrors the wire) -------------------

/**
 * One scope in an activation preview.  ``qualified`` and ``reasons`` come
 * from the receipt scope result; abstention reason codes are preserved
 * verbatim.
 */
export type ActivationScopePreview = Readonly<{
  scope_digest: FlockDigest;
  project_id: string;
  task_family: string;
  risk: FlockRiskLevel;
  capabilities: string[];
  static_target_id: string;
  selected_target_id: string | null;
  alternative_target_ids: string[];
  total_support: number;
  selected_target_support: number;
  confidence: number;
  static_utility: number | null;
  learned_utility: number | null;
  utility_delta: number;
  cost_coverage: number;
  estimated_savings_usd: number | null;
  guardrail_violations: number;
  reasons: string[];
  qualified: boolean;
}>;

export type ActivationPreview = Readonly<{
  receipt_id: string;
  run_id: string;
  run_revision: number;
  owner_principal: string;
  receipt_digest: FlockDigest;
  scopes: ActivationScopePreview[];
  replay: Record<string, unknown> | null;
  target_snapshot: Record<string, unknown>;
  price_snapshot: Record<string, unknown>;
  binding_digests: Record<string, string>;
  binding_changes: Record<string, boolean>;
  authority_changed: boolean;
  suspension_conditions: string[];
  revocation_behavior: string;
}>;

export type ActivationGrant = Readonly<{
  grant_id: string;
  run_id: string;
  target_id: string;
  scope: QualificationScopePayload;
  scope_digest: FlockDigest;
  policy_id: string;
  policy_revision: number;
  qualification_receipt_id: string;
  created_by: string;
  created_at: string;
}>;

export type ActivationTransition = Readonly<{
  transition_id: string;
  grant_id: string;
  sequence: number;
  transition_type: FlockTransitionType;
  reason: string;
  receipt_id: string | null;
  created_at: string;
}>;

export type ActivationResult = Readonly<{
  grants: ActivationGrant[];
  transitions: ActivationTransition[];
  superseded: ActivationTransition[];
}>;

/**
 * Server-side grant evaluation.  ``effective`` (never ``status === "active"``)
 * is the only authority for routing effect; ``reason_codes`` preserves every
 * suspension/drift reason verbatim.
 */
export type GrantEvaluation = Readonly<{
  grant_id: string;
  run_id: string;
  scope_digest: FlockDigest;
  status: FlockGrantStatus;
  effective: boolean;
  reason_codes: string[];
  receipt_authenticates: boolean;
  binding_changes: Record<string, boolean>;
  latest_transition: ActivationTransition | null;
  transition_count: number;
}>;
