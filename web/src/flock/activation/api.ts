/**
 * Typed Flock activation client (Adaptive Flock plan, Task 18).
 *
 * Contract invariants:
 * - Only explicitly selected, qualified scope digests are sent; the exact
 *   receipt digest and run revision bind every activation.
 * - Grant effectiveness comes from the server evaluation (``effective``),
 *   never from ``grant.status === "active"``.
 * - Abstention/suspension reason codes pass through verbatim.
 */

import { getJson, postJson } from "../../api";
import type { FlockGrantStatus, FlockTransitionType } from "../types";
import type { QualificationScopePayload } from "../qualification/types";
import type {
  ActivationGrant,
  ActivationPreview,
  ActivationResult,
  ActivationScopePreview,
  ActivationTransition,
  CreateActivationInput,
  GrantEvaluation,
  ListActivationsOptions,
  PreviewActivationInput,
  RevokeActivationInput,
} from "./types";

const RUN_ID = /^qual_[0-9a-f]{24}$/;
const GRANT_ID = /^grant_[0-9a-f]{24}$/;
const RECEIPT_ID = /^rcpt_[0-9a-f]{24}$/;
const DIGEST = /^[0-9a-f]{64}$/;
const TIMESTAMP =
  /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)$/;
const MAX_RECORD_BYTES = 64 * 1_024;

const GRANT_STATUSES = new Set<FlockGrantStatus>([
  "inactive",
  "active",
  "suspended",
  "revoked",
]);
const TRANSITION_TYPES = new Set<FlockTransitionType>([
  "activated",
  "resumed",
  "suspended",
  "revoked",
]);
const RISK_LEVELS = new Set(["low", "medium", "high", "critical"]);
const BINDING_KEYS = [
  "target_inventory",
  "price",
  "policy",
  "learned",
  "project_authority",
] as const;

export async function previewActivation(
  input: PreviewActivationInput,
): Promise<ActivationPreview> {
  const payload = await postJson<unknown>("/api/flock/activations/preview", {
    receipt_id: requireReceiptId(input.receiptId),
    scope_digests: scopeDigestList(input.scopeDigests),
  });
  return parsePreview(payload);
}

export async function createActivation(
  input: CreateActivationInput,
): Promise<ActivationResult> {
  const payload = await postJson<unknown>("/api/flock/activations", {
    receipt_id: requireReceiptId(input.receiptId),
    scope_digests: scopeDigestList(input.scopeDigests),
    expected_receipt_digest: requireDigest(input.expectedReceiptDigest),
    expected_run_revision: positiveInteger(input.expectedRunRevision),
    bindings: {
      project_authority: boundedRecord(input.bindings.projectAuthority),
      target_snapshot: boundedRecord(input.bindings.targetSnapshot),
      price_snapshot: boundedRecord(input.bindings.priceSnapshot),
      policy_payload: boundedRecord(input.bindings.policyPayload),
      learned_payload: boundedRecord(input.bindings.learnedPayload),
    },
  });
  const record = exactRecord(payload, ["grants", "transitions", "superseded"]);
  if (!Array.isArray(record.grants) || record.grants.length > 64) {
    invalidResponse();
  }
  return {
    grants: record.grants.map(parseGrant),
    transitions: transitionList(record.transitions),
    superseded: transitionList(record.superseded),
  };
}

export async function listActivations(
  options: ListActivationsOptions = {},
): Promise<ActivationGrant[]> {
  const query =
    options.receiptId === undefined
      ? ""
      : `?receipt_id=${encodeURIComponent(requireReceiptId(options.receiptId))}`;
  const payload = await getJson<unknown>(`/api/flock/activations${query}`, {
    signal: options.signal,
  });
  const record = exactRecord(payload, ["grants"]);
  if (!Array.isArray(record.grants) || record.grants.length > 1_024) {
    invalidResponse();
  }
  return record.grants.map(parseGrant);
}

export async function evaluateActivation(
  grantId: string,
  signal?: AbortSignal,
): Promise<GrantEvaluation> {
  const payload = await getJson<unknown>(
    `/api/flock/activations/${requireGrantId(grantId)}/evaluate`,
    { signal },
  );
  return parseEvaluation(payload);
}

export async function revokeActivation(
  input: RevokeActivationInput,
): Promise<ActivationTransition> {
  const payload = await postJson<unknown>(
    `/api/flock/activations/${requireGrantId(input.grantId)}/revoke`,
    {
      expected_revision: positiveInteger(input.expectedRevision),
      reason: requireText(input.reason ?? "owner_revocation", 240),
    },
  );
  return parseTransition(payload);
}

/**
 * The only client-side effectiveness signal: the server evaluation.  An
 * ``active`` grant with drifted bindings or a failed receipt is not
 * effective; reason codes explain exactly why.
 */
export function isGrantEffective(evaluation: GrantEvaluation): boolean {
  return evaluation.effective;
}

/** Digest list of preview scopes whose receipt scope result is qualified. */
export function selectableScopeDigests(preview: ActivationPreview): string[] {
  return preview.scopes
    .filter((scope) => scope.qualified)
    .map((scope) => scope.scope_digest);
}

// --- response parsers ----------------------------------------------------------

function parsePreview(value: unknown): ActivationPreview {
  const record = exactRecord(value, [
    "receipt_id",
    "run_id",
    "run_revision",
    "owner_principal",
    "receipt_digest",
    "scopes",
    "replay",
    "target_snapshot",
    "price_snapshot",
    "binding_digests",
    "binding_changes",
    "authority_changed",
    "suspension_conditions",
    "revocation_behavior",
  ]);
  if (!Array.isArray(record.scopes) || record.scopes.length > 64) {
    invalidResponse();
  }
  if (typeof record.authority_changed !== "boolean") invalidResponse();
  if (record.replay !== null) boundedRecordResponse(record.replay);
  return {
    receipt_id: receiptIdResponse(record.receipt_id),
    run_id: runIdResponse(record.run_id),
    run_revision: positiveIntegerResponse(record.run_revision),
    owner_principal: textResponse(record.owner_principal, 240),
    receipt_digest: digestResponse(record.receipt_digest),
    scopes: record.scopes.map(parseScopePreview),
    replay:
      record.replay === null
        ? null
        : boundedRecordResponse(record.replay),
    target_snapshot: boundedRecordResponse(record.target_snapshot),
    price_snapshot: boundedRecordResponse(record.price_snapshot),
    binding_digests: digestMap(record.binding_digests),
    binding_changes: bindingChanges(record.binding_changes),
    authority_changed: record.authority_changed,
    suspension_conditions: textListResponse(
      record.suspension_conditions,
      32,
      240,
    ),
    revocation_behavior: textResponse(record.revocation_behavior, 512),
  };
}

function parseScopePreview(value: unknown): ActivationScopePreview {
  const record = exactRecord(value, [
    "scope_digest",
    "project_id",
    "task_family",
    "risk",
    "capabilities",
    "static_target_id",
    "selected_target_id",
    "alternative_target_ids",
    "total_support",
    "selected_target_support",
    "confidence",
    "static_utility",
    "learned_utility",
    "utility_delta",
    "cost_coverage",
    "estimated_savings_usd",
    "guardrail_violations",
    "reasons",
    "qualified",
  ]);
  if (
    !RISK_LEVELS.has(String(record.risk)) ||
    typeof record.qualified !== "boolean"
  ) {
    invalidResponse();
  }
  const qualified = record.qualified;
  const selectedTarget = nullableTextResponse(record.selected_target_id, 240);
  if (!qualified && selectedTarget !== null) invalidResponse();
  return {
    scope_digest: digestResponse(record.scope_digest),
    project_id: textResponse(record.project_id, 240),
    task_family: textResponse(record.task_family, 240),
    risk: record.risk as ActivationScopePreview["risk"],
    capabilities: textListResponse(record.capabilities, 32, 240, 1),
    static_target_id: textResponse(record.static_target_id, 240),
    selected_target_id: selectedTarget,
    alternative_target_ids: textListResponse(
      record.alternative_target_ids,
      64,
      240,
    ),
    total_support: nonnegativeInteger(record.total_support),
    selected_target_support: nonnegativeInteger(record.selected_target_support),
    confidence: finiteNumber(record.confidence),
    static_utility: nullableFiniteNumber(record.static_utility),
    learned_utility: nullableFiniteNumber(record.learned_utility),
    utility_delta: finiteNumber(record.utility_delta),
    cost_coverage: finiteNumber(record.cost_coverage),
    estimated_savings_usd: nullableFiniteNumber(record.estimated_savings_usd),
    guardrail_violations: nonnegativeInteger(record.guardrail_violations),
    reasons: textListResponse(record.reasons, 64, 240),
    qualified,
  };
}

function parseGrant(value: unknown): ActivationGrant {
  const record = exactRecord(value, [
    "grant_id",
    "run_id",
    "target_id",
    "scope",
    "scope_digest",
    "policy_id",
    "policy_revision",
    "qualification_receipt_id",
    "created_by",
    "created_at",
  ]);
  return {
    grant_id: grantIdResponse(record.grant_id),
    run_id: runIdResponse(record.run_id),
    target_id: textResponse(record.target_id, 240),
    scope: parseScopePayload(record.scope),
    scope_digest: digestResponse(record.scope_digest),
    policy_id: textResponse(record.policy_id, 240),
    policy_revision: positiveIntegerResponse(record.policy_revision),
    qualification_receipt_id: receiptIdResponse(record.qualification_receipt_id),
    created_by: textResponse(record.created_by, 240),
    created_at: timestamp(record.created_at),
  };
}

function parseTransition(value: unknown): ActivationTransition {
  const record = exactRecord(value, [
    "transition_id",
    "grant_id",
    "sequence",
    "transition_type",
    "reason",
    "receipt_id",
    "created_at",
  ]);
  const transitionType = record.transition_type;
  if (
    typeof transitionType !== "string" ||
    !TRANSITION_TYPES.has(transitionType as FlockTransitionType)
  ) {
    invalidResponse();
  }
  const grantId = grantIdResponse(record.grant_id);
  const sequence = positiveIntegerResponse(record.sequence);
  const transitionId = textResponse(record.transition_id, 280);
  if (transitionId !== `${grantId}:${sequence}`) invalidResponse();
  return {
    transition_id: transitionId,
    grant_id: grantId,
    sequence,
    transition_type: transitionType as FlockTransitionType,
    reason: textResponse(record.reason, 240),
    receipt_id: nullableReceiptId(record.receipt_id),
    created_at: timestamp(record.created_at),
  };
}

function parseEvaluation(value: unknown): GrantEvaluation {
  const record = exactRecord(value, [
    "grant_id",
    "run_id",
    "scope_digest",
    "status",
    "effective",
    "reason_codes",
    "receipt_authenticates",
    "binding_changes",
    "latest_transition",
    "transition_count",
  ]);
  const status = record.status;
  if (
    typeof status !== "string" ||
    !GRANT_STATUSES.has(status as FlockGrantStatus) ||
    typeof record.effective !== "boolean" ||
    typeof record.receipt_authenticates !== "boolean"
  ) {
    invalidResponse();
  }
  const transitionCount = nonnegativeInteger(record.transition_count);
  const latest = record.latest_transition;
  if ((latest === null) !== (transitionCount === 0)) invalidResponse();
  return {
    grant_id: grantIdResponse(record.grant_id),
    run_id: runIdResponse(record.run_id),
    scope_digest: digestResponse(record.scope_digest),
    status: status as FlockGrantStatus,
    effective: record.effective,
    reason_codes: textListResponse(record.reason_codes, 32, 240),
    receipt_authenticates: record.receipt_authenticates,
    binding_changes: bindingChanges(record.binding_changes),
    latest_transition: latest === null ? null : parseTransition(latest),
    transition_count: transitionCount,
  };
}

function parseScopePayload(value: unknown): QualificationScopePayload {
  const record = exactRecord(value, [
    "project_id",
    "task_family",
    "risk",
    "capability_key",
    "policy_id",
    "policy_revision",
    "target_ids",
    "target_inventory_digest",
    "price_digest",
    "learned_config_digest",
    "project_authority_digest",
  ]);
  if (!RISK_LEVELS.has(String(record.risk))) invalidResponse();
  return {
    project_id: textResponse(record.project_id, 240),
    task_family: textResponse(record.task_family, 240),
    risk: record.risk as QualificationScopePayload["risk"],
    capability_key: textResponse(record.capability_key, 512),
    policy_id: textResponse(record.policy_id, 240),
    policy_revision: positiveIntegerResponse(record.policy_revision),
    target_ids: textListResponse(record.target_ids, 64, 240, 2),
    target_inventory_digest: digestResponse(record.target_inventory_digest),
    price_digest: digestResponse(record.price_digest),
    learned_config_digest: digestResponse(record.learned_config_digest),
    project_authority_digest: digestResponse(record.project_authority_digest),
  };
}

function transitionList(value: unknown): ActivationTransition[] {
  if (!Array.isArray(value) || value.length > 64) invalidResponse();
  return value.map(parseTransition);
}

function digestMap(value: unknown): Record<string, string> {
  if (!isRecord(value)) invalidResponse();
  const entries = Object.entries(value);
  if (entries.length > 16) invalidResponse();
  return Object.fromEntries(
    entries.map(([key, item]) => [textResponse(key, 64), digestResponse(item)]),
  );
}

function bindingChanges(value: unknown): Record<string, boolean> {
  if (!isRecord(value)) invalidResponse();
  const entries = Object.entries(value);
  if (entries.length > 16) invalidResponse();
  return Object.fromEntries(
    entries.map(([key, item]) => {
      if (
        !BINDING_KEYS.includes(key as (typeof BINDING_KEYS)[number]) ||
        typeof item !== "boolean"
      ) {
        invalidResponse();
      }
      return [key, item];
    }),
  );
}

// --- scalar validators ---------------------------------------------------------

function invalidRequest(): never {
  throw new Error("flock_activation_request_invalid");
}

function invalidResponse(): never {
  throw new Error("flock_activation_response_invalid");
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function exactRecord(
  value: unknown,
  keys: readonly string[],
): Record<string, unknown> {
  if (!isRecord(value)) invalidResponse();
  for (const key of keys) {
    if (!(key in value)) invalidResponse();
  }
  for (const key of Object.keys(value)) {
    if (!keys.includes(key)) invalidResponse();
  }
  return value;
}

function byteLength(text: string): number {
  return new TextEncoder().encode(text).length;
}

function requireText(value: string, maximum: number): string {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value.length > maximum
  ) {
    invalidRequest();
  }
  return value;
}

function positiveInteger(value: number): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 1) {
    invalidRequest();
  }
  return value;
}

function requireDigest(value: string): string {
  if (typeof value !== "string" || !DIGEST.test(value)) invalidRequest();
  return value;
}

function scopeDigestList(value: string[]): string[] {
  if (!Array.isArray(value) || value.length === 0 || value.length > 64) {
    invalidRequest();
  }
  return value.map((item) => requireDigest(item));
}

function boundedRecord(value: Record<string, unknown>): Record<string, unknown> {
  if (!isRecord(value)) invalidRequest();
  if (byteLength(JSON.stringify(value)) > MAX_RECORD_BYTES) invalidRequest();
  return { ...value };
}

function requireReceiptId(value: string): string {
  if (typeof value !== "string" || !RECEIPT_ID.test(value)) invalidRequest();
  return value;
}

function requireGrantId(value: string): string {
  if (typeof value !== "string" || !GRANT_ID.test(value)) invalidRequest();
  return value;
}

function textResponse(value: unknown, maximum: number): string {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value.length > maximum
  ) {
    invalidResponse();
  }
  return value;
}

function nullableTextResponse(value: unknown, maximum: number): string | null {
  if (value === null) return null;
  return textResponse(value, maximum);
}

function textListResponse(
  value: unknown,
  maximumItems: number,
  maximumLength: number,
  minimumItems = 0,
): string[] {
  if (
    !Array.isArray(value) ||
    value.length < minimumItems ||
    value.length > maximumItems ||
    value.some(
      (item) =>
        typeof item !== "string" ||
        item.length === 0 ||
        item.length > maximumLength,
    )
  ) {
    invalidResponse();
  }
  return [...value] as string[];
}

function timestamp(value: unknown): string {
  if (typeof value !== "string" || !TIMESTAMP.test(value)) invalidResponse();
  return value;
}

function nonnegativeInteger(value: unknown): number {
  if (!Number.isSafeInteger(value) || Number(value) < 0) invalidResponse();
  return Number(value);
}

function positiveIntegerResponse(value: unknown): number {
  if (!Number.isSafeInteger(value) || Number(value) < 1) invalidResponse();
  return Number(value);
}

function finiteNumber(value: unknown): number {
  if (typeof value !== "number" || !Number.isFinite(value)) invalidResponse();
  return value;
}

function nullableFiniteNumber(value: unknown): number | null {
  if (value === null) return null;
  return finiteNumber(value);
}

function digestResponse(value: unknown): string {
  if (typeof value !== "string" || !DIGEST.test(value)) invalidResponse();
  return value;
}

function runIdResponse(value: unknown): string {
  if (typeof value !== "string" || !RUN_ID.test(value)) invalidResponse();
  return value;
}

function grantIdResponse(value: unknown): string {
  if (typeof value !== "string" || !GRANT_ID.test(value)) invalidResponse();
  return value;
}

function receiptIdResponse(value: unknown): string {
  if (typeof value !== "string" || !RECEIPT_ID.test(value)) invalidResponse();
  return value;
}

function nullableReceiptId(value: unknown): string | null {
  if (value === null) return null;
  return receiptIdResponse(value);
}

function boundedRecordResponse(value: unknown): Record<string, unknown> {
  if (!isRecord(value)) invalidResponse();
  if (byteLength(JSON.stringify(value)) > MAX_RECORD_BYTES) invalidResponse();
  return { ...value };
}
