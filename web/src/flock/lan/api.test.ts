// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  confirmLanImport,
  confirmLanTargetReview,
  previewLanImport,
  previewLanTargetReview,
} from "../../routing/api";
import {
  removeFakeDesktopEnvironment,
} from "../../testing/fakeDesktopBridge";
import {
  cancelLanScan,
  confirmManualLanProbe,
  createLanScan,
  getLanInterfaces,
  getLanScan,
  getLanScanPage,
  listLanScans,
  previewLanScope,
  previewManualLanProbe,
  startLanScan,
  streamLanScanEvents,
} from "./api";
import type { LanScanEvent } from "./types";

const scanId = `lan_${"1".repeat(32)}`;
const digestA = `sha256:${"a".repeat(64)}`;
const digestB = `sha256:${"b".repeat(64)}`;
const digestC = `sha256:${"c".repeat(64)}`;
const interfaceId = `sha256:${"d".repeat(64)}`;
const profileId = `lan-provider-${"1".repeat(64)}`;
const targetId = `lan-target-${"2".repeat(64)}`;
const observationCursor =
  "eyJzY2hlbWEiOiJrZXN0cmVsLmxhbi5vYnNlcnZhdGlvbi1jdXJzb3IudjEifQ";
const nextObservationCursor = "bmV4dC1vYnNlcnZhdGlvbi1jdXJzb3I";

type CapturedRequest = {
  path: string;
  method: string;
  headers: Headers;
  body: unknown;
};

function jsonResponse(payload: unknown = {}): Response {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

const automaticLimits = {
  known_model_service_ports: [1_234, 8_000, 8_080, 11_434],
  max_active_hosts: 256,
  max_scan_concurrency: 16,
  tcp_connect_timeout_seconds: 0.75,
  http_probe_timeout_seconds: 2,
  total_scan_deadline_seconds: 45,
  max_probe_response_bytes: 262_144,
  max_discovered_models: 8,
  mdns_window_seconds: 2.5,
} as const;

function scanResponse() {
  return {
    scan_id: scanId,
    status: "draft",
    revision: 1,
    confirmed_interface_id: interfaceId,
    network: "192.168.50.0/24",
    limits: automaticLimits,
    limits_digest: digestB,
    preview_digest: digestA,
    created_at: "2026-08-01T12:00:00+00:00",
    updated_at: "2026-08-01T12:00:00+00:00",
    started_at: null,
    finished_at: null,
    cancel_reason: null,
    terminal_reason: null,
    candidate_count: null,
    error_count: null,
    timeout_count: null,
    terminal_receipt_digest: null,
  };
}

function durableObservation() {
  return {
    scan_id: scanId,
    endpoint_id: digestC,
    source: "active",
    interface_id: interfaceId,
    address: "192.168.50.8",
    port: 11_434,
    api_shape: "ollama_compatible",
    tls_enabled: false,
    certificate_sha256: null,
    catalog_digest: digestB,
    capability_digest: digestC,
    public_payload: {
      schema: "kestrel.lan.durable-observation.v1",
      endpoint_binding_digest: digestC,
      observation_digest: digestA,
      reachability: "reachable",
      transport_security: "plain_http",
      api_shape: "ollama_compatible",
      catalog_complete: true,
      catalog_truncated: false,
      model_ids: ["llama3.2"],
      capability_route: "/api/generate",
      selected_model_id: "llama3.2",
      capabilities: [
        {
          capability: "generation",
          provenance: "observed",
          status: "observed_pass",
          supported: true,
        },
        ...["streaming", "structured_output", "tools", "vision"].map(
          (capability) => ({
            capability,
            provenance: "not_run",
            status: "not_run",
            supported: null,
          }),
        ),
      ],
      failure_category: null,
    },
    freshness_timestamp: "2026-08-01T12:00:01Z",
    error_category: null,
    created_at: "2026-08-01T12:00:01+00:00",
  };
}

function providerResponse(revision = 1) {
  return {
    profile_id: profileId,
    display_name: "LAN model server",
    adapter: "ollama",
    base_url_configured: true,
    secret_configured: false,
    enabled: false,
    locality: "local",
    trust_class: "unreviewed",
    max_concurrency: 1,
    metadata: {
      lan_discovery: {
        endpoint_binding_digest: digestA,
        observation_digest: digestC,
        endpoint_fingerprint: digestA,
      },
    },
    revision,
    created_at: "2026-08-01T12:00:00+00:00",
    updated_at: "2026-08-01T12:00:01+00:00",
  };
}

function targetResponse(revision = 1) {
  return {
    target_id: targetId,
    provider_profile_id: profileId,
    provider: "ollama",
    model: "llama3.2",
    enabled: false,
    locality: "local",
    trust_class: "unreviewed",
    capability_tags: ["generation"],
    role_affinities: [],
    task_family_affinities: [],
    max_context_tokens: null,
    supports_tools: false,
    supports_json: false,
    supports_vision: false,
    supports_reasoning: false,
    supports_streaming: false,
    quality_tier: 1,
    latency_tier: 3,
    operator_priority: 0,
    estimated_cost_usd: null,
    input_cost_per_million_usd: null,
    output_cost_per_million_usd: null,
    health: "unknown",
    recent_failure_rate: 0,
    predicted_success: null,
    metadata: {
      lan_discovery: {
        endpoint_binding_digest: digestA,
        observation_digest: digestC,
        endpoint_fingerprint: digestA,
      },
    },
    revision,
    created_at: "2026-08-01T12:00:00+00:00",
    updated_at: "2026-08-01T12:00:01+00:00",
  };
}

function importResultResponse() {
  return {
    profile: providerResponse(),
    targets: [targetResponse()],
    observation_digest: digestC,
    endpoint_fingerprint: digestA,
    outage_observed: false,
    affected_target_ids: [targetId],
    invalidated_binding_digests: [],
    stale_reasons_by_target: [],
  };
}

function existingProfileOutageResultResponse() {
  const existingProfileMetadata = {
    lan_discovery: {
      endpoint_binding_digest: digestA,
      observation_digest: digestC,
      endpoint_fingerprint: digestA,
    },
  };
  return {
    profile: {
      ...providerResponse(),
      metadata: existingProfileMetadata,
    },
    targets: [
      {
        ...targetResponse(),
        metadata: existingProfileMetadata,
      },
    ],
    observation_digest: digestB,
    endpoint_fingerprint: digestA,
    outage_observed: true,
    affected_target_ids: [targetId],
    invalidated_binding_digests: [],
    stale_reasons_by_target: [],
  };
}

function existingProfileOutagePreviewResponse(
  selector = {
    scan_id: scanId,
    endpoint_id: digestA,
    replacement_provider_profile_id: null as string | null,
  },
) {
  return {
    selector,
    preview_digest: digestB,
    evidence_expires_at: "2026-08-01T12:05:00Z",
    authority: {
      expected_terminal_receipt_digest: digestB,
      expected_observation_digest: digestB,
      expected_profile_revision: 1,
      expected_target_revisions: [{ resource_id: targetId, revision: 0 }],
      endpoint_fingerprint: digestA,
      replacement: null,
    },
    result: existingProfileOutageResultResponse(),
    requires_confirmation: true,
  };
}

function importPreviewResponse(
  selector = {
    scan_id: scanId,
    endpoint_id: digestA,
    replacement_provider_profile_id: null as string | null,
  },
) {
  return {
    selector,
    preview_digest: digestB,
    evidence_expires_at: "2026-08-01T12:05:00Z",
    authority: {
      expected_terminal_receipt_digest: digestB,
      expected_observation_digest: digestC,
      expected_profile_revision: 0,
      expected_target_revisions: [{ resource_id: targetId, revision: 0 }],
      endpoint_fingerprint: digestA,
      replacement: null,
    },
    result: importResultResponse(),
    requires_confirmation: true,
  };
}

function reviewedTargetResponse(
  options: {
    intended_roles: string[];
    task_family_affinities: string[];
    enabled: boolean;
  } = {
    intended_roles: ["worker"],
    task_family_affinities: ["coding"],
    enabled: false,
  },
) {
  return {
    ...targetResponse(2),
    enabled: options.enabled,
    trust_class: "operator_confirmed",
    role_affinities: options.intended_roles,
    task_family_affinities: options.task_family_affinities,
    metadata: {
      lan_discovery: {
        endpoint_binding_digest: digestA,
        observation_digest: digestB,
        endpoint_fingerprint: digestC,
        privacy_acknowledgement_digest: digestB,
        material_binding_digest: digestA,
        reviewed_runtime_interface_binding_digest: options.enabled
          ? digestC
          : null,
      },
    },
  };
}

function reviewPreviewResponse(
  options = {
    target_id: targetId,
    intended_roles: ["worker"],
    task_family_affinities: ["coding"],
    enabled: false,
  },
) {
  return {
    options,
    preview_digest: digestC,
    evidence_expires_at: "2026-08-01T12:05:00Z",
    authority: {
      provider_profile_id: profileId,
      expected_profile_revision: 1,
      expected_target_revision: 1,
      expected_terminal_receipt_digest: digestA,
      expected_observation_digest: digestB,
      expected_endpoint_fingerprint: digestC,
      expected_material_binding_digest: digestA,
      expected_stale_reasons: [],
      trust_class: "operator_confirmed",
      privacy_acknowledgement_digest: digestB,
      review_digest: digestC,
      reviewed_material_binding_digest: digestA,
      reviewed_runtime_interface_binding_digest: null,
    },
    profile: providerResponse(2),
    target: reviewedTargetResponse(options),
    requires_privacy_acknowledgement: true,
    requires_confirmation: true,
  };
}

function reviewResultResponse(options: {
  intended_roles: string[];
  task_family_affinities: string[];
  enabled: boolean;
}) {
  return {
    profile: providerResponse(2),
    target: reviewedTargetResponse(options),
    privacy_acknowledgement_digest: digestB,
    material_binding_digest: digestA,
  };
}

function replacementImportPreviewResponse(options: {
  replacementEndpointFingerprint?: string;
  replacementMaterialDigests?: string[];
  targetMaterialDigest?: (index: number) => string | undefined;
  invalidatedBindingDigests?: string[];
}) {
  const replacementProfileId = `lan-provider-${"3".repeat(64)}`;
  const currentTargetId = `lan-target-${"f".repeat(64)}`;
  const replacementTargetIds = Array.from(
    { length: 2 },
    (_, index) => `lan-target-${index.toString(16).padStart(64, "0")}`,
  );
  const affectedTargetIds = [currentTargetId, ...replacementTargetIds];
  const targetRevisions = affectedTargetIds.map((resourceId) => ({
    resource_id: resourceId,
    revision: 1,
  }));
  const replacementEndpointFingerprint =
    options.replacementEndpointFingerprint ?? digestB;
  const replacementMaterialDigests =
    options.replacementMaterialDigests ??
    Array.from(
      { length: 2 },
      (_, index) => `sha256:${index.toString(16).padStart(64, "0")}`,
    );
  const selector = {
    scan_id: scanId,
    endpoint_id: digestA,
    replacement_provider_profile_id: replacementProfileId,
  };
  return {
    response: {
      ...importPreviewResponse(selector),
      authority: {
        ...importPreviewResponse(selector).authority,
        expected_target_revisions: targetRevisions,
        replacement: {
          provider_profile_id: replacementProfileId,
          expected_profile_revision: 1,
          expected_endpoint_fingerprint: replacementEndpointFingerprint,
          expected_material_binding_digests: replacementMaterialDigests,
        },
      },
      result: {
        ...importResultResponse(),
        targets: affectedTargetIds.map((resourceId, index) => {
          const isCurrent = index === 0;
          const materialDigest =
            options.targetMaterialDigest?.(index) ??
            (isCurrent ? undefined : replacementMaterialDigests[index - 1]);
          return {
            ...targetResponse(),
            target_id: resourceId,
            provider_profile_id: isCurrent ? profileId : replacementProfileId,
            model: isCurrent ? "current" : `replaced-${index}`,
            metadata: {
              lan_discovery: {
                endpoint_binding_digest: digestA,
                observation_digest: digestC,
                endpoint_fingerprint: isCurrent
                  ? digestA
                  : replacementEndpointFingerprint,
                ...(materialDigest !== undefined
                  ? { material_binding_digest: materialDigest }
                  : {}),
              },
            },
          };
        }),
        affected_target_ids: affectedTargetIds,
        invalidated_binding_digests:
          options.invalidatedBindingDigests ?? replacementMaterialDigests,
        stale_reasons_by_target: [],
      },
    },
    replacementProfileId,
  };
}

function responseFor(path: string, method: string, body: unknown): unknown {
  if (path === "/api/routing/lan/interfaces") return [];
  if (path === "/api/routing/lan/preview") {
    return {
      interface_id: interfaceId,
      network: "192.168.50.0/24",
      limits: automaticLimits,
      active_host_count: 254,
      passive_or_manual_only: false,
      port_count: 1_016,
      mdns_status: "available",
      server_version: "0.5.0",
      contract_version: "kestrel.lan.discovery.v1",
      preview_digest: digestA,
      issued_at: "2026-08-01T12:00:00Z",
      expires_at: "2026-08-01T12:01:00Z",
    };
  }
  if (
    path === "/api/routing/lan/manual-probe" &&
    typeof body === "object" &&
    body !== null &&
    "mode" in body &&
    body.mode === "preview"
  ) {
    return {
      schema: "kestrel.lan.manual-preview.v1",
      interface_id: interfaceId,
      port: 5_001,
      resolved_addresses: ["192.168.50.8"],
      preview_digest: digestA,
      issued_at: "2026-08-01T12:00:00Z",
      expires_at: "2026-08-01T12:01:00Z",
      server_version: "0.5.0",
      contract_version: "kestrel.lan.manual-preview-authorization.v1",
      requires_confirmation: true,
    };
  }
  if (path === "/api/routing/lan/scans" && method === "GET") return [];
  if (path === `/api/routing/lan/scans/${scanId}` && method === "GET") {
    return {
      ...scanResponse(),
      observations: [durableObservation()],
      observation_total_count: 1,
      observations_truncated: false,
      observation_next_cursor: null,
    };
  }
  if (path === "/api/routing/lan/import/preview") {
    const request = body as {
      scan_id: string;
      endpoint_id: string;
      replacement_provider_profile_id: string | null;
    };
    return importPreviewResponse(request);
  }
  if (path === "/api/routing/lan/import") {
    const request = body as {
      preview_digest: string;
    };
    return {
      preview_digest: request.preview_digest,
      result: importResultResponse(),
    };
  }
  if (path === `/api/routing/lan/targets/${targetId}/review/preview`) {
    const request = body as {
      intended_roles: string[];
      task_family_affinities: string[];
      enabled: boolean;
    };
    return reviewPreviewResponse({ target_id: targetId, ...request });
  }
  if (path === `/api/routing/lan/targets/${targetId}/review`) {
    const request = body as {
      intended_roles: string[];
      task_family_affinities: string[];
      enabled: boolean;
      preview_digest: string;
    };
    return {
      preview_digest: request.preview_digest,
      result: reviewResultResponse(request),
    };
  }
  if (path.startsWith("/api/routing/lan/")) return scanResponse();
  return [];
}

function captureFetch(requests: CapturedRequest[]) {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = typeof input === "string" ? input : input.toString();
    const method = init?.method ?? "GET";
    const body =
      typeof init?.body === "string"
        ? JSON.parse(init.body)
        : null;
    requests.push({
      path,
      method,
      headers: new Headers(init?.headers),
      body,
    });
    return jsonResponse(responseFor(path, method, body));
  });
}

describe("typed LAN discovery API", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    removeFakeDesktopEnvironment();
    sessionStorage.clear();
    localStorage.clear();
  });

  it("uses exact automatic-scan authority bodies without renderer maxima", async () => {
    const requests: CapturedRequest[] = [];
    vi.stubGlobal("fetch", captureFetch(requests));

    await previewLanScope({
      interfaceId,
      network: "192.168.50.0/24",
      clientMaxHosts: 65_535,
    } as Parameters<typeof previewLanScope>[0] & {
      clientMaxHosts: number;
    });
    await createLanScan({
      previewDigest: digestA,
      expectedRevision: 0,
      confirmed: true,
      clientPortCount: 65_535,
    } as Parameters<typeof createLanScan>[0] & {
      clientPortCount: number;
    });
    await startLanScan({
      scanId,
      expectedRevision: 1,
      previewDigest: digestA,
      confirmed: true,
      network: "0.0.0.0/0",
    } as Parameters<typeof startLanScan>[0] & {
      network: string;
    });
    await cancelLanScan({ scanId, expectedRevision: 2 });

    expect(requests.map(({ path, method, body }) => ({ path, method, body }))).toEqual([
      {
        path: "/api/routing/lan/preview",
        method: "POST",
        body: {
          interface_id: interfaceId,
          network: "192.168.50.0/24",
        },
      },
      {
        path: "/api/routing/lan/scans",
        method: "POST",
        body: {
          preview_digest: digestA,
          expected_revision: 0,
          confirmed: true,
        },
      },
      {
        path: `/api/routing/lan/scans/${scanId}/start`,
        method: "POST",
        body: {
          expected_revision: 1,
          preview_digest: digestA,
          confirmed: true,
        },
      },
      {
        path: `/api/routing/lan/scans/${scanId}/cancel`,
        method: "POST",
        body: { expected_revision: 2 },
      },
    ]);
  });

  it("keeps manual preview and confirmation as two exact calls", async () => {
    const requests: CapturedRequest[] = [];
    vi.stubGlobal("fetch", captureFetch(requests));

    await previewManualLanProbe({
      interfaceId,
      host: "model-box.local",
      port: 5_001,
    });
    await confirmManualLanProbe({
      expectedRevision: 0,
      previewDigest: digestA,
      selectedAddress: "192.168.50.8",
      confirmed: true,
      privacyAcknowledged: true,
    });

    expect(requests.map(({ path, body }) => ({ path, body }))).toEqual([
      {
        path: "/api/routing/lan/manual-probe",
        body: {
          mode: "preview",
          interface_id: interfaceId,
          host: "model-box.local",
          port: 5_001,
        },
      },
      {
        path: "/api/routing/lan/manual-probe",
        body: {
          mode: "confirm",
          expected_revision: 0,
          preview_digest: digestA,
          selected_address: "192.168.50.8",
          confirmed: true,
          privacy_acknowledged: true,
        },
      },
    ]);
  });

  it("uses bounded read paths and encodes the scan identifier", async () => {
    const requests: CapturedRequest[] = [];
    vi.stubGlobal("fetch", captureFetch(requests));

    await getLanInterfaces();
    await listLanScans();
    const detail = await getLanScan(scanId);

    expect(requests.map(({ path, method }) => ({ path, method }))).toEqual([
      { path: "/api/routing/lan/interfaces", method: "GET" },
      { path: "/api/routing/lan/scans", method: "GET" },
      {
        path: `/api/routing/lan/scans/${scanId}`,
        method: "GET",
      },
    ]);
    expect(detail.observations[0]?.public_payload).toMatchObject({
      schema: "kestrel.lan.durable-observation.v1",
      model_ids: ["llama3.2"],
      failure_category: null,
    });
  });

  it("sends an opaque observation cursor only in the bounded request header", async () => {
    const requests: CapturedRequest[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = typeof input === "string" ? input : input.toString();
        requests.push({
          path,
          method: init?.method ?? "GET",
          headers: new Headers(init?.headers),
          body: null,
        });
        return jsonResponse({
          ...scanResponse(),
          observations: [durableObservation()],
          observation_total_count: 2,
          observations_truncated: true,
          observation_next_cursor: nextObservationCursor,
        });
      }),
    );

    const page = await getLanScanPage(scanId, {
      cursor: observationCursor,
    });

    expect(requests).toHaveLength(1);
    expect(requests[0]?.path).toBe(`/api/routing/lan/scans/${scanId}`);
    expect(
      requests[0]?.headers.get("Kestrel-Lan-Observation-Cursor"),
    ).toBe(observationCursor);
    expect(page.observation_next_cursor).toBe(nextObservationCursor);
    expect(page.observations_truncated).toBe(true);
  });

  it.each([
    {
      name: "mismatched scan",
      inputCursor: undefined,
      response: {
        ...scanResponse(),
        scan_id: `lan_${"f".repeat(32)}`,
        observations: [],
        observation_total_count: 0,
        observations_truncated: false,
        observation_next_cursor: null,
      },
    },
    {
      name: "replayed cursor",
      inputCursor: observationCursor,
      response: {
        ...scanResponse(),
        observations: [durableObservation()],
        observation_total_count: 2,
        observations_truncated: true,
        observation_next_cursor: observationCursor,
      },
    },
    {
      name: "cursor without truncation",
      inputCursor: undefined,
      response: {
        ...scanResponse(),
        observations: [durableObservation()],
        observation_total_count: 1,
        observations_truncated: false,
        observation_next_cursor: nextObservationCursor,
      },
    },
    {
      name: "truncated first page without cursor",
      inputCursor: undefined,
      response: {
        ...scanResponse(),
        observations: [durableObservation()],
        observation_total_count: 2,
        observations_truncated: true,
        observation_next_cursor: null,
      },
    },
  ])("rejects an incoherent scan page response: $name", async ({
    inputCursor,
    response,
  }) => {
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(response)),
    );

    await expect(
      getLanScanPage(
        scanId,
        inputCursor === undefined ? {} : { cursor: inputCursor },
      ),
    ).rejects.toThrow("lan_response_invalid");
  });

  it.each(["", "cursor with spaces", "abc=", "*", "AB", "A".repeat(1_025)])(
    "rejects a malformed observation cursor before fetch: %s",
    async (cursor) => {
      const fetchMock = captureFetch([]);
      vi.stubGlobal("fetch", fetchMock);

      await expect(getLanScanPage(scanId, { cursor })).rejects.toThrow(
        "lan_request_invalid",
      );
      expect(fetchMock).not.toHaveBeenCalled();
    },
  );

  it.each(["", "abc=", "*", "AB", "A".repeat(1_025), 7])(
    "rejects a malformed observation cursor in a scan response: %s",
    async (cursor) => {
      vi.stubGlobal(
        "fetch",
        vi.fn<typeof fetch>().mockResolvedValue(
          jsonResponse({
            ...scanResponse(),
            observations: [durableObservation()],
            observation_total_count: 1,
            observations_truncated: false,
            observation_next_cursor: cursor,
          }),
        ),
      );

      await expect(getLanScan(scanId)).rejects.toThrow(
        "lan_response_invalid",
      );
    },
  );

  it("rejects a noncanonical scan identifier before fetch", async () => {
    const fetchMock = captureFetch([]);
    vi.stubGlobal("fetch", fetchMock);

    await expect(getLanScan("lan id/with separators")).rejects.toThrow(
      "lan_request_invalid",
    );
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("rejects malformed LAN mutation authority before fetch", async () => {
    const fetchMock = captureFetch([]);
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      previewLanScope({
        interfaceId: "sha256:not-a-digest",
        network: "192.168.50.0/24",
      }),
    ).rejects.toThrow("lan_request_invalid");
    await expect(
      previewManualLanProbe({
        interfaceId,
        host: "https://credential@example.invalid/path",
        port: 5_001,
      }),
    ).rejects.toThrow("lan_request_invalid");
    await expect(
      startLanScan({
        scanId,
        expectedRevision: -1,
        previewDigest: digestA,
        confirmed: true,
      }),
    ).rejects.toThrow("lan_request_invalid");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("replays SSE after the persisted sequence through authenticated fetch", async () => {
    sessionStorage.setItem("kestrel.apiToken", "browser-token");
    const encoder = new TextEncoder();
    const event: LanScanEvent = {
      scan_id: scanId,
      sequence: "8",
      event_type: "scan_progress",
      payload: {
        planned_count: 4,
        admitted_count: 4,
        completed_count: 2,
        persisted_observation_count: 1,
        error_category_counts: {},
        timeout_count: 0,
        mdns_status: "available",
      },
      created_at: "2026-08-01T12:00:00Z",
    };
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        new ReadableStream<Uint8Array>({
          start(controller) {
            controller.enqueue(encoder.encode(": heartbeat\n\nid: 8\nevent: scan_progress\n"));
            controller.enqueue(
              encoder.encode(
                `data: ${JSON.stringify({ ...event, sequence: 8 })}\n\n`,
              ),
            );
            controller.close();
          },
        }),
        { status: 200, headers: { "Content-Type": "text/event-stream" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const events: LanScanEvent[] = [];

    await streamLanScanEvents(scanId, {
      afterSequence: "7",
      signal: new AbortController().signal,
      onEvent: (next) => events.push(next),
    });

    expect(events).toEqual([event]);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [path, init] = fetchMock.mock.calls[0] ?? [];
    const headers = new Headers(init?.headers);
    expect(path).toBe(`/api/routing/lan/scans/${scanId}/events`);
    expect(headers.get("accept")).toBe("text/event-stream");
    expect(headers.get("last-event-id")).toBe("7");
    expect(headers.get("authorization")).toBe("Bearer browser-token");
  });

  it("accepts many bounded SSE frames coalesced into one network read", async () => {
    const encoder = new TextEncoder();
    const frames = Array.from({ length: 150 }, (_, index) => {
      const sequence = index + 1;
      return [
        `id: ${sequence}`,
        "event: scan_progress",
        `data: ${JSON.stringify({
          scan_id: scanId,
          sequence,
          event_type: "scan_progress",
          payload: {
            planned_count: 256,
            admitted_count: 256,
            completed_count: sequence,
            persisted_observation_count: Math.min(sequence, 8),
            error_category_counts: {},
            timeout_count: 0,
            mdns_status: "available",
          },
          created_at: "2026-08-01T12:00:00Z",
        })}`,
        "",
        "",
      ].join("\n");
    }).join("");
    expect(encoder.encode(frames).byteLength).toBeGreaterThan(32 * 1_024);
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>().mockResolvedValue(
        new Response(
          new ReadableStream<Uint8Array>({
            start(controller) {
              controller.enqueue(encoder.encode(frames));
              controller.close();
            },
          }),
          { status: 200, headers: { "Content-Type": "text/event-stream" } },
        ),
      ),
    );
    const events: LanScanEvent[] = [];

    await streamLanScanEvents(scanId, {
      afterSequence: "0",
      signal: new AbortController().signal,
      onEvent: (event) => events.push(event),
    });

    expect(events).toHaveLength(150);
    expect(events.at(-1)?.sequence).toBe("150");
  });

  it("preserves signed-int64 event cursors when JSON carries decimal text", async () => {
    const sequence = "9007199254740993";
    const payload = {
      scan_id: scanId,
      sequence,
      event_type: "scan_progress",
      payload: {
        planned_count: 1,
        admitted_count: 1,
        completed_count: 1,
        persisted_observation_count: 0,
        error_category_counts: {},
        timeout_count: 0,
        mdns_status: "available",
      },
      created_at: "2026-08-01T12:00:00Z",
    };
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>().mockResolvedValue(
        new Response(
          `id: ${sequence}\nevent: scan_progress\ndata: ${JSON.stringify(payload)}\n\n`,
          { status: 200, headers: { "Content-Type": "text/event-stream" } },
        ),
      ),
    );
    const events: LanScanEvent[] = [];

    await streamLanScanEvents(scanId, {
      afterSequence: "9007199254740992",
      signal: new AbortController().signal,
      onEvent: (event) => events.push(event),
    });

    expect(events[0]?.sequence).toBe(sequence);
  });

  it("fails closed on an unterminated SSE frame at EOF", async () => {
    const payload = JSON.stringify({
      scan_id: scanId,
      sequence: 1,
      event_type: "scan_progress",
      payload: {
        planned_count: 1,
        admitted_count: 1,
        completed_count: 1,
        persisted_observation_count: 0,
        error_category_counts: {},
        timeout_count: 0,
        mdns_status: "available",
      },
      created_at: "2026-08-01T12:00:00Z",
    });
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>().mockResolvedValue(
        new Response(`id: 1\nevent: scan_progress\ndata: ${payload}\n`, {
          status: 200,
          headers: { "Content-Type": "text/event-stream" },
        }),
      ),
    );

    await expect(
      streamLanScanEvents(scanId, {
        afterSequence: "0",
        signal: new AbortController().signal,
        onEvent: () => undefined,
      }),
    ).rejects.toThrow("lan_event_stream_invalid");
  });

  it("fails closed on malformed UTF-8 even inside an SSE comment", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>().mockResolvedValue(
        new Response(new Uint8Array([0x3a, 0x20, 0xff, 0x0a, 0x0a]), {
          status: 200,
          headers: { "Content-Type": "text/event-stream" },
        }),
      ),
    );

    await expect(
      streamLanScanEvents(scanId, {
        afterSequence: "0",
        signal: new AbortController().signal,
        onEvent: () => undefined,
      }),
    ).rejects.toThrow("lan_event_stream_invalid");
  });

  it("rejects mismatched SSE authority with a fixed non-echoing error", async () => {
    const secret = "raw-stream-secret-must-not-leak";
    const event = {
      scan_id: scanId,
      sequence: 9,
      event_type: "scan_progress",
      payload: { secret },
      created_at: "2026-08-01T12:00:00Z",
    };
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>().mockResolvedValue(
        new Response(
          `id: 8\nevent: scan_progress\ndata: ${JSON.stringify(event)}\n\n`,
          { status: 200, headers: { "Content-Type": "text/event-stream" } },
        ),
      ),
    );

    let caught: unknown;
    try {
      await streamLanScanEvents(scanId, {
        afterSequence: "7",
        signal: new AbortController().signal,
        onEvent: () => undefined,
      });
    } catch (error) {
      caught = error;
    }

    expect(caught).toBeInstanceOf(Error);
    expect((caught as Error).message).toBe("lan_event_stream_invalid");
    expect((caught as Error).message).not.toContain(secret);
  });

  it("rejects malformed LAN JSON with a fixed non-echoing error", async () => {
    const secret = "raw-json-secret-must-not-leak";
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>().mockResolvedValue(
        jsonResponse([
          {
            interface_id: interfaceId,
            display_name: secret,
            addresses: "not-an-array",
          },
        ]),
      ),
    );

    let caught: unknown;
    try {
      await getLanInterfaces();
    } catch (error) {
      caught = error;
    }

    expect(caught).toBeInstanceOf(Error);
    expect((caught as Error).message).toBe("lan_response_invalid");
    expect((caught as Error).message).not.toContain(secret);
  });

  it("rejects observation evidence that disagrees with durable columns", async () => {
    const hostile = durableObservation();
    hostile.public_payload.endpoint_binding_digest = digestB;
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>().mockResolvedValue(
        jsonResponse({
          ...scanResponse(),
          observations: [hostile],
          observation_total_count: 1,
          observations_truncated: false,
          observation_next_cursor: null,
        }),
      ),
    );

    await expect(getLanScan(scanId)).rejects.toThrow("lan_response_invalid");
  });

  it("uses four exact server-owned preview and confirmation bodies", async () => {
    const requests: CapturedRequest[] = [];
    vi.stubGlobal("fetch", captureFetch(requests));
    const selector = {
      scanId,
      endpointId: digestA,
      replacementProviderProfileId: null,
    } as const;
    const options = {
      targetId,
      intendedRoles: ["worker"],
      taskFamilyAffinities: ["coding"],
      enabled: false,
    };

    const importPreview = await previewLanImport(selector);
    const importConfirmation = await confirmLanImport({
      selector,
      previewDigest: importPreview.preview_digest,
      confirmed: true,
    });
    const reviewPreview = await previewLanTargetReview(options);
    const reviewConfirmation = await confirmLanTargetReview({
      ...options,
      previewDigest: reviewPreview.preview_digest,
      privacyAcknowledged: true,
      confirmed: true,
    });

    expect(requests.map(({ path, body }) => ({ path, body }))).toEqual([
      {
        path: "/api/routing/lan/import/preview",
        body: {
          scan_id: scanId,
          endpoint_id: digestA,
          replacement_provider_profile_id: null,
        },
      },
      {
        path: "/api/routing/lan/import",
        body: {
          selector: {
            scan_id: scanId,
            endpoint_id: digestA,
            replacement_provider_profile_id: null,
          },
          preview_digest: digestB,
          confirmed: true,
        },
      },
      {
        path: `/api/routing/lan/targets/${targetId}/review/preview`,
        body: {
          intended_roles: ["worker"],
          task_family_affinities: ["coding"],
          enabled: false,
        },
      },
      {
        path: `/api/routing/lan/targets/${targetId}/review`,
        body: {
          intended_roles: ["worker"],
          task_family_affinities: ["coding"],
          enabled: false,
          preview_digest: digestC,
          privacy_acknowledged: true,
          confirmed: true,
        },
      },
    ]);
    expect(importConfirmation.preview_digest).toBe(digestB);
    expect(reviewConfirmation.preview_digest).toBe(digestC);
  });

  it("rejects legacy renderer-owned authority fields before fetch", async () => {
    const fetchMock = captureFetch([]);
    vi.stubGlobal("fetch", fetchMock);
    const selector = {
      scanId,
      endpointId: digestA,
      replacementProviderProfileId: null,
    } as const;
    const options = {
      targetId,
      intendedRoles: ["worker"],
      taskFamilyAffinities: ["coding"],
      enabled: false,
    };
    const calls = [
      () =>
        previewLanImport({
          ...selector,
          expectedObservationDigest: digestC,
        } as unknown as Parameters<typeof previewLanImport>[0]),
      () =>
        confirmLanImport({
          selector: {
            ...selector,
            expectedProfileRevision: 1,
          },
          previewDigest: digestB,
          confirmed: true,
        } as unknown as Parameters<typeof confirmLanImport>[0]),
      () =>
        confirmLanImport({
          selector,
          previewDigest: digestB,
          confirmed: true,
          replacement: null,
        } as unknown as Parameters<typeof confirmLanImport>[0]),
      () =>
        previewLanTargetReview({
          ...options,
          trustClass: "operator_confirmed",
        } as unknown as Parameters<typeof previewLanTargetReview>[0]),
      () =>
        confirmLanTargetReview({
          ...options,
          previewDigest: digestC,
          privacyAcknowledged: true,
          confirmed: true,
          expectedReviewDigest: digestB,
        } as unknown as Parameters<typeof confirmLanTargetReview>[0]),
    ];

    for (const call of calls) {
      await expect(call()).rejects.toThrow("lan_request_invalid");
    }
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("requires exact literal confirmation and privacy booleans before fetch", async () => {
    const fetchMock = captureFetch([]);
    vi.stubGlobal("fetch", fetchMock);
    const selector = {
      scanId,
      endpointId: digestA,
      replacementProviderProfileId: null,
    } as const;
    const options = {
      targetId,
      intendedRoles: [] as string[],
      taskFamilyAffinities: [] as string[],
      enabled: false,
      previewDigest: digestC,
      privacyAcknowledged: true,
      confirmed: true,
    };

    for (const confirmed of [false, 1]) {
      await expect(
        confirmLanImport({
          selector,
          previewDigest: digestB,
          confirmed,
        } as unknown as Parameters<typeof confirmLanImport>[0]),
      ).rejects.toThrow("lan_request_invalid");
    }
    for (const privacyAcknowledged of [false, 1]) {
      await expect(
        confirmLanTargetReview({
          ...options,
          privacyAcknowledged,
        } as unknown as Parameters<typeof confirmLanTargetReview>[0]),
      ).rejects.toThrow("lan_request_invalid");
    }
    for (const confirmed of [false, 1]) {
      await expect(
        confirmLanTargetReview({
          ...options,
          confirmed,
        } as unknown as Parameters<typeof confirmLanTargetReview>[0]),
      ).rejects.toThrow("lan_request_invalid");
    }
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("preserves Python code-point ordering and spaces through both review phases", async () => {
    const requests: CapturedRequest[] = [];
    vi.stubGlobal("fetch", captureFetch(requests));
    const options = {
      targetId,
      intendedRoles: ["Ａ", "😀"],
      taskFamilyAffinities: [" family "],
      enabled: false,
    };

    const preview = await previewLanTargetReview(options);
    const confirmation = await confirmLanTargetReview({
      ...options,
      previewDigest: preview.preview_digest,
      privacyAcknowledged: true,
      confirmed: true,
    });

    expect(requests[0]?.body).toEqual({
      intended_roles: ["Ａ", "😀"],
      task_family_affinities: [" family "],
      enabled: false,
    });
    expect(requests[1]?.body).toMatchObject({
      intended_roles: ["Ａ", "😀"],
      task_family_affinities: [" family "],
    });
    expect(preview.target.role_affinities).toEqual(["Ａ", "😀"]);
    expect(confirmation.result.target.task_family_affinities).toEqual([
      " family ",
    ]);
  });

  it.each([
    ["more than 64 UTF-8 bytes", ["😀".repeat(17)]],
    ["non-NFC text", ["e\u0301"]],
    ["a Unicode control category", ["worker\u0000"]],
    ["a duplicate", ["worker", "worker"]],
    ["a noncanonical order", ["😀", "Ａ"]],
    [
      "more than 16 entries",
      Array.from({ length: 17 }, (_, index) => `family-${index
        .toString()
        .padStart(2, "0")}`),
    ],
  ])("rejects review affinity lists containing %s before fetch", async (_label, affinities) => {
    const fetchMock = captureFetch([]);
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      previewLanTargetReview({
        targetId,
        intendedRoles: affinities,
        taskFamilyAffinities: [],
        enabled: false,
      }),
    ).rejects.toThrow("lan_request_invalid");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("accepts a complete 65-target server-owned import preview without caps", async () => {
    const replacementProfileId = `lan-provider-${"3".repeat(64)}`;
    const currentTargetId = `lan-target-${"f".repeat(64)}`;
    const replacementTargetIds = Array.from(
      { length: 64 },
      (_, index) => `lan-target-${index.toString(16).padStart(64, "0")}`,
    );
    const affectedTargetIds = [currentTargetId, ...replacementTargetIds];
    const targetRevisions = affectedTargetIds.map((resourceId) => ({
      resource_id: resourceId,
      revision: 1,
    }));
    const replacementEndpointFingerprint = digestB;
    const replacementMaterialDigests = Array.from(
      { length: 64 },
      (_, index) => `sha256:${index.toString(16).padStart(64, "0")}`,
    );
    const selector = {
      scan_id: scanId,
      endpoint_id: digestA,
      replacement_provider_profile_id: replacementProfileId,
    };
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>().mockResolvedValue(
        jsonResponse({
          ...importPreviewResponse(selector),
          authority: {
            ...importPreviewResponse(selector).authority,
            expected_target_revisions: targetRevisions,
            replacement: {
              provider_profile_id: replacementProfileId,
              expected_profile_revision: 1,
              expected_endpoint_fingerprint: replacementEndpointFingerprint,
              expected_material_binding_digests: replacementMaterialDigests,
            },
          },
          result: {
            ...importResultResponse(),
            targets: affectedTargetIds.map((resourceId, index) => {
              const isCurrent = index === 0;
              return {
                ...targetResponse(),
                target_id: resourceId,
                provider_profile_id: isCurrent
                  ? profileId
                  : replacementProfileId,
                model: isCurrent ? "current" : `replaced-${index}`,
                metadata: {
                  lan_discovery: {
                    endpoint_binding_digest: digestA,
                    observation_digest: digestC,
                    endpoint_fingerprint: isCurrent
                      ? digestA
                      : replacementEndpointFingerprint,
                    material_binding_digest: isCurrent
                      ? undefined
                      : replacementMaterialDigests[index - 1],
                  },
                },
              };
            }),
            affected_target_ids: affectedTargetIds,
            invalidated_binding_digests: replacementMaterialDigests,
            stale_reasons_by_target: [
              { target_id: currentTargetId, reasons: ["catalog_changed"] },
              {
                target_id: replacementTargetIds[63],
                reasons: ["catalog_changed"],
              },
            ],
          },
        }),
      ),
    );

    const preview = await previewLanImport({
      scanId,
      endpointId: digestA,
      replacementProviderProfileId: replacementProfileId,
    });

    expect(preview.authority.expected_target_revisions).toHaveLength(65);
    expect(
      preview.authority.replacement?.expected_material_binding_digests,
    ).toHaveLength(64);
    expect(preview.result.targets).toHaveLength(65);
    expect(preview.result.affected_target_ids).toEqual(affectedTargetIds);
  });

  it("rejects a replacement-family target whose endpoint fingerprint does not match the replacement authority", async () => {
    const { response, replacementProfileId } =
      replacementImportPreviewResponse({});
    const hostile = {
      ...response,
      result: {
        ...response.result,
        targets: response.result.targets.map((target) =>
          target.provider_profile_id === replacementProfileId
            ? {
                ...target,
                metadata: {
                  lan_discovery: {
                    ...target.metadata.lan_discovery,
                    endpoint_fingerprint: digestC,
                  },
                },
              }
            : target,
        ),
      },
    };
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(hostile)),
    );

    await expect(
      previewLanImport({
        scanId,
        endpointId: digestA,
        replacementProviderProfileId: replacementProfileId,
      }),
    ).rejects.toThrow("lan_response_invalid");
  });

  it("rejects a replacement-family target whose material binding digest is not expected by the replacement authority", async () => {
    const { response, replacementProfileId } =
      replacementImportPreviewResponse({});
    const hostile = {
      ...response,
      result: {
        ...response.result,
        targets: response.result.targets.map((target, index) =>
          target.provider_profile_id === replacementProfileId && index === 1
            ? {
                ...target,
                metadata: {
                  lan_discovery: {
                    ...target.metadata.lan_discovery,
                    material_binding_digest: digestC,
                  },
                },
              }
            : target,
        ),
      },
    };
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(hostile)),
    );

    await expect(
      previewLanImport({
        scanId,
        endpointId: digestA,
        replacementProviderProfileId: replacementProfileId,
      }),
    ).rejects.toThrow("lan_response_invalid");
  });

  it("rejects replacement import when invalidated binding digests disagree with the authority", async () => {
    const { response, replacementProfileId } =
      replacementImportPreviewResponse({});
    const hostile = {
      ...response,
      result: {
        ...response.result,
        invalidated_binding_digests: [digestC],
      },
    };
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(hostile)),
    );

    await expect(
      previewLanImport({
        scanId,
        endpointId: digestA,
        replacementProviderProfileId: replacementProfileId,
      }),
    ).rejects.toThrow("lan_response_invalid");
  });

  it("rejects import preview evidence/result correlation failures", async () => {
    const otherProfileId = `lan-provider-${"4".repeat(64)}`;
    const otherTargetId = `lan-target-${"5".repeat(64)}`;
    const responses = [
      {
        ...importPreviewResponse(),
        result: { ...importResultResponse(), affected_target_ids: [] },
      },
      {
        ...importPreviewResponse(),
        result: {
          ...importResultResponse(),
          targets: [
            { ...targetResponse(), provider_profile_id: otherProfileId },
          ],
        },
      },
      {
        ...importPreviewResponse(),
        result: { ...importResultResponse(), observation_digest: digestB },
      },
      {
        ...importPreviewResponse(),
        result: { ...importResultResponse(), endpoint_fingerprint: digestB },
      },
      {
        ...importPreviewResponse(),
        result: {
          ...importResultResponse(),
          stale_reasons_by_target: [
            { target_id: otherTargetId, reasons: ["catalog_changed"] },
          ],
        },
      },
    ];
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>().mockImplementation(async () =>
        jsonResponse(responses.shift()),
      ),
    );

    for (let index = 0; index < 5; index += 1) {
      await expect(
        previewLanImport({
          scanId,
          endpointId: digestA,
          replacementProviderProfileId: null,
        }),
      ).rejects.toThrow("lan_response_invalid");
    }
  });

  it("rejects an import result whose current profile is not bound to the selected endpoint", async () => {
    const replacementProfileId = `lan-provider-${"3".repeat(64)}`;
    const selector = {
      scan_id: scanId,
      endpoint_id: digestA,
      replacement_provider_profile_id: replacementProfileId,
    };
    const mismatchedMetadata = {
      lan_discovery: {
        endpoint_binding_digest: digestB,
        observation_digest: digestC,
        endpoint_fingerprint: digestA,
      },
    };
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>().mockResolvedValue(
        jsonResponse({
          ...importPreviewResponse(selector),
          authority: {
            ...importPreviewResponse(selector).authority,
            replacement: {
              provider_profile_id: replacementProfileId,
              expected_profile_revision: 1,
              expected_endpoint_fingerprint: digestA,
              expected_material_binding_digests: [digestA],
            },
          },
          result: {
            ...importResultResponse(),
            profile: {
              ...providerResponse(),
              profile_id: replacementProfileId,
              metadata: mismatchedMetadata,
            },
            targets: [
              {
                ...targetResponse(),
                provider_profile_id: replacementProfileId,
                metadata: mismatchedMetadata,
              },
            ],
          },
        }),
      ),
    );

    await expect(
      previewLanImport({
        scanId,
        endpointId: digestA,
        replacementProviderProfileId: replacementProfileId,
      }),
    ).rejects.toThrow("lan_response_invalid");
  });

  it("accepts an existing-profile outage import preview with a stale observation in metadata", async () => {
    const selector = {
      scan_id: scanId,
      endpoint_id: digestA,
      replacement_provider_profile_id: null,
    };
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>().mockResolvedValue(
        jsonResponse(existingProfileOutagePreviewResponse(selector)),
      ),
    );

    const preview = await previewLanImport({
      scanId,
      endpointId: digestA,
      replacementProviderProfileId: null,
    });

    expect(preview.result.outage_observed).toBe(true);
    expect(preview.result.observation_digest).toBe(digestB);
    expect(preview.result.profile?.profile_id).toBe(profileId);
    expect(preview.result.targets[0]?.target_id).toBe(targetId);
  });

  it("accepts an existing-profile outage import confirmation with a stale observation in metadata", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>().mockResolvedValue(
        jsonResponse({
          preview_digest: digestB,
          result: existingProfileOutageResultResponse(),
        }),
      ),
    );

    const confirmation = await confirmLanImport({
      selector: {
        scanId,
        endpointId: digestA,
        replacementProviderProfileId: null,
      },
      previewDigest: digestB,
      confirmed: true,
    });

    expect(confirmation.result.outage_observed).toBe(true);
    expect(confirmation.result.observation_digest).toBe(digestB);
    expect(confirmation.result.profile?.profile_id).toBe(profileId);
    expect(confirmation.result.targets[0]?.target_id).toBe(targetId);
  });

  it("rejects preview selector/options and confirmation digest disagreement", async () => {
    const otherTargetId = `lan-target-${"5".repeat(64)}`;
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        jsonResponse(
          importPreviewResponse({
            scan_id: scanId,
            endpoint_id: digestB,
            replacement_provider_profile_id: null,
          }),
        ),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          preview_digest: digestC,
          result: importResultResponse(),
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse(
          reviewPreviewResponse({
            target_id: otherTargetId,
            intended_roles: ["worker"],
            task_family_affinities: ["coding"],
            enabled: false,
          }),
        ),
      );
    vi.stubGlobal("fetch", fetchMock);
    const selector = {
      scanId,
      endpointId: digestA,
      replacementProviderProfileId: null,
    } as const;

    await expect(previewLanImport(selector)).rejects.toThrow(
      "lan_response_invalid",
    );
    await expect(
      confirmLanImport({
        selector,
        previewDigest: digestB,
        confirmed: true,
      }),
    ).rejects.toThrow("lan_response_invalid");
    await expect(
      previewLanTargetReview({
        targetId,
        intendedRoles: ["worker"],
        taskFamilyAffinities: ["coding"],
        enabled: false,
      }),
    ).rejects.toThrow("lan_response_invalid");
  });

  it("rejects review authority and returned-target disagreement", async () => {
    const otherProfileId = `lan-provider-${"4".repeat(64)}`;
    const otherTargetId = `lan-target-${"5".repeat(64)}`;
    const baseOptions = {
      target_id: targetId,
      intended_roles: ["worker"],
      task_family_affinities: ["coding"],
      enabled: false,
    };
    const responses = [
      {
        ...reviewPreviewResponse(baseOptions),
        authority: {
          ...reviewPreviewResponse(baseOptions).authority,
          provider_profile_id: otherProfileId,
        },
      },
      {
        ...reviewPreviewResponse(baseOptions),
        target: { ...reviewedTargetResponse(), target_id: otherTargetId },
      },
      {
        ...reviewPreviewResponse(baseOptions),
        target: {
          ...reviewedTargetResponse(),
          provider_profile_id: otherProfileId,
        },
      },
      {
        ...reviewPreviewResponse(baseOptions),
        target: { ...reviewedTargetResponse(), enabled: true },
      },
      {
        ...reviewPreviewResponse(baseOptions),
        target: { ...reviewedTargetResponse(), trust_class: "unreviewed" },
      },
      {
        ...reviewPreviewResponse(baseOptions),
        target: { ...reviewedTargetResponse(), role_affinities: [] },
      },
      {
        ...reviewPreviewResponse(baseOptions),
        target: {
          ...reviewedTargetResponse(),
          task_family_affinities: [],
        },
      },
    ];
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>().mockImplementation(async () =>
        jsonResponse(responses.shift()),
      ),
    );

    for (let index = 0; index < 7; index += 1) {
      await expect(
        previewLanTargetReview({
          targetId,
          intendedRoles: ["worker"],
          taskFamilyAffinities: ["coding"],
          enabled: false,
        }),
      ).rejects.toThrow("lan_response_invalid");
    }
  });

  it("binds review runtime and receipt evidence to the planned target", async () => {
    const enabledOptions = {
      target_id: targetId,
      intended_roles: ["worker"],
      task_family_affinities: ["coding"],
      enabled: true,
    };
    const missingRuntimeBinding = reviewPreviewResponse(enabledOptions);
    const mismatchedReceipt = reviewPreviewResponse();
    mismatchedReceipt.target = {
      ...mismatchedReceipt.target,
      metadata: {
        lan_discovery: {
          endpoint_binding_digest: digestA,
          observation_digest: digestB,
          endpoint_fingerprint: digestC,
          privacy_acknowledgement_digest: digestA,
          material_binding_digest: digestA,
          reviewed_runtime_interface_binding_digest: null,
        },
      },
    };
    vi.stubGlobal(
      "fetch",
      vi
        .fn<typeof fetch>()
        .mockResolvedValueOnce(jsonResponse(missingRuntimeBinding))
        .mockResolvedValueOnce(jsonResponse(mismatchedReceipt)),
    );

    await expect(
      previewLanTargetReview({
        targetId,
        intendedRoles: ["worker"],
        taskFamilyAffinities: ["coding"],
        enabled: true,
      }),
    ).rejects.toThrow("lan_response_invalid");
    await expect(
      previewLanTargetReview({
        targetId,
        intendedRoles: ["worker"],
        taskFamilyAffinities: ["coding"],
        enabled: false,
      }),
    ).rejects.toThrow("lan_response_invalid");
  });

  it("requires canonical Z preview evidence expiry timestamps", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn<typeof fetch>()
        .mockResolvedValueOnce(
          jsonResponse({
            ...importPreviewResponse(),
            evidence_expires_at: "2026-08-01T12:05:00+00:00",
          }),
        )
        .mockResolvedValueOnce(
          jsonResponse({
            ...reviewPreviewResponse(),
            evidence_expires_at: "2026-08-01T12:05:00+00:00",
          }),
        ),
    );

    await expect(
      previewLanImport({
        scanId,
        endpointId: digestA,
        replacementProviderProfileId: null,
      }),
    ).rejects.toThrow("lan_response_invalid");
    await expect(
      previewLanTargetReview({
        targetId,
        intendedRoles: ["worker"],
        taskFamilyAffinities: ["coding"],
        enabled: false,
      }),
    ).rejects.toThrow("lan_response_invalid");
  });

  it("rejects missing or additional preview/confirmation response fields", async () => {
    const importMissing = importPreviewResponse() as Record<string, unknown>;
    delete importMissing.requires_confirmation;
    const reviewExtra = {
      ...reviewPreviewResponse(),
      expected_review_digest: digestB,
    };
    const confirmationExtra = {
      preview_digest: digestB,
      result: importResultResponse(),
      authority: {},
    };
    vi.stubGlobal(
      "fetch",
      vi
        .fn<typeof fetch>()
        .mockResolvedValueOnce(jsonResponse(importMissing))
        .mockResolvedValueOnce(jsonResponse(reviewExtra))
        .mockResolvedValueOnce(jsonResponse(confirmationExtra)),
    );
    const selector = {
      scanId,
      endpointId: digestA,
      replacementProviderProfileId: null,
    } as const;

    await expect(previewLanImport(selector)).rejects.toThrow(
      "lan_response_invalid",
    );
    await expect(
      previewLanTargetReview({
        targetId,
        intendedRoles: ["worker"],
        taskFamilyAffinities: ["coding"],
        enabled: false,
      }),
    ).rejects.toThrow("lan_response_invalid");
    await expect(
      confirmLanImport({
        selector,
        previewDigest: digestB,
        confirmed: true,
      }),
    ).rejects.toThrow("lan_response_invalid");
  });
});
