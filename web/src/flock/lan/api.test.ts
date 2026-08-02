import { afterEach, describe, expect, it, vi } from "vitest";
import {
  importLanObservation,
  reviewLanTarget,
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
    metadata: {},
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
    metadata: {},
    revision,
    created_at: "2026-08-01T12:00:00+00:00",
    updated_at: "2026-08-01T12:00:01+00:00",
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
    };
  }
  if (path === "/api/routing/lan/import") {
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
  if (path === `/api/routing/lan/targets/${targetId}/review`) {
    const request =
      typeof body === "object" && body !== null
        ? (body as Record<string, unknown>)
        : {};
    return {
      profile: providerResponse(2),
      target: {
        ...targetResponse(2),
        role_affinities: Array.isArray(request.intended_roles)
          ? request.intended_roles
          : [],
        task_family_affinities: Array.isArray(
          request.task_family_affinities,
        )
          ? request.task_family_affinities
          : [],
      },
      privacy_acknowledgement_digest: digestB,
      material_binding_digest: digestC,
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
        }),
      ),
    );

    await expect(getLanScan(scanId)).rejects.toThrow("lan_response_invalid");
  });

  it("serializes import and review authority without camel-case leakage", async () => {
    const requests: CapturedRequest[] = [];
    vi.stubGlobal("fetch", captureFetch(requests));
    await importLanObservation({
      scanId,
      endpointBindingDigest: digestA,
      expectedTerminalReceiptDigest: digestB,
      expectedObservationDigest: digestC,
      expectedProfileRevision: 0,
      expectedTargetRevisions: [{ resourceId: targetId, revision: 0 }],
      replacement: null,
    });
    await reviewLanTarget({
      targetId,
      expectedProfileRevision: 1,
      expectedTargetRevision: 1,
      expectedTerminalReceiptDigest: digestA,
      expectedObservationDigest: digestB,
      expectedEndpointFingerprint: digestC,
      expectedMaterialBindingDigest: digestA,
      expectedReviewDigest: digestB,
      expectedStaleReasons: ["freshness_expired"],
      trustClass: "operator_confirmed",
      intendedRoles: ["worker"],
      taskFamilyAffinities: ["coding"],
      privacyAcknowledged: true,
      enabled: false,
    });

    expect(requests.map(({ path, body }) => ({ path, body }))).toEqual([
      {
        path: "/api/routing/lan/import",
        body: {
          scan_id: scanId,
          endpoint_binding_digest: digestA,
          expected_terminal_receipt_digest: digestB,
          expected_observation_digest: digestC,
          expected_profile_revision: 0,
          expected_target_revisions: [{ resource_id: targetId, revision: 0 }],
          replacement: null,
        },
      },
      {
        path: `/api/routing/lan/targets/${targetId}/review`,
        body: {
          expected_profile_revision: 1,
          expected_target_revision: 1,
          expected_terminal_receipt_digest: digestA,
          expected_observation_digest: digestB,
          expected_endpoint_fingerprint: digestC,
          expected_material_binding_digest: digestA,
          expected_review_digest: digestB,
          expected_stale_reasons: ["freshness_expired"],
          trust_class: "operator_confirmed",
          intended_roles: ["worker"],
          task_family_affinities: ["coding"],
          privacy_acknowledged: true,
          enabled: false,
        },
      },
    ]);
  });

  it("uses backend code-point ordering for Unicode review affinities", async () => {
    const requests: CapturedRequest[] = [];
    vi.stubGlobal("fetch", captureFetch(requests));

    const result = await reviewLanTarget({
      targetId,
      expectedProfileRevision: 1,
      expectedTargetRevision: 1,
      expectedTerminalReceiptDigest: digestA,
      expectedObservationDigest: digestB,
      expectedEndpointFingerprint: digestC,
      expectedMaterialBindingDigest: digestA,
      expectedReviewDigest: digestB,
      expectedStaleReasons: [],
      trustClass: "operator_confirmed",
      intendedRoles: ["Ａ", "😀"],
      taskFamilyAffinities: [" family "],
      privacyAcknowledged: true,
      enabled: false,
    });

    expect(requests[0]?.body).toMatchObject({
      intended_roles: ["Ａ", "😀"],
      task_family_affinities: [" family "],
    });
    expect(result.target.role_affinities).toEqual(["Ａ", "😀"]);
    expect(result.target.task_family_affinities).toEqual([" family "]);
  });

  it.each([
    ["more than 64 UTF-8 bytes", "😀".repeat(17)],
    ["non-NFC text", "e\u0301"],
    ["a Unicode control category", "worker\u0000"],
  ])("rejects review affinities containing %s before fetch", async (_label, affinity) => {
    const fetchMock = captureFetch([]);
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      reviewLanTarget({
        targetId,
        expectedProfileRevision: 1,
        expectedTargetRevision: 1,
        expectedTerminalReceiptDigest: digestA,
        expectedObservationDigest: digestB,
        expectedEndpointFingerprint: digestC,
        expectedMaterialBindingDigest: digestA,
        expectedReviewDigest: digestB,
        expectedStaleReasons: [],
        trustClass: "operator_confirmed",
        intendedRoles: [affinity],
        taskFamilyAffinities: [],
        privacyAcknowledged: true,
        enabled: false,
      }),
    ).rejects.toThrow("lan_request_invalid");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("does not invent an eight-item cap for exact import CAS sets", async () => {
    const requests: CapturedRequest[] = [];
    vi.stubGlobal("fetch", captureFetch(requests));
    const expectedTargetRevisions = Array.from({ length: 9 }, (_, index) => ({
      resourceId: `lan-target-${index.toString(16).padStart(64, "0")}`,
      revision: 0,
    }));
    const expectedMaterialBindingDigests = Array.from(
      { length: 9 },
      (_, index) => `sha256:${index.toString(16).padStart(64, "0")}`,
    );

    await importLanObservation({
      scanId,
      endpointBindingDigest: digestA,
      expectedTerminalReceiptDigest: digestB,
      expectedObservationDigest: digestC,
      expectedProfileRevision: 0,
      expectedTargetRevisions,
      replacement: {
        providerProfileId: profileId,
        expectedProfileRevision: 1,
        expectedEndpointFingerprint: digestA,
        expectedMaterialBindingDigests,
      },
    });

    expect(requests).toHaveLength(1);
    expect(requests[0]?.body).toMatchObject({
      expected_target_revisions: expectedTargetRevisions.map((item) => ({
        resource_id: item.resourceId,
        revision: item.revision,
      })),
      replacement: {
        expected_material_binding_digests: expectedMaterialBindingDigests,
      },
    });
  });

  it("accepts the complete accumulated replacement family in server order", async () => {
    const replacedProfileId = `lan-provider-${"3".repeat(64)}`;
    const currentTargetId = `lan-target-${"f".repeat(64)}`;
    const replacedTargetIds = Array.from(
      { length: 64 },
      (_, index) => `lan-target-${index.toString(16).padStart(64, "0")}`,
    );
    const affectedTargetIds = [currentTargetId, ...replacedTargetIds];
    const targets = affectedTargetIds.map((resourceId, index) => ({
      ...targetResponse(),
      target_id: resourceId,
      provider_profile_id: index === 0 ? profileId : replacedProfileId,
      model: index === 0 ? "current" : `replaced-${index}`,
    }));
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>().mockResolvedValue(
        jsonResponse({
          profile: providerResponse(),
          targets,
          observation_digest: digestC,
          endpoint_fingerprint: digestA,
          outage_observed: false,
          affected_target_ids: affectedTargetIds,
          invalidated_binding_digests: [digestA, digestB],
          stale_reasons_by_target: [
            { target_id: currentTargetId, reasons: ["catalog_changed"] },
            { target_id: replacedTargetIds[0], reasons: ["catalog_changed"] },
          ],
        }),
      ),
    );

    const result = await importLanObservation({
      scanId,
      endpointBindingDigest: digestA,
      expectedTerminalReceiptDigest: digestB,
      expectedObservationDigest: digestC,
      expectedProfileRevision: 1,
      expectedTargetRevisions: [...affectedTargetIds]
        .sort()
        .map((resourceId) => ({ resourceId, revision: 1 })),
      replacement: {
        providerProfileId: replacedProfileId,
        expectedProfileRevision: 1,
        expectedEndpointFingerprint: digestA,
        expectedMaterialBindingDigests: [digestA, digestB],
      },
    });

    expect(result.targets).toHaveLength(65);
    expect(result.targets.map((item) => item.target_id)).toEqual(
      affectedTargetIds,
    );
    expect(result.affected_target_ids).toEqual(affectedTargetIds);
    expect(result.stale_reasons_by_target.map((item) => item.target_id)).toEqual(
      [currentTargetId, replacedTargetIds[0]],
    );
  });

  it("rejects an import result whose targets disagree with its affected set", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>().mockResolvedValue(
        jsonResponse({
          profile: providerResponse(),
          targets: [targetResponse()],
          observation_digest: digestC,
          endpoint_fingerprint: digestA,
          outage_observed: false,
          affected_target_ids: [],
          invalidated_binding_digests: [],
          stale_reasons_by_target: [],
        }),
      ),
    );

    await expect(
      importLanObservation({
        scanId,
        endpointBindingDigest: digestA,
        expectedTerminalReceiptDigest: digestB,
        expectedObservationDigest: digestC,
        expectedProfileRevision: 0,
        expectedTargetRevisions: [],
        replacement: null,
      }),
    ).rejects.toThrow("lan_response_invalid");
  });

  it("rejects import results without the profile authority required by their effect", async () => {
    const replacedProfileId = `lan-provider-${"3".repeat(64)}`;
    const replacedTarget = {
      ...targetResponse(),
      provider_profile_id: replacedProfileId,
    };
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        jsonResponse({
          profile: null,
          targets: [],
          observation_digest: digestC,
          endpoint_fingerprint: null,
          outage_observed: false,
          affected_target_ids: [],
          invalidated_binding_digests: [],
          stale_reasons_by_target: [],
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          profile: null,
          targets: [replacedTarget],
          observation_digest: digestC,
          endpoint_fingerprint: null,
          outage_observed: true,
          affected_target_ids: [targetId],
          invalidated_binding_digests: [],
          stale_reasons_by_target: [],
        }),
      );
    vi.stubGlobal("fetch", fetchMock);
    const common: Omit<
      Parameters<typeof importLanObservation>[0],
      "replacement"
    > = {
      scanId,
      endpointBindingDigest: digestA,
      expectedTerminalReceiptDigest: digestB,
      expectedObservationDigest: digestC,
      expectedProfileRevision: 0,
      expectedTargetRevisions: [],
    };

    await expect(
      importLanObservation({ ...common, replacement: null }),
    ).rejects.toThrow("lan_response_invalid");
    await expect(
      importLanObservation({
        ...common,
        replacement: {
          providerProfileId: replacedProfileId,
          expectedProfileRevision: 1,
          expectedEndpointFingerprint: digestA,
          expectedMaterialBindingDigests: [digestA],
        },
      }),
    ).rejects.toThrow("lan_response_invalid");
  });

  it("rejects malformed import and review mutation results", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>().mockImplementation(async () =>
        jsonResponse({ profile: null, targets: "not-an-array" }),
      ),
    );

    await expect(
      importLanObservation({
        scanId,
        endpointBindingDigest: digestA,
        expectedTerminalReceiptDigest: digestB,
        expectedObservationDigest: digestC,
        expectedProfileRevision: 0,
        expectedTargetRevisions: [],
        replacement: null,
      }),
    ).rejects.toThrow("lan_response_invalid");
    await expect(
      reviewLanTarget({
        targetId,
        expectedProfileRevision: 1,
        expectedTargetRevision: 1,
        expectedTerminalReceiptDigest: digestA,
        expectedObservationDigest: digestB,
        expectedEndpointFingerprint: digestC,
        expectedMaterialBindingDigest: digestA,
        expectedReviewDigest: digestB,
        expectedStaleReasons: [],
        trustClass: "operator_confirmed",
        intendedRoles: [],
        taskFamilyAffinities: [],
        privacyAcknowledged: true,
        enabled: false,
      }),
    ).rejects.toThrow("lan_response_invalid");
  });

  it("rejects malformed routing mutation inputs with fixed errors before fetch", async () => {
    const fetchMock = captureFetch([]);
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      importLanObservation({
        scanId,
        endpointBindingDigest: digestA,
        expectedTerminalReceiptDigest: digestB,
        expectedObservationDigest: digestC,
        expectedProfileRevision: 0,
        expectedTargetRevisions: null,
        replacement: null,
      } as unknown as Parameters<typeof importLanObservation>[0]),
    ).rejects.toThrow("lan_request_invalid");
    await expect(
      reviewLanTarget({
        targetId,
        expectedProfileRevision: 1,
        expectedTargetRevision: 1,
        expectedTerminalReceiptDigest: digestA,
        expectedObservationDigest: digestB,
        expectedEndpointFingerprint: digestC,
        expectedMaterialBindingDigest: digestA,
        expectedReviewDigest: digestB,
        expectedStaleReasons: null,
        trustClass: "operator_confirmed",
        intendedRoles: [],
        taskFamilyAffinities: [],
        privacyAcknowledged: true,
        enabled: false,
      } as unknown as Parameters<typeof reviewLanTarget>[0]),
    ).rejects.toThrow("lan_request_invalid");
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
