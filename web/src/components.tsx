import { ReactNode } from "react";
import { X } from "lucide-react";
import { Button } from "./design/Button";
import { Card } from "./design/Card";
import { Disclosure } from "./design/Disclosure";
import { EmptyState } from "./design/EmptyState";
import { Field } from "./design/Field";
import { Notice } from "./design/Notice";
import { Skeleton } from "./design/Skeleton";
import {
  StatusPill,
  type StatusState,
} from "./design/StatusPill";

export {
  Button,
  Card,
  Disclosure,
  EmptyState,
  Field,
  Notice,
  Skeleton,
  StatusPill,
};

export function Panel({
  id,
  title,
  icon,
  actions,
  children,
  className = ""
}: {
  id?: string;
  title: string;
  icon?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <Card
      id={id}
      title={title}
      icon={icon}
      actions={actions}
      labelled={Boolean(id)}
      className={`panel ${className}`}
    >
      {children}
    </Card>
  );
}

export function JsonBlock({ value, maxHeight = "220px" }: { value: unknown; maxHeight?: string }) {
  const content = typeof value === "string" ? value : JSON.stringify(value ?? {}, null, 2);
  return (
    <pre className="json-block" style={{ maxHeight }}>
      {content}
    </pre>
  );
}

export function StatusBadge({ value }: { value: string | boolean | number | null | undefined }) {
  const text = String(value ?? "unknown");
  const normalized = text.toLowerCase();
  const status = statusPresentation(normalized);
  return (
    <StatusPill
      state={status.state}
      iconLabel={status.iconLabel}
      className={`badge ${status.tone}`}
    >
      {text}
    </StatusPill>
  );
}

export function InlineMeta({ items }: { items: Array<string | number | null | undefined> }) {
  return (
    <div className="inline-meta">
      {items
        .filter((item) => item !== null && item !== undefined && String(item).trim() !== "")
        .map((item, index) => (
          <span key={`${String(item)}-${index}`}>{item}</span>
        ))}
    </div>
  );
}

export function ActionError({
  message,
  onDismiss,
}: {
  message: string;
  onDismiss: () => void;
}) {
  return (
    <Notice
      variant="danger"
      title="Action failed"
      className="alert"
      actions={(
        <Button
          variant="quiet"
          size="small"
          onClick={onDismiss}
          aria-label="Dismiss error"
        >
          <X size={15} aria-hidden="true" />
        </Button>
      )}
    >
      {message}
    </Notice>
  );
}

export function Metric({
  label,
  value,
}: {
  label: string;
  value: string | number;
}) {
  return (
    <div className="metric">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function statusPresentation(normalized: string): {
  state: StatusState;
  tone: "good" | "warn" | "danger" | "neutral";
  iconLabel?: string;
} {
  if (
    /\b(?:not\s+(?:ready|healthy|eligible|available)|unhealthy|ineligible)\b/.test(
      normalized,
    )
  ) {
    return { state: "blocked", tone: "danger" };
  }
  if (
    ["fail", "denied", "error", "blocked", "rejected", "unavailable"].some(
      (value) => normalized.includes(value),
    )
  ) {
    return { state: "blocked", tone: "danger" };
  }
  if (
    ["degraded", "warning", "caution", "sparse", "missing", "partial"].some(
      (value) => normalized.includes(value),
    )
  ) {
    return { state: "caution", tone: "warn" };
  }
  if (
    ["pending", "queued", "loading", "running", "waiting", "unknown"].some(
      (value) => normalized.includes(value),
    )
  ) {
    return { state: "waiting", tone: "warn" };
  }
  if (
    normalized === "true"
    || [
      "healthy",
      "enabled",
      "available",
      "ready",
      "success",
      "complete",
      "done",
      "eligible",
      "measured",
      "evidence recorded",
      "pass",
      "ok",
    ].some((value) => normalized.includes(value))
  ) {
    return { state: "healthy", tone: "good" };
  }
  if (
    ["disabled", "paused", "inactive", "off", "false"].some((value) =>
      normalized.includes(value),
    )
  ) {
    return { state: "inactive", tone: "neutral" };
  }
  return {
    state: "inactive",
    tone: "neutral",
    iconLabel: "Information",
  };
}
