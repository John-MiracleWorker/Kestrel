/**
 * Qualification draft form (Adaptive Flock plan, Task 19).
 *
 * The owner edits the budget cap and thresholds before anything runs.  Money
 * stays decimal text end to end: the cap is forwarded verbatim to the preview
 * endpoint and never parsed into a JS float.
 */

import { useState } from "react";
import { Field } from "../../components";
import { CorpusReview } from "./CorpusReview";
import { previewQualification } from "./api";
import type {
  PreviewQualificationInput,
  QualificationPreview,
  QualificationThresholdsInput,
} from "./types";

export type QualificationDraftValues = Readonly<{
  input: PreviewQualificationInput;
  thresholds: QualificationThresholdsInput;
  attemptCeilingUsd: string;
}>;

const USD_TEXT = /^[0-9]{1,9}(\.[0-9]{1,6})?$/;

const DEFAULT_THRESHOLDS = {
  minExamplesPerScope: "5",
  minExamplesPerTarget: "3",
  confidenceThreshold: "0.7",
  utilityMargin: "0.08",
  costCoverageThreshold: "0.8",
  decayHalfLifeDays: "30",
  maxGuardrailViolations: "0",
  replayRuns: "20",
  replaySuccessesRequired: "20",
} as const;

export function QualificationDraft({
  fixture,
  busy = false,
  preview = previewQualification,
  onPreviewed,
  onError,
}: {
  fixture: PreviewQualificationInput;
  busy?: boolean;
  preview?: (input: PreviewQualificationInput) => Promise<QualificationPreview>;
  onPreviewed?: (
    preview: QualificationPreview,
    values: QualificationDraftValues,
  ) => void;
  onError?: (message: string) => void;
}) {
  const [cap, setCap] = useState(fixture.maximumSpendUsd ?? "50.00");
  const [ceiling, setCeiling] = useState("5.00");
  const [capError, setCapError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [thresholds, setThresholds] = useState({ ...DEFAULT_THRESHOLDS });

  function setThreshold(key: keyof typeof DEFAULT_THRESHOLDS) {
    return (event: { target: { value: string } }) => {
      setThresholds((current) => ({ ...current, [key]: event.target.value }));
    };
  }

  async function refreshPreview() {
    if (!USD_TEXT.test(cap)) {
      setCapError(
        "Enter the cap as decimal text (for example 35.00). It is forwarded verbatim and never rounded.",
      );
      return;
    }
    if (!USD_TEXT.test(ceiling)) {
      setCapError(
        "Enter the per-attempt ceiling as decimal text (for example 5.00).",
      );
      return;
    }
    setCapError(null);
    const values: QualificationDraftValues = {
      input: { ...fixture, maximumSpendUsd: cap },
      thresholds: parseThresholds(thresholds),
      attemptCeilingUsd: ceiling,
    };
    setRefreshing(true);
    try {
      const next = await preview(values.input);
      onPreviewed?.(next, values);
    } catch (value) {
      onError?.(value instanceof Error ? value.message : String(value));
    } finally {
      setRefreshing(false);
    }
  }

  return (
    <div className="qual-draft">
      <p>
        Nothing runs from this draft. A qualification run starts only after you
        refresh the preview, review the exact target matrix and corpus, and
        explicitly confirm the review.
      </p>
      <dl className="qual-draft-context">
        <div>
          <dt>Project</dt>
          <dd>{fixture.projectId}</dd>
        </div>
        <div>
          <dt>Task families</dt>
          <dd>{fixture.taskFamilies.join(", ")}</dd>
        </div>
        <div>
          <dt>Policy</dt>
          <dd>
            {fixture.policyId ?? "balanced"} (revision{" "}
            {fixture.policyRevision ?? 1})
          </dd>
        </div>
        <div>
          <dt>Default privacy class</dt>
          <dd>{fixture.defaultPrivacyClass ?? "approved_cloud"}</dd>
        </div>
      </dl>

      <Field
        label="Maximum provider spend"
        hint="Decimal text, forwarded verbatim. This becomes the immutable maximum once the run starts; afterwards the stop cap can only move down."
        error={capError ?? undefined}
      >
        <input
          value={cap}
          inputMode="decimal"
          disabled={busy || refreshing}
          aria-invalid={capError !== null}
          onChange={(event) => {
            setCap(event.target.value);
            setCapError(null);
          }}
        />
      </Field>
      <Field
        label="Per-attempt cost ceiling"
        hint="No single attempt may reserve more than this."
      >
        <input
          value={ceiling}
          inputMode="decimal"
          disabled={busy || refreshing}
          onChange={(event) => setCeiling(event.target.value)}
        />
      </Field>

      <fieldset className="qual-thresholds" disabled={busy || refreshing}>
        <legend>Qualification thresholds</legend>
        <Field label="Minimum examples per scope">
          <input
            value={thresholds.minExamplesPerScope}
            inputMode="numeric"
            onChange={setThreshold("minExamplesPerScope")}
          />
        </Field>
        <Field label="Minimum examples per target">
          <input
            value={thresholds.minExamplesPerTarget}
            inputMode="numeric"
            onChange={setThreshold("minExamplesPerTarget")}
          />
        </Field>
        <Field label="Confidence threshold">
          <input
            value={thresholds.confidenceThreshold}
            inputMode="decimal"
            onChange={setThreshold("confidenceThreshold")}
          />
        </Field>
        <Field label="Utility margin">
          <input
            value={thresholds.utilityMargin}
            inputMode="decimal"
            onChange={setThreshold("utilityMargin")}
          />
        </Field>
        <Field label="Cost coverage threshold">
          <input
            value={thresholds.costCoverageThreshold}
            inputMode="decimal"
            onChange={setThreshold("costCoverageThreshold")}
          />
        </Field>
        <Field label="Decay half-life (days)">
          <input
            value={thresholds.decayHalfLifeDays}
            inputMode="numeric"
            onChange={setThreshold("decayHalfLifeDays")}
          />
        </Field>
        <Field label="Maximum guardrail violations">
          <input
            value={thresholds.maxGuardrailViolations}
            inputMode="numeric"
            onChange={setThreshold("maxGuardrailViolations")}
          />
        </Field>
        <Field label="Replay runs">
          <input
            value={thresholds.replayRuns}
            inputMode="numeric"
            onChange={setThreshold("replayRuns")}
          />
        </Field>
        <Field label="Replay successes required">
          <input
            value={thresholds.replaySuccessesRequired}
            inputMode="numeric"
            onChange={setThreshold("replaySuccessesRequired")}
          />
        </Field>
      </fieldset>

      <CorpusReview corpus={fixture.corpus} />

      <button
        type="button"
        onClick={() => void refreshPreview()}
        disabled={busy || refreshing}
      >
        {refreshing ? "Refreshing preview…" : "Refresh preview"}
      </button>
    </div>
  );
}

function parseThresholds(
  thresholds: Record<keyof typeof DEFAULT_THRESHOLDS, string>,
): QualificationThresholdsInput {
  const integer = (value: string): number | undefined => {
    const parsed = Number(value);
    return Number.isSafeInteger(parsed) && parsed >= 0 ? parsed : undefined;
  };
  const ratio = (value: string): number | undefined => {
    const parsed = Number(value);
    return Number.isFinite(parsed) && parsed >= 0 ? parsed : undefined;
  };
  return {
    minExamplesPerScope: integer(thresholds.minExamplesPerScope),
    minExamplesPerTarget: integer(thresholds.minExamplesPerTarget),
    confidenceThreshold: ratio(thresholds.confidenceThreshold),
    utilityMargin: ratio(thresholds.utilityMargin),
    costCoverageThreshold: ratio(thresholds.costCoverageThreshold),
    decayHalfLifeDays: integer(thresholds.decayHalfLifeDays),
    maxGuardrailViolations: integer(thresholds.maxGuardrailViolations),
    replayRuns: integer(thresholds.replayRuns),
    replaySuccessesRequired: integer(thresholds.replaySuccessesRequired),
  };
}
