import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { LanTargetReviewPreview } from "./types";
import { LanTargetReview } from "./LanTargetReview";

const digestA = `sha256:${"a".repeat(64)}`;
const digestB = `sha256:${"b".repeat(64)}`;
const digestC = `sha256:${"c".repeat(64)}`;
const profileId = `lan-provider-${"1".repeat(64)}`;
const targetId = `lan-target-${"2".repeat(64)}`;

function reviewPreview(staleReasons: string[] = []): LanTargetReviewPreview {
  return {
    options: {
      target_id: targetId,
      intended_roles: ["worker"],
      task_family_affinities: ["coding"],
      enabled: true,
    },
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
      expected_stale_reasons: staleReasons as LanTargetReviewPreview["authority"]["expected_stale_reasons"],
      trust_class: "operator_confirmed",
      privacy_acknowledgement_digest: digestB,
      review_digest: digestC,
      reviewed_material_binding_digest: digestA,
      reviewed_runtime_interface_binding_digest: null,
    },
    profile: {
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
      revision: 1,
      created_at: "2026-08-01T12:00:00Z",
      updated_at: "2026-08-01T12:00:01Z",
    },
    target: {
      target_id: targetId,
      provider_profile_id: profileId,
      provider: "ollama",
      model: "llama3.2",
      enabled: false,
      locality: "local",
      trust_class: "unreviewed",
      capability_tags: ["generation"],
      role_affinities: ["worker"],
      task_family_affinities: ["coding"],
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
      revision: 1,
      created_at: "2026-08-01T12:00:00Z",
      updated_at: "2026-08-01T12:00:01Z",
    },
    requires_privacy_acknowledgement: true,
    requires_confirmation: true,
  };
}

afterEach(() => {
  cleanup();
});

describe("LanTargetReview", () => {
  it("requires privacy acknowledgement before enabling a LAN target", () => {
    const onConfirm = vi.fn();
    render(
      <LanTargetReview
        preview={reviewPreview()}
        busy={false}
        onConfirm={onConfirm}
        onRescan={() => undefined}
        onCancel={() => undefined}
      />,
    );

    const enableButton = screen.getByRole("button", { name: "Enable target" });
    expect(enableButton).toBeDisabled();
    expect(
      screen.getByText(/prompts and code leave this computer/i),
    ).toBeVisible();
    expect(screen.getByText("llama3.2")).toBeVisible();

    fireEvent.click(
      screen.getByRole("checkbox", {
        name: /prompts and code leave this computer/i,
      }),
    );
    expect(enableButton).toBeEnabled();
    fireEvent.click(enableButton);

    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(onConfirm).toHaveBeenCalledWith({
      targetId,
      intendedRoles: ["worker"],
      taskFamilyAffinities: ["coding"],
      enabled: true,
      previewDigest: digestC,
      privacyAcknowledged: true,
      confirmed: true,
    });
  });

  it("disables enable and shows the exact drift reason for a stale target", () => {
    const onConfirm = vi.fn();
    const onRescan = vi.fn();
    render(
      <LanTargetReview
        preview={reviewPreview(["catalog_changed"])}
        busy={false}
        onConfirm={onConfirm}
        onRescan={onRescan}
        onCancel={() => undefined}
      />,
    );

    expect(screen.getByRole("button", { name: "Enable target" }))
      .toBeDisabled();
    expect(screen.getByText("catalog_changed")).toBeVisible();
    expect(screen.getByText(/stale/i)).toBeVisible();

    fireEvent.click(
      screen.getByRole("checkbox", {
        name: /prompts and code leave this computer/i,
      }),
    );
    expect(screen.getByRole("button", { name: "Enable target" }))
      .toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "Re-scan" }));
    expect(onRescan).toHaveBeenCalledTimes(1);
    expect(onConfirm).not.toHaveBeenCalled();
  });
});
