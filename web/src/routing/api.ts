import { getJson, postJson, queryString } from "../api";
import type {
  LanImportRequest,
  LanImportResult,
  LanTargetReviewRequest,
  LanTargetReviewResult,
  ModelTarget,
  ModelTargetDraft,
  ProviderProfile,
  ProviderProfileDraft,
  RoutePolicy,
  RoutingRunReport,
  RoutingStatus,
  TaskRoutePreview
} from "./types";

export async function getRoutingStatus(signal?: AbortSignal): Promise<RoutingStatus> {
  return getJson<RoutingStatus>("/api/routing/status", { signal });
}

export async function getProviderProfiles(signal?: AbortSignal): Promise<ProviderProfile[]> {
  return getJson<ProviderProfile[]>("/api/routing/providers", { signal });
}

export async function putProviderProfile(draft: ProviderProfileDraft): Promise<ProviderProfile> {
  return postJson<ProviderProfile>("/api/routing/providers", {
    ...draft,
    base_url: optionalText(draft.base_url),
    secret_ref: optionalText(draft.secret_ref),
    expected_revision: draft.expected_revision ?? null
  });
}

export async function getModelTargets(signal?: AbortSignal): Promise<ModelTarget[]> {
  return getJson<ModelTarget[]>("/api/routing/targets", { signal });
}

export async function putModelTarget(draft: ModelTargetDraft): Promise<ModelTarget> {
  return postJson<ModelTarget>("/api/routing/targets", {
    ...draft,
    expected_revision: draft.expected_revision ?? null
  });
}

export async function getRoutePolicies(signal?: AbortSignal): Promise<RoutePolicy[]> {
  return getJson<RoutePolicy[]>("/api/routing/policies", { signal });
}

export async function previewTaskRoute(
  taskId: string,
  options: {
    policyId?: string;
    directTargetId?: string;
    localRequired?: boolean;
    maximumCostUsd?: number | null;
  } = {}
): Promise<TaskRoutePreview> {
  return postJson<TaskRoutePreview>("/api/routing/preview", {
    task_id: taskId,
    policy_id: optionalText(options.policyId ?? ""),
    direct_target_id: optionalText(options.directTargetId ?? ""),
    local_required: options.localRequired ?? false,
    maximum_cost_usd: options.maximumCostUsd ?? null
  });
}

export async function getRunRouting(
  runId: string,
  taskId?: string,
  signal?: AbortSignal
): Promise<RoutingRunReport> {
  return getJson<RoutingRunReport>(
    `/api/runs/${encodeURIComponent(runId)}/routing${queryString({ task_id: taskId })}`,
    { signal }
  );
}

export async function importLanObservation(
  request: LanImportRequest,
): Promise<LanImportResult> {
  validateLanImportRequest(request);
  const payload = await postJson<unknown>("/api/routing/lan/import", {
    scan_id: request.scanId,
    endpoint_binding_digest: request.endpointBindingDigest,
    expected_terminal_receipt_digest:
      request.expectedTerminalReceiptDigest,
    expected_observation_digest: request.expectedObservationDigest,
    expected_profile_revision: request.expectedProfileRevision,
    expected_target_revisions: request.expectedTargetRevisions.map(
      (item) => ({
        resource_id: item.resourceId,
        revision: item.revision,
      }),
    ),
    replacement:
      request.replacement === null
        ? null
        : {
            provider_profile_id:
              request.replacement.providerProfileId,
            expected_profile_revision:
              request.replacement.expectedProfileRevision,
            expected_endpoint_fingerprint:
              request.replacement.expectedEndpointFingerprint,
            expected_material_binding_digests:
              request.replacement.expectedMaterialBindingDigests,
      },
  });
  return parseLanImportResult(payload, request);
}

export async function reviewLanTarget(
  request: LanTargetReviewRequest,
): Promise<LanTargetReviewResult> {
  validateLanReviewRequest(request);
  const payload = await postJson<unknown>(
    `/api/routing/lan/targets/${encodeURIComponent(
      request.targetId,
    )}/review`,
    {
      expected_profile_revision: request.expectedProfileRevision,
      expected_target_revision: request.expectedTargetRevision,
      expected_terminal_receipt_digest:
        request.expectedTerminalReceiptDigest,
      expected_observation_digest: request.expectedObservationDigest,
      expected_endpoint_fingerprint:
        request.expectedEndpointFingerprint,
      expected_material_binding_digest:
        request.expectedMaterialBindingDigest,
      expected_review_digest: request.expectedReviewDigest,
      expected_stale_reasons: request.expectedStaleReasons,
      trust_class: request.trustClass,
      intended_roles: request.intendedRoles,
      task_family_affinities: request.taskFamilyAffinities,
      privacy_acknowledged: request.privacyAcknowledged,
      enabled: request.enabled,
    },
  );
  return parseLanReviewResult(payload, request);
}

const LAN_SCAN_ID = /^lan_[0-9a-f]{32}$/;
const LAN_PROFILE_ID = /^lan-provider-[0-9a-f]{64}$/;
const LAN_TARGET_ID = /^lan-target-[0-9a-f]{64}$/;
const LAN_DIGEST = /^sha256:[0-9a-f]{64}$/;
const LAN_TIMESTAMP =
  /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)$/;
const UNICODE_OTHER = /\p{C}/u;
const LAN_STALE_REASONS = [
  "interface_changed",
  "network_changed",
  "address_changed",
  "port_changed",
  "transport_security_changed",
  "certificate_changed",
  "api_shape_changed",
  "catalog_changed",
  "model_identity_changed",
  "model_missing",
  "capability_changed",
  "freshness_expired",
] as const;

function validateLanImportRequest(request: LanImportRequest): void {
  if (
    !isRecord(request) ||
    typeof request.scanId !== "string" ||
    !LAN_SCAN_ID.test(request.scanId) ||
    !isDigestText(request.endpointBindingDigest) ||
    !isDigestText(request.expectedTerminalReceiptDigest) ||
    !isDigestText(request.expectedObservationDigest) ||
    !isNonnegativeInteger(request.expectedProfileRevision) ||
    !Array.isArray(request.expectedTargetRevisions)
  ) {
    invalidLanRequest();
  }
  const targetIds = request.expectedTargetRevisions.map((item) => {
    if (
      !LAN_TARGET_ID.test(item.resourceId) ||
      !isNonnegativeInteger(item.revision)
    ) {
      invalidLanRequest();
    }
    return item.resourceId;
  });
  if (!isCanonicalSequence(targetIds)) invalidLanRequest();
  const replacement = request.replacement;
  if (replacement !== null) {
    if (
      !isRecord(replacement) ||
      typeof replacement.providerProfileId !== "string" ||
      !LAN_PROFILE_ID.test(replacement.providerProfileId) ||
      !isNonnegativeInteger(replacement.expectedProfileRevision) ||
      !isDigestText(replacement.expectedEndpointFingerprint) ||
      !Array.isArray(replacement.expectedMaterialBindingDigests) ||
      replacement.expectedMaterialBindingDigests.length === 0 ||
      replacement.expectedMaterialBindingDigests.some(
        (value) => !LAN_DIGEST.test(value),
      ) ||
      !isCanonicalSequence(replacement.expectedMaterialBindingDigests)
    ) {
      invalidLanRequest();
    }
  }
}

function validateLanReviewRequest(request: LanTargetReviewRequest): void {
  if (
    !isRecord(request) ||
    typeof request.targetId !== "string" ||
    !LAN_TARGET_ID.test(request.targetId) ||
    !isNonnegativeInteger(request.expectedProfileRevision) ||
    !isNonnegativeInteger(request.expectedTargetRevision) ||
    !isDigestText(request.expectedTerminalReceiptDigest) ||
    !isDigestText(request.expectedObservationDigest) ||
    !isDigestText(request.expectedEndpointFingerprint) ||
    !isDigestText(request.expectedMaterialBindingDigest) ||
    !isDigestText(request.expectedReviewDigest) ||
    request.trustClass !== "operator_confirmed" ||
    request.privacyAcknowledged !== true ||
    typeof request.enabled !== "boolean" ||
    !Array.isArray(request.expectedStaleReasons) ||
    !isCanonicalStaleReasons(request.expectedStaleReasons) ||
    !Array.isArray(request.intendedRoles) ||
    !isCanonicalAffinityList(request.intendedRoles) ||
    !Array.isArray(request.taskFamilyAffinities) ||
    !isCanonicalAffinityList(request.taskFamilyAffinities)
  ) {
    invalidLanRequest();
  }
}

function parseLanImportResult(
  value: unknown,
  request: LanImportRequest,
): LanImportResult {
  const record = exactRecord(value, [
    "profile",
    "targets",
    "observation_digest",
    "endpoint_fingerprint",
    "outage_observed",
    "affected_target_ids",
    "invalidated_binding_digests",
    "stale_reasons_by_target",
  ]);
  const profile =
    record.profile === null ? null : parseLanProviderProfile(record.profile);
  if (!Array.isArray(record.targets)) invalidLanResponse();
  const targets = record.targets.map(parseLanModelTarget);
  const observationDigest = digest(record.observation_digest);
  const endpointFingerprint = nullableDigest(record.endpoint_fingerprint);
  if (
    observationDigest !== request.expectedObservationDigest ||
    typeof record.outage_observed !== "boolean"
  ) {
    invalidLanResponse();
  }
  const affectedTargetIds = uniqueLanTargetIdArray(record.affected_target_ids);
  if (
    !sameSequence(
      targets.map((target) => target.target_id),
      affectedTargetIds,
    ) ||
    (profile === null) !== (endpointFingerprint === null) ||
    (!record.outage_observed && profile === null) ||
    (profile === null && targets.length !== 0)
  ) {
    invalidLanResponse();
  }
  const allowedProfileIds = new Set<string>();
  if (profile !== null) allowedProfileIds.add(profile.profile_id);
  if (request.replacement !== null) {
    allowedProfileIds.add(request.replacement.providerProfileId);
  }
  if (
    targets.some(
      (target) => !allowedProfileIds.has(target.provider_profile_id),
    )
  ) {
    invalidLanResponse();
  }
  const invalidatedDigests = canonicalDigestArray(
    record.invalidated_binding_digests,
  );
  if (!Array.isArray(record.stale_reasons_by_target)) {
    invalidLanResponse();
  }
  const staleReasons = record.stale_reasons_by_target.map((item) => {
    const entry = exactRecord(item, ["target_id", "reasons"]);
    const targetId = lanTargetId(entry.target_id);
    const reasons = staleReasonArray(entry.reasons);
    return { target_id: targetId, reasons };
  });
  if (
    !isOrderedUniqueSubset(
      staleReasons.map((item) => item.target_id),
      affectedTargetIds,
    )
  ) {
    invalidLanResponse();
  }
  return {
    profile,
    targets,
    observation_digest: observationDigest,
    endpoint_fingerprint: endpointFingerprint,
    outage_observed: record.outage_observed,
    affected_target_ids: affectedTargetIds,
    invalidated_binding_digests: invalidatedDigests,
    stale_reasons_by_target: staleReasons,
  };
}

function parseLanReviewResult(
  value: unknown,
  request: LanTargetReviewRequest,
): LanTargetReviewResult {
  const record = exactRecord(value, [
    "profile",
    "target",
    "privacy_acknowledgement_digest",
    "material_binding_digest",
  ]);
  const profile = parseLanProviderProfile(record.profile);
  const target = parseLanModelTarget(record.target);
  if (
    target.target_id !== request.targetId ||
    target.provider_profile_id !== profile.profile_id
  ) {
    invalidLanResponse();
  }
  return {
    profile,
    target,
    privacy_acknowledgement_digest: digest(
      record.privacy_acknowledgement_digest,
    ),
    material_binding_digest: digest(record.material_binding_digest),
  };
}

function parseLanProviderProfile(value: unknown): ProviderProfile {
  const record = exactRecord(value, [
    "profile_id",
    "display_name",
    "adapter",
    "base_url_configured",
    "secret_configured",
    "enabled",
    "locality",
    "trust_class",
    "max_concurrency",
    "metadata",
    "revision",
    "created_at",
    "updated_at",
  ]);
  if (
    typeof record.base_url_configured !== "boolean" ||
    typeof record.secret_configured !== "boolean" ||
    typeof record.enabled !== "boolean" ||
    !["local", "cloud", "hybrid"].includes(String(record.locality))
  ) {
    invalidLanResponse();
  }
  return {
    profile_id: lanProfileId(record.profile_id),
    display_name: boundedText(record.display_name, 512),
    adapter: boundedText(record.adapter, 128),
    base_url_configured: record.base_url_configured,
    secret_configured: record.secret_configured,
    enabled: record.enabled,
    locality: record.locality as "local" | "cloud" | "hybrid",
    trust_class: boundedText(record.trust_class, 128),
    max_concurrency: positiveInteger(record.max_concurrency),
    metadata: boundedMetadata(record.metadata),
    revision: positiveInteger(record.revision),
    created_at: utcTimestamp(record.created_at),
    updated_at: utcTimestamp(record.updated_at),
  };
}

function parseLanModelTarget(value: unknown): ModelTarget {
  const record = exactRecord(value, [
    "target_id",
    "provider_profile_id",
    "provider",
    "model",
    "enabled",
    "locality",
    "trust_class",
    "capability_tags",
    "role_affinities",
    "task_family_affinities",
    "max_context_tokens",
    "supports_tools",
    "supports_json",
    "supports_vision",
    "supports_reasoning",
    "supports_streaming",
    "quality_tier",
    "latency_tier",
    "operator_priority",
    "estimated_cost_usd",
    "input_cost_per_million_usd",
    "output_cost_per_million_usd",
    "health",
    "recent_failure_rate",
    "predicted_success",
    "metadata",
    "revision",
    "created_at",
    "updated_at",
  ]);
  const booleanKeys = [
    "enabled",
    "supports_tools",
    "supports_json",
    "supports_vision",
    "supports_reasoning",
    "supports_streaming",
  ] as const;
  if (
    booleanKeys.some((key) => typeof record[key] !== "boolean") ||
    !["local", "cloud", "hybrid"].includes(String(record.locality)) ||
    !["unknown", "healthy", "degraded", "open", "unavailable"].includes(
      String(record.health),
    )
  ) {
    invalidLanResponse();
  }
  const qualityTier = positiveInteger(record.quality_tier);
  const latencyTier = positiveInteger(record.latency_tier);
  const failureRate = boundedRate(record.recent_failure_rate);
  if (qualityTier > 5 || latencyTier > 5) invalidLanResponse();
  return {
    target_id: lanTargetId(record.target_id),
    provider_profile_id: lanProfileId(record.provider_profile_id),
    provider: boundedText(record.provider, 128),
    model: boundedText(record.model, 512),
    enabled: record.enabled as boolean,
    locality: record.locality as "local" | "cloud" | "hybrid",
    trust_class: boundedText(record.trust_class, 128),
    capability_tags: textArray(record.capability_tags, 64),
    role_affinities: affinityArray(record.role_affinities),
    task_family_affinities: affinityArray(record.task_family_affinities),
    max_context_tokens: nullablePositiveInteger(record.max_context_tokens),
    supports_tools: record.supports_tools as boolean,
    supports_json: record.supports_json as boolean,
    supports_vision: record.supports_vision as boolean,
    supports_reasoning: record.supports_reasoning as boolean,
    supports_streaming: record.supports_streaming as boolean,
    quality_tier: qualityTier,
    latency_tier: latencyTier,
    operator_priority: integer(record.operator_priority),
    estimated_cost_usd: nullableNonnegativeNumber(record.estimated_cost_usd),
    input_cost_per_million_usd: nullableNonnegativeNumber(
      record.input_cost_per_million_usd,
    ),
    output_cost_per_million_usd: nullableNonnegativeNumber(
      record.output_cost_per_million_usd,
    ),
    health: record.health as ModelTarget["health"],
    recent_failure_rate: failureRate,
    predicted_success:
      record.predicted_success === null
        ? null
        : boundedRate(record.predicted_success),
    metadata: boundedMetadata(record.metadata),
    revision: positiveInteger(record.revision),
    created_at: utcTimestamp(record.created_at),
    updated_at: utcTimestamp(record.updated_at),
  };
}

function exactRecord(
  value: unknown,
  keys: readonly string[],
): Record<string, unknown> {
  if (!isRecord(value)) invalidLanResponse();
  const actual = Object.keys(value);
  if (
    actual.length !== keys.length ||
    actual.some((key) => !keys.includes(key))
  ) {
    invalidLanResponse();
  }
  return value;
}

function boundedMetadata(value: unknown): Record<string, unknown> {
  if (!isRecord(value)) invalidLanResponse();
  let encoded: string;
  try {
    encoded = JSON.stringify(value);
  } catch {
    invalidLanResponse();
  }
  if (new TextEncoder().encode(encoded).byteLength > 128 * 1_024) {
    invalidLanResponse();
  }
  return value;
}

function textArray(value: unknown, maximum: number): string[] {
  if (!Array.isArray(value) || value.length > maximum) invalidLanResponse();
  const result = value.map((item) => boundedText(item, 128));
  if (!isCanonicalSequence(result)) invalidLanResponse();
  return result;
}

function affinityArray(value: unknown): string[] {
  if (!Array.isArray(value) || !isCanonicalAffinityList(value)) {
    invalidLanResponse();
  }
  return [...value];
}

function uniqueLanTargetIdArray(value: unknown): string[] {
  if (!Array.isArray(value)) invalidLanResponse();
  const result = value.map(lanTargetId);
  if (new Set(result).size !== result.length) invalidLanResponse();
  return result;
}

function canonicalDigestArray(value: unknown): string[] {
  if (!Array.isArray(value)) invalidLanResponse();
  const result = value.map(digest);
  if (!isCanonicalSequence(result)) invalidLanResponse();
  return result;
}

function sameSequence(left: readonly string[], right: readonly string[]): boolean {
  return (
    left.length === right.length &&
    left.every((value, index) => value === right[index])
  );
}

function isOrderedUniqueSubset(
  candidate: readonly string[],
  authority: readonly string[],
): boolean {
  let authorityIndex = -1;
  for (const value of candidate) {
    const nextIndex = authority.indexOf(value, authorityIndex + 1);
    if (nextIndex < 0) return false;
    authorityIndex = nextIndex;
  }
  return true;
}

function staleReasonArray(value: unknown): LanImportResult["stale_reasons_by_target"][number]["reasons"] {
  if (!Array.isArray(value) || !isCanonicalStaleReasons(value)) {
    invalidLanResponse();
  }
  return [...value];
}

function isCanonicalStaleReasons(
  value: readonly unknown[],
): value is Array<(typeof LAN_STALE_REASONS)[number]> {
  if (value.length > LAN_STALE_REASONS.length) return false;
  const expected = LAN_STALE_REASONS.filter((reason) => value.includes(reason));
  return (
    value.every((reason) =>
      LAN_STALE_REASONS.includes(reason as (typeof LAN_STALE_REASONS)[number]),
    ) && JSON.stringify(value) === JSON.stringify(expected)
  );
}

function isCanonicalAffinityList(value: readonly unknown[]): boolean {
  if (
    value.length > 16 ||
    !value.every(
      (item) =>
        typeof item === "string" &&
        item.length > 0 &&
        item.normalize("NFC") === item &&
        !UNICODE_OTHER.test(item) &&
        new TextEncoder().encode(item).byteLength <= 64,
    )
  ) {
    return false;
  }
  const affinities = value as string[];
  return sameSequence(
    affinities,
    [...new Set(affinities)].sort(compareUnicodeCodePoints),
  );
}

function compareUnicodeCodePoints(left: string, right: string): number {
  const leftCharacters = Array.from(left);
  const rightCharacters = Array.from(right);
  const sharedLength = Math.min(leftCharacters.length, rightCharacters.length);
  for (let index = 0; index < sharedLength; index += 1) {
    const leftPoint = leftCharacters[index]?.codePointAt(0) ?? 0;
    const rightPoint = rightCharacters[index]?.codePointAt(0) ?? 0;
    if (leftPoint !== rightPoint) return leftPoint - rightPoint;
  }
  return leftCharacters.length - rightCharacters.length;
}

function isCanonicalSequence(value: readonly string[]): boolean {
  return JSON.stringify(value) === JSON.stringify([...new Set(value)].sort());
}

function lanProfileId(value: unknown): string {
  if (typeof value !== "string" || !LAN_PROFILE_ID.test(value)) {
    invalidLanResponse();
  }
  return value;
}

function lanTargetId(value: unknown): string {
  if (typeof value !== "string" || !LAN_TARGET_ID.test(value)) {
    invalidLanResponse();
  }
  return value;
}

function digest(value: unknown): string {
  if (typeof value !== "string" || !LAN_DIGEST.test(value)) {
    invalidLanResponse();
  }
  return value;
}

function nullableDigest(value: unknown): string | null {
  return value === null ? null : digest(value);
}

function boundedText(value: unknown, maximum: number): string {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value.length > maximum ||
    value.trim() !== value
  ) {
    invalidLanResponse();
  }
  return value;
}

function integer(value: unknown): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value)) {
    invalidLanResponse();
  }
  return value;
}

function positiveInteger(value: unknown): number {
  const parsed = integer(value);
  if (parsed < 1) invalidLanResponse();
  return parsed;
}

function nullablePositiveInteger(value: unknown): number | null {
  return value === null ? null : positiveInteger(value);
}

function nullableNonnegativeNumber(value: unknown): number | null {
  if (value === null) return null;
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
    invalidLanResponse();
  }
  return value;
}

function boundedRate(value: unknown): number {
  if (
    typeof value !== "number" ||
    !Number.isFinite(value) ||
    value < 0 ||
    value > 1
  ) {
    invalidLanResponse();
  }
  return value;
}

function utcTimestamp(value: unknown): string {
  if (typeof value !== "string" || !LAN_TIMESTAMP.test(value)) {
    invalidLanResponse();
  }
  return value;
}

function isNonnegativeInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}

function isDigestText(value: unknown): value is string {
  return typeof value === "string" && LAN_DIGEST.test(value);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function invalidLanRequest(): never {
  throw new Error("lan_request_invalid");
}

function invalidLanResponse(): never {
  throw new Error("lan_response_invalid");
}

function optionalText(value: string): string | null {
  const normalized = value.trim();
  return normalized ? normalized : null;
}
