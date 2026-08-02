import {
  ApiAuthError,
  ApiResponseError,
  getJson,
  postJson,
} from "../../api";
import { apiAuthHeaders } from "../../auth";
import { runtimeTransport } from "../../platform/runtimeTransport";
import type {
  CancelLanScanInput,
  ConfirmManualLanProbeInput,
  CreateLanScanInput,
  LanApiShape,
  LanCapabilityEvidenceTuple,
  LanDurableObservationEvidence,
  LanFailureCategory,
  LanInterface,
  LanManualPreview,
  LanObservationCursor,
  LanObservationPublicEvidence,
  LanScan,
  LanScanDetail,
  LanScanEvent,
  LanScanEventStreamOptions,
  LanScanEventType,
  LanScanPageOptions,
  LanScopePreview,
  PreviewLanScopeInput,
  PreviewManualLanProbeInput,
  StartLanScanInput,
} from "./types";

const LAN_EVENT_TYPES = new Set<LanScanEventType>([
  "scan_started",
  "scan_progress",
  "scan_cancel_requested",
  "scan_completed",
  "scan_cancelled",
  "scan_failed",
  "scan_interrupted",
]);
const LAN_SCAN_ID = /^lan_[0-9a-f]{32}$/;
const LAN_DIGEST = /^sha256:[0-9a-f]{64}$/;
const LAN_OBSERVATION_CURSOR = /^[A-Za-z0-9_-]{1,1024}$/;
const BASE64URL_ALPHABET =
  "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
const LAN_OBSERVATION_CURSOR_HEADER = "Kestrel-Lan-Observation-Cursor";
const LAN_TIMESTAMP =
  /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)$/;
const MAX_EVENT_SEQUENCE = 9_223_372_036_854_775_807n;
const MAX_EVENT_FRAME_BYTES = 16 * 1_024;
const LAN_API_SHAPES = new Set<LanApiShape>([
  "ollama_compatible",
  "openai_compatible",
]);
const LAN_FAILURE_CATEGORIES = new Set<LanFailureCategory>([
  "cancelled",
  "scan_deadline_exceeded",
  "interface_drift",
  "interface_pinning_unavailable",
  "tcp_timeout",
  "tcp_refused",
  "tcp_unreachable",
  "tcp_error",
  "http_timeout",
  "http_protocol_rejected",
  "redirect_rejected",
  "response_too_large",
  "unsupported_content_encoding",
  "http_status_rejected",
  "catalog_not_found",
  "catalog_invalid",
  "catalog_empty",
  "generation_request_failed",
  "generation_response_invalid",
]);
const LAN_CAPABILITY_NAMES = [
  "generation",
  "streaming",
  "structured_output",
  "tools",
  "vision",
] as const;

export async function getLanInterfaces(
  signal?: AbortSignal,
): Promise<LanInterface[]> {
  const payload = await getJson<unknown>("/api/routing/lan/interfaces", {
    signal,
  });
  return parseInterfaces(payload);
}

export async function previewLanScope(
  input: PreviewLanScopeInput,
): Promise<LanScopePreview> {
  const interfaceId = requireRequestDigest(input.interfaceId);
  const network = requireNetwork(input.network);
  const payload = await postJson<unknown>("/api/routing/lan/preview", {
    interface_id: interfaceId,
    network,
  });
  return parseScopePreview(payload);
}

export async function previewManualLanProbe(
  input: PreviewManualLanProbeInput,
): Promise<LanManualPreview> {
  const interfaceId = requireRequestDigest(input.interfaceId);
  const host = requireManualHost(input.host);
  const port = requireRequestPort(input.port);
  const payload = await postJson<unknown>(
    "/api/routing/lan/manual-probe",
    {
      mode: "preview",
      interface_id: interfaceId,
      host,
      port,
    },
  );
  return parseManualPreview(payload);
}

export async function confirmManualLanProbe(
  input: ConfirmManualLanProbeInput,
): Promise<LanScan> {
  if (
    input.expectedRevision !== 0 ||
    input.confirmed !== true ||
    input.privacyAcknowledged !== true
  ) {
    invalidRequest();
  }
  const payload = await postJson<unknown>("/api/routing/lan/manual-probe", {
    mode: "confirm",
    expected_revision: input.expectedRevision,
    preview_digest: requireRequestDigest(input.previewDigest),
    selected_address: requireSelectedAddress(input.selectedAddress),
    confirmed: input.confirmed,
    privacy_acknowledged: input.privacyAcknowledged,
  });
  return parseScan(payload);
}

export async function createLanScan(
  input: CreateLanScanInput,
): Promise<LanScan> {
  if (input.expectedRevision !== 0 || input.confirmed !== true) {
    invalidRequest();
  }
  const payload = await postJson<unknown>("/api/routing/lan/scans", {
    preview_digest: requireRequestDigest(input.previewDigest),
    expected_revision: input.expectedRevision,
    confirmed: input.confirmed,
  });
  return parseScan(payload);
}

export async function startLanScan(
  input: StartLanScanInput,
): Promise<LanScan> {
  const identifier = requireScanId(input.scanId);
  const revision = requirePositiveRevision(input.expectedRevision);
  if (input.confirmed !== true) invalidRequest();
  const payload = await postJson<unknown>(
    `/api/routing/lan/scans/${identifier}/start`,
    {
      expected_revision: revision,
      preview_digest: requireRequestDigest(input.previewDigest),
      confirmed: input.confirmed,
    },
  );
  return parseScan(payload);
}

export async function listLanScans(
  signal?: AbortSignal,
): Promise<LanScan[]> {
  const payload = await getJson<unknown>("/api/routing/lan/scans", {
    signal,
  });
  if (!Array.isArray(payload) || payload.length > 100) invalidResponse();
  return payload.map(parseScan);
}

export async function getLanScan(
  scanId: string,
  signal?: AbortSignal,
): Promise<LanScanDetail> {
  return getLanScanPage(
    scanId,
    signal === undefined ? {} : { signal },
  );
}

export async function getLanScanPage(
  scanId: string,
  options: LanScanPageOptions = {},
): Promise<LanScanDetail> {
  const identifier = requireScanId(scanId);
  const cursor =
    options.cursor === undefined
      ? undefined
      : requireObservationCursor(options.cursor);
  const payload = await getJson<unknown>(
    `/api/routing/lan/scans/${identifier}`,
    {
      signal: options.signal,
      headers:
        cursor === undefined
          ? undefined
          : { [LAN_OBSERVATION_CURSOR_HEADER]: cursor },
    },
  );
  const page = parseScanDetail(payload);
  if (
    page.scan_id !== identifier ||
    (page.observation_next_cursor !== null &&
      (!page.observations_truncated || page.observations.length === 0)) ||
    (cursor === undefined &&
      page.observations_truncated &&
      page.observation_next_cursor === null) ||
    (cursor !== undefined && page.observation_next_cursor === cursor)
  ) {
    invalidResponse();
  }
  return page;
}

export async function cancelLanScan(
  input: CancelLanScanInput,
): Promise<LanScan> {
  const identifier = requireScanId(input.scanId);
  const revision = requirePositiveRevision(input.expectedRevision);
  const payload = await postJson<unknown>(
    `/api/routing/lan/scans/${identifier}/cancel`,
    { expected_revision: revision },
  );
  return parseScan(payload);
}

export async function streamLanScanEvents(
  scanId: string,
  options: LanScanEventStreamOptions,
): Promise<void> {
  const identifier = requireScanId(scanId);
  const initialCursor = requireEventSequence(
    options.afterSequence,
    true,
  );
  const transport = runtimeTransport(apiAuthHeaders);
  const response = await transport.fetch(
    `/api/routing/lan/scans/${identifier}/events`,
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
    throw new Error("lan_event_stream_invalid");
  }
  if (response.body === null) return;

  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8", { fatal: true });
  let buffer = "";
  let cursor = initialCursor;
  const emitCompleteFrames = () => {
    for (;;) {
      const boundary = /\r?\n\r?\n/.exec(buffer);
      if (boundary === null || boundary.index === undefined) break;
      const frame = buffer.slice(0, boundary.index);
      buffer = buffer.slice(boundary.index + boundary[0].length);
      if (utf8Length(frame) > MAX_EVENT_FRAME_BYTES) {
        throw new Error("lan_event_stream_invalid");
      }
      const event = parseLanEventFrame(frame, identifier, cursor);
      if (event !== null) {
        cursor = event.sequence;
        options.onEvent(event);
      }
    }
    if (utf8Length(buffer) > MAX_EVENT_FRAME_BYTES) {
      throw new Error("lan_event_stream_invalid");
    }
  };
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      try {
        buffer += decoder.decode(value, { stream: true });
      } catch {
        throw new Error("lan_event_stream_invalid");
      }
      emitCompleteFrames();
    }
    try {
      buffer += decoder.decode();
    } catch {
      throw new Error("lan_event_stream_invalid");
    }
    emitCompleteFrames();
    if (buffer.trim()) throw new Error("lan_event_stream_invalid");
  } catch (error) {
    await reader.cancel().catch(() => undefined);
    throw error;
  } finally {
    reader.releaseLock();
  }
}

async function throwStreamResponseError(response: Response): Promise<never> {
  let code = "lan_event_stream_unavailable";
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

function parseLanEventFrame(
  frame: string,
  expectedScanId: string,
  afterSequence: string,
): LanScanEvent | null {
  const ids: string[] = [];
  const eventTypes: string[] = [];
  const data: string[] = [];
  for (const line of frame.split(/\r?\n/)) {
    if (!line || line.startsWith(":")) continue;
    const separator = line.indexOf(":");
    const field = separator < 0 ? line : line.slice(0, separator);
    const rawValue = separator < 0 ? "" : line.slice(separator + 1);
    const value = rawValue.startsWith(" ")
      ? rawValue.slice(1)
      : rawValue;
    if (field === "id") ids.push(value);
    else if (field === "event") eventTypes.push(value);
    else if (field === "data") data.push(value);
    else throw new Error("lan_event_stream_invalid");
  }
  if (ids.length === 0 && eventTypes.length === 0 && data.length === 0) {
    return null;
  }
  if (ids.length !== 1 || eventTypes.length !== 1 || data.length === 0) {
    throw new Error("lan_event_stream_invalid");
  }
  const encodedData = data.join("\n");
  if (utf8Length(frame) > MAX_EVENT_FRAME_BYTES) {
    throw new Error("lan_event_stream_invalid");
  }

  let value: unknown;
  try {
    value = JSON.parse(encodedData);
  } catch {
    throw new Error("lan_event_stream_invalid");
  }
  try {
    const envelope = exactRecord(value, [
      "scan_id",
      "sequence",
      "event_type",
      "payload",
      "created_at",
    ]);
    const sequence = requireEventSequence(ids[0] ?? "", false);
    const wireSequence = envelope.sequence;
    const sequenceMatches =
      typeof wireSequence === "string"
        ? requireEventSequence(wireSequence, false) === sequence
        : Number.isSafeInteger(wireSequence) &&
          String(wireSequence) === sequence;
    if (!sequenceMatches || BigInt(sequence) <= BigInt(afterSequence)) {
      throw new Error("lan_event_stream_invalid");
    }
    const eventType = envelope.event_type;
    if (
      envelope.scan_id !== expectedScanId ||
      typeof eventType !== "string" ||
      !LAN_EVENT_TYPES.has(eventType as LanScanEventType) ||
      eventTypes[0] !== eventType
    ) {
      throw new Error("lan_event_stream_invalid");
    }
    const payload = parseEventPayload(
      eventType as LanScanEventType,
      envelope.payload,
    );
    const createdAt = timestamp(envelope.created_at);
    return {
      scan_id: expectedScanId,
      sequence,
      event_type: eventType,
      payload,
      created_at: createdAt,
    } as LanScanEvent;
  } catch {
    throw new Error("lan_event_stream_invalid");
  }
}

const SCAN_KEYS = [
  "scan_id",
  "status",
  "revision",
  "confirmed_interface_id",
  "network",
  "limits",
  "limits_digest",
  "preview_digest",
  "created_at",
  "updated_at",
  "started_at",
  "finished_at",
  "cancel_reason",
  "terminal_reason",
  "candidate_count",
  "error_count",
  "timeout_count",
  "terminal_receipt_digest",
] as const;

function parseInterfaces(value: unknown): LanInterface[] {
  if (!Array.isArray(value) || value.length > 64) invalidResponse();
  return value.map((item) => {
    const record = exactRecord(item, [
      "interface_id",
      "display_name",
      "addresses",
    ]);
    const addresses = record.addresses;
    if (
      !Array.isArray(addresses) ||
      addresses.length === 0 ||
      addresses.length > 64 ||
      addresses.some(
        (address) =>
          typeof address !== "string" ||
          address.length === 0 ||
          address.length > 128,
      )
    ) {
      invalidResponse();
    }
    return {
      interface_id: digest(record.interface_id),
      display_name: text(record.display_name, 256),
      addresses: [...addresses] as string[],
    };
  });
}

function parseScopePreview(value: unknown): LanScopePreview {
  const record = exactRecord(value, [
    "interface_id",
    "network",
    "limits",
    "active_host_count",
    "passive_or_manual_only",
    "port_count",
    "mdns_status",
    "server_version",
    "contract_version",
    "preview_digest",
    "issued_at",
    "expires_at",
  ]);
  const activeHostCount = nonnegativeInteger(record.active_host_count);
  const portCount = nonnegativeInteger(record.port_count);
  const passive = record.passive_or_manual_only;
  if (
    activeHostCount > 256 ||
    portCount > 1_024 ||
    typeof passive !== "boolean" ||
    passive !== (activeHostCount === 0) ||
    portCount !== activeHostCount * 4
  ) {
    invalidResponse();
  }
  return {
    interface_id: digest(record.interface_id),
    network: text(record.network, 128),
    limits: automaticLimits(record.limits),
    active_host_count: activeHostCount,
    passive_or_manual_only: passive,
    port_count: portCount,
    mdns_status: mdnsStatus(record.mdns_status),
    server_version: text(record.server_version, 128),
    contract_version: text(record.contract_version, 128),
    preview_digest: digest(record.preview_digest),
    issued_at: timestamp(record.issued_at),
    expires_at: timestamp(record.expires_at),
  };
}

function parseManualPreview(value: unknown): LanManualPreview {
  const record = exactRecord(value, [
    "schema",
    "interface_id",
    "port",
    "resolved_addresses",
    "preview_digest",
    "issued_at",
    "expires_at",
    "server_version",
    "contract_version",
    "requires_confirmation",
  ]);
  const port = positiveInteger(record.port, 65_535);
  const addresses = record.resolved_addresses;
  if (
    record.schema !== "kestrel.lan.manual-preview.v1" ||
    record.requires_confirmation !== true ||
    !Array.isArray(addresses) ||
    addresses.length === 0 ||
    addresses.length > 16 ||
    addresses.some(
      (address) =>
        typeof address !== "string" ||
        address.length === 0 ||
        address.length > 64,
    )
  ) {
    invalidResponse();
  }
  return {
    schema: "kestrel.lan.manual-preview.v1",
    interface_id: digest(record.interface_id),
    port,
    resolved_addresses: [...addresses] as string[],
    preview_digest: digest(record.preview_digest),
    issued_at: timestamp(record.issued_at),
    expires_at: timestamp(record.expires_at),
    server_version: text(record.server_version, 128),
    contract_version: text(record.contract_version, 128),
    requires_confirmation: true,
  };
}

function parseScan(value: unknown): LanScan {
  const record = exactRecord(value, [...SCAN_KEYS]);
  const status = record.status;
  if (
    typeof status !== "string" ||
    ![
      "draft",
      "running",
      "cancelling",
      "cancelled",
      "completed",
      "failed",
      "interrupted",
    ].includes(status)
  ) {
    invalidResponse();
  }
  const cancelReason = nullableText(record.cancel_reason, 64);
  if (
    cancelReason !== null &&
    cancelReason !== "owner_cancelled" &&
    cancelReason !== "shutdown_cancelled"
  ) {
    invalidResponse();
  }
  const terminalReason = nullableText(record.terminal_reason, 64);
  if (
    terminalReason !== null &&
    ![
      "scan_complete",
      "owner_cancelled",
      "shutdown_cancelled",
      "worker_error",
      "deadline_expired",
      "startup_interrupted",
      "worker_interrupted",
    ].includes(terminalReason)
  ) {
    invalidResponse();
  }
  const result = {
    scan_id: requireScanId(record.scan_id),
    status,
    revision: positiveInteger(record.revision),
    confirmed_interface_id: digest(record.confirmed_interface_id),
    network: text(record.network, 128),
    limits: scanLimits(record.limits),
    limits_digest: digest(record.limits_digest),
    preview_digest: digest(record.preview_digest),
    created_at: timestamp(record.created_at),
    updated_at: timestamp(record.updated_at),
    started_at: nullableTimestamp(record.started_at),
    finished_at: nullableTimestamp(record.finished_at),
    cancel_reason: cancelReason,
    terminal_reason: terminalReason,
    candidate_count: nullableNonnegativeInteger(record.candidate_count),
    error_count: nullableNonnegativeInteger(record.error_count),
    timeout_count: nullableNonnegativeInteger(record.timeout_count),
    terminal_receipt_digest: nullableDigest(
      record.terminal_receipt_digest,
    ),
  };
  return result as LanScan;
}

function parseScanDetail(value: unknown): LanScanDetail {
  const record = exactRecord(value, [
    ...SCAN_KEYS,
    "observations",
    "observation_total_count",
    "observations_truncated",
    "observation_next_cursor",
  ]);
  const scanProjection = Object.fromEntries(
    SCAN_KEYS.map((key) => [key, record[key]]),
  );
  const base = parseScan(scanProjection);
  const observations = record.observations;
  const total = nonnegativeInteger(record.observation_total_count);
  const nextCursor = nullableObservationCursor(
    record.observation_next_cursor,
  );
  if (
    !Array.isArray(observations) ||
    observations.length > 200 ||
    total > 1_024 ||
    total < observations.length ||
    typeof record.observations_truncated !== "boolean" ||
    record.observations_truncated !== (total > observations.length)
  ) {
    invalidResponse();
  }
  const parsedObservations = observations.map(parseObservation);
  const manualLimitsValue =
    "mode" in base.limits && base.limits.mode === "manual"
      ? base.limits
      : null;
  let previousEndpoint = "";
  for (const observation of parsedObservations) {
    if (
      observation.scan_id !== base.scan_id ||
      observation.interface_id !== base.confirmed_interface_id ||
      observation.endpoint_id <= previousEndpoint ||
      (manualLimitsValue === null && observation.source === "manual") ||
      (manualLimitsValue !== null &&
        (observation.source !== "manual" ||
          observation.port !== manualLimitsValue.exact_port))
    ) {
      invalidResponse();
    }
    previousEndpoint = observation.endpoint_id;
  }
  return {
    ...base,
    observations: parsedObservations,
    observation_total_count: total,
    observations_truncated: record.observations_truncated,
    observation_next_cursor: nextCursor,
  };
}

function parseObservation(value: unknown): LanScanDetail["observations"][number] {
  const record = exactRecord(value, [
    "scan_id",
    "endpoint_id",
    "source",
    "interface_id",
    "address",
    "port",
    "api_shape",
    "tls_enabled",
    "certificate_sha256",
    "catalog_digest",
    "capability_digest",
    "public_payload",
    "freshness_timestamp",
    "error_category",
    "created_at",
  ]);
  const source = record.source;
  if (
    source !== "mdns" &&
    source !== "active" &&
    source !== "manual"
  ) {
    invalidResponse();
  }
  if (typeof record.tls_enabled !== "boolean") invalidResponse();
  const publicPayload = parseObservationPublicEvidence(
    record.public_payload,
    source,
  );
  const endpointId = digest(record.endpoint_id);
  const certificate = nullableDigest(record.certificate_sha256);
  if ("schema" in publicPayload) {
    const apiShape = nullableApiShape(record.api_shape);
    const failureCategory = nullableFailureCategory(record.error_category);
    const catalogDigest = digest(record.catalog_digest);
    const capabilityDigest = digest(record.capability_digest);
    if (
      publicPayload.endpoint_binding_digest !== endpointId ||
      publicPayload.api_shape !== apiShape ||
      publicPayload.failure_category !== failureCategory ||
      record.tls_enabled !== false ||
      certificate !== null
    ) {
      invalidResponse();
    }
    const port = positiveInteger(record.port, 65_535);
    if (
      source !== "manual" &&
      ![1_234, 8_000, 8_080, 11_434].includes(port)
    ) {
      invalidResponse();
    }
    return {
      evidence_kind: "durable",
      scan_id: requireScanId(record.scan_id),
      endpoint_id: endpointId,
      source,
      interface_id: digest(record.interface_id),
      address: text(record.address, 64),
      port,
      api_shape: apiShape,
      tls_enabled: false,
      certificate_sha256: null,
      catalog_digest: catalogDigest,
      capability_digest: capabilityDigest,
      public_payload: publicPayload,
      freshness_timestamp: timestamp(record.freshness_timestamp),
      error_category: failureCategory,
      created_at: timestamp(record.created_at),
    };
  }
  return {
    evidence_kind: "legacy",
    scan_id: requireScanId(record.scan_id),
    endpoint_id: endpointId,
    source,
    interface_id: digest(record.interface_id),
    address: text(record.address, 64),
    port: positiveInteger(record.port, 65_535),
    api_shape: nullableText(record.api_shape, 64),
    tls_enabled: record.tls_enabled,
    certificate_sha256: certificate,
    catalog_digest: nullableDigest(record.catalog_digest),
    capability_digest: nullableDigest(record.capability_digest),
    public_payload: publicPayload,
    freshness_timestamp: timestamp(record.freshness_timestamp),
    error_category: nullableText(record.error_category, 128),
    created_at: timestamp(record.created_at),
  };
}

function parseObservationPublicEvidence(
  value: unknown,
  source: "mdns" | "active" | "manual",
): LanObservationPublicEvidence {
  if (!isRecord(value) || utf8Length(JSON.stringify(value)) > 16 * 1_024) {
    invalidResponse();
  }
  if (value.schema === "kestrel.lan.durable-observation.v1") {
    const manual = source === "manual";
    const record = exactRecord(value, [
      "schema",
      "endpoint_binding_digest",
      "observation_digest",
      "reachability",
      "transport_security",
      "api_shape",
      "catalog_complete",
      "catalog_truncated",
      "model_ids",
      "capability_route",
      "selected_model_id",
      "capabilities",
      "failure_category",
      ...(manual ? ["observation_source", "endpoint_kind"] : []),
    ]);
    if (
      (manual &&
        (record.observation_source !== "manual" ||
          record.endpoint_kind !== "manual")) ||
      (!manual &&
        ("observation_source" in record || "endpoint_kind" in record)) ||
      !["not_attempted", "unreachable", "reachable"].includes(
        String(record.reachability),
      ) ||
      (record.transport_security !== null &&
        record.transport_security !== "plain_http") ||
      typeof record.catalog_complete !== "boolean" ||
      typeof record.catalog_truncated !== "boolean" ||
      (record.catalog_complete && record.catalog_truncated)
    ) {
      invalidResponse();
    }
    const models = boundedTextArray(record.model_ids, 8, 512, true);
    if (record.catalog_truncated && models.length !== 8) invalidResponse();
    const apiShape = nullableApiShape(record.api_shape);
    const route = nullableText(record.capability_route, 512);
    const selectedModel = nullableText(record.selected_model_id, 512);
    const expectedRoute =
      apiShape === "ollama_compatible"
        ? "/api/generate"
        : apiShape === "openai_compatible"
          ? "/v1/chat/completions"
          : null;
    if (
      (route === null) !== (selectedModel === null) ||
      (selectedModel !== null && selectedModel !== models[0]) ||
      (route !== null && route !== expectedRoute)
    ) {
      invalidResponse();
    }
    const capabilities = parseCapabilities(record.capabilities);
    const failure = nullableFailureCategory(record.failure_category);
    const generation = capabilities[0];
    const reachability = record.reachability as
      | "not_attempted"
      | "unreachable"
      | "reachable";
    const transport = record.transport_security as "plain_http" | null;
    const generationObserved = generation.status !== "not_run";
    if (
      (reachability !== "reachable" &&
        (transport !== null || apiShape !== null || models.length > 0)) ||
      (apiShape === null &&
        (models.length > 0 ||
          record.catalog_complete ||
          record.catalog_truncated)) ||
      (apiShape !== null &&
        (reachability !== "reachable" || transport !== "plain_http")) ||
      (generationObserved &&
        (apiShape === null ||
          models.length === 0 ||
          route === null ||
          selectedModel === null)) ||
      (!generationObserved && (route !== null || selectedModel !== null)) ||
      (generation.status === "observed_pass" && failure !== null) ||
      (generation.status === "observed_failure" && failure === null)
    ) {
      invalidResponse();
    }
    const durableBase = {
      schema: "kestrel.lan.durable-observation.v1",
      endpoint_binding_digest: digest(record.endpoint_binding_digest),
      observation_digest: digest(record.observation_digest),
      reachability,
      transport_security: transport,
      api_shape: apiShape,
      catalog_complete: record.catalog_complete,
      catalog_truncated: record.catalog_truncated,
      model_ids: models,
      capability_route: route,
      selected_model_id: selectedModel,
      capabilities,
      failure_category: failure,
    } as const;
    if (manual) {
      return {
        ...durableBase,
        observation_source: "manual",
        endpoint_kind: "manual",
      };
    }
    return durableBase;
  }
  const record = exactSubsetRecord(value, [
    "service",
    "service_version",
    "model_count",
    "model_ids",
    "capabilities",
    "metadata",
  ]);
  const result: Record<string, unknown> = {};
  if ("service" in record) result.service = text(record.service, 128);
  if ("service_version" in record) {
    result.service_version = text(record.service_version, 128);
  }
  if ("model_count" in record) {
    result.model_count = nonnegativeInteger(record.model_count);
  }
  if ("model_ids" in record) {
    result.model_ids = boundedTextArray(record.model_ids, 64, 256, false);
  }
  if ("capabilities" in record) {
    result.capabilities = boundedTextArray(
      record.capabilities,
      64,
      256,
      false,
    );
  }
  if ("metadata" in record) {
    const metadata = exactSubsetRecord(record.metadata, [
      "display_name",
      "vendor",
      "product",
      "description",
    ]);
    result.metadata = Object.fromEntries(
      Object.entries(metadata).map(([key, item]) => [key, text(item, 512)]),
    );
  }
  return result as LanObservationPublicEvidence;
}

function parseCapabilities(value: unknown): LanCapabilityEvidenceTuple {
  if (!Array.isArray(value) || value.length !== LAN_CAPABILITY_NAMES.length) {
    invalidResponse();
  }
  const parsed = value.map((item, index) => {
    const record = exactRecord(item, [
      "capability",
      "provenance",
      "status",
      "supported",
    ]);
    const capability = LAN_CAPABILITY_NAMES[index];
    if (record.capability !== capability) invalidResponse();
    const notRun =
      record.provenance === "not_run" &&
      record.status === "not_run" &&
      record.supported === null;
    const generationObserved =
      capability === "generation" &&
      record.provenance === "observed" &&
      ((record.status === "observed_pass" && record.supported === true) ||
        (record.status === "observed_failure" &&
          record.supported === null));
    if (!notRun && !generationObserved) invalidResponse();
    return {
      capability,
      provenance: record.provenance as "observed" | "not_run",
      status: record.status as
        | "observed_pass"
        | "observed_failure"
        | "not_run",
      supported: record.supported as boolean | null,
    };
  });
  return parsed as unknown as LanCapabilityEvidenceTuple;
}

function parseEventPayload(
  eventType: LanScanEventType,
  value: unknown,
): LanScanEvent["payload"] {
  if (eventType === "scan_started") return parseStartedPayload(value);
  if (eventType === "scan_progress") {
    const record = exactRecord(value, [
      "planned_count",
      "admitted_count",
      "completed_count",
      "persisted_observation_count",
      "error_category_counts",
      "timeout_count",
      "mdns_status",
    ]);
    const planned = nonnegativeInteger(record.planned_count);
    const admitted = nonnegativeInteger(record.admitted_count);
    const completed = nonnegativeInteger(record.completed_count);
    const persisted = nonnegativeInteger(
      record.persisted_observation_count,
    );
    const timeout = nonnegativeInteger(record.timeout_count);
    const errorCounts = record.error_category_counts;
    if (!isRecord(errorCounts)) invalidResponse();
    const errorEntries = Object.entries(errorCounts);
    const errorTotal = errorEntries.reduce((total, [category, count]) => {
      if (
        !LAN_FAILURE_CATEGORIES.has(category as LanFailureCategory) ||
        !Number.isSafeInteger(count) ||
        Number(count) <= 0
      ) {
        invalidResponse();
      }
      return total + Number(count);
    }, 0);
    if (
      completed > admitted ||
      admitted > planned ||
      persisted > completed ||
      planned > 1_024 ||
      errorTotal > completed ||
      timeout > errorTotal
    ) {
      invalidResponse();
    }
    return {
      planned_count: planned,
      admitted_count: admitted,
      completed_count: completed,
      persisted_observation_count: persisted,
      error_category_counts: Object.fromEntries(errorEntries) as Partial<
        Record<LanFailureCategory, number>
      >,
      timeout_count: timeout,
      mdns_status: mdnsStatus(record.mdns_status),
    };
  }
  if (eventType === "scan_cancel_requested") {
    const record = exactRecord(value, ["reason"]);
    if (
      record.reason !== "owner_cancelled" &&
      record.reason !== "shutdown_cancelled"
    ) {
      invalidResponse();
    }
    return { reason: record.reason };
  }
  const record = exactRecord(value, [
    "status",
    "terminal_reason",
    "cancel_reason",
  ]);
  if (
    eventType === "scan_completed" &&
    record.status === "completed" &&
    record.terminal_reason === "scan_complete" &&
    record.cancel_reason === null
  ) {
    return {
      status: "completed",
      terminal_reason: "scan_complete",
      cancel_reason: null,
    };
  }
  if (
    eventType === "scan_cancelled" &&
    record.status === "cancelled" &&
    ["owner_cancelled", "shutdown_cancelled"].includes(
      String(record.terminal_reason),
    ) &&
    record.cancel_reason === record.terminal_reason
  ) {
    return {
      status: "cancelled",
      terminal_reason: record.terminal_reason as
        | "owner_cancelled"
        | "shutdown_cancelled",
      cancel_reason: record.cancel_reason as
        | "owner_cancelled"
        | "shutdown_cancelled",
    };
  }
  if (
    eventType === "scan_failed" &&
    record.status === "failed" &&
    ((record.terminal_reason === "worker_error" &&
      [null, "owner_cancelled", "shutdown_cancelled"].includes(
        record.cancel_reason as null | string,
      )) ||
      (record.terminal_reason === "deadline_expired" &&
        record.cancel_reason === null))
  ) {
    return record as LanScanEvent["payload"];
  }
  if (
    eventType === "scan_interrupted" &&
    record.status === "interrupted" &&
    ["startup_interrupted", "worker_interrupted"].includes(
      String(record.terminal_reason),
    ) &&
    [null, "owner_cancelled", "shutdown_cancelled"].includes(
      record.cancel_reason as null | string,
    )
  ) {
    return record as LanScanEvent["payload"];
  }
  invalidResponse();
}

function parseStartedPayload(value: unknown): LanScanEvent["payload"] {
  if (isRecord(value) && value.mode === "manual") {
    const record = exactRecord(value, [
      "mode",
      "endpoint_kind",
      "observation_source",
      "interface_id",
      "network",
      "limits",
      "active_host_count",
      "passive_or_manual_only",
      "port_count",
      "exact_port",
      "mdns_status",
      "server_version",
      "contract_version",
      "preview_digest",
      "expires_at",
      "confirmed",
      "privacy_acknowledged",
    ]);
    const limits = manualLimits(record.limits);
    if (
      record.endpoint_kind !== "manual" ||
      record.observation_source !== "manual" ||
      record.active_host_count !== 1 ||
      record.passive_or_manual_only !== true ||
      record.port_count !== 1 ||
      record.exact_port !== limits.exact_port ||
      record.mdns_status !== "unavailable" ||
      record.confirmed !== true ||
      record.privacy_acknowledged !== true
    ) {
      invalidResponse();
    }
    return {
      mode: "manual",
      endpoint_kind: "manual",
      observation_source: "manual",
      interface_id: digest(record.interface_id),
      network: text(record.network, 128),
      limits,
      active_host_count: 1,
      passive_or_manual_only: true,
      port_count: 1,
      exact_port: limits.exact_port,
      mdns_status: "unavailable",
      server_version: text(record.server_version, 128),
      contract_version: text(record.contract_version, 128),
      preview_digest: digest(record.preview_digest),
      expires_at: timestamp(record.expires_at),
      confirmed: true,
      privacy_acknowledged: true,
    };
  }
  const record = exactRecord(value, [
    "interface_id",
    "network",
    "limits",
    "active_host_count",
    "passive_or_manual_only",
    "port_count",
    "mdns_status",
    "server_version",
    "contract_version",
    "preview_digest",
    "expires_at",
  ]);
  const activeHostCount = nonnegativeInteger(record.active_host_count);
  const portCount = nonnegativeInteger(record.port_count);
  const passive = record.passive_or_manual_only;
  if (
    activeHostCount > 256 ||
    portCount > 1_024 ||
    typeof passive !== "boolean" ||
    passive !== (activeHostCount === 0) ||
    portCount !== activeHostCount * 4
  ) {
    invalidResponse();
  }
  return {
    interface_id: digest(record.interface_id),
    network: text(record.network, 128),
    limits: automaticLimits(record.limits),
    active_host_count: activeHostCount,
    passive_or_manual_only: passive,
    port_count: portCount,
    mdns_status: mdnsStatus(record.mdns_status),
    server_version: text(record.server_version, 128),
    contract_version: text(record.contract_version, 128),
    preview_digest: digest(record.preview_digest),
    expires_at: timestamp(record.expires_at),
  };
}

function scanLimits(value: unknown): LanScan["limits"] {
  if (isRecord(value) && value.mode === "manual") {
    return manualLimits(value);
  }
  return automaticLimits(value);
}

function automaticLimits(value: unknown): LanScopePreview["limits"] {
  const record = exactRecord(value, [
    "known_model_service_ports",
    "max_active_hosts",
    "max_scan_concurrency",
    "tcp_connect_timeout_seconds",
    "http_probe_timeout_seconds",
    "total_scan_deadline_seconds",
    "max_probe_response_bytes",
    "max_discovered_models",
    "mdns_window_seconds",
  ]);
  if (
    JSON.stringify(record.known_model_service_ports) !==
      JSON.stringify([1_234, 8_000, 8_080, 11_434]) ||
    record.max_active_hosts !== 256 ||
    record.max_scan_concurrency !== 16 ||
    record.tcp_connect_timeout_seconds !== 0.75 ||
    record.http_probe_timeout_seconds !== 2 ||
    record.total_scan_deadline_seconds !== 45 ||
    record.max_probe_response_bytes !== 262_144 ||
    record.max_discovered_models !== 8 ||
    record.mdns_window_seconds !== 2.5
  ) {
    invalidResponse();
  }
  return record as LanScopePreview["limits"];
}

function manualLimits(value: unknown): Extract<LanScan["limits"], { mode: "manual" }> {
  const record = exactRecord(value, [
    "mode",
    "exact_port",
    "max_active_hosts",
    "max_scan_concurrency",
    "tcp_connect_timeout_seconds",
    "http_probe_timeout_seconds",
    "total_scan_deadline_seconds",
    "max_probe_response_bytes",
    "max_discovered_models",
    "mdns_enabled",
  ]);
  const port = positiveInteger(record.exact_port, 65_535);
  if (
    record.mode !== "manual" ||
    record.max_active_hosts !== 1 ||
    record.max_scan_concurrency !== 1 ||
    record.tcp_connect_timeout_seconds !== 0.75 ||
    record.http_probe_timeout_seconds !== 2 ||
    record.total_scan_deadline_seconds !== 45 ||
    record.max_probe_response_bytes !== 262_144 ||
    record.max_discovered_models !== 8 ||
    record.mdns_enabled !== false
  ) {
    invalidResponse();
  }
  return {
    mode: "manual",
    exact_port: port,
    max_active_hosts: 1,
    max_scan_concurrency: 1,
    tcp_connect_timeout_seconds: 0.75,
    http_probe_timeout_seconds: 2,
    total_scan_deadline_seconds: 45,
    max_probe_response_bytes: 262_144,
    max_discovered_models: 8,
    mdns_enabled: false,
  };
}

function requireScanId(value: unknown): string {
  if (typeof value !== "string" || !LAN_SCAN_ID.test(value)) {
    invalidRequest();
  }
  return value;
}

function requireObservationCursor(value: unknown): LanObservationCursor {
  if (!isObservationCursor(value)) {
    invalidRequest();
  }
  return value;
}

function requireRequestDigest(value: unknown): string {
  if (typeof value !== "string" || !LAN_DIGEST.test(value)) {
    invalidRequest();
  }
  return value;
}

function requireNetwork(value: unknown): string {
  const network = requestText(value, 128);
  if (
    network.split("/").length !== 2 ||
    /[@?#\\]/.test(network)
  ) {
    invalidRequest();
  }
  return network;
}

function requireManualHost(value: unknown): string {
  const host = requestText(value, 253);
  if (
    host.includes("://") ||
    /[@/?#\\\s]/.test(host)
  ) {
    invalidRequest();
  }
  return host;
}

function requireSelectedAddress(value: unknown): string {
  const address = requestText(value, 64);
  if (!/^[0-9A-Fa-f:.]+$/.test(address)) invalidRequest();
  return address;
}

function requireRequestPort(value: unknown): number {
  if (
    typeof value !== "number" ||
    !Number.isSafeInteger(value) ||
    value < 1 ||
    value > 65_535
  ) {
    invalidRequest();
  }
  return value;
}

function requirePositiveRevision(value: unknown): number {
  if (
    typeof value !== "number" ||
    !Number.isSafeInteger(value) ||
    value < 1
  ) {
    invalidRequest();
  }
  return value;
}

function requestText(value: unknown, maximum: number): string {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value.length > maximum ||
    value.trim() !== value ||
    /[\u0000-\u001f\u007f]/.test(value)
  ) {
    invalidRequest();
  }
  return value;
}

function requireEventSequence(value: unknown, allowZero: boolean): string {
  if (
    typeof value !== "string" ||
    !/^(?:0|[1-9][0-9]{0,18})$/.test(value)
  ) {
    throw new Error("lan_event_cursor_invalid");
  }
  const parsed = BigInt(value);
  if (parsed > MAX_EVENT_SEQUENCE || (!allowZero && parsed === 0n)) {
    throw new Error("lan_event_cursor_invalid");
  }
  return value;
}

function exactRecord(
  value: unknown,
  keys: readonly string[],
): Record<string, unknown> {
  if (!isRecord(value)) invalidResponse();
  const actual = Object.keys(value);
  if (
    actual.length !== keys.length ||
    actual.some((key) => !keys.includes(key))
  ) {
    invalidResponse();
  }
  return value;
}

function exactSubsetRecord(
  value: unknown,
  allowedKeys: readonly string[],
): Record<string, unknown> {
  if (!isRecord(value)) invalidResponse();
  if (Object.keys(value).some((key) => !allowedKeys.includes(key))) {
    invalidResponse();
  }
  return value;
}

function boundedTextArray(
  value: unknown,
  maximumItems: number,
  maximumText: number,
  canonical: boolean,
): string[] {
  if (!Array.isArray(value) || value.length > maximumItems) {
    invalidResponse();
  }
  const items = value.map((item) => text(item, maximumText));
  if (
    canonical &&
    JSON.stringify(items) !== JSON.stringify([...new Set(items)].sort())
  ) {
    invalidResponse();
  }
  return items;
}

function digest(value: unknown): string {
  if (typeof value !== "string" || !LAN_DIGEST.test(value)) {
    invalidResponse();
  }
  return value;
}

function nullableDigest(value: unknown): string | null {
  return value === null ? null : digest(value);
}

function nullableObservationCursor(value: unknown): LanObservationCursor | null {
  if (value === null) return null;
  if (!isObservationCursor(value)) invalidResponse();
  return value;
}

function isObservationCursor(value: unknown): value is LanObservationCursor {
  if (
    typeof value !== "string" ||
    !LAN_OBSERVATION_CURSOR.test(value) ||
    value.length % 4 === 1
  ) {
    return false;
  }
  const remainder = value.length % 4;
  if (remainder === 0) return true;
  const finalSextet = BASE64URL_ALPHABET.indexOf(value.at(-1) ?? "");
  return remainder === 2
    ? (finalSextet & 0b001111) === 0
    : (finalSextet & 0b000011) === 0;
}

function text(value: unknown, maximum: number): string {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value.length > maximum ||
    value.trim() !== value
  ) {
    invalidResponse();
  }
  return value;
}

function nullableText(value: unknown, maximum: number): string | null {
  return value === null ? null : text(value, maximum);
}

function nullableApiShape(value: unknown): LanApiShape | null {
  if (value === null) return null;
  if (typeof value !== "string" || !LAN_API_SHAPES.has(value as LanApiShape)) {
    invalidResponse();
  }
  return value as LanApiShape;
}

function nullableFailureCategory(value: unknown): LanFailureCategory | null {
  if (value === null) return null;
  if (
    typeof value !== "string" ||
    !LAN_FAILURE_CATEGORIES.has(value as LanFailureCategory)
  ) {
    invalidResponse();
  }
  return value as LanFailureCategory;
}

function timestamp(value: unknown): string {
  if (typeof value !== "string" || !LAN_TIMESTAMP.test(value)) {
    invalidResponse();
  }
  return value;
}

function nullableTimestamp(value: unknown): string | null {
  return value === null ? null : timestamp(value);
}

function positiveInteger(value: unknown, maximum = Number.MAX_SAFE_INTEGER): number {
  if (
    typeof value !== "number" ||
    !Number.isSafeInteger(value) ||
    value < 1 ||
    value > maximum
  ) {
    invalidResponse();
  }
  return value;
}

function nonnegativeInteger(value: unknown): number {
  if (
    typeof value !== "number" ||
    !Number.isSafeInteger(value) ||
    value < 0
  ) {
    invalidResponse();
  }
  return value;
}

function nullableNonnegativeInteger(value: unknown): number | null {
  return value === null ? null : nonnegativeInteger(value);
}

function mdnsStatus(value: unknown): "available" | "unavailable" | "timed_out" {
  if (
    value !== "available" &&
    value !== "unavailable" &&
    value !== "timed_out"
  ) {
    invalidResponse();
  }
  return value;
}

function utf8Length(value: string): number {
  return new TextEncoder().encode(value).byteLength;
}

function invalidResponse(): never {
  throw new Error("lan_response_invalid");
}

function invalidRequest(): never {
  throw new Error("lan_request_invalid");
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
