/**
 * All-target preview matrix (Adaptive Flock plan, Task 19).
 *
 * Every eligible target and every exclusion reason is shown before launch;
 * nothing is silently dropped from the owner's review.
 */

import type { QualificationPreview } from "./types";

export function TargetMatrix({ preview }: { preview: QualificationPreview }) {
  const excludedTargets = Object.entries(preview.excluded_targets);
  const excludedScopes = Object.entries(preview.excluded_scopes);
  return (
    <div className="qual-target-matrix">
      <h3>Target matrix</h3>
      <p className="muted">
        {preview.matrix_size} scope/target cells across{" "}
        {preview.scopes.length} scope(s). Every eligible target and every
        exclusion reason is listed here before launch.
      </p>
      <table>
        <thead>
          <tr>
            <th scope="col">Target</th>
            <th scope="col">Eligibility</th>
            <th scope="col">Exclusion reasons</th>
          </tr>
        </thead>
        <tbody>
          {preview.target_ids.map((targetId) => (
            <tr key={targetId}>
              <td>{targetId}</td>
              <td>eligible</td>
              <td>—</td>
            </tr>
          ))}
          {excludedTargets.map(([targetId, reasons]) => (
            <tr key={targetId}>
              <td>{targetId}</td>
              <td>excluded</td>
              <td>{reasons.join(", ")}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {excludedScopes.length > 0 ? (
        <div className="qual-excluded-scopes">
          <h4>Excluded scopes</h4>
          <ul>
            {excludedScopes.map(([scopeKey, reasons]) => (
              <li key={scopeKey}>
                <code>{scopeKey}</code>: {reasons.join(", ")}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}
