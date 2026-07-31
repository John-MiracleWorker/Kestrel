import {
  Eye,
  LockKeyhole,
  Settings2,
  ShieldCheck,
} from "lucide-react";
import {
  Button,
  Card,
  Disclosure,
  Notice,
  StatusPill,
} from "../../components";
import type {
  SetupReadinessCheck,
  SetupReadinessReport,
} from "../../types";

const safetyCheckIds = new Set([
  "permission_gates",
  "validation_container",
  "repair_isolation",
  "proactive_routines",
  "api_auth",
  "credential_storage",
]);

export function SafetyStage({
  readiness,
  pending,
  error,
  onOpenSafetySettings,
  onContinue,
}: {
  readiness: SetupReadinessReport;
  pending: boolean;
  error: string | null;
  onOpenSafetySettings: () => void;
  onContinue: () => void;
}) {
  const reportedChecks = readiness.checks.filter((check) =>
    safetyCheckIds.has(check.check_id),
  );

  return (
    <div className="setup-stage">
      <header className="setup-stage-heading">
        <p className="page-eyebrow">Owner control</p>
        <h2 tabIndex={-1} data-setup-stage-heading>
          Review safety defaults
        </h2>
        <p>
          This stage explains the current boundary. It does not change any
          capability setting or turn on a dangerous tool.
        </p>
      </header>

      <Notice variant="success" title="Conservative by default">
        High-risk actions stay behind explicit configuration and exact-call
        approval. Kestrel cannot enable them just to make setup look
        complete.
      </Notice>

      {error ? (
        <Notice variant="danger" title="Safety review was relocked">
          {error}
        </Notice>
      ) : null}

      <div className="setup-safety-grid">
        <SafetyPrinciple
          icon={<LockKeyhole size={19} />}
          title="Local and private"
        >
          Project data remains on this owner-controlled Kestrel unless a
          reviewed provider policy allows otherwise.
        </SafetyPrinciple>
        <SafetyPrinciple
          icon={<Eye size={19} />}
          title="Exact-call approval"
        >
          High-risk tool requests must show the exact action, arguments,
          target, and consequences before approval.
        </SafetyPrinciple>
        <SafetyPrinciple
          icon={<ShieldCheck size={19} />}
          title="No setup escalation"
        >
          Setup inspects configuration. It never widens the project or tool
          capability ceiling.
        </SafetyPrinciple>
      </div>

      {reportedChecks.length ? (
        <section
          className="setup-reported-safety"
          aria-labelledby="reported-safety-title"
        >
          <h3 id="reported-safety-title">Reported safety checks</h3>
          {reportedChecks.map((check) => (
            <SafetyCheck check={check} key={check.check_id} />
          ))}
        </section>
      ) : null}

      <footer className="setup-stage-actions">
        <Button variant="secondary" onClick={onOpenSafetySettings}>
          <Settings2 size={16} aria-hidden="true" />
          Open safety settings
        </Button>
        <Button
          variant="primary"
          pending={pending}
          onClick={onContinue}
        >
          Verify and continue
        </Button>
      </footer>
    </div>
  );
}

function SafetyPrinciple({
  icon,
  title,
  children,
}: {
  icon: React.ReactNode;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <Card title={title} icon={icon} headingLevel={3}>
      <p>{children}</p>
    </Card>
  );
}

function SafetyCheck({ check }: { check: SetupReadinessCheck }) {
  return (
    <article className="setup-safety-check">
      <div>
        <strong>{check.title}</strong>
        <p>{check.detail}</p>
      </div>
      <StatusPill state={statusState(check.status)}>
        {check.status}
      </StatusPill>
      {check.status !== "pass" ? (
        <Disclosure title={`Advanced diagnostics for ${check.title}`}>
          <p>{check.recovery}</p>
        </Disclosure>
      ) : null}
    </article>
  );
}

function statusState(status: SetupReadinessCheck["status"]) {
  if (status === "pass") return "healthy" as const;
  if (status === "warn") return "caution" as const;
  return "blocked" as const;
}
