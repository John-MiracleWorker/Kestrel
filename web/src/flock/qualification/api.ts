/**
 * Typed Flock qualification client (Adaptive Flock plan, Task 18).
 *
 * Contract invariants:
 * - Money is decimal text end to end ("37.25"); the client never parses a
 *   cap into a JS float and never sends a number as the mutation authority.
 * - Qualified scopes come from each receipt scope result (``state``), never
 *   from ``status === "completed"``.
 * - Abstention/suspension reason codes pass through verbatim.
 * - SSE accelerates run state; the GET payload remains the authority.
 */

import { ApiAuthError, ApiResponseError, getJson, postJson } from "../../api";
import { apiAuthHeaders } from "../../auth";
import { runtimeTransport } from "../../platform/runtimeTransport";
import type {
  FlockRunStatus,
  FlockScopeQualificationState,
  FlockTerminalRunStatus,
} from "../types";
import type {
  CreateQualificationInput,
  LowerQualificationCapInput,
  PreviewQualificationInput,
  QualificationAction,
  QualificationCorpusItemInput,
  QualificationEvent,
  QualificationEventStreamOptions,
  QualificationEventType,
  QualificationLifecycleInput,
  QualificationPreview,
  QualificationReceipt,
  QualificationRun,
  QualificationRunCaps,
  QualificationRunSpend,
  QualificationScopeInput,
  QualificationScopePayload,
  QualificationThresholdsInput,
  ScopeQualificationResult,
} from "./types";

const RUN_ID = /^qual_[0-9a-f]{24}$/;
const RECEIPT_ID = /^rcpt_[0-9a-f]{24}$/;
const DIGEST = /^[0-9a-f]{64}$/;
const USD_TEXT = /^[0-9]{1,9}(\.[0-9]{1,6})?$/;
const TIMESTAMP =
  /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)$/;
const EVENT_SEQUENCE = /^(?:0|[1-9][0-9]{0,18})$/;
const MAX_EVENT_SEQUENCE = 9_223_372_036_854_775_807n;
const MAX_EVENT_FRAME_BYTES = 16 * 1_024;
const MAX_RECORD_BYTES = 64 * 1_024;

const RUN_STATUSES = new Set<FlockRunStatus>([
  "draft",
  "ready",
  "running",
  "pausing",
  "paused",
  "cancelled",
  "failed",
  "completed",
]);
const TERMINAL_STATUSES = new Set<FlockRunStatus>([
  "cancelled",
  "failed",
  "completed",
]);
const SCOPE_STATES = new Set<FlockScopeQualificationState>([
  "qualified",
  "abstained",
  "deterministic_only",
]);
const EVENT_TYPES = new Set<QualificationEventType>([
  "run_completed",
  "run_failed",
  "run_cancelled",
  "budget_projection_overrun",
]);
const RISK_LEVELS = new Set(["low", "medium", "high", "critical"]);
const EVIDENCE_KINDS = new Set(["synthetic", "real_project"]);
const PRIVACY_CLASSES = new Set([
  "local_required",
  "local_preferred",
  "approved_cloud",
  "any",
]);

export async function previewQualification(
  input: PreviewQualificationInput,
): Promise<QualificationPreview> {
  const payload = await postJson<unknown>("/api/flock/qualifications/preview", {
    project_id: requireText(input.projectId, 240),
    task_families: textList(input.taskFamilies, 64, 240),
    corpus: input.corpus.map(corpusBody),
    policy_id: requireText(input.policyId ?? "balanced", 240),
    policy_revision: positiveInteger(input.policyRevision ?? 1),
    maximum_spend_usd: usdText(input.maximumSpendUsd ?? "50.00"),
    default_privacy_class: privacyClass(input.defaultPrivacyClass ?? "approved_cloud"),
    project_authority: boundedRecord(input.projectAuthority ?? {}),
    learned_config: boundedRecord(input.learnedConfig ?? {}),
  });
  return parsePreview(payload);
}

export async function createQualification(
  input: CreateQualificationInput,
): Promise<QualificationRun> {
  const body: Record<string, unknown> = {
    scope: scopeBody(input.scope),
    corpus: input.corpus.map(corpusBody),
    target_snapshot: boundedRecord(input.targetSnapshot),
    price_snapshot: boundedRecord(input.priceSnapshot),
    policy_payload: boundedRecord(input.policyPayload),
    learned_payload: boundedRecord(input.learnedPayload),
    project_authority: boundedRecord(input.projectAuthority),
    maximum_spend_usd: usdText(input.maximumSpendUsd ?? "50.00"),
    attempt_ceiling_usd: usdText(input.attemptCeilingUsd ?? "5.00"),
  };
  if (input.thresholds !== undefined) {
    body.thresholds = thresholdsBody(input.thresholds);
  }
  if (input.build !== undefined) body.build = boundedRecord(input.build);
  if (input.effectiveStopCapUsd !== undefined) {
    body.effective_stop_cap_usd = usdText(input.effectiveStopCapUsd);
  }
  const payload = await postJson<unknown>("/api/flock/qualifications", body);
  return parseRun(payload);
}

export async function listQualifications(
  signal?: AbortSignal,
): Promise<QualificationRun[]> {
  const payload = await getJson<unknown>("/api/flock/qualifications", {
    signal,
  });
  const record = exactRecord(payload, ["runs"]);
  if (!Array.isArray(record.runs) || record.runs.length > 512) {
    invalidResponse();
  }
  return record.runs.map(parseRun);
}

export async function getQualification(
  runId: string,
  signal?: AbortSignal,
): Promise<QualificationRun> {
  const payload = await getJson<unknown>(
    `/api/flock/qualifications/${requireRunId(runId)}`,
    { signal },
  );
  return parseRun(payload);
}

export async function startQualification(
  input: QualificationLifecycleInput,
): Promise<QualificationRun> {
  return lifecycle(input, "start");
}

export async function pauseQualification(
  input: QualificationLifecycleInput,
): Promise<QualificationRun> {
  return lifecycle(input, "pause");
}

export async function resumeQualification(
  input: QualificationLifecycleInput,
): Promise<QualificationRun> {
  return lifecycle(input, "resume");
}

export async function cancelQualification(
  input: QualificationLifecycleInput,
): Promise<QualificationRun> {
  return lifecycle(input, "cancel");
}

export async function lowerQualificationCap(
  input: LowerQualificationCapInput,
): Promise<QualificationRun> {
  const payload = await postJson<unknown>(
    `/api/flock/qualifications/${requireRunId(input.runId)}/lower-cap`,
    {
      maximum_spend_usd: usdText(input.maximumSpendUsd),
      expected_revision: positiveInteger(input.expectedRevision),
    },
  );
  return parseRun(payload);
}

export async function getQualificationReceipt(
  runId: string,
  signal?: AbortSignal,
): Promise<QualificationReceipt> {
  const payload = await getJson<unknown>(
    `/api/flock/qualifications/${requireRunId(runId)}/receipt`,
    { signal },
  );
  return parseReceipt(payload);
}

export async function streamQualificationEvents(
  runId: string,
  options: QualificationEventStreamOptions,
): Promise<void> {
  const identifier = requireRunId(runId);
  const initialCursor = eventSequence(options.afterSequence ?? "0");
  const transport = runtimeTransport(apiAuthHeaders);
  const response = await transport.fetch(
    `/api/flock/qualifications/${identifier}/events`,
    {
      signal: options.signal,
      headers: {
        Accept: "text/event-stream",
        "Last-Event-ID": initialCursor,
      },
    },
  );
  if (!response.ok) {
    await throwStreamResponseError(response);
  }
  const contentType = response.headers.get("content-type") ?? "";
  if (!/^text\/event-stream(?:\s*;\s*charset=utf-8)?$/i.test(contentType)) {
    throw new Error("flock_event_stream_invalid");
  }
  if (response.body === null) return;

  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8", { fatal: true });
  let buffer = "";
  let cursor = initialCursor;
  const emitCompleteFrames = () => {
    for (;;) {
      const boundary = /\r?\n\r?\n/.exec(buffer);
      if (boundary === null) break;
      const frame = buffer.slice(0, boundary.index);
      buffer = buffer.slice(boundary.index + boundary[0].length);
      const event = parseEventFrame(frame, cursor);
      if (event !== null) {
        cursor = event.sequence;
        options.onEvent(event);
      }
    }
    if (byteLength(buffer) > MAX_EVENT_FRAME_BYTES) {
      throw new Error("flock_event_stream_invalid");
    }
  };
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      try {
        buffer += decoder.decode(value, { stream: true });
      } catch {
        throw new Error("flock_event_stream_invalid");
      }
      emitCompleteFrames();
    }
    try {
      buffer += decoder.decode();
    } catch {
      throw new Error("flock_event_stream_invalid");
    }
    emitCompleteFrames();
    if (buffer.trim()) throw new Error("flock_event_stream_invalid");
  } catch (error) {
    await reader.cancel().catch(() => undefined);
    throw error;
  } finally {
    reader.releaseLock();
  }
}

/**
 * Actions offered for a run.  The effective stop cap can only move down once
 * the run exists, so ``lower_cap`` is the only cap action and ``raise_cap``
 * is never produced — for any status.
 */
export function qualificationActions(
  run: QualificationRun,
): QualificationAction[] {
  if (TERMINAL_STATUSES.has(run.status)) return [];
  const actions: QualificationAction[] = [];
  if (run.status === "draft" || run.status === "ready") actions.push("start");
  if (run.status === "running") actions.push("pause");
  if (run.status === "paused") actions.push("resume");
  actions.push("cancel", "lower_cap");
  return actions;
}

/**
 * Qualified scope digests read from each receipt scope result.  A completed
 * run with abstained or deterministic-only scopes yields none.
 */
export function qualifiedScopeDigests(receipt: QualificationReceipt): string[] {
  return receipt.payload.scopes
    .filter((scope) => scope.state === "qualified" && scope.qualified)
    .map((scope) => scope.scope_digest);
}

async function lifecycle(
  input: QualificationLifecycleInput,
  action: "start" | "pause" | "resume" | "cancel",
): Promise<QualificationRun> {
  const payload = await postJson<unknown>(
    `/api/flock/qualifications/${requireRunId(input.runId)}/${action}`,
    { expected_revision: positiveInteger(input.expectedRevision) },
  );
  return parseRun(payload);
}

// --- request bodies -----------------------------------------------------------

function corpusBody(item: QualificationCorpusItemInput): Record<string, unknown> {
  if (!RISK_LEVELS.has(item.risk) || !EVIDENCE_KINDS.has(item.evidenceKind)) {
    invalidRequest();
  }
  return {
    item_id: requireText(item.itemId, 240),
    task_family: requireText(item.taskFamily, 240),
    risk: item.risk,
    capabilities: textList(item.capabilities, 32, 240, 1),
    task_contract_digest: digest(item.taskContractDigest),
    acceptance_plan_digest: digest(item.acceptancePlanDigest),
    evidence_kind: item.evidenceKind,
    actionable: item.actionable ?? true,
    exclusion_reasons: textList(item.exclusionReasons ?? [], 32, 240),
  };
}

function scopeBody(scope: QualificationScopeInput): Record<string, unknown> {
  if (!RISK_LEVELS.has(scope.risk)) invalidRequest();
  return {
    project_id: requireText(scope.projectId, 240),
    task_family: requireText(scope.taskFamily, 240),
    risk: scope.risk,
    capability_key: requireText(scope.capabilityKey, 512),
    policy_id: requireText(scope.policyId, 240),
    policy_revision: positiveInteger(scope.policyRevision),
    target_ids: textList(scope.targetIds, 64, 240, 2),
    target_inventory_digest: digest(scope.targetInventoryDigest),
    price_digest: digest(scope.priceDigest),
    learned_config_digest: digest(scope.learnedConfigDigest),
    project_authority_digest: digest(scope.projectAuthorityDigest),
  };
}

function thresholdsBody(
  thresholds: QualificationThresholdsInput,
): Record<string, unknown> {
  const body: Record<string, unknown> = {};
  const integer = (value: number | undefined, minimum: number) => {
    if (value === undefined) return undefined;
    if (!Number.isSafeInteger(value) || value < minimum) invalidRequest();
    return value;
  };
  const ratio = (value: number | undefined, minimum: number, maximum: number) => {
    if (value === undefined) return undefined;
    if (
      typeof value !== "number" ||
      !Number.isFinite(value) ||
      value < minimum ||
      value > maximum
    ) {
      invalidRequest();
    }
    return value;
  };
  const entries: [string, number | undefined][] = [
    ["min_examples_per_scope", integer(thresholds.minExamplesPerScope, 1)],
    ["min_examples_per_target", integer(thresholds.minExamplesPerTarget, 1)],
    ["confidence_threshold", ratio(thresholds.confidenceThreshold, 0, 1)],
    ["utility_margin", ratio(thresholds.utilityMargin, 0, Number.MAX_SAFE_INTEGER)],
    [
      "cost_coverage_threshold",
      ratio(thresholds.costCoverageThreshold, 0, 1),
    ],
    ["decay_half_life_days", integer(thresholds.decayHalfLifeDays, 1)],
    ["max_guardrail_violations", integer(thresholds.maxGuardrailViolations, 0)],
    ["replay_runs", integer(thresholds.replayRuns, 1)],
    ["replay_successes_required", integer(thresholds.replaySuccessesRequired, 1)],
  ];
  for (const [key, value] of entries) {
    if (value !== undefined) body[key] = value;
  }
  return body;
}

// --- response parsers ----------------------------------------------------------

function parsePreview(value: unknown): QualificationPreview {
  const record = exactRecord(value, [
    "schema",
    "created_at",
    "scopes",
    "excluded_scopes",
    "target_snapshot_digest",
    "target_ids",
    "excluded_targets",
    "start_blockers",
    "warnings",
    "matrix_size",
    "estimated_reserved_cost_range",
    "policy_digest",
    "corpus_digest",
    "project_authority_digest",
    "target_inventory_digest",
    "learned_config_digest",
    "budget",
    "preview_digest",
  ]);
  if (record.schema !== "kestrel.flock.qualification_preview.v1") {
    invalidResponse();
  }
  if (!Array.isArray(record.scopes) || record.scopes.length > 1_024) {
    invalidResponse();
  }
  const budget = exactRecord(record.budget, [
    "maximum_spend_micros",
    "maximum_spend_usd",
    "estimated_reserved_cost_range_micros",
  ]);
  const maximumMicros = micros(budget.maximum_spend_micros);
  const maximumUsd = usdTextResponse(budget.maximum_spend_usd);
  if (usdTextToMicros(maximumUsd) !== maximumMicros) invalidResponse();
  return {
    schema: "kestrel.flock.qualification_preview.v1",
    created_at: timestamp(record.created_at),
    scopes: record.scopes.map(parseScopePayload),
    excluded_scopes: reasonMap(record.excluded_scopes),
    target_snapshot_digest: digestResponse(record.target_snapshot_digest),
    target_ids: textListResponse(record.target_ids, 1_024, 240),
    excluded_targets: reasonMap(record.excluded_targets),
    start_blockers: reasonMap(record.start_blockers),
    warnings: reasonMap(record.warnings),
    matrix_size: nonnegativeInteger(record.matrix_size),
    estimated_reserved_cost_range: microsRange(
      record.estimated_reserved_cost_range,
    ),
    policy_digest: digestResponse(record.policy_digest),
    corpus_digest: digestResponse(record.corpus_digest),
    project_authority_digest: digestResponse(record.project_authority_digest),
    target_inventory_digest: digestResponse(record.target_inventory_digest),
    learned_config_digest: digestResponse(record.learned_config_digest),
    budget: {
      maximum_spend_micros: maximumMicros,
      maximum_spend_usd: maximumUsd,
      estimated_reserved_cost_range_micros: microsRange(
        budget.estimated_reserved_cost_range_micros,
      ),
    },
    preview_digest: digestResponse(record.preview_digest),
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

function parseRun(value: unknown): QualificationRun {
  const record = exactRecord(value, [
    "run_id",
    "status",
    "revision",
    "owner_principal",
    "scope_digest",
    "corpus_digest",
    "target_digest",
    "price_digest",
    "policy_digest",
    "learned_digest",
    "project_authority_digest",
    "thresholds_digest",
    "build_digest",
    "caps",
    "spend",
    "blockers",
    "created_at",
    "updated_at",
    "started_at",
    "finished_at",
    "terminal_reason",
  ]);
  const status = record.status;
  if (typeof status !== "string" || !RUN_STATUSES.has(status as FlockRunStatus)) {
    invalidResponse();
  }
  const terminal = TERMINAL_STATUSES.has(status as FlockRunStatus);
  const startedAt = nullableTimestamp(record.started_at);
  const finishedAt = nullableTimestamp(record.finished_at);
  const terminalReason = nullableTextResponse(record.terminal_reason, 240);
  if (terminal && finishedAt === null) invalidResponse();
  if (!terminal && finishedAt !== null) invalidResponse();
  return {
    run_id: runIdResponse(record.run_id),
    status: status as FlockRunStatus,
    revision: positiveIntegerResponse(record.revision),
    owner_principal: textResponse(record.owner_principal, 240),
    scope_digest: digestResponse(record.scope_digest),
    corpus_digest: digestResponse(record.corpus_digest),
    target_digest: digestResponse(record.target_digest),
    price_digest: digestResponse(record.price_digest),
    policy_digest: digestResponse(record.policy_digest),
    learned_digest: digestResponse(record.learned_digest),
    project_authority_digest: digestResponse(record.project_authority_digest),
    thresholds_digest: digestResponse(record.thresholds_digest),
    build_digest: digestResponse(record.build_digest),
    caps: parseCaps(record.caps),
    spend: parseSpend(record.spend),
    blockers: textListResponse(record.blockers, 64, 240),
    created_at: timestamp(record.created_at),
    updated_at: timestamp(record.updated_at),
    started_at: startedAt,
    finished_at: finishedAt,
    terminal_reason: terminalReason,
  };
}

function parseCaps(value: unknown): QualificationRunCaps {
  const record = exactRecord(value, [
    "max_spend_micros",
    "max_spend_usd",
    "effective_stop_cap_micros",
    "effective_stop_cap_usd",
    "attempt_ceiling_micros",
    "attempt_ceiling_usd",
  ]);
  const maxMicros = micros(record.max_spend_micros);
  const maxUsd = usdTextResponse(record.max_spend_usd);
  const stopMicros = micros(record.effective_stop_cap_micros);
  const stopUsd = usdTextResponse(record.effective_stop_cap_usd);
  const ceilingMicros = micros(record.attempt_ceiling_micros);
  const ceilingUsd = usdTextResponse(record.attempt_ceiling_usd);
  if (
    usdTextToMicros(maxUsd) !== maxMicros ||
    usdTextToMicros(stopUsd) !== stopMicros ||
    usdTextToMicros(ceilingUsd) !== ceilingMicros ||
    stopMicros > maxMicros
  ) {
    invalidResponse();
  }
  return {
    max_spend_micros: maxMicros,
    max_spend_usd: maxUsd,
    effective_stop_cap_micros: stopMicros,
    effective_stop_cap_usd: stopUsd,
    attempt_ceiling_micros: ceilingMicros,
    attempt_ceiling_usd: ceilingUsd,
  };
}

function parseSpend(value: unknown): QualificationRunSpend {
  const record = exactRecord(value, [
    "actual_spend_micros",
    "actual_spend_usd",
    "unresolved_reserve_micros",
    "inflight_reserve_micros",
  ]);
  const actualMicros = micros(record.actual_spend_micros);
  const actualUsd = usdTextResponse(record.actual_spend_usd);
  if (usdTextToMicros(actualUsd) !== actualMicros) invalidResponse();
  return {
    actual_spend_micros: actualMicros,
    actual_spend_usd: actualUsd,
    unresolved_reserve_micros: micros(record.unresolved_reserve_micros),
    inflight_reserve_micros: micros(record.inflight_reserve_micros),
  };
}

function parseReceipt(value: unknown): QualificationReceipt {
  const record = exactRecord(value, [
    "receipt_id",
    "run_id",
    "receipt_type",
    "payload_digest",
    "payload",
    "created_at",
  ]);
  if (record.receipt_type !== "run_terminal") invalidResponse();
  if (!isRecord(record.payload)) invalidResponse();
  const status = record.payload.status;
  if (
    typeof status !== "string" ||
    !TERMINAL_STATUSES.has(status as FlockRunStatus)
  ) {
    invalidResponse();
  }
  if (!Array.isArray(record.payload.scopes) || record.payload.scopes.length > 1_024) {
    invalidResponse();
  }
  const scopes = record.payload.scopes.map(parseScopeResult);
  const qualifying = record.payload.qualifying === true;
  if (record.payload.qualifying !== qualifying) invalidResponse();
  const anyQualified = scopes.some((scope) => scope.qualified);
  if (qualifying !== (anyQualified && status === "completed")) {
    invalidResponse();
  }
  const payload: QualificationReceipt["payload"] = {
    ...record.payload,
    schema: textResponse(record.payload.schema, 128),
    status: status as FlockTerminalRunStatus,
    terminal_reason: textResponse(record.payload.terminal_reason, 240),
    qualifying,
    scopes,
  };
  return {
    receipt_id: receiptIdResponse(record.receipt_id),
    run_id: runIdResponse(record.run_id),
    receipt_type: "run_terminal",
    payload_digest: digestResponse(record.payload_digest),
    payload,
    created_at: timestamp(record.created_at),
  };
}

function parseScopeResult(value: unknown): ScopeQualificationResult {
  const record = exactRecord(value, [
    "scope_digest",
    "state",
    "qualified",
    "static_target_id",
    "selected_target_id",
    "total_support",
    "selected_target_support",
    "confidence",
    "static_utility",
    "learned_utility",
    "utility_delta",
    "cost_coverage",
    "estimated_savings_usd",
    "estimated_regret_usd",
    "guardrail_violations",
    "evaluated_target_ids",
    "reasons",
    "router_state",
    "thresholds_digest",
  ]);
  const state = record.state;
  if (
    typeof state !== "string" ||
    !SCOPE_STATES.has(state as FlockScopeQualificationState) ||
    record.qualified !== (state === "qualified")
  ) {
    invalidResponse();
  }
  const selectedTarget = nullableTextResponse(record.selected_target_id, 240);
  if (state !== "qualified" && selectedTarget !== null) invalidResponse();
  return {
    scope_digest: digestResponse(record.scope_digest),
    state: state as FlockScopeQualificationState,
    qualified: record.qualified === true,
    static_target_id: textResponse(record.static_target_id, 240),
    selected_target_id: selectedTarget,
    total_support: nonnegativeInteger(record.total_support),
    selected_target_support: nonnegativeInteger(record.selected_target_support),
    confidence: finiteNumber(record.confidence),
    static_utility: nullableFiniteNumber(record.static_utility),
    learned_utility: nullableFiniteNumber(record.learned_utility),
    utility_delta: finiteNumber(record.utility_delta),
    cost_coverage: finiteNumber(record.cost_coverage),
    estimated_savings_usd: nullableFiniteNumber(record.estimated_savings_usd),
    estimated_regret_usd: nullableFiniteNumber(record.estimated_regret_usd),
    guardrail_violations: nonnegativeInteger(record.guardrail_violations),
    evaluated_target_ids: textListResponse(record.evaluated_target_ids, 64, 240),
    reasons: textListResponse(record.reasons, 64, 240),
    router_state: boundedRecordResponse(record.router_state),
    thresholds_digest: digestResponse(record.thresholds_digest),
  };
}

function parseEventFrame(
  frame: string,
  afterSequence: string,
): QualificationEvent | null {
  if (byteLength(frame) > MAX_EVENT_FRAME_BYTES) {
    throw new Error("flock_event_stream_invalid");
  }
  const ids: string[] = [];
  const eventTypes: string[] = [];
  const data: string[] = [];
  for (const line of frame.split(/\r?\n/)) {
    if (!line || line.startsWith(":")) continue;
    const separator = line.indexOf(":");
    const field = separator < 0 ? line : line.slice(0, separator);
    const rawValue = separator < 0 ? "" : line.slice(separator + 1);
    const value = rawValue.startsWith(" ") ? rawValue.slice(1) : rawValue;
    if (field === "id") ids.push(value);
    else if (field === "event") eventTypes.push(value);
    else if (field === "data") data.push(value);
    else throw new Error("flock_event_stream_invalid");
  }
  if (ids.length === 0 && eventTypes.length === 0 && data.length === 0) {
    return null;
  }
  if (ids.length !== 1 || eventTypes.length !== 1 || data.length === 0) {
    throw new Error("flock_event_stream_invalid");
  }
  let value: unknown;
  try {
    value = JSON.parse(data.join("\n"));
  } catch {
    throw new Error("flock_event_stream_invalid");
  }
  const envelope = exactRecord(value, [
    "sequence",
    "event_type",
    "payload",
    "created_at",
  ]);
  const sequence = eventSequence(ids[0] ?? "");
  const wireSequence = envelope.sequence;
  const sequenceMatches =
    typeof wireSequence === "string"
      ? eventSequence(wireSequence) === sequence
      : Number.isSafeInteger(wireSequence) && String(wireSequence) === sequence;
  if (!sequenceMatches || BigInt(sequence) <= BigInt(afterSequence)) {
    throw new Error("flock_event_stream_invalid");
  }
  const eventType = envelope.event_type;
  if (
    typeof eventType !== "string" ||
    !EVENT_TYPES.has(eventType as QualificationEventType) ||
    eventTypes[0] !== eventType
  ) {
    throw new Error("flock_event_stream_invalid");
  }
  return {
    sequence,
    event_type: eventType as QualificationEventType,
    payload: parseEventPayload(eventType as QualificationEventType, envelope.payload),
    created_at: timestamp(envelope.created_at),
  };
}

function parseEventPayload(
  eventType: QualificationEventType,
  value: unknown,
): Readonly<Record<string, unknown>> {
  if (eventType === "budget_projection_overrun") {
    const record = exactRecord(value, [
      "attempt_id",
      "reserve_micros",
      "actual_micros",
      "scope_digest",
    ]);
    return {
      attempt_id: textResponse(record.attempt_id, 240),
      reserve_micros: micros(record.reserve_micros),
      actual_micros: micros(record.actual_micros),
      scope_digest: digestResponse(record.scope_digest),
    };
  }
  const record = exactRecord(value, ["terminal_reason"]);
  return { terminal_reason: textResponse(record.terminal_reason, 240) };
}

async function throwStreamResponseError(response: Response): Promise<never> {
  let code = "flock_event_stream_unavailable";
  try {
    const payload: unknown = JSON.parse(await response.text());
    if (isRecord(payload) && isRecord(payload.detail)) {
      const candidate = payload.detail.code;
      if (
        typeof candidate === "string" &&
        /^[a-z][a-z0-9_]{0,63}$/.test(candidate)
      ) {
        code = candidate;
      }
    }
  } catch {
    // Keep the fixed non-echoing fallback.
  }
  if (response.status === 401) throw new ApiAuthError(code);
  throw new ApiResponseError(code, response.status);
}

// --- scalar validators ---------------------------------------------------------

function invalidRequest(): never {
  throw new Error("flock_qualification_request_invalid");
}

function invalidResponse(): never {
  throw new Error("flock_qualification_response_invalid");
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

function textList(
  value: string[],
  maximumItems: number,
  maximumLength: number,
  minimumItems = 0,
): string[] {
  if (
    !Array.isArray(value) ||
    value.length < minimumItems ||
    value.length > maximumItems
  ) {
    invalidRequest();
  }
  return value.map((item) => requireText(item, maximumLength));
}

function positiveInteger(value: number): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 1) {
    invalidRequest();
  }
  return value;
}

function usdText(value: string): string {
  if (typeof value !== "string" || !USD_TEXT.test(value)) invalidRequest();
  return value;
}

function usdTextToMicros(text: string): number {
  const [whole, fraction = ""] = text.split(".");
  return Number(whole) * 1_000_000 + Number((fraction + "000000").slice(0, 6));
}

function digest(value: string): string {
  if (typeof value !== "string" || !DIGEST.test(value)) invalidRequest();
  return value;
}

function privacyClass(value: string): string {
  if (!PRIVACY_CLASSES.has(value)) invalidRequest();
  return value;
}

function boundedRecord(value: Record<string, unknown>): Record<string, unknown> {
  if (!isRecord(value)) invalidRequest();
  if (byteLength(JSON.stringify(value)) > MAX_RECORD_BYTES) invalidRequest();
  return { ...value };
}

function requireRunId(value: string): string {
  if (typeof value !== "string" || !RUN_ID.test(value)) invalidRequest();
  return value;
}

function eventSequence(value: string): string {
  if (typeof value !== "string" || !EVENT_SEQUENCE.test(value)) {
    throw new Error("flock_event_stream_invalid");
  }
  try {
    if (BigInt(value) > MAX_EVENT_SEQUENCE) {
      throw new Error("flock_event_stream_invalid");
    }
  } catch {
    throw new Error("flock_event_stream_invalid");
  }
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

function reasonMap(value: unknown): Record<string, string[]> {
  if (!isRecord(value)) invalidResponse();
  const entries = Object.entries(value);
  if (entries.length > 1_024) invalidResponse();
  return Object.fromEntries(
    entries.map(([key, reasons]) => [
      textResponse(key, 240),
      textListResponse(reasons, 32, 240),
    ]),
  );
}

function timestamp(value: unknown): string {
  if (typeof value !== "string" || !TIMESTAMP.test(value)) invalidResponse();
  return value;
}

function nullableTimestamp(value: unknown): string | null {
  if (value === null) return null;
  return timestamp(value);
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

function micros(value: unknown): number {
  if (!Number.isSafeInteger(value) || Number(value) < 0) invalidResponse();
  return Number(value);
}

function microsRange(value: unknown): [number, number] {
  if (!Array.isArray(value) || value.length !== 2) invalidResponse();
  const low = micros(value[0]);
  const high = micros(value[1]);
  if (low > high) invalidResponse();
  return [low, high];
}

function usdTextResponse(value: unknown): string {
  if (typeof value !== "string" || !USD_TEXT.test(value)) invalidResponse();
  return value;
}

function digestResponse(value: unknown): string {
  if (typeof value !== "string" || !DIGEST.test(value)) invalidResponse();
  return value;
}

function runIdResponse(value: unknown): string {
  if (typeof value !== "string" || !RUN_ID.test(value)) invalidResponse();
  return value;
}

function receiptIdResponse(value: unknown): string {
  if (typeof value !== "string" || !RECEIPT_ID.test(value)) invalidResponse();
  return value;
}

function boundedRecordResponse(value: unknown): Record<string, unknown> {
  if (!isRecord(value)) invalidResponse();
  if (byteLength(JSON.stringify(value)) > MAX_RECORD_BYTES) invalidResponse();
  return { ...value };
}
