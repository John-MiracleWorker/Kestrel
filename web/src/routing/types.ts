export type RoutingMode = "off" | "shadow" | "constrained" | "adaptive";
export type RoutingLocality = "local" | "cloud" | "hybrid";
export type RoutingHealth = "unknown" | "healthy" | "degraded" | "open" | "unavailable";

export type AdaptiveFlockRuntimeStatus = {
  enabled: boolean;
  mode: RoutingMode;
  policy_id: string;
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
  decisions: Array<Record<string, unknown>>;
  outcomes: Array<Record<string, unknown>>;
};
