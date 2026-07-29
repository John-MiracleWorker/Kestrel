import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { TaskNode } from "../types";
import { RepairReviewPanel } from "./RepairReviewPanel";

afterEach(cleanup);

const validationId = `repair_validation_${"b".repeat(24)}`;
const reviewId = `repair_review_${"a".repeat(24)}`;
const digest = "d".repeat(64);
const snapshot = {
  branch: "kestrel/worker/review/repair",
  head_sha: "1".repeat(40),
  diff_digest: digest
};

function validationTask(success = true): TaskNode {
  return {
    task_id: "validate",
    title: "Validate",
    goal: "Validate candidate",
    profile: "worker",
    status: "completed",
    approved: true,
    required_tools: ["repair.validate"],
    result: {
      repair_artifact: {
        schema_version: 1,
        tool: "repair.validate",
        validation_id: validationId,
        success,
        repair_snapshot: snapshot
      }
    }
  };
}

function reviewTask(result: Record<string, unknown>): TaskNode {
  return {
    task_id: "review",
    title: "Review",
    goal: "Review candidate",
    profile: "reviewer",
    status: "completed",
    approved: true,
    required_tools: ["repair.review"],
    result
  };
}

describe("RepairReviewPanel authority boundaries", () => {
  it.each([
    [
      "legacy raw task fields",
      {
        review_id: reviewId,
        validation_id: validationId,
        diff_hash: digest,
        commit_gate: {
          commit_allowed: true,
          approval_required_before_commit: true
        }
      }
    ],
    [
      "a projected artifact without explicit commit permission",
      {
        repair_artifact: {
          schema_version: 1,
          tool: "repair.review",
          review_id: reviewId,
          validation_id: validationId,
          repair_snapshot: snapshot,
          commit_gate: {
            approval_required_before_commit: true
          }
        }
      }
    ]
  ])("keeps acceptance actions disabled for %s", (_label, result) => {
    const onPrepareTool = vi.fn();
    render(
      <RepairReviewPanel
        tasks={[validationTask(), reviewTask(result)]}
        allowedPaths={["."]}
        onPrepareTool={onPrepareTool}
      />
    );

    const exportButton = screen.getByRole("button", {
      name: "Prepare exact-call patch export"
    });
    const commitButton = screen.getByRole("button", {
      name: "Prepare exact-call git.commit request"
    });
    expect(exportButton).toBeDisabled();
    expect(commitButton).toBeDisabled();
    fireEvent.click(exportButton);
    fireEvent.click(commitButton);
    expect(onPrepareTool).not.toHaveBeenCalled();
  });

  it("does not treat a completed failed-validation receipt as passing", () => {
    const onPrepareTool = vi.fn();
    render(
      <RepairReviewPanel
        tasks={[
          validationTask(false),
          reviewTask({
            repair_artifact: {
              schema_version: 1,
              tool: "repair.review",
              review_id: reviewId,
              validation_id: validationId,
              repair_snapshot: snapshot,
              commit_gate: {
                commit_allowed: true,
                approval_required_before_commit: true
              }
            }
          })
        ]}
        allowedPaths={["."]}
        onPrepareTool={onPrepareTool}
      />
    );

    expect(screen.getByText("Validation failed")).toBeInTheDocument();
    expect(screen.getByRole("button", {
      name: "Prepare exact-call patch export"
    })).toBeDisabled();
    expect(screen.getByRole("button", {
      name: "Prepare exact-call git.commit request"
    })).toBeDisabled();
  });
});
