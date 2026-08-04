/**
 * Bounded Flock qualification workspace (Adaptive Flock plan, Task 19).
 *
 * Owner flow: draft → explicit preview review → create + start → live
 * progress → per-scope results.  Opening the workspace never contacts the
 * qualification endpoints; the first request is the owner-initiated preview.
 */

import { ShieldCheck } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { Panel } from "../../components";
import {
  cancelQualification,
  createQualification,
  getQualificationReceipt,
  lowerQualificationCap,
  pauseQualification,
  previewQualification,
  resumeQualification,
  startQualification,
} from "./api";
import { CorpusReview } from "./CorpusReview";
import { QualificationDraft, type QualificationDraftValues } from "./QualificationDraft";
import { QualificationProgress } from "./QualificationProgress";
import { QualificationResults } from "./QualificationResults";
import { TargetMatrix } from "./TargetMatrix";
import {
  useQualificationRun,
  type UseQualificationRunOptions,
} from "./useQualificationRun";
import type {
  CreateQualificationInput,
  LowerQualificationCapInput,
  PreviewQualificationInput,
  QualificationLifecycleInput,
  QualificationPreview,
  QualificationReceipt,
  QualificationRun,
  QualificationScopeInput,
  QualificationScopePayload,
} from "./types";
import "./qualification.css";

export type QualificationWorkspaceClient = Readonly<{
  preview: (input: PreviewQualificationInput) => Promise<QualificationPreview>;
  create: (input: CreateQualificationInput) => Promise<QualificationRun>;
  start: (input: QualificationLifecycleInput) => Promise<QualificationRun>;
  pause: (input: QualificationLifecycleInput) => Promise<QualificationRun>;
  resume: (input: QualificationLifecycleInput) => Promise<QualificationRun>;
  cancel: (input: QualificationLifecycleInput) => Promise<QualificationRun>;
  lowerCap: (input: LowerQualificationCapInput) => Promise<QualificationRun>;
  getReceipt: (runId: string) => Promise<QualificationReceipt>;
}>;

const defaultClient: QualificationWorkspaceClient = {
  preview: previewQualification,
  create: createQualification,
  start: startQualification,
  pause: pauseQualification,
  resume: resumeQualification,
  cancel: cancelQualification,
  lowerCap: lowerQualificationCap,
  getReceipt: getQualificationReceipt,
};

const TERMINAL_STATUSES = new Set(["cancelled", "failed", "completed"]);

function defaultFixture(): PreviewQualificationInput {
  return {
    projectId: "project-1",
    taskFamilies: ["code_repair"],
    corpus: [
      {
        itemId: "sample-task-1",
        taskFamily: "code_repair",
        risk: "low",
        capabilities: ["generation"],
        taskContractDigest: "0".repeat(64),
        acceptancePlanDigest: "1".repeat(64),
        evidenceKind: "synthetic",
      },
    ],
    policyId: "balanced",
    policyRevision: 1,
    defaultPrivacyClass: "approved_cloud",
    maximumSpendUsd: "50.00",
  };
}

export function QualificationWorkspace({
  onError,
  onNotice,
  client = defaultClient,
  runOptions,
}: {
  onError: (message: string) => void;
  onNotice: (message: string) => void;
  client?: QualificationWorkspaceClient;
  runOptions?: UseQualificationRunOptions;
}) {
  const [fixture] = useState<PreviewQualificationInput>(defaultFixture);
  const [preview, setPreview] = useState<QualificationPreview | null>(null);
  const [draftValues, setDraftValues] = useState<QualificationDraftValues | null>(
    null,
  );
  const [reviewed, setReviewed] = useState(false);
  const [starting, setStarting] = useState(false);
  const [runIds, setRunIds] = useState<string[]>([]);

  const reportError = useCallback(
    (value: unknown) => {
      onError(value instanceof Error ? value.message : String(value));
    },
    [onError],
  );

  const handlePreviewed = useCallback(
    (next: QualificationPreview, values: QualificationDraftValues) => {
      setPreview(next);
      setDraftValues(values);
      setReviewed(false);
    },
    [],
  );

  async function createAndStart() {
    if (preview === null || draftValues === null || !reviewed) return;
    setStarting(true);
    try {
      const started: string[] = [];
      for (const scope of preview.scopes) {
        const created = await client.create({
          scope: scopeInputFromPayload(scope),
          corpus: draftValues.input.corpus,
          thresholds: draftValues.thresholds,
          targetSnapshot: {},
          priceSnapshot: {},
          policyPayload: {},
          learnedPayload: draftValues.input.learnedConfig ?? {},
          projectAuthority: draftValues.input.projectAuthority ?? {},
          maximumSpendUsd: draftValues.input.maximumSpendUsd ?? "50.00",
          attemptCeilingUsd: draftValues.attemptCeilingUsd,
        });
        const running = await client.start({
          runId: created.run_id,
          expectedRevision: created.revision,
        });
        started.push(running.run_id);
      }
      setRunIds(started);
      onNotice(
        `Qualification started for ${started.length} scope(s). The maximum spend is now immutable; the stop cap can only move down.`,
      );
    } catch (value) {
      reportError(value);
    } finally {
      setStarting(false);
    }
  }

  const startBlockers = Object.entries(preview?.start_blockers ?? {});
  const warnings = Object.entries(preview?.warnings ?? {});

  return (
    <section
      className="content-grid wide-left qual-workspace"
      aria-label="Qualification workspace"
    >
      <Panel
        title="Adaptive Flock qualification"
        icon={<ShieldCheck size={19} />}
      >
        {runIds.length === 0 ? (
          <>
            <QualificationDraft
              fixture={fixture}
              busy={starting}
              preview={client.preview}
              onPreviewed={handlePreviewed}
              onError={onError}
            />
            {preview !== null && draftValues !== null ? (
              <div className="qual-review">
                <TargetMatrix preview={preview} />
                <CorpusReview corpus={draftValues.input.corpus} />
                <div className="qual-review-budget">
                  <h3>Budget</h3>
                  <ul className="qual-run-facts">
                    <li>
                      Maximum provider spend: $
                      {preview.budget.maximum_spend_usd} (immutable after
                      start)
                    </li>
                    <li>
                      Estimated reserved cost range: $
                      {microsToUsdText(
                        preview.budget
                          .estimated_reserved_cost_range_micros[0],
                      )}{" "}
                      – $
                      {microsToUsdText(
                        preview.budget
                          .estimated_reserved_cost_range_micros[1],
                      )}
                    </li>
                    <li>
                      Per-attempt ceiling: ${draftValues.attemptCeilingUsd}
                    </li>
                  </ul>
                </div>
                {warnings.length > 0 ? (
                  <div className="qual-review-warnings">
                    <h3>Warnings</h3>
                    <ul>
                      {warnings.map(([key, reasons]) => (
                        <li key={key}>
                          <code>{key}</code>: {reasons.join(", ")}
                        </li>
                      ))}
                    </ul>
                  </div>
                ) : null}
                {startBlockers.length > 0 ? (
                  <div className="qual-review-blockers" role="alert">
                    <h3>Start blockers</h3>
                    <ul>
                      {startBlockers.map(([key, reasons]) => (
                        <li key={key}>
                          <code>{key}</code>: {reasons.join(", ")}
                        </li>
                      ))}
                    </ul>
                  </div>
                ) : null}
                <label className="qual-review-confirm">
                  <input
                    type="checkbox"
                    checked={reviewed}
                    onChange={(event) => setReviewed(event.target.checked)}
                  />
                  I have reviewed the preview, the full target matrix, and the
                  corpus.
                </label>
                <button
                  type="button"
                  onClick={() => void createAndStart()}
                  disabled={
                    !reviewed || starting || startBlockers.length > 0
                  }
                >
                  {starting
                    ? "Starting qualification…"
                    : "Create and start qualification"}
                </button>
              </div>
            ) : null}
          </>
        ) : (
          runIds.map((runId) => (
            <QualificationRunPanel
              key={runId}
              runId={runId}
              client={client}
              runOptions={runOptions}
              onError={onError}
            />
          ))
        )}
      </Panel>
    </section>
  );
}

function QualificationRunPanel({
  runId,
  client,
  runOptions,
  onError,
}: {
  runId: string;
  client: QualificationWorkspaceClient;
  runOptions?: UseQualificationRunOptions;
  onError: (message: string) => void;
}) {
  const { run, connection, lastEvent, error, refresh } = useQualificationRun(
    runId,
    runOptions,
  );
  const [receipt, setReceipt] = useState<QualificationReceipt | null>(null);
  const [acting, setActing] = useState(false);
  const terminal = run !== null && TERMINAL_STATUSES.has(run.status);

  useEffect(() => {
    if (!terminal || receipt !== null) return;
    let active = true;
    client
      .getReceipt(runId)
      .then((next) => {
        if (active) setReceipt(next);
      })
      .catch((value: unknown) => {
        if (active) {
          onError(value instanceof Error ? value.message : String(value));
        }
      });
    return () => {
      active = false;
    };
  }, [terminal, receipt, runId, client, onError]);

  async function act(
    action: (
      input: QualificationLifecycleInput,
    ) => Promise<QualificationRun>,
  ) {
    if (run === null) return;
    setActing(true);
    try {
      await action({ runId, expectedRevision: run.revision });
      await refresh();
    } catch (value) {
      onError(value instanceof Error ? value.message : String(value));
    } finally {
      setActing(false);
    }
  }

  async function lowerCap(cap: string) {
    if (run === null) return;
    setActing(true);
    try {
      await client.lowerCap({
        runId,
        maximumSpendUsd: cap,
        expectedRevision: run.revision,
      });
      await refresh();
    } catch (value) {
      onError(value instanceof Error ? value.message : String(value));
    } finally {
      setActing(false);
    }
  }

  if (run === null) {
    return (
      <p className="qual-run-loading">
        Loading run state from the durable ledger…
      </p>
    );
  }
  if (terminal) {
    return receipt !== null ? (
      <QualificationResults receipt={receipt} />
    ) : (
      <p className="qual-run-loading">Loading the terminal receipt…</p>
    );
  }
  return (
    <>
      {error !== null ? (
        <p className="qual-run-error" role="alert">
          Live updates degraded: {error}. The durable ledger remains the
          authority.
        </p>
      ) : null}
      <QualificationProgress
        run={run}
        connection={connection}
        lastEvent={lastEvent}
        busy={acting}
        onPause={() => void act(client.pause)}
        onResume={() => void act(client.resume)}
        onCancel={() => void act(client.cancel)}
        onLowerCap={(cap) => void lowerCap(cap)}
      />
    </>
  );
}

function scopeInputFromPayload(
  scope: QualificationScopePayload,
): QualificationScopeInput {
  return {
    projectId: scope.project_id,
    taskFamily: scope.task_family,
    risk: scope.risk,
    capabilityKey: scope.capability_key,
    policyId: scope.policy_id,
    policyRevision: scope.policy_revision,
    targetIds: [...scope.target_ids],
    targetInventoryDigest: scope.target_inventory_digest,
    priceDigest: scope.price_digest,
    learnedConfigDigest: scope.learned_config_digest,
    projectAuthorityDigest: scope.project_authority_digest,
  };
}

/** Integer-exact micros → decimal text for display (no float money). */
function microsToUsdText(micros: number): string {
  const whole = Math.floor(micros / 1_000_000);
  const fraction = String(micros % 1_000_000).padStart(6, "0");
  const trimmed = fraction.replace(/0+$/, "");
  const cents = trimmed.length >= 2 ? trimmed : fraction.slice(0, 2);
  return `${whole}.${cents}`;
}
