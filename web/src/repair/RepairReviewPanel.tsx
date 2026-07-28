import { useState } from "react";
import { InlineMeta, StatusBadge } from "../components";
import type { TaskNode } from "../types";

export function RepairReviewPanel({
  tasks,
  onPrepareTool
}: {
  tasks: TaskNode[];
  onPrepareTool: (name: string, args: Record<string, unknown>) => void;
}) {
  const [diffMode, setDiffMode] = useState<"unified" | "split">("unified");
  const repairTasks = tasks.filter((task) =>
    (task.required_tools ?? []).some((tool) => tool.startsWith("repair.") || tool === "git.commit")
  );
  if (repairTasks.length === 0) return null;

  const validationTask = repairTasks.find((task) =>
    taskUsesTool(task, "repair.validate") || taskUsesTool(task, "repair.orchestrate_validate")
  );
  const reviewTask = repairTasks.find((task) => taskUsesTool(task, "repair.review"));
  const rollbackTask = repairTasks.find((task) => taskUsesTool(task, "repair.rollback"));

  const validationResult = validationTask?.result ?? null;
  const validationArtifact = readRecord(validationResult?.repair_artifact);
  const validation = readRecord(validationResult?.validation);
  const validationSnapshot = readRecord(validationArtifact?.repair_snapshot);
  const validationId = String(validationArtifact?.validation_id ?? validation?.validation_id ?? "pending");
  const explicitValidationSuccess = validation?.success;
  const validationSuccess = explicitValidationSuccess === true || (
    explicitValidationSuccess === undefined
    && validationTask?.status === "completed"
    && ["repair.validate", "repair.orchestrate_validate"].includes(String(validationArtifact?.tool ?? ""))
    && validationId !== "pending"
  );
  const validationFailed = explicitValidationSuccess === false;
  const validationLabel = validationSuccess
    ? "Validation passed"
    : validationFailed
      ? "Validation failed"
      : "Validation pending";
  const validationCommand = formatCommand(validation?.command);
  const validationEvidence = validationCommand
    || (validationId !== "pending" ? validationId : validationTask?.title ?? "pending");

  const reviewResult = reviewTask?.result ?? null;
  const reviewArtifact = readRecord(reviewResult?.repair_artifact);
  const reviewSnapshot = readRecord(reviewArtifact?.repair_snapshot);
  const reviewId = String(reviewArtifact?.review_id ?? reviewResult?.review_id ?? "pending");
  const diffHash = String(reviewSnapshot?.diff_digest ?? reviewResult?.diff_hash ?? "pending");
  const reviewBranch = String(reviewSnapshot?.branch ?? "pending");
  const reviewHead = String(reviewSnapshot?.head_sha ?? "pending");
  const changedFiles = asStringArray(reviewArtifact?.changed_files ?? reviewResult?.changed_files);
  const diffPreview = readRecord(reviewArtifact?.diff_preview ?? reviewResult?.diff_preview);
  const diffPreviewBound = diffPreview?.bound_diff_digest === diffHash
    && diffPreview?.redacted === true
    && diffPreview?.authoritative === false;
  const diffContent = diffPreviewBound && typeof diffPreview?.content === "string"
    ? diffPreview.content
    : "";
  const diffTruncated = diffPreview?.truncated === true;
  const diffOmittedFiles = typeof diffPreview?.omitted_files === "number"
    ? diffPreview.omitted_files
    : 0;
  const hasReviewArtifact = reviewId !== "pending";
  const commitGate = readRecord(reviewArtifact?.commit_gate ?? reviewResult?.commit_gate);
  const commitApprovalRequired = commitGate?.approval_required_before_commit === true;
  const commitAllowed = hasReviewArtifact
    && reviewTask?.status === "completed"
    && commitApprovalRequired
    && commitGate?.commit_allowed !== false;
  const reviewSummary = String(reviewArtifact?.summary ?? reviewResult?.summary ?? "").trim();
  const riskNotes = asStringArray(reviewArtifact?.risks ?? reviewResult?.risks);

  const rollbackResult = rollbackTask?.result ?? null;
  const rollbackId = String(rollbackResult?.rollback_id ?? "pending");
  const restoredFiles = asStringArray(rollbackResult?.restored_files);
  const artifactPath = String(rollbackResult?.artifact_path ?? ".nest/repair_rollbacks");
  const criteria = uniqueStrings(
    repairTasks.flatMap((task) => task.acceptance_criteria ?? [])
  );

  const prepareCommit = () => {
    onPrepareTool("git.commit", {
      message: `repair: commit reviewed changes for ${reviewId}`,
      repair_review_id: reviewId
    });
  };
  const prepareExport = () => {
    onPrepareTool("git.export_patch", {
      staged: false
    });
  };
  const prepareRollback = () => {
    onPrepareTool("repair.rollback", {
      reason: `Rollback reviewed repair ${reviewId}`,
      review_id: reviewId,
      expected_current_diff_digest: diffHash
    });
  };

  return (
    <section aria-label="Repair Patch Review" className="run-detail repair-review-panel">
      <div className="run-title">
        <h3>Repair Patch Review</h3>
        <StatusBadge value={reviewTask?.status ?? validationTask?.status ?? "pending"} />
      </div>
      <p className="muted">Validation, reviewer gate, acceptance evidence, patch, and rollback for this candidate.</p>
      <div className="list compact-list">
        {validationTask && (
          <div className="data-row">
            <strong>{validationLabel}</strong>
            <InlineMeta items={[validationTask.status, validationTask.risk, validationTask.scheduler_reason]} />
            <p>{`${validationSuccess || validationFailed ? validationLabel : "Validation state"}: ${validationEvidence}`}</p>
            {Boolean(validationSnapshot?.diff_digest) && <p>{`Candidate digest ${String(validationSnapshot?.diff_digest)}`}</p>}
          </div>
        )}
        {criteria.length > 0 && (
          <div className="data-row repair-acceptance-map">
            <strong>Acceptance evidence</strong>
            <p className="muted">
              A passing candidate receipt is evidence to inspect, not an automatic claim that every criterion is satisfied.
            </p>
            <ul>
              {criteria.map((criterion) => (
                <li key={criterion}>
                  <span>{criterion}</span>
                  <StatusBadge value={validationFailed ? "failed" : validationSuccess ? "evidence recorded" : "pending"} />
                  <small>{validationId}</small>
                </li>
              ))}
            </ul>
          </div>
        )}
        {reviewTask && (
          <div className="data-row">
            <strong>Review gate</strong>
            <InlineMeta items={[
              reviewTask.status,
              reviewTask.profile,
              commitApprovalRequired ? "exact-call commit approval" : "commit gate pending"
            ]} />
            <p>{`Review gate: ${reviewId} · ${commitApprovalRequired ? "commit approval required" : "commit gate pending"}`}</p>
            <p>{`Diff ${diffHash} · ${changedFiles.length ? changedFiles.join(", ") : "no changed files recorded"}`}</p>
            {reviewBranch !== "pending" && <p>{`Candidate ${reviewBranch} @ ${reviewHead}`}</p>}
            {reviewSummary && <p>{reviewSummary}</p>}
            {riskNotes.length > 0 && (
              <div className="repair-risk-notes">
                <strong>Known risks</strong>
                <ul>
                  {riskNotes.map((risk) => <li key={risk}>{risk}</li>)}
                </ul>
              </div>
            )}
            {diffContent ? (
              <section className="repair-diff" aria-label="Repair diff preview">
                <header>
                  <h4>{diffMode === "unified" ? "Unified diff preview" : "Split diff preview"}</h4>
                  <div className="repair-diff-mode" role="group" aria-label="Diff layout">
                    <button
                      type="button"
                      className={diffMode === "unified" ? "active" : ""}
                      aria-pressed={diffMode === "unified"}
                      onClick={() => setDiffMode("unified")}
                    >
                      Unified
                    </button>
                    <button
                      type="button"
                      className={diffMode === "split" ? "active" : ""}
                      aria-pressed={diffMode === "split"}
                      onClick={() => setDiffMode("split")}
                    >
                      Split
                    </button>
                  </div>
                </header>
                <p className="muted">
                  Redacted advisory preview. The signed candidate digest and commit-time
                  revalidation remain authoritative.
                  {diffTruncated || diffOmittedFiles > 0
                    ? ` ${diffOmittedFiles} changed file${diffOmittedFiles === 1 ? " is" : "s are"} omitted from the inline preview.`
                    : ""}
                </p>
                {diffMode === "unified" ? (
                  <pre className="repair-diff-unified">
                    {diffContent.split("\n").map((line, index) => (
                      <code className={diffLineClass(line)} key={`${index}-${line.slice(0, 32)}`}>
                        {line || " "}
                      </code>
                    ))}
                  </pre>
                ) : (
                  <div className="repair-diff-split">
                    {splitDiffRows(diffContent).map((row, index) => (
                      <div
                        className={`repair-diff-row ${row.kind}`}
                        key={`${index}-${row.left.slice(0, 20)}-${row.right.slice(0, 20)}`}
                      >
                        <code>{row.left || " "}</code>
                        <code>{row.right || " "}</code>
                      </div>
                    ))}
                  </div>
                )}
              </section>
            ) : (
              <p className="muted">Inline diff preview unavailable; changed-file and digest gates remain recorded.</p>
            )}
            <div className="page-actions">
              <button type="button" className="btn subtle" disabled={!hasReviewArtifact} onClick={prepareExport}>
                Prepare exact-call patch export
              </button>
              <button type="button" className="btn subtle" disabled={!commitAllowed} onClick={prepareCommit}>
                Prepare exact-call git.commit request
              </button>
            </div>
          </div>
        )}
        {rollbackTask && (
          <div className="data-row">
            <strong>Rollback state</strong>
            <InlineMeta items={[rollbackTask.status, rollbackTask.risk, rollbackTask.approved ? "approved" : "approval required"]} />
            <p>{`Rollback state: ${rollbackTask.status} · ${rollbackId}`}</p>
            <p>{`Restores ${restoredFiles.length ? restoredFiles.join(", ") : "recorded repair files"} and preserves ${artifactPath}`}</p>
            <button type="button" className="btn subtle" disabled={!hasReviewArtifact} onClick={prepareRollback}>
              Prepare exact-call repair.rollback request
            </button>
          </div>
        )}
      </div>
    </section>
  );
}

function taskUsesTool(task: TaskNode, toolName: string): boolean {
  return (task.required_tools ?? []).includes(toolName);
}

function readRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function formatCommand(value: unknown): string {
  if (Array.isArray(value)) return value.map((part) => String(part)).filter(Boolean).join(" ");
  return typeof value === "string" ? value : "";
}

function diffLineClass(line: string): string {
  if (line.startsWith("diff --git") || line.startsWith("index ") || line.startsWith("@@")) return "diff-meta";
  if (line.startsWith("--- ") || line.startsWith("+++ ")) return "diff-file";
  if (line.startsWith("+")) return "diff-add";
  if (line.startsWith("-")) return "diff-delete";
  return "diff-context";
}

function splitDiffRows(content: string): Array<{ left: string; right: string; kind: string }> {
  return content.split("\n").map((line) => {
    const lineClass = diffLineClass(line);
    if (lineClass === "diff-add") return { left: "", right: line, kind: "add" };
    if (lineClass === "diff-delete") return { left: line, right: "", kind: "delete" };
    return {
      left: line,
      right: line,
      kind: lineClass === "diff-context" ? "context" : "meta"
    };
  });
}

function asStringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.map((item) => String(item)).filter(Boolean) : [];
}

function uniqueStrings(values: string[]): string[] {
  return Array.from(new Set(values.map((value) => value.trim()).filter(Boolean)));
}
