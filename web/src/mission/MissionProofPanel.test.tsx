import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  MissionProofPanel,
  type MissionProof,
} from "./MissionProofPanel";

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function proof(
  overrides: Partial<MissionProof> = {},
): MissionProof {
  const section = (status: MissionProof["evidence"][string]["status"]) => ({
    status,
    detail: `${status} evidence detail`,
    evidence: {},
  });
  const sections: MissionProof["evidence"] = {
    binding: section("present"),
    contract: section("present"),
    roles: section("present"),
    routing: {
      status: "missing",
      detail: "no shadow observations for this run",
      evidence: {},
    },
    isolation: section("present"),
    change: section("present"),
    validation: section("present"),
    review: section("present"),
    risks: section("present"),
    approval: section("present"),
    shipping: section("present"),
    capsule: {
      status: "missing",
      detail: "no completed capsule marker for this run",
      evidence: {},
    },
    learning: section("present"),
  };
  const present = Object.entries(sections)
    .filter(([, value]) => value.status === "present")
    .map(([key]) => key);
  const missing = Object.entries(sections)
    .filter(([, value]) => value.status === "missing")
    .map(([key]) => key);
  return {
    schema: "kestrel.mission_proof.v1",
    run_id: "run_1",
    project_id: "project_1",
    generated_at: "2026-08-23T00:00:00Z",
    binding: { persisted: true, preflight_persisted: true },
    evidence: sections,
    summary: {
      present,
      missing,
      stale: [],
      mismatched: [],
      counts: {
        present: present.length,
        missing: missing.length,
        stale: 0,
        mismatched: 0,
      },
    },
    ...overrides,
  };
}

describe("MissionProofPanel", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("renders server-authored evidence statuses for every section", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse(proof())),
    );
    render(<MissionProofPanel runId="run_1" />);

    expect(
      await screen.findByRole("heading", { name: "Mission proof" }),
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByText("11/13 present")).toBeInTheDocument();
    });

    expect(screen.getByText("Binding")).toBeInTheDocument();
    expect(screen.getByText("Shipping")).toBeInTheDocument();
    expect(screen.getByText("Learning")).toBeInTheDocument();
    // Server says these two are missing; the UI must render missing exactly
    // as reported — it never infers presence from presentation.
    const missingSections = screen.getAllByText("missing");
    expect(missingSections.length).toBeGreaterThanOrEqual(2);
    expect(
      screen.getByText("no shadow observations for this run"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("no completed capsule marker for this run"),
    ).toBeInTheDocument();
  });

  it("surfaces stale and mismatched evidence explicitly", async () => {
    const base = proof();
    base.evidence.approval = {
      status: "stale",
      detail: "stale approval evidence",
      evidence: {},
    };
    base.evidence.capsule = {
      status: "mismatched",
      detail: "capsule marker does not report completion",
      evidence: { marker_status: "failed" },
    };
    base.summary.stale = ["approval"];
    base.summary.mismatched = ["capsule"];
    base.summary.counts = {
      present: 10,
      missing: 1,
      stale: 1,
      mismatched: 1,
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse(base)),
    );
    render(<MissionProofPanel runId="run_1" />);

    expect(
      await screen.findByText("Mismatched evidence"),
    ).toBeInTheDocument();
    expect(screen.getByText("Stale evidence")).toBeInTheDocument();
    expect(screen.getByText("capsule marker does not report completion")).toBeInTheDocument();
    expect(screen.getByText("stale approval evidence")).toBeInTheDocument();
  });

  it("renders the projection exactly as the server reports it", async () => {
    // Even a projection with NO present evidence renders that truthfully.
    const allMissing = proof();
    for (const key of Object.keys(allMissing.evidence)) {
      allMissing.evidence[key] = {
        status: "missing",
        detail: `${key} has no durable evidence`,
        evidence: {},
      };
    }
    allMissing.summary.present = [];
    allMissing.summary.missing = Object.keys(allMissing.evidence);
    allMissing.summary.counts = {
      present: 0,
      missing: Object.keys(allMissing.evidence).length,
      stale: 0,
      mismatched: 0,
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse(allMissing)),
    );
    render(<MissionProofPanel runId="run_1" />);

    await waitFor(() => {
      expect(screen.getByText("0/13 present")).toBeInTheDocument();
    });
    const missingRows = screen.getAllByText("missing");
    expect(missingRows.length).toBe(13);
  });

  it("shows an error state when the projection cannot be loaded", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ detail: "not_found" }, 404)),
    );
    render(<MissionProofPanel runId="run_1" />);

    expect(
      await screen.findByText("Mission proof unavailable"),
    ).toBeInTheDocument();
  });
});
