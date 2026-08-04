import { describe, expect, it } from "vitest";
import {
  DESKTOP_CREDENTIAL_IPC_CHANNELS,
  desktopConnectionSchema,
  desktopCredentialIntentSchema,
  desktopCredentialProviderIdSchema,
  desktopCredentialResultSchema,
  desktopErrorCodeSchema,
  desktopLifecycleStateSchema,
  desktopRecoveryReasonSchema,
  desktopRecoveryActionResultSchema,
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
    expect(desktopRecoveryReasonSchema.options).toContain(
      "state_corrupt"
    );
    expect(desktopRecoveryReasonSchema.options).toContain(
      "memvid_reopen_failed"
    );
  });

  it("projects bounded retry rejection without authority or free text", () => {
    expect(
      desktopRecoveryActionResultSchema.parse({
        accepted: false,
        reason: "retry_rate_limited"
      })
    ).toEqual({
      accepted: false,
      reason: "retry_rate_limited"
    });
    expect(() =>
      desktopRecoveryActionResultSchema.parse({
        accepted: false,
        reason: "retry_rate_limited",
        detail: "must not cross"
      })
    ).toThrow();
  });
});

describe("Desktop credential contracts", () => {
  const providerIds = [
    "openai",
    "openrouter",
    "deepseek",
    "kimi",
    "ollama-cloud",
    "anthropic",
    "grok",
    "gemini"
  ] as const;

  it("owns the exact provider and purpose allowlist without normalization", () => {
    expect(desktopCredentialProviderIdSchema.options).toEqual(
      providerIds
    );
    for (const providerId of providerIds) {
      expect(
        desktopCredentialIntentSchema.parse({
          providerId,
          purpose: "provider_api_key"
        })
      ).toEqual({ providerId, purpose: "provider_api_key" });
    }
    for (const providerId of [
      "mock",
      "ollama",
      "lm-studio",
      "codex-cli",
      "openai-compatible",
      "OPENAI",
      " openai",
      "openai ",
      "custom"
    ]) {
      expect(() =>
        desktopCredentialIntentSchema.parse({
          providerId,
          purpose: "provider_api_key"
        })
      ).toThrow();
    }
    expect(() =>
      desktopCredentialIntentSchema.parse({
        providerId: "openai",
        purpose: "custom-purpose"
      })
    ).toThrow();
  });

  it("projects only stored metadata or normal cancellation", () => {
    expect(
      desktopCredentialResultSchema.parse({
        status: "stored",
        secretRef: "secret://openai_api_key",
        validation: "unverified",
        fingerprint: "sha256:0123456789ab"
      })
    ).toEqual({
      status: "stored",
      secretRef: "secret://openai_api_key",
      validation: "unverified",
      fingerprint: "sha256:0123456789ab"
    });
    expect(
      desktopCredentialResultSchema.parse({ status: "cancelled" })
    ).toEqual({ status: "cancelled" });
    expect(() =>
      desktopCredentialResultSchema.parse({
        status: "stored",
        secretRef: "secret://openai_api_key",
        validation: "valid",
        fingerprint: "sha256:0123456789ab",
        value: "credential-must-not-cross"
      })
    ).toThrow();
  });

  it("uses only the three private credential channels and stable race errors", () => {
    expect(DESKTOP_CREDENTIAL_IPC_CHANNELS).toEqual({
      bootstrap: "kestrel:credential:bootstrap",
      submit: "kestrel:credential:submit",
      cancel: "kestrel:credential:cancel"
    });
    expect(desktopErrorCodeSchema.options).toEqual([
      "invalid_desktop_request",
      "invalid_desktop_response",
      "desktop_sender_untrusted",
      "desktop_feature_unavailable",
      "desktop_operation_failed",
      "desktop_operation_in_progress",
      "desktop_operation_ambiguous"
    ]);
  });
});
