export type RoutingMode = "off" | "shadow" | "constrained" | "adaptive";
export type RoutingLocality = "local" | "cloud" | "hybrid";
export type RoutingHealth = "unknown" | "healthy" | "degraded" | "open" | "unavailable";

export type AdaptiveFlockRuntimeStatus = {
  enabled: boolean;
  mode: RoutingMode;
  policy_id: string;
  learned?: {
    min_examples: number;
    min_target_examples: number;
    confidence_threshold: number;
    activation_margin: number;
    cost_coverage_threshold: number;
    decay_half_life_days: number;
    activation_replay_verified: boolean;
  };
};

export type RoutingStatus = {
  schema: string;
  runtime: AdaptiveFlockRuntimeStatus;
  routing_schema_version: number;
  counts: {
    provider_profiles: number;
    enabled_provider_profiles: number;
    model_targets: number;
    enabled_model_targets: number;
    policies: number;
    enabled_policies: number;
    calibrations?: number;
  };
};

export type ProviderProfile = {
  profile_id: string;
  display_name: string;
  adapter: string;
  base_url_configured: boolean;
  secret_configured: boolean;
  enabled: boolean;
  locality: RoutingLocality;
  trust_class: string;
  max_concurrency: number;
  metadata: Record<string, unknown>;
  revision: number;
  created_at: string;
  updated_at: string;
};

export type ProviderProfileDraft = {
  profile_id: string;
  display_name: string;
  adapter: string;
  base_url: string;
  secret_ref: string;
  enabled: boolean;
  locality: RoutingLocality;
  trust_class: string;
  max_concurrency: number;
  metadata: Record<string, unknown>;
  expected_revision?: number;
};

export type ModelTarget = {
  target_id: string;
  provider_profile_id: string;
  provider: string;
  model: string;
  enabled: boolean;
  locality: RoutingLocality;
  trust_class: string;
  capability_tags: string[];
  role_affinities: string[];
  task_family_affinities: string[];
  max_context_tokens: number | null;
  supports_tools: boolean;
  supports_json: boolean;
  supports_vision: boolean;
  supports_reasoning: boolean;
  supports_streaming: boolean;
  quality_tier: number;
  latency_tier: number;
  operator_priority: number;
  estimated_cost_usd: number | null;
  input_cost_per_million_usd: number | null;
  output_cost_per_million_usd: number | null;
  health: RoutingHealth;
  recent_failure_rate: number;
  predicted_success: number | null;
  metadata: Record<string, unknown>;
  revision: number;
  created_at: string;
  updated_at: string;
};

export type ModelTargetDraft = {
  target_id: string;
  provider_profile_id: string;
  provider: string;
  model: string;
  enabled: boolean;
  locality: RoutingLocality;
  trust_class: string;
  capability_tags: string[];
  role_affinities: string[];
  task_family_affinities: string[];
  max_context_tokens: number | null;
  supports_tools: boolean;
  supports_json: boolean;
  supports_vision: boolean;
  supports_reasoning: boolean;
  supports_streaming: boolean;
  quality_tier: number;
  latency_tier: number;
  operator_priority: number;
  estimated_cost_usd: number | null;
  input_cost_per_million_usd: number | null;
  output_cost_per_million_usd: number | null;
  health: RoutingHealth;
  recent_failure_rate: number;
  predicted_success: number | null;
  metadata: Record<string, unknown>;
  expected_revision?: number;
};

export type RoutePolicy = {
  policy_id: string;
  enabled: boolean;
  quality_weight: number;
  affinity_weight: number;
  health_weight: number;
  context_weight: number;
  locality_weight: number;
  operator_weight: number;
  cost_weight: number;
  latency_weight: number;
  failure_weight: number;
  require_different_target_for_review: boolean;
  require_different_model_family_for_review: boolean;
  prefer_different_provider_for_review: boolean;
  minimum_quality_by_risk: Record<string, number>;
  revision: number;
  created_at: string;
  updated_at: string;
};

export type RouteCandidate = {
  target_id: string;
  provider_profile_id: string;
  provider: string;
  model: string;
  eligible: boolean;
  score: number | null;
  reason_codes: string[];
  components: Record<string, number>;
};

export type TaskRoutePreview = {
  schema: string;
  task: {
    task_id: string;
    run_id: string;
    title: string;
    status: string;
  };
  contract: Record<string, unknown>;
  decision: {
    mode: RoutingMode;
    policy_id: string;
    contract_digest: string;
    selected_target_id: string;
    selected_provider_profile_id: string;
    selected_provider: string;
    selected_model: string;
    selection_kind: string;
    score: number;
    reason_codes: string[];
    actionable: boolean;
    candidates: RouteCandidate[];
  };
};

export type RoutingRunReport = {
  run_id: string;
  task_id: string | null;
  decisions: RoutingDecisionRecord[];
  outcomes: RoutingOutcomeRecord[];
  shadows?: RoutingShadowRecord[];
  calibrations?: TargetCalibrationRecord[];
};

export type RoutingDecisionRecord = {
  decision_id: string;
  task_id: string;
  attempt: number;
  status: string;
  mode: RoutingMode;
  selected_target_id: string;
  selected_provider: string;
  selected_model: string;
  selection_kind: string;
  actionable: boolean;
  task_family: string;
  risk: string;
};

export type RoutingOutcomeRecord = {
  decision_id: string;
  validation_passed: boolean;
  execution_status: string;
  failure_category: string | null;
  latency_seconds: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  actual_cost_usd: number | null;
  retry_count: number;
  escalated: boolean;
  outcome_labels: string[];
};

export type RoutingShadowRecord = {
  shadow_id: string;
  decision_id: string;
  project_id: string | null;
  task_family: string;
  risk: string;
  static_target_id: string;
  learned_target_id: string | null;
  actual_target_id: string | null;
  actual_provider: string;
  actual_model: string;
  evidence_count: number;
  target_example_count: number;
  cost_coverage: number;
  confidence: number;
  utility_delta: number;
  estimated_savings_usd: number | null;
  route_regret_usd: number | null;
  activated: boolean;
  abstention_reason: string | null;
  resolved_at: string | null;
  actual_validation_passed: boolean | null;
  actual_cost_usd: number | null;
};

export type TargetCalibrationRecord = {
  calibration_key: string;
  project_id: string | null;
  target_id: string;
  task_family: string;
  risk: string;
  validation_rate: number;
  recent_failure_rate: number;
  provider_outage_rate: number;
  average_cost_usd: number | null;
  average_latency_seconds: number | null;
  cost_coverage: number;
  example_count: number;
  effective_sample_size: number;
  updated_at: string;
};

export type LanStaleReason =
  | "interface_changed"
  | "network_changed"
  | "address_changed"
  | "port_changed"
  | "transport_security_changed"
  | "certificate_changed"
  | "api_shape_changed"
  | "catalog_changed"
  | "model_identity_changed"
  | "model_missing"
  | "capability_changed"
  | "freshness_expired";

export type LanExpectedRevision = Readonly<{
  resource_id: string;
  revision: number;
}>;

export type LanReplacementConfirmation = Readonly<{
  provider_profile_id: string;
  expected_profile_revision: number;
  expected_endpoint_fingerprint: string;
  expected_material_binding_digests: string[];
}>;

export type LanImportSelector = Readonly<{
  scanId: string;
  endpointId: string;
  replacementProviderProfileId: string | null;
}>;

export type LanImportSelectorProjection = Readonly<{
  scan_id: string;
  endpoint_id: string;
  replacement_provider_profile_id: string | null;
}>;

export type LanImportAuthority = Readonly<{
  expected_terminal_receipt_digest: string;
  expected_observation_digest: string;
  expected_profile_revision: number;
  expected_target_revisions: LanExpectedRevision[];
  endpoint_fingerprint: string | null;
  replacement: LanReplacementConfirmation | null;
}>;

export type LanImportResult = Readonly<{
  profile: ProviderProfile | null;
  targets: ModelTarget[];
  observation_digest: string;
  endpoint_fingerprint: string | null;
  outage_observed: boolean;
  affected_target_ids: string[];
  invalidated_binding_digests: string[];
  stale_reasons_by_target: Array<{
    target_id: string;
    reasons: LanStaleReason[];
  }>;
}>;

export type LanImportPreview = Readonly<{
  selector: LanImportSelectorProjection;
  preview_digest: string;
  evidence_expires_at: string;
  authority: LanImportAuthority;
  result: LanImportResult;
  requires_confirmation: true;
}>;

export type LanImportConfirmation = Readonly<{
  selector: LanImportSelector;
  previewDigest: string;
  confirmed: true;
}>;

export type LanImportConfirmationResult = Readonly<{
  preview_digest: string;
  result: LanImportResult;
}>;

export type LanTargetReviewOptions = Readonly<{
  targetId: string;
  intendedRoles: string[];
  taskFamilyAffinities: string[];
  enabled: boolean;
}>;

export type LanTargetReviewOptionsProjection = Readonly<{
  target_id: string;
  intended_roles: string[];
  task_family_affinities: string[];
  enabled: boolean;
}>;

export type LanTargetReviewAuthority = Readonly<{
  provider_profile_id: string;
  expected_profile_revision: number;
  expected_target_revision: number;
  expected_terminal_receipt_digest: string;
  expected_observation_digest: string;
  expected_endpoint_fingerprint: string;
  expected_material_binding_digest: string;
  expected_stale_reasons: LanStaleReason[];
  trust_class: "operator_confirmed";
  privacy_acknowledgement_digest: string;
  review_digest: string;
  reviewed_material_binding_digest: string;
  reviewed_runtime_interface_binding_digest: string | null;
}>;

export type LanTargetReviewResult = Readonly<{
  profile: ProviderProfile;
  target: ModelTarget;
  privacy_acknowledgement_digest: string;
  material_binding_digest: string;
}>;

export type LanTargetReviewPreview = Readonly<{
  options: LanTargetReviewOptionsProjection;
  preview_digest: string;
  evidence_expires_at: string;
  authority: LanTargetReviewAuthority;
  profile: ProviderProfile;
  target: ModelTarget;
  requires_privacy_acknowledgement: true;
  requires_confirmation: true;
}>;

export type LanTargetReviewConfirmation = LanTargetReviewOptions &
  Readonly<{
    previewDigest: string;
    privacyAcknowledged: true;
    confirmed: true;
  }>;

export type LanTargetReviewConfirmationResult = Readonly<{
  preview_digest: string;
  result: LanTargetReviewResult;
}>;
