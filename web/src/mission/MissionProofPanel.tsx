import { ShieldCheck } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { getJson } from "../api";
import { Notice, StatusPill } from "../components";
import type { StatusState } from "../design/StatusPill";

export type MissionProofSection = {
  status: "present" | "missing" | "stale" | "mismatched";
  detail: string;
  evidence: Record<string, unknown>;
};

export type MissionProof = {
  schema: "kestrel.mission_proof.v1";
  run_id: string;
  project_id: string | null;
  generated_at: string;
  binding: {
    persisted: boolean;
    preflight_persisted: boolean;
  };
  evidence: Record<string, MissionProofSection>;
  summary: {
    present: string[];
    missing: string[];
    stale: string[];
    mismatched: string[];
    counts: {
      present: number;
      missing: number;
      stale: number;
      mismatched: number;
    };
  };
};

export function MissionProofPanel({
  runId,
  onAuthRequired,
}: {
  runId: string;
  onAuthRequired?: () => void;
}) {
  const [proof, setProof] = useState<MissionProof | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const projection = await getJson<MissionProof>(
        `/api/runs/${encodeURIComponent(runId)}/mission-proof`,
      );
      setProof(projection);
    } catch (loadError) {
      if (
        loadError instanceof Error &&
        loadError.name === "ApiAuthError"
      ) {
        onAuthRequired?.();
        return;
      }
      setError(
        loadError instanceof Error ? loadError.message : String(loadError),
      );
    } finally {
      setLoading(false);
    }
  }, [runId, onAuthRequired]);

  useEffect(() => {
    void load();
  }, [load]);

  if (loading) {
    return (
      <section
        className="mission-proof-panel"
        aria-label="Mission proof"
      >
        <header>
          <ShieldCheck size={17} aria-hidden="true" />
          <div>
            <h3>Mission proof</h3>
            <p>Loading the server-authored evidence projection…</p>
          </div>
        </header>
      </section>
    );
  }

  if (error || !proof) {
    return (
      <section
        className="mission-proof-panel"
        aria-label="Mission proof"
      >
        <header>
          <ShieldCheck size={17} aria-hidden="true" />
          <div>
            <h3>Mission proof</h3>
            <p>Server-authored evidence could not be loaded.</p>
          </div>
        </header>
        {error ? (
          <Notice variant="danger" title="Mission proof unavailable">
            {error}
          </Notice>
        ) : null}
      </section>
    );
  }

  const sections = Object.entries(proof.evidence).sort(
    ([left], [right]) => left.localeCompare(right),
  );

  return (
    <section
      className="mission-proof-panel"
      aria-label="Mission proof"
      data-proof-status={
        proof.summary.mismatched.length > 0
          ? "mismatched"
          : proof.summary.stale.length > 0
            ? "stale"
            : proof.summary.missing.length > 0
              ? "partial"
              : "complete"
      }
    >
      <header>
        <ShieldCheck size={17} aria-hidden="true" />
        <div>
          <h3>Mission proof</h3>
          <p>
            Server-authored evidence for this run. The UI renders exactly what
            the projection reports — it never infers authority from
            presentation state.
          </p>
        </div>
        <StatusPill state={summaryState(proof)} iconLabel="Proof summary">
          {summaryLabel(proof)}
        </StatusPill>
      </header>

      {proof.summary.mismatched.length > 0 ? (
        <Notice
          variant="danger"
          title="Mismatched evidence"
        >
          The projection reports mismatched evidence:{" "}
          {proof.summary.mismatched.join(", ")}. Reconcile before treating the
          mission as fully proven.
        </Notice>
      ) : null}

      {proof.summary.stale.length > 0 ? (
        <Notice variant="caution" title="Stale evidence">
          Stale evidence: {proof.summary.stale.join(", ")}.
        </Notice>
      ) : null}

      <div className="mission-proof-sections">
        {sections.map(([key, section]) => (
          <article
            key={key}
            className={`mission-proof-section is-${section.status}`}
            data-proof-section={key}
            data-status={section.status}
          >
            <div className="mission-proof-section-head">
              <strong>{sectionTitle(key)}</strong>
              <StatusPill state={sectionStatusState(section.status)}>
                {section.status}
              </StatusPill>
            </div>
            <p>{section.detail}</p>
          </article>
        ))}
      </div>
    </section>
  );
}

function sectionTitle(key: string): string {
  const titles: Record<string, string> = {
    binding: "Binding",
    contract: "Contract",
    roles: "Roles",
    routing: "Routing",
    isolation: "Isolation",
    change: "Change",
    validation: "Validation",
    review: "Review",
    risks: "Risks",
    approval: "Approval",
    shipping: "Shipping",
    capsule: "Capsule",
    learning: "Learning",
  };
  return titles[key] ?? key.replaceAll("_", " ");
}

function sectionStatusState(
  status: MissionProofSection["status"],
): StatusState {
  if (status === "present") return "healthy";
  if (status === "missing") return "inactive";
  if (status === "stale") return "waiting";
  return "blocked";
}

function summaryState(proof: MissionProof): StatusState {
  if (proof.summary.mismatched.length > 0) return "blocked";
  if (proof.summary.stale.length > 0) return "waiting";
  if (proof.summary.missing.length > 0) return "inactive";
  return "healthy";
}

function summaryLabel(proof: MissionProof): string {
  const { counts } = proof.summary;
  const total =
    counts.present +
    counts.missing +
    counts.stale +
    counts.mismatched;
  return `${counts.present}/${total} present`;
}
