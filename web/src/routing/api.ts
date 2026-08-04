import { getJson, postJson, queryString } from "../api";
import type {
  LanImportAuthority,
  LanImportConfirmation,
  LanImportConfirmationResult,
  LanImportPreview,
  LanImportResult,
  LanImportSelector,
  LanImportSelectorProjection,
  LanTargetReviewAuthority,
  LanTargetReviewConfirmation,
  LanTargetReviewConfirmationResult,
  LanTargetReviewOptions,
  LanTargetReviewOptionsProjection,
  LanTargetReviewPreview,
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

export async function previewLanImport(
  selector: LanImportSelector,
): Promise<LanImportPreview> {
  validateLanImportSelector(selector);
  const payload = await postJson<unknown>(
    "/api/routing/lan/import/preview",
    lanImportSelectorBody(selector),
  );
  return parseLanImportPreview(payload, selector);
}

export async function confirmLanImport(
  confirmation: LanImportConfirmation,
): Promise<LanImportConfirmationResult> {
  exactRequestRecord(confirmation, ["selector", "previewDigest", "confirmed"]);
  validateLanImportSelector(confirmation.selector);
  if (!isDigestText(confirmation.previewDigest) || confirmation.confirmed !== true) {
    invalidLanRequest();
  }
  const payload = await postJson<unknown>("/api/routing/lan/import", {
    selector: lanImportSelectorBody(confirmation.selector),
    preview_digest: confirmation.previewDigest,
    confirmed: true,
  });
  return parseLanImportConfirmation(payload, confirmation);
}

export async function previewLanTargetReview(
  options: LanTargetReviewOptions,
): Promise<LanTargetReviewPreview> {
  exactRequestRecord(options, [
    "targetId",
    "intendedRoles",
    "taskFamilyAffinities",
    "enabled",
  ]);
  validateLanReviewOptions(options);
  const payload = await postJson<unknown>(
    `/api/routing/lan/targets/${encodeURIComponent(options.targetId)}/review/preview`,
    lanReviewOptionsBody(options),
  );
  return parseLanReviewPreview(payload, options);
}

export async function confirmLanTargetReview(
  confirmation: LanTargetReviewConfirmation,
): Promise<LanTargetReviewConfirmationResult> {
  exactRequestRecord(confirmation, [
    "targetId",
    "intendedRoles",
    "taskFamilyAffinities",
    "enabled",
    "previewDigest",
    "privacyAcknowledged",
    "confirmed",
  ]);
  validateLanReviewOptions(confirmation);
  if (
    !isDigestText(confirmation.previewDigest) ||
    confirmation.privacyAcknowledged !== true ||
    confirmation.confirmed !== true
  ) {
    invalidLanRequest();
  }
  const payload = await postJson<unknown>(
    `/api/routing/lan/targets/${encodeURIComponent(
      confirmation.targetId,
    )}/review`,
    {
      ...lanReviewOptionsBody(confirmation),
      preview_digest: confirmation.previewDigest,
      privacy_acknowledged: true,
      confirmed: true,
    },
  );
  return parseLanReviewConfirmation(payload, confirmation);
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

function validateLanImportSelector(selector: LanImportSelector): void {
  exactRequestRecord(selector, [
    "scanId",
    "endpointId",
    "replacementProviderProfileId",
  ]);
  if (
    typeof selector.scanId !== "string" ||
    !LAN_SCAN_ID.test(selector.scanId) ||
    !isDigestText(selector.endpointId) ||
    (selector.replacementProviderProfileId !== null &&
      (typeof selector.replacementProviderProfileId !== "string" ||
        !LAN_PROFILE_ID.test(selector.replacementProviderProfileId)))
  ) {
    invalidLanRequest();
  }
}

function lanImportSelectorBody(selector: LanImportSelector) {
  return {
    scan_id: selector.scanId,
    endpoint_id: selector.endpointId,
    replacement_provider_profile_id: selector.replacementProviderProfileId,
  };
}

function validateLanReviewOptions(options: LanTargetReviewOptions): void {
  if (
    typeof options.targetId !== "string" ||
    !LAN_TARGET_ID.test(options.targetId) ||
    typeof options.enabled !== "boolean" ||
    !Array.isArray(options.intendedRoles) ||
    !isCanonicalAffinityList(options.intendedRoles) ||
    !Array.isArray(options.taskFamilyAffinities) ||
    !isCanonicalAffinityList(options.taskFamilyAffinities)
  ) {
    invalidLanRequest();
  }
}

function lanReviewOptionsBody(options: LanTargetReviewOptions) {
  return {
    intended_roles: options.intendedRoles,
    task_family_affinities: options.taskFamilyAffinities,
    enabled: options.enabled,
  };
}

function parseLanImportPreview(
  value: unknown,
  requested: LanImportSelector,
): LanImportPreview {
  const record = exactRecord(value, [
    "selector",
    "preview_digest",
    "evidence_expires_at",
    "authority",
    "result",
    "requires_confirmation",
  ]);
  const selector = parseLanImportSelectorProjection(record.selector, requested);
  const authority = parseLanImportAuthority(record.authority, requested);
  if (record.requires_confirmation !== true) invalidLanResponse();
  const result = parseLanImportResult(record.result, {
    expectedObservationDigest: authority.expected_observation_digest,
    expectedEndpointFingerprint: authority.endpoint_fingerprint,
    replacementProviderProfileId: selector.replacement_provider_profile_id,
    replacement:
      authority.replacement === null
        ? null
        : {
            expectedEndpointFingerprint:
              authority.replacement.expected_endpoint_fingerprint,
            expectedMaterialBindingDigests:
              authority.replacement.expected_material_binding_digests,
          },
    currentEndpointId: selector.endpoint_id,
  });
  if (
    !sameSequence(
      authority.expected_target_revisions.map((item) => item.resource_id),
      result.affected_target_ids,
    )
  ) {
    invalidLanResponse();
  }
  return {
    selector,
    preview_digest: digest(record.preview_digest),
    evidence_expires_at: canonicalPreviewExpiry(record.evidence_expires_at),
    authority,
    result,
    requires_confirmation: true,
  };
}

function parseLanImportConfirmation(
  value: unknown,
  confirmation: LanImportConfirmation,
): LanImportConfirmationResult {
  const record = exactRecord(value, ["preview_digest", "result"]);
  const previewDigest = digest(record.preview_digest);
  if (previewDigest !== confirmation.previewDigest) {
    invalidLanResponse();
  }
  return {
    preview_digest: previewDigest,
    result: parseLanImportResult(record.result, {
      expectedObservationDigest: null,
      expectedEndpointFingerprint: undefined,
      replacementProviderProfileId:
        confirmation.selector.replacementProviderProfileId,
      replacement: null,
      currentEndpointId: confirmation.selector.endpointId,
    }),
  };
}

function parseLanImportSelectorProjection(
  value: unknown,
  requested: LanImportSelector,
): LanImportSelectorProjection {
  const record = exactRecord(value, [
    "scan_id",
    "endpoint_id",
    "replacement_provider_profile_id",
  ]);
  const scanId = lanScanId(record.scan_id);
  const endpointId = digest(record.endpoint_id);
  const replacementProfileId = nullableLanProfileId(
    record.replacement_provider_profile_id,
  );
  if (
    scanId !== requested.scanId ||
    endpointId !== requested.endpointId ||
    replacementProfileId !== requested.replacementProviderProfileId
  ) {
    invalidLanResponse();
  }
  return {
    scan_id: scanId,
    endpoint_id: endpointId,
    replacement_provider_profile_id: replacementProfileId,
  };
}

function parseLanImportAuthority(
  value: unknown,
  requested: LanImportSelector,
): LanImportAuthority {
  const record = exactRecord(value, [
    "expected_terminal_receipt_digest",
    "expected_observation_digest",
    "expected_profile_revision",
    "expected_target_revisions",
    "endpoint_fingerprint",
    "replacement",
  ]);
  if (!Array.isArray(record.expected_target_revisions)) invalidLanResponse();
  const expectedTargetRevisions = record.expected_target_revisions.map((item) => {
    const revision = exactRecord(item, ["resource_id", "revision"]);
    return {
      resource_id: lanTargetId(revision.resource_id),
      revision: nonnegativeInteger(revision.revision),
    };
  });
  if (
    new Set(expectedTargetRevisions.map((item) => item.resource_id)).size !==
    expectedTargetRevisions.length
  ) {
    invalidLanResponse();
  }
  let replacement: LanImportAuthority["replacement"] = null;
  if (record.replacement !== null) {
    const replacementRecord = exactRecord(record.replacement, [
      "provider_profile_id",
      "expected_profile_revision",
      "expected_endpoint_fingerprint",
      "expected_material_binding_digests",
    ]);
    if (!Array.isArray(replacementRecord.expected_material_binding_digests)) {
      invalidLanResponse();
    }
    const materials = replacementRecord.expected_material_binding_digests.map(digest);
    if (materials.length === 0 || !isCanonicalSequence(materials)) {
      invalidLanResponse();
    }
    replacement = {
      provider_profile_id: lanProfileId(replacementRecord.provider_profile_id),
      expected_profile_revision: nonnegativeInteger(
        replacementRecord.expected_profile_revision,
      ),
      expected_endpoint_fingerprint: digest(
        replacementRecord.expected_endpoint_fingerprint,
      ),
      expected_material_binding_digests: materials,
    };
  }
  if (
    (replacement === null) !==
      (requested.replacementProviderProfileId === null) ||
    (replacement !== null &&
      replacement.provider_profile_id !== requested.replacementProviderProfileId)
  ) {
    invalidLanResponse();
  }
  return {
    expected_terminal_receipt_digest: digest(
      record.expected_terminal_receipt_digest,
    ),
    expected_observation_digest: digest(record.expected_observation_digest),
    expected_profile_revision: nonnegativeInteger(
      record.expected_profile_revision,
    ),
    expected_target_revisions: expectedTargetRevisions,
    endpoint_fingerprint: nullableDigest(record.endpoint_fingerprint),
    replacement,
  };
}

type LanImportResultContext = Readonly<{
  expectedObservationDigest: string | null;
  expectedEndpointFingerprint: string | null | undefined;
  replacementProviderProfileId: string | null;
  replacement: Readonly<{
    expectedEndpointFingerprint: string;
    expectedMaterialBindingDigests: string[];
  }> | null;
  currentEndpointId: string;
}>;

function parseLanImportResult(
  value: unknown,
  context: LanImportResultContext,
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
    (context.expectedObservationDigest !== null &&
      observationDigest !== context.expectedObservationDigest) ||
    (context.expectedEndpointFingerprint !== undefined &&
      endpointFingerprint !== context.expectedEndpointFingerprint) ||
    typeof record.outage_observed !== "boolean"
  ) {
    invalidLanResponse();
  }
  const outageObserved = record.outage_observed;
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
  if (context.replacementProviderProfileId !== null) {
    allowedProfileIds.add(context.replacementProviderProfileId);
  }
  if (
    targets.some(
      (target) => !allowedProfileIds.has(target.provider_profile_id),
    )
  ) {
    invalidLanResponse();
  }
  if (
    profile !== null &&
    (endpointFingerprint === null ||
      !lanImportEvidenceMatches(
        profile.metadata,
        context.currentEndpointId,
        observationDigest,
        endpointFingerprint,
        outageObserved,
      ) ||
      targets.some(
        (target) =>
          target.provider_profile_id === profile.profile_id &&
          !lanImportEvidenceMatches(
            target.metadata,
            context.currentEndpointId,
            observationDigest,
            endpointFingerprint,
            outageObserved,
          ),
      ))
  ) {
    invalidLanResponse();
  }
  const invalidatedDigests = canonicalDigestArray(
    record.invalidated_binding_digests,
  );
  if (context.replacement !== null) {
    if (context.replacementProviderProfileId === null) {
      invalidLanResponse();
    }
    if (
      !sameSequence(
        invalidatedDigests,
        context.replacement.expectedMaterialBindingDigests,
      )
    ) {
      invalidLanResponse();
    }
    const expectedMaterials = new Set(
      context.replacement.expectedMaterialBindingDigests,
    );
    for (const target of targets) {
      if (
        target.provider_profile_id !== context.replacementProviderProfileId
      ) {
        continue;
      }
      if (
        !lanImportReplacementEvidenceMatches(
          target.metadata,
          context.replacement.expectedEndpointFingerprint,
          expectedMaterials,
        )
      ) {
        invalidLanResponse();
      }
    }
  }
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

function parseLanReviewPreview(
  value: unknown,
  requested: LanTargetReviewOptions,
): LanTargetReviewPreview {
  const record = exactRecord(value, [
    "options",
    "preview_digest",
    "evidence_expires_at",
    "authority",
    "profile",
    "target",
    "requires_privacy_acknowledgement",
    "requires_confirmation",
  ]);
  const options = parseLanReviewOptionsProjection(record.options, requested);
  const authority = parseLanReviewAuthority(record.authority);
  const profile = parseLanProviderProfile(record.profile);
  const target = parseLanModelTarget(record.target);
  if (
    record.requires_privacy_acknowledgement !== true ||
    record.requires_confirmation !== true ||
    authority.provider_profile_id !== profile.profile_id ||
    !reviewTargetMatchesOptions(target, profile.profile_id, options)
    || !reviewTargetMatchesAuthority(target, authority, options.enabled)
  ) {
    invalidLanResponse();
  }
  return {
    options,
    preview_digest: digest(record.preview_digest),
    evidence_expires_at: canonicalPreviewExpiry(record.evidence_expires_at),
    authority,
    profile,
    target,
    requires_privacy_acknowledgement: true,
    requires_confirmation: true,
  };
}

function parseLanReviewConfirmation(
  value: unknown,
  confirmation: LanTargetReviewConfirmation,
): LanTargetReviewConfirmationResult {
  const record = exactRecord(value, ["preview_digest", "result"]);
  const previewDigest = digest(record.preview_digest);
  if (previewDigest !== confirmation.previewDigest) {
    invalidLanResponse();
  }
  const options: LanTargetReviewOptionsProjection = {
    target_id: confirmation.targetId,
    intended_roles: confirmation.intendedRoles,
    task_family_affinities: confirmation.taskFamilyAffinities,
    enabled: confirmation.enabled,
  };
  return {
    preview_digest: previewDigest,
    result: parseLanReviewResult(record.result, options),
  };
}

function parseLanReviewOptionsProjection(
  value: unknown,
  requested: LanTargetReviewOptions,
): LanTargetReviewOptionsProjection {
  const record = exactRecord(value, [
    "target_id",
    "intended_roles",
    "task_family_affinities",
    "enabled",
  ]);
  const options = {
    target_id: lanTargetId(record.target_id),
    intended_roles: affinityArray(record.intended_roles),
    task_family_affinities: affinityArray(record.task_family_affinities),
    enabled: exactBoolean(record.enabled),
  };
  if (
    options.target_id !== requested.targetId ||
    !sameSequence(options.intended_roles, requested.intendedRoles) ||
    !sameSequence(
      options.task_family_affinities,
      requested.taskFamilyAffinities,
    ) ||
    options.enabled !== requested.enabled
  ) {
    invalidLanResponse();
  }
  return options;
}

function parseLanReviewAuthority(value: unknown): LanTargetReviewAuthority {
  const record = exactRecord(value, [
    "provider_profile_id",
    "expected_profile_revision",
    "expected_target_revision",
    "expected_terminal_receipt_digest",
    "expected_observation_digest",
    "expected_endpoint_fingerprint",
    "expected_material_binding_digest",
    "expected_stale_reasons",
    "trust_class",
    "privacy_acknowledgement_digest",
    "review_digest",
    "reviewed_material_binding_digest",
    "reviewed_runtime_interface_binding_digest",
  ]);
  if (record.trust_class !== "operator_confirmed") invalidLanResponse();
  return {
    provider_profile_id: lanProfileId(record.provider_profile_id),
    expected_profile_revision: nonnegativeInteger(
      record.expected_profile_revision,
    ),
    expected_target_revision: nonnegativeInteger(record.expected_target_revision),
    expected_terminal_receipt_digest: digest(
      record.expected_terminal_receipt_digest,
    ),
    expected_observation_digest: digest(record.expected_observation_digest),
    expected_endpoint_fingerprint: digest(record.expected_endpoint_fingerprint),
    expected_material_binding_digest: digest(
      record.expected_material_binding_digest,
    ),
    expected_stale_reasons: staleReasonArray(record.expected_stale_reasons),
    trust_class: "operator_confirmed",
    privacy_acknowledgement_digest: digest(
      record.privacy_acknowledgement_digest,
    ),
    review_digest: digest(record.review_digest),
    reviewed_material_binding_digest: digest(
      record.reviewed_material_binding_digest,
    ),
    reviewed_runtime_interface_binding_digest: nullableDigest(
      record.reviewed_runtime_interface_binding_digest,
    ),
  };
}

function reviewTargetMatchesOptions(
  target: ModelTarget,
  profileId: string,
  options: LanTargetReviewOptionsProjection,
): boolean {
  return (
    target.target_id === options.target_id &&
    target.provider_profile_id === profileId &&
    target.enabled === options.enabled &&
    target.trust_class === "operator_confirmed" &&
    sameSequence(target.role_affinities, options.intended_roles) &&
    sameSequence(
      target.task_family_affinities,
      options.task_family_affinities,
    )
  );
}

function reviewTargetMatchesAuthority(
  target: ModelTarget,
  authority: LanTargetReviewAuthority,
  enabled: boolean,
): boolean {
  const evidence = lanDiscoveryMetadata(target.metadata);
  const runtimeBinding = nullableDigest(
    evidence.reviewed_runtime_interface_binding_digest,
  );
  return (
    digest(evidence.observation_digest) ===
      authority.expected_observation_digest &&
    digest(evidence.endpoint_fingerprint) ===
      authority.expected_endpoint_fingerprint &&
    digest(evidence.privacy_acknowledgement_digest) ===
      authority.privacy_acknowledgement_digest &&
    digest(evidence.material_binding_digest) ===
      authority.reviewed_material_binding_digest &&
    runtimeBinding === authority.reviewed_runtime_interface_binding_digest &&
    (runtimeBinding !== null) === enabled
  );
}

function parseLanReviewResult(
  value: unknown,
  options: LanTargetReviewOptionsProjection,
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
    !reviewTargetMatchesOptions(target, profile.profile_id, options)
  ) {
    invalidLanResponse();
  }
  const evidence = lanDiscoveryMetadata(target.metadata);
  const privacyDigest = digest(record.privacy_acknowledgement_digest);
  const materialDigest = digest(record.material_binding_digest);
  const runtimeBinding = nullableDigest(
    evidence.reviewed_runtime_interface_binding_digest,
  );
  if (
    digest(evidence.privacy_acknowledgement_digest) !== privacyDigest ||
    digest(evidence.material_binding_digest) !== materialDigest ||
    (runtimeBinding !== null) !== options.enabled
  ) {
    invalidLanResponse();
  }
  return {
    profile,
    target,
    privacy_acknowledgement_digest: privacyDigest,
    material_binding_digest: materialDigest,
  };
}

function lanImportEvidenceMatches(
  metadata: Record<string, unknown>,
  endpointId: string,
  observationDigest: string,
  endpointFingerprint: string,
  outageObserved: boolean,
): boolean {
  const evidence = lanDiscoveryMetadata(metadata);
  return (
    digest(evidence.endpoint_binding_digest) === endpointId &&
    (outageObserved ||
      digest(evidence.observation_digest) === observationDigest) &&
    digest(evidence.endpoint_fingerprint) === endpointFingerprint
  );
}

function lanImportReplacementEvidenceMatches(
  metadata: Record<string, unknown>,
  expectedEndpointFingerprint: string,
  expectedMaterialBindingDigests: Set<string>,
): boolean {
  const evidence = lanDiscoveryMetadata(metadata);
  return (
    digest(evidence.endpoint_fingerprint) === expectedEndpointFingerprint &&
    expectedMaterialBindingDigests.has(digest(evidence.material_binding_digest))
  );
}

function lanDiscoveryMetadata(
  metadata: Record<string, unknown>,
): Record<string, unknown> {
  const evidence = metadata.lan_discovery;
  if (!isRecord(evidence)) invalidLanResponse();
  return evidence;
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

function exactRequestRecord(
  value: unknown,
  keys: readonly string[],
): asserts value is Record<string, unknown> {
  if (!isRecord(value)) invalidLanRequest();
  const actual = Object.keys(value);
  if (
    actual.length !== keys.length ||
    actual.some((key) => !keys.includes(key))
  ) {
    invalidLanRequest();
  }
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

function lanScanId(value: unknown): string {
  if (typeof value !== "string" || !LAN_SCAN_ID.test(value)) {
    invalidLanResponse();
  }
  return value;
}

function lanProfileId(value: unknown): string {
  if (typeof value !== "string" || !LAN_PROFILE_ID.test(value)) {
    invalidLanResponse();
  }
  return value;
}

function nullableLanProfileId(value: unknown): string | null {
  return value === null ? null : lanProfileId(value);
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

function nonnegativeInteger(value: unknown): number {
  const parsed = integer(value);
  if (parsed < 0) invalidLanResponse();
  return parsed;
}

function exactBoolean(value: unknown): boolean {
  if (typeof value !== "boolean") invalidLanResponse();
  return value;
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

function canonicalPreviewExpiry(value: unknown): string {
  const parsed = utcTimestamp(value);
  if (!parsed.endsWith("Z")) invalidLanResponse();
  return parsed;
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
