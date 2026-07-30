import { describe, expect, it } from "vitest";
import {
  desktopConnectionSchema,
  desktopLifecycleStateSchema,
  desktopRecoveryReasonSchema,
  desktopRuntimeMarkerSchema,
  desktopUpdateStatusSchema
} from "./contracts";

describe("desktopConnectionSchema", () => {
  it("rejects a token-bearing renderer payload", () => {
    expect(() =>
      desktopConnectionSchema.parse({
        schema: "kestrel.desktop.connection.v1",
        state: "ready",
        generation: 1,
        baseUrl: "http://127.0.0.1:43123/",
        profileId: "default",
        sidecarVersion: "0.5.0",
        recovery: null,
        apiToken: "must-never-cross"
      })
    ).toThrow();
  });

  it("requires non-ready projections to omit active runtime authority", () => {
    expect(
      desktopConnectionSchema.parse({
        schema: "kestrel.desktop.connection.v1",
        state: "recovery",
        generation: null,
        baseUrl: null,
        profileId: null,
        sidecarVersion: null,
        recovery: { reason: "sidecar_unavailable" }
      })
    ).toEqual({
      schema: "kestrel.desktop.connection.v1",
      state: "recovery",
      generation: null,
      baseUrl: null,
      profileId: null,
      sidecarVersion: null,
      recovery: { reason: "sidecar_unavailable" }
    });
    expect(() =>
      desktopConnectionSchema.parse({
        schema: "kestrel.desktop.connection.v1",
        state: "starting",
        generation: 4,
        baseUrl: "http://127.0.0.1:43123/",
        profileId: null,
        sidecarVersion: null,
        recovery: null
      })
    ).toThrow();
  });

  it("accepts only exact metadata-only runtime and update projections", () => {
    expect(
      desktopRuntimeMarkerSchema.parse({
        schema: "kestrel.desktop.runtime.v1",
        baseUrl: "http://127.0.0.1:43123/",
        generation: 1
      })
    ).toEqual({
      schema: "kestrel.desktop.runtime.v1",
      baseUrl: "http://127.0.0.1:43123/",
      generation: 1
    });
    expect(() =>
      desktopRuntimeMarkerSchema.parse({
        schema: "kestrel.desktop.runtime.v1",
        baseUrl: "http://127.0.0.1:43123/",
        generation: 1,
        apiToken: "must-not-cross"
      })
    ).toThrow();
    expect(() =>
      desktopRuntimeMarkerSchema.parse({
        schema: "kestrel.desktop.runtime.v1",
        baseUrl: "http://[::1]:43123/",
        generation: 1
      })
    ).toThrow();
    expect(
      desktopUpdateStatusSchema.parse({
        schema: "kestrel.desktop.update.v1",
        state: "unavailable",
        reason: "not_configured"
      })
    ).toEqual({
      schema: "kestrel.desktop.update.v1",
      state: "unavailable",
      reason: "not_configured"
    });
  });

  it("represents every supervisor transition without exposing authority", () => {
    expect(
      ["verifying", "starting", "ready", "stopping", "recovery"].map(
        (state) => desktopLifecycleStateSchema.parse(state)
      )
    ).toEqual([
      "verifying",
      "starting",
      "ready",
      "stopping",
      "recovery"
    ]);
    expect(desktopLifecycleStateSchema.options).toEqual([
      "verifying",
      "starting",
      "ready",
      "stopping",
      "recovery"
    ]);
    expect(
      desktopRecoveryReasonSchema.parse("reconciliation_required")
    ).toBe("reconciliation_required");
  });
});
