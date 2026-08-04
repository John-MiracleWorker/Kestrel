import "@testing-library/jest-dom/vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { LanDiscoveryPanel } from "./LanDiscoveryPanel";

const scanId = `lan_${"1".repeat(32)}`;
const digestA = `sha256:${"a".repeat(64)}`;
const digestB = `sha256:${"b".repeat(64)}`;
const digestC = `sha256:${"c".repeat(64)}`;
const digestE = `sha256:${"e".repeat(64)}`;
const interfaceId = `sha256:${"d".repeat(64)}`;

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

const manualLimits = {
  mode: "manual",
  exact_port: 11_434,
  max_active_hosts: 1,
  max_scan_concurrency: 1,
  tcp_connect_timeout_seconds: 0.75,
  http_probe_timeout_seconds: 2,
  total_scan_deadline_seconds: 45,
  max_probe_response_bytes: 262_144,
  max_discovered_models: 8,
  mdns_enabled: false,
} as const;

function interfaceResponse() {
  return {
    interface_id: interfaceId,
    display_name: "Wi-Fi",
    addresses: ["192.168.1.8"],
  };
}

function scopePreviewResponse() {
  return {
    interface_id: interfaceId,
    network: "192.168.1.0/24",
    limits: automaticLimits,
    active_host_count: 254,
    passive_or_manual_only: false,
    port_count: 1_016,
    mdns_status: "available",
    server_version: "0.5.0",
    contract_version: "kestrel.lan.v1",
    preview_digest: digestA,
    issued_at: "2026-08-01T12:00:00Z",
    expires_at: "2026-08-01T12:05:00Z",
  };
}

function scanResponse(status: string, revision: number, limits: object) {
  const running = status === "running";
  return {
    scan_id: scanId,
    status,
    revision,
    confirmed_interface_id: interfaceId,
    network: "192.168.1.0/24",
    limits,
    limits_digest: digestB,
    preview_digest: digestA,
    created_at: "2026-08-01T12:00:00Z",
    updated_at: "2026-08-01T12:00:01Z",
    started_at: running || status === "completed" ? "2026-08-01T12:00:01Z" : null,
    finished_at: status === "completed" ? "2026-08-01T12:00:02Z" : null,
    cancel_reason: null,
    terminal_reason: status === "completed" ? "scan_complete" : null,
    candidate_count: running ? null : 1,
    error_count: running ? null : 0,
    timeout_count: running ? null : 0,
    terminal_receipt_digest: status === "completed" ? digestB : null,
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
    created_at: "2026-08-01T12:00:01Z",
  };
}

function scanDetailResponse(status: string, withObservation: boolean) {
  return {
    ...scanResponse(status, status === "completed" ? 3 : 2, automaticLimits),
    observations: withObservation ? [durableObservation()] : [],
    observation_total_count: withObservation ? 1 : 0,
    observations_truncated: false,
    observation_next_cursor: null,
  };
}

function manualPreviewResponse() {
  return {
    schema: "kestrel.lan.manual-preview.v1",
    interface_id: interfaceId,
    port: 11_434,
    resolved_addresses: ["192.168.50.8"],
    preview_digest: digestE,
    issued_at: "2026-08-01T12:00:00Z",
    expires_at: "2026-08-01T12:05:00Z",
    server_version: "0.5.0",
    contract_version: "kestrel.lan.v1",
    requires_confirmation: true,
  };
}

function manualScanResponse() {
  return {
    ...scanResponse("running", 1, manualLimits),
    network: "manual:192.168.50.8",
    preview_digest: digestE,
  };
}

type CapturedRequest = { path: string; method: string; body: unknown };

let requests: CapturedRequest[];
let detailPayload: () => object;

function jsonResponse(payload: unknown = {}): Response {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => {
  requests = [];
  detailPayload = () => scanDetailResponse("running", false);
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = typeof input === "string" ? input : input.toString();
      const method = init?.method ?? "GET";
      const body =
        typeof init?.body === "string" ? JSON.parse(init.body) : null;
      requests.push({ path, method, body });

      if (path === "/api/routing/lan/interfaces") {
        return jsonResponse([interfaceResponse()]);
      }
      if (path === "/api/routing/lan/preview") {
        return jsonResponse(scopePreviewResponse());
      }
      if (path === "/api/routing/lan/scans" && method === "POST") {
        return jsonResponse(scanResponse("draft", 1, automaticLimits));
      }
      if (path === `/api/routing/lan/scans/${scanId}/start`) {
        return jsonResponse(scanResponse("running", 2, automaticLimits));
      }
      if (path === `/api/routing/lan/scans/${scanId}`) {
        return jsonResponse(detailPayload());
      }
      if (path === `/api/routing/lan/scans/${scanId}/events`) {
        return jsonResponse({ detail: "stream unavailable in test" });
      }
      if (path === "/api/routing/lan/manual-probe" && method === "POST") {
        if (body?.mode === "preview") {
          return jsonResponse(manualPreviewResponse());
        }
        return jsonResponse(manualScanResponse());
      }
      return new Response(
        JSON.stringify({ detail: `Unhandled ${method} ${path}` }),
        { status: 404 },
      );
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function renderPanel() {
  return render(
    <LanDiscoveryPanel onError={() => undefined} onNotice={() => undefined} />,
  );
}

async function openScopeSelection() {
  fireEvent.click(screen.getByRole("button", { name: "Scan network" }));
  await screen.findByRole("radio", { name: /Wi-Fi/ });
}

async function previewScope() {
  await openScopeSelection();
  fireEvent.click(screen.getByRole("radio", { name: /Wi-Fi/ }));
  fireEvent.click(screen.getByRole("button", { name: "Preview scope" }));
  await screen.findByText("Up to 254 hosts × 4 known model ports");
}

describe("LanDiscoveryPanel", () => {
  it("does not call discovery endpoints merely by opening the panel", async () => {
    renderPanel();

    expect(
      screen.getByRole("heading", { name: "LAN model discovery" }),
    ).toBeVisible();
    expect(screen.getByText(/no LAN scan has run/i)).toBeVisible();
    expect(
      screen.getByRole("button", { name: "Scan network" }),
    ).toBeVisible();
    await waitFor(() => {
      expect(
        requests.some((request) =>
          request.path.startsWith("/api/routing/lan/"),
        ),
      ).toBe(false);
    });
  });

  it("shows the exact scope before scanning and locks it while running", async () => {
    renderPanel();
    await previewScope();

    expect(screen.getByLabelText("Network scope")).toHaveValue(
      "192.168.1.0/24",
    );
    fireEvent.click(screen.getByRole("button", { name: "Confirm and scan" }));

    await waitFor(() => {
      const create = requests.find(
        (request) =>
          request.path === "/api/routing/lan/scans" &&
          request.method === "POST",
      );
      expect(create?.body).toEqual({
        preview_digest: digestA,
        expected_revision: 0,
        confirmed: true,
      });
    });
    await waitFor(() => {
      const start = requests.find(
        (request) =>
          request.path === `/api/routing/lan/scans/${scanId}/start`,
      );
      expect(start?.body).toEqual({
        expected_revision: 1,
        preview_digest: digestA,
        confirmed: true,
      });
    });
    expect(await screen.findByText("Scan status: running")).toBeVisible();
    expect(screen.getByLabelText("Network scope")).toBeDisabled();
    expect(screen.getByRole("radio", { name: /Wi-Fi/ })).toBeDisabled();
  });

  it("shows discovered servers disabled with privacy warning and evidence", async () => {
    detailPayload = () => scanDetailResponse("completed", true);
    renderPanel();
    await previewScope();
    fireEvent.click(screen.getByRole("button", { name: "Confirm and scan" }));

    expect(await screen.findByText("Scan status: completed")).toBeVisible();
    expect(await screen.findByText("192.168.50.8:11434")).toBeVisible();
    expect(screen.getByText("llama3.2")).toBeVisible();
    expect(screen.getAllByText(/ollama_compatible/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/plain HTTP/i).length).toBeGreaterThan(0);
    expect(screen.getByText(/prompts and code leave this computer/i))
      .toBeVisible();
    expect(screen.getAllByText("disabled").length).toBeGreaterThan(0);
    expect(screen.getByText(/not enabled/i)).toBeVisible();

    fireEvent.click(screen.getByText("Evidence"));
    expect(screen.getAllByText(digestC).length).toBeGreaterThan(0);
    expect(screen.getByText(/observed_pass/)).toBeVisible();
    expect(screen.getAllByText("2026-08-01T12:00:01Z").length).toBeGreaterThan(0);
  });

  it("keeps manual endpoint entry separate and requires exact host, port, and privacy acknowledgement", async () => {
    renderPanel();

    const previewButton = screen.getByRole("button", {
      name: "Preview endpoint",
    });
    expect(previewButton).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "Load interfaces" }));
    const manualInterface = await screen.findByRole("combobox", {
      name: "Manual interface",
    });
    fireEvent.change(manualInterface, { target: { value: interfaceId } });
    fireEvent.change(screen.getByLabelText("Exact host"), {
      target: { value: "192.168.50.8" },
    });
    fireEvent.change(screen.getByLabelText("Exact port"), {
      target: { value: "11434" },
    });
    expect(previewButton).toBeEnabled();

    fireEvent.click(previewButton);
    await waitFor(() => {
      const preview = requests.find(
        (request) => request.path === "/api/routing/lan/manual-probe",
      );
      expect(preview?.body).toEqual({
        mode: "preview",
        interface_id: interfaceId,
        host: "192.168.50.8",
        port: 11_434,
      });
    });

    const probeButton = await screen.findByRole("button", {
      name: "Probe endpoint",
    });
    expect(probeButton).toBeDisabled();
    expect(
      screen.getByText(/prompts and code leave this computer/i),
    ).toBeVisible();

    fireEvent.click(
      screen.getByRole("checkbox", {
        name: /prompts and code leave this computer/i,
      }),
    );
    expect(probeButton).toBeEnabled();
    fireEvent.click(probeButton);

    await waitFor(() => {
      const confirm = requests.find(
        (request) =>
          request.path === "/api/routing/lan/manual-probe" &&
          (request.body as { mode?: string } | null)?.mode === "confirm",
      );
      expect(confirm?.body).toEqual({
        mode: "confirm",
        expected_revision: 0,
        preview_digest: digestE,
        selected_address: "192.168.50.8",
        confirmed: true,
        privacy_acknowledged: true,
      });
    });
  });
});
