import type { AxeResults, Result } from "axe-core";

/**
 * Shared axe runner for Workbench accessibility gates.
 *
 * jsdom cannot perform layout, so `color-contrast` is disabled inside axe and
 * covered instead by the programmatic token-pair checks in contrast.test.ts.
 * No other rule is disabled here; violations are fixed at the primitive or
 * owning-feature level, never by widening this configuration.
 */
export const WORKBENCH_AXE_OPTIONS = {
  rules: {
    "color-contrast": { enabled: false },
  },
} as const;

export async function runWorkbenchAxe(
  container: Element,
): Promise<AxeResults> {
  const axe = (await import("axe-core")).default;
  return axe.run(container, WORKBENCH_AXE_OPTIONS);
}

export function seriousViolations(report: AxeResults): Result[] {
  return report.violations.filter(
    (violation) =>
      violation.impact === "serious" || violation.impact === "critical",
  );
}

export function formatViolations(violations: Result[]): string {
  return violations
    .map(
      (violation) =>
        `[${violation.impact}] ${violation.id}: ${violation.help}\n` +
        violation.nodes
          .map((node) => `  - ${node.target.join(" ")} :: ${node.failureSummary ?? ""}`)
          .join("\n"),
    )
    .join("\n");
}
