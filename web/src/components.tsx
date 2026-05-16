import { ReactNode } from "react";

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
    <section id={id} className={`panel ${className}`} aria-labelledby={id ? `${id}-title` : undefined}>
      <div className="panel-head">
        <h2 id={id ? `${id}-title` : undefined}>
          {icon}
          {title}
        </h2>
        {actions ? <div className="panel-actions">{actions}</div> : null}
      </div>
      {children}
    </section>
  );
}

export function Field({
  label,
  hint,
  children
}: {
  label: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <label className="field">
      <span>{label}</span>
      {children}
      {hint ? <small>{hint}</small> : null}
    </label>
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
  const tone =
    normalized.includes("fail") || normalized.includes("denied") || normalized.includes("error")
      ? "danger"
      : normalized.includes("pending") || normalized.includes("blocked") || normalized.includes("queued")
        ? "warn"
        : normalized.includes("running") || normalized.includes("enabled") || normalized === "true"
          ? "good"
          : "neutral";
  return <span className={`badge ${tone}`}>{text}</span>;
}

export function EmptyState({ children }: { children: ReactNode }) {
  return <p className="empty-state">{children}</p>;
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
