/**
 * Corpus review list (Adaptive Flock plan, Task 19).
 *
 * Each corpus item is shown with its risk and evidence kind.  High-risk items
 * are labelled deterministic-only: they never gain learned routing authority.
 */

import { StatusBadge } from "../../components";
import type { QualificationCorpusItemInput } from "./types";

export function CorpusReview({
  corpus,
}: {
  corpus: readonly QualificationCorpusItemInput[];
}) {
  return (
    <div className="qual-corpus-review">
      <h3>Corpus review</h3>
      {corpus.length === 0 ? (
        <p className="muted">The corpus is empty; there is nothing to run.</p>
      ) : (
        <ul className="qual-corpus-list">
          {corpus.map((item) => {
            const deterministicOnly =
              item.risk === "high" || item.risk === "critical";
            return (
              <li key={item.itemId} className="qual-corpus-item">
                <div className="qual-corpus-head">
                  <strong>{item.itemId}</strong>
                  <StatusBadge value={item.risk} />
                  {deterministicOnly ? (
                    <span className="qual-deterministic-note">
                      deterministic-only
                    </span>
                  ) : null}
                </div>
                <ul className="qual-corpus-facts">
                  <li>Task family: {item.taskFamily}</li>
                  <li>Capabilities: {item.capabilities.join(", ")}</li>
                  <li>Evidence: {item.evidenceKind}</li>
                  <li>
                    {item.actionable === false
                      ? "Not actionable"
                      : "Actionable"}
                  </li>
                  {(item.exclusionReasons ?? []).map((reason) => (
                    <li key={reason}>
                      Excluded: <code>{reason}</code>
                    </li>
                  ))}
                </ul>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
