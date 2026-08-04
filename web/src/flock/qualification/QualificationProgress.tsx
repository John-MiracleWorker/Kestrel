/**
 * Running qualification view (Adaptive Flock plan, Task 19).
 *
 * Cap invariants:
 * - The maximum provider spend is immutable once the run starts and is shown
 *   separately from the current stop cap.
 * - The stop cap can only be lowered; there is no raise control anywhere.
 * - Unknown provider usage shows "cost unresolved", never "$0".
 * - Budget exhaustion stops new attempts but retains completed evidence.
 */

import { useState } from "react";
import { Field, StatusBadge } from "../../components";
import { qualificationActions } from "./api";
import type { QualificationEvent, QualificationRun } from "./types";
import type { QualificationRunConnection } from "./useQualificationRun";

const USD_TEXT = /^[0-9]{1,9}(\.[0-9]{1,6})?$/;

export function QualificationProgress({
  run,
  connection,
  lastEvent = null,
  busy = false,
  onPause,
  onResume,
  onCancel,
  onLowerCap,
}: {
  run: QualificationRun;
  connection?: QualificationRunConnection;
  lastEvent?: QualificationEvent | null;
  busy?: boolean;
  onPause?: () => void;
  onResume?: () => void;
  onCancel?: () => void;
  onLowerCap?: (cap: string) => void;
}) {
  const [capDraft, setCapDraft] = useState(run.caps.effective_stop_cap_usd);
  const [capError, setCapError] = useState<string | null>(null);

  const actions = qualificationActions(run);
  const budgetExhausted =
    lastEvent?.event_type === "budget_projection_overrun" ||
    run.blockers.some((blocker) => blocker.includes("budget"));
  const remainingMicros = Math.max(
    0,
    run.caps.effective_stop_cap_micros -
      run.spend.actual_spend_micros -
      run.spend.inflight_reserve_micros,
  );

  function submitLowerCap() {
    if (!USD_TEXT.test(capDraft)) {
      setCapError("Enter the stop cap as decimal text (for example 30.00).");
      return;
    }
    if (usdTextToMicros(capDraft) >= run.caps.effective_stop_cap_micros) {
      setCapError(
        "The stop cap can only be lowered after start; it can never be raised.",
      );
      return;
    }
    setCapError(null);
    onLowerCap?.(capDraft);
  }

  return (
    <div className="qual-progress">
      <div className="qual-progress-status">
        <StatusBadge value={run.status} />
        <span>Run {run.run_id}</span>
        <span>Revision {run.revision}</span>
        {connection !== undefined ? (
          <span>Live updates: {connection}</span>
        ) : null}
      </div>

      <ul className="qual-run-facts">
        <li>Maximum provider spend (immutable): ${run.caps.max_spend_usd}</li>
        <li>Per-attempt ceiling: ${run.caps.attempt_ceiling_usd}</li>
        <li>Actual spend: ${run.spend.actual_spend_usd}</li>
        <li>
          In-flight reserve: ${microsToUsdText(run.spend.inflight_reserve_micros)}
        </li>
        <li>Remaining before stop cap: ${microsToUsdText(remainingMicros)}</li>
      </ul>

      {run.spend.unresolved_reserve_micros > 0 ? (
        <p className="qual-cost-unresolved" role="status">
          Some provider usage has not been reported yet — cost unresolved. The
          unreported reserve stays blocked until providers report.
        </p>
      ) : null}

      {budgetExhausted ? (
        <p className="qual-budget-exhausted" role="alert">
          Budget limit reached: new attempts stopped; completed evidence
          retained.
        </p>
      ) : null}

      {run.blockers.length > 0 ? (
        <ul className="qual-run-blockers">
          {run.blockers.map((blocker) => (
            <li key={blocker}>
              <code>{blocker}</code>
            </li>
          ))}
        </ul>
      ) : null}

      <Field
        label="Current stop cap"
        hint="Lowerable at any time; raising is impossible after start. The immutable maximum above never changes."
        error={capError ?? undefined}
      >
        <input
          value={capDraft}
          inputMode="decimal"
          disabled={busy || !actions.includes("lower_cap")}
          onChange={(event) => {
            setCapDraft(event.target.value);
            setCapError(null);
          }}
        />
      </Field>
      <div className="qual-run-actions">
        {actions.includes("lower_cap") ? (
          <button
            type="button"
            onClick={submitLowerCap}
            disabled={busy}
          >
            Lower stop cap
          </button>
        ) : null}
        {actions.includes("pause") ? (
          <button type="button" onClick={onPause} disabled={busy}>
            Pause
          </button>
        ) : null}
        {actions.includes("resume") ? (
          <button type="button" onClick={onResume} disabled={busy}>
            Resume
          </button>
        ) : null}
        {actions.includes("cancel") ? (
          <button type="button" onClick={onCancel} disabled={busy}>
            Cancel run
          </button>
        ) : null}
      </div>

      <p className="muted">
        Partial evidence collected so far is retained even if the run is
        paused or cancelled.
      </p>
    </div>
  );
}

function usdTextToMicros(text: string): number {
  const [whole, fraction = ""] = text.split(".");
  return Number(whole) * 1_000_000 + Number((fraction + "000000").slice(0, 6));
}

/** Integer-exact micros → decimal text for display (no float money). */
function microsToUsdText(micros: number): string {
  const whole = Math.floor(micros / 1_000_000);
  const fraction = String(micros % 1_000_000).padStart(6, "0");
  const trimmed = fraction.replace(/0+$/, "");
  const cents = trimmed.length >= 2 ? trimmed : fraction.slice(0, 2);
  return `${whole}.${cents}`;
}
