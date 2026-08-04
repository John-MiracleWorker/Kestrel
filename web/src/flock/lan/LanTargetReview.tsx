import { useState } from "react";
import { StatusBadge } from "../../components";
import type {
  LanTargetReviewConfirmation,
  LanTargetReviewPreview,
} from "./types";

export function LanTargetReview({
  preview,
  busy,
  onConfirm,
  onRescan,
  onCancel,
}: {
  preview: LanTargetReviewPreview;
  busy: boolean;
  onConfirm: (confirmation: LanTargetReviewConfirmation) => void;
  onRescan: () => void;
  onCancel: () => void;
}) {
  const [acknowledged, setAcknowledged] = useState(false);
  const staleReasons = preview.authority.expected_stale_reasons;
  const stale = staleReasons.length > 0;
  const { options } = preview;

  function confirm() {
    onConfirm({
      targetId: options.target_id,
      intendedRoles: options.intended_roles,
      taskFamilyAffinities: options.task_family_affinities,
      enabled: options.enabled,
      previewDigest: preview.preview_digest,
      privacyAcknowledged: true,
      confirmed: true,
    });
  }

  return (
    <div className="lan-target-review">
      <header className="lan-target-review-head">
        <strong>{preview.target.model}</strong>
        <StatusBadge value={preview.target.enabled ? "enabled" : "disabled"} />
        <span>trust: {preview.authority.trust_class}</span>
      </header>
      <ul className="lan-target-review-facts">
        <li>Provider: {preview.profile.display_name}</li>
        <li>
          Intended roles:{" "}
          {options.intended_roles.length > 0
            ? options.intended_roles.join(", ")
            : "none"}
        </li>
        <li>
          Task-family affinities:{" "}
          {options.task_family_affinities.length > 0
            ? options.task_family_affinities.join(", ")
            : "none"}
        </li>
        <li>Evidence expires: {preview.evidence_expires_at}</li>
      </ul>
      {stale ? (
        <div className="lan-target-stale" role="alert">
          <p>
            Stale target — the discovered evidence drifted, so this target
            cannot be enabled:
          </p>
          <ul>
            {staleReasons.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
          <button type="button" onClick={onRescan}>
            Re-scan
          </button>
        </div>
      ) : null}
      <label className="check-row lan-privacy-ack">
        <input
          type="checkbox"
          checked={acknowledged}
          onChange={(event) => setAcknowledged(event.target.checked)}
        />
        <span>
          I understand that prompts and code leave this computer when this LAN
          target is enabled.
        </span>
      </label>
      <div className="lan-target-review-actions">
        <button
          type="button"
          onClick={confirm}
          disabled={busy || !acknowledged || stale}
        >
          {options.enabled ? "Enable target" : "Confirm review"}
        </button>
        <button type="button" onClick={onCancel} disabled={busy}>
          Cancel
        </button>
      </div>
    </div>
  );
}
