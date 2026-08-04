import {
  Box,
  RefreshCw,
} from "lucide-react";
import {
  Button,
  Card,
  Disclosure,
  EmptyState,
  Notice,
  StatusPill,
} from "../../components";
import type {
  SetupReadinessCheck,
  SetupReadinessReport,
} from "../../types";

const providerCheckIds = new Set([
  "provider_configuration",
  "provider_operational",
]);
const nativePathRepairIds = new Set([
  "workspace",
]);

export function CoreCheckStage({
  readiness,
  pending,
  pendingCheckId,
  error,
  supportsNativePathRepair,
  onRefresh,
  onRepair,
  onReviewSettings,
  onContinue,
}: {
  readiness: SetupReadinessReport;
  pending: boolean;
  pendingCheckId: string | null;
  error: string | null;
  supportsNativePathRepair: boolean;
  onRefresh: () => void;
  onRepair: (checkId: string) => void;
  onReviewSettings: (checkId: string) => void;
  onContinue: () => void;
}) {
  const checks = readiness.checks.filter(
    (check) => !providerCheckIds.has(check.check_id),
  );
  const failures = checks.filter((check) => check.status === "fail");
  const issues = checks.filter((check) => check.status !== "pass");

  return (
    <div className="setup-stage">
      <header className="setup-stage-heading">
        <p className="page-eyebrow">Bundled core</p>
        <h2 tabIndex={-1} data-setup-stage-heading>
          Check the bundled core
        </h2>
        <p>
          Kestrel checks its local runtime, storage, containment, and owner
          safety boundary. This inspection is read-only.
        </p>
      </header>

      <Notice
        variant={failures.length ? "danger" : issues.length ? "caution" : "success"}
        title={
          failures.length
            ? `${failures.length} core check${
                failures.length === 1 ? "" : "s"
              } need repair`
            : issues.length
              ? "The core can continue with cautions"
              : "The bundled core is ready"
        }
      >
        {failures.length
          ? "Use a bounded GUI repair where one is available. Launch-controlled checks are labeled honestly and remain blocked until restart recovery succeeds."
          : "No command-line setup is required for this stage."}
      </Notice>

      {error ? (
        <Notice variant="danger" title="Core repair did not complete">
          {error}
        </Notice>
      ) : null}

      <div className="setup-check-grid">
        {checks.length ? (
          checks.map((check) => (
            <CoreCheck
              check={check}
              key={check.check_id}
              pending={pendingCheckId === check.check_id}
              supportsNativePathRepair={
                supportsNativePathRepair
              }
              onRepair={onRepair}
              onReviewSettings={onReviewSettings}
            />
          ))
        ) : (
          <EmptyState
            title="Core response received"
            icon={<Box size={22} />}
            headingLevel={3}
          >
            This build did not report separate storage or containment
            checks. The local control plane is responding.
          </EmptyState>
        )}
      </div>

      <footer className="setup-stage-actions">
        <Button
          variant="secondary"
          pending={pending}
          onClick={onRefresh}
        >
          <RefreshCw size={16} aria-hidden="true" />
          Check again
        </Button>
        {!failures.length ? (
          <Button variant="primary" onClick={onContinue}>
            Continue
          </Button>
        ) : null}
      </footer>
    </div>
  );
}

function CoreCheck({
  check,
  pending,
  supportsNativePathRepair,
  onRepair,
  onReviewSettings,
}: {
  check: SetupReadinessCheck;
  pending: boolean;
  supportsNativePathRepair: boolean;
  onRepair: (checkId: string) => void;
  onReviewSettings: (checkId: string) => void;
}) {
  const hasNativeRepair =
    supportsNativePathRepair &&
    nativePathRepairIds.has(check.check_id);
  return (
    <Card
      title={check.title}
      headingLevel={3}
      className={`setup-check-card is-${check.status}`}
      actions={
        <StatusPill state={statusState(check.status)}>
          {check.status}
        </StatusPill>
      }
    >
      <div className="setup-evidence">
        <strong>Evidence</strong>
        <p>{check.detail}</p>
      </div>
      {check.status !== "pass" ? (
        <>
          <Button
            variant="secondary"
            size="small"
            pending={pending}
            aria-label={
              hasNativeRepair
                ? `Choose a new location for ${check.title}`
                : `Review recovery for ${check.title}`
            }
            onClick={() =>
              hasNativeRepair
                ? onRepair(check.check_id)
                : onReviewSettings(check.check_id)
            }
          >
            {hasNativeRepair
              ? "Choose location and verify"
              : restartControlled(check.check_id)
                ? "Review restart recovery"
                : "Review in Settings"}
          </Button>
          <Disclosure
            title={`Advanced diagnostics for ${check.title}`}
          >
            <p>{check.recovery}</p>
          </Disclosure>
        </>
      ) : null}
    </Card>
  );
}

function restartControlled(checkId: string): boolean {
  return new Set([
    "memory_storage",
    "state_storage",
    "log_storage",
    "api_auth",
    "validation_container",
  ]).has(checkId);
}

function statusState(status: SetupReadinessCheck["status"]) {
  if (status === "pass") return "healthy" as const;
  if (status === "warn") return "caution" as const;
  return "blocked" as const;
}
