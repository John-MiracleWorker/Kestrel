import { useState } from "react";
import { InlineMeta, StatusBadge } from "../components";
import { EvidenceDrawer } from "../mission/EvidenceDrawer";
import type { TaskNode } from "../types";

export function RepairReviewPanel({
  tasks,
  allowedPaths,
  onPrepareTool
}: {
  tasks: TaskNode[];
  allowedPaths: string[];
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

  const validationArtifact = projectedRepairArtifact(
    validationTask?.result?.repair_artifact,
    ["repair.validate", "repair.orchestrate_validate"]
  );
  const validationSnapshot = projectedRepairSnapshot(validationArtifact?.repair_snapshot);
  const validationId = validIdentifier(
    validationArtifact?.validation_id,
    /^repair_validation_[0-9a-f]{24}$/
  ) ?? "pending";
  const validationSuccess = validationTask?.status === "completed"
    && validationArtifact !== null
    && validationSnapshot !== null
    && validationId !== "pending"
    && validationArtifact.success === true;
  const validationFailed = validationTask?.status === "failed"
    || validationArtifact?.success === false;
  const validationLabel = validationSuccess
    ? "Validation passed"
    : validationFailed
      ? "Validation failed"
      : "Validation pending";
  const validationEvidence = validationId !== "pending"
    ? validationId
    : validationTask?.title ?? "pending";

  const reviewArtifact = projectedRepairArtifact(
    reviewTask?.result?.repair_artifact,
    ["repair.review"]
  );
  const reviewSnapshot = projectedRepairSnapshot(reviewArtifact?.repair_snapshot);
  const reviewId = validIdentifier(
    reviewArtifact?.review_id,
    /^repair_review_[0-9a-f]{24}$/
  ) ?? "pending";
  const reviewValidationId = validIdentifier(
    reviewArtifact?.validation_id,
    /^repair_validation_[0-9a-f]{24}$/
  );
  const diffHash = validIdentifier(
    reviewSnapshot?.diff_digest,
    /^[0-9a-f]{64}$/
  ) ?? "pending";
  const reviewBranch = String(reviewSnapshot?.branch ?? "pending");
  const reviewHead = String(reviewSnapshot?.head_sha ?? "pending");
  const changedFiles = asStringArray(reviewArtifact?.changed_files);
  const diffPreview = readRecord(reviewArtifact?.diff_preview);
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
  const commitGate = readRecord(reviewArtifact?.commit_gate);
  const commitApprovalRequired = commitGate?.approval_required_before_commit === true;
  const hasReviewArtifact = reviewTask?.status === "completed"
    && reviewArtifact !== null
    && reviewSnapshot !== null
    && reviewId !== "pending"
    && reviewValidationId === validationId
    && validationSuccess
    && validationSnapshot?.branch === reviewSnapshot.branch
    && validationSnapshot?.head_sha === reviewSnapshot.head_sha
    && validationSnapshot?.diff_digest === reviewSnapshot.diff_digest;
  const commitAllowed = hasReviewArtifact
    && reviewTask?.status === "completed"
    && commitApprovalRequired
    && commitGate?.commit_allowed === true;
  const exportDestinationAllowed = projectArtifactDestinationAllowed(allowedPaths);
  const reviewSummary = String(reviewArtifact?.summary ?? "").trim();
  const riskNotes = asStringArray(reviewArtifact?.risks);

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
      repair_review_id: reviewId,
      expected_current_diff_digest: diffHash
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
              <button
                type="button"
                className="btn subtle"
                disabled={!commitAllowed || !exportDestinationAllowed}
                title={exportDestinationAllowed
                  ? "The backend revalidates the signed review before writing."
                  : "Project allowed paths exclude .kestrel/improvements."}
                onClick={prepareExport}
              >
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
      <EvidenceDrawer
        title="Repair evidence records"
        records={[
          ...(validationArtifact
            ? [
                {
                  label: "Validation receipt",
                  value: validationArtifact,
                },
              ]
            : []),
          ...(reviewArtifact
            ? [
                {
                  label: "Signed review receipt",
                  value: reviewArtifact,
                },
              ]
            : []),
          ...(rollbackResult
            ? [
                {
                  label: "Rollback receipt",
                  value: rollbackResult,
                },
              ]
            : []),
        ]}
      />
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

function projectedRepairArtifact(
  value: unknown,
  expectedTools: string[]
): Record<string, unknown> | null {
  const artifact = readRecord(value);
  return artifact?.schema_version === 1
    && expectedTools.includes(String(artifact.tool ?? ""))
    ? artifact
    : null;
}

function projectedRepairSnapshot(value: unknown): Record<string, string> | null {
  const snapshot = readRecord(value);
  const branch = typeof snapshot?.branch === "string" ? snapshot.branch : "";
  const head = validIdentifier(snapshot?.head_sha, /^(?:[0-9a-f]{40}|[0-9a-f]{64})$/);
  const digest = validIdentifier(snapshot?.diff_digest, /^[0-9a-f]{64}$/);
  return branch && head && digest
    ? { branch, head_sha: head, diff_digest: digest }
    : null;
}

function validIdentifier(value: unknown, pattern: RegExp): string | null {
  return typeof value === "string" && pattern.test(value) ? value : null;
}

function projectArtifactDestinationAllowed(allowedPaths: string[]): boolean {
  return allowedPaths.some((path) => {
    const normalized = path.replaceAll("\\", "/").replace(/^\.\/|\/$/g, "");
    return normalized === "" || normalized === "."
      || normalized === ".kestrel"
      || normalized.startsWith(".kestrel/improvements");
  });
}
