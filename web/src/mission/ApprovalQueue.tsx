import {
  Clock3,
  FileKey2,
  ShieldAlert,
  Target,
} from "lucide-react";
import {
  Button,
  Card,
  Notice,
  StatusPill,
} from "../components";
import type { Approval } from "../types";
import { EvidenceDrawer } from "./EvidenceDrawer";

export function ApprovalQueue({
  approvals,
  onDecision,
  pendingApprovalId = null,
}: {
  approvals: Approval[];
  onDecision: (
    approval: Approval,
    approved: boolean,
  ) => void | Promise<void>;
  pendingApprovalId?: string | null;
}) {
  const pending = approvals.filter(
    (approval) => approval.status === "pending",
  );
  if (pending.length === 0) return null;

  return (
    <section
      className="mission-approval-queue"
      aria-labelledby="mission-approval-heading"
    >
      <header>
        <p className="page-eyebrow">Owner checkpoint</p>
        <h2 id="mission-approval-heading">
          Exact-call approvals
        </h2>
        <p>
          Each decision authorizes only the displayed call and immutable
          binding. Denial never needs a complete grant packet.
        </p>
      </header>
      <div className="mission-approval-list">
        {pending.map((approval) => (
          <ApprovalCard
            approval={approval}
            key={approval.approval_id}
            pending={
              pendingApprovalId === approval.approval_id
            }
            onDecision={onDecision}
          />
        ))}
      </div>
    </section>
  );
}

function ApprovalCard({
  approval,
  pending,
  onDecision,
}: {
  approval: Approval;
  pending: boolean;
  onDecision: (
    approval: Approval,
    approved: boolean,
  ) => void | Promise<void>;
}) {
  const complete = immutableEvidenceComplete(approval);
  const expired = complete && approvalExpired(approval);
  const target = targetResource(approval.arguments);
  const consequence =
    `If approved, Kestrel may invoke ${approval.tool_name} once ` +
    "with the exact displayed arguments. Any argument, capability, " +
    "or resource change requires a new approval.";

  return (
    <Card
      className="mission-approval-card"
      headingLevel={3}
      title={approval.tool_name}
      labelled={false}
      actions={
        <StatusPill state="blocked">
          {approval.risk} risk
        </StatusPill>
      }
      role="group"
      aria-label={`Approval for ${approval.tool_name}`}
    >
      {!complete ? (
        <Notice
          variant="danger"
          title="Approval evidence is incomplete"
        >
          The immutable resource digest, capability revision, or expiry
          is missing. Approval is disabled; deny this request and let
          Kestrel create a fresh bound call.
        </Notice>
      ) : null}
      {expired ? (
        <Notice
          variant="danger"
          title="Approval has expired"
        >
          The binding expiry has passed. Approval is disabled; deny
          this request and let Kestrel create a fresh bound call.
        </Notice>
      ) : null}
      <dl className="mission-approval-facts">
        <div>
          <dt>
            <FileKey2 size={15} aria-hidden="true" />
            Exact call
          </dt>
          <dd>
            <code>{approval.tool_call_id}</code>
          </dd>
        </div>
        <div>
          <dt>
            <ShieldAlert size={15} aria-hidden="true" />
            Capability
          </dt>
          <dd>
            <code>{`tool:${approval.tool_name}`}</code>
            {" · "}
            revision {approval.capability_revision ?? "missing"}
          </dd>
        </div>
        <div>
          <dt>
            <Target size={15} aria-hidden="true" />
            Target resource
          </dt>
          <dd>
            <span>{target}</span>
            <code>
              {approval.resource_digest || "digest missing"}
            </code>
          </dd>
        </div>
        <div>
          <dt>
            <Clock3 size={15} aria-hidden="true" />
            Expires
          </dt>
          <dd>
            {expired
              ? `Expired ${expiryLabel(approval.expires_at)}`
              : expiryLabel(approval.expires_at)}
          </dd>
        </div>
      </dl>
      <div className="mission-approval-arguments">
        <strong>Exact arguments</strong>
        <pre className="json-block">
          {JSON.stringify(approval.arguments, null, 2)}
        </pre>
      </div>
      <div className="mission-approval-consequence">
        <strong>Consequence</strong>
        <p>{consequence}</p>
      </div>
      <EvidenceDrawer
        title={`Arguments for ${approval.tool_name}`}
        records={[
          {
            label: "Exact arguments",
            value: approval.arguments,
          },
        ]}
      />
      <footer className="mission-approval-actions">
        <Button
          variant="danger"
          pending={pending}
          aria-label={`Deny ${approval.tool_name}`}
          onClick={() => void onDecision(approval, false)}
        >
          Deny
        </Button>
        <Button
          variant="primary"
          pending={pending}
          disabled={!complete || expired}
          aria-label={`Approve ${approval.tool_name}`}
          onClick={() => void onDecision(approval, true)}
        >
          Approve exact call
        </Button>
      </footer>
    </Card>
  );
}

function immutableEvidenceComplete(
  approval: Approval,
): boolean {
  return Boolean(
    typeof approval.capability_revision === "number" &&
      Number.isSafeInteger(approval.capability_revision) &&
      approval.capability_revision >= 0 &&
      validResourceDigest(approval.resource_digest) &&
      typeof approval.expires_at === "string" &&
      Number.isFinite(Date.parse(approval.expires_at)),
  );
}

// The runtime emits canonical `sha256:<64 lowercase hex>` resource
// digests (run_manager.tool_resource_digest). Accept exactly that
// API shape and stay fail-closed for anything malformed: wrong
// algorithm, uppercase hex, wrong length, or stray whitespace.
function validResourceDigest(value: unknown): boolean {
  return (
    typeof value === "string" &&
    /^sha256:[a-f0-9]{64}$/.test(value)
  );
}

function approvalExpired(approval: Approval): boolean {
  if (typeof approval.expires_at !== "string") return false;
  const expiry = Date.parse(approval.expires_at);
  if (!Number.isFinite(expiry)) return false;
  return expiry <= Date.now();
}

function targetResource(
  args: Record<string, unknown>,
): string {
  const keys = [
    "path",
    "repository_path",
    "target",
    "target_url",
    "url",
    "command",
  ];
  for (const key of keys) {
    const value = args[key];
    if (typeof value === "string" && value.trim()) {
      return value;
    }
  }
  return "Bound resource identified by digest";
}

function expiryLabel(value?: string | null): string {
  if (!value) return "Expiry missing";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Expiry invalid";
  return new Intl.DateTimeFormat(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}
