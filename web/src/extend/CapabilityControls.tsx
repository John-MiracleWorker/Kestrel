import { InlineMeta, StatusBadge } from "../components";
import type { Capability } from "../types";
import { capabilityDomId, formatCapabilityBlocker } from "./extendUtils";

export type CapabilityChangeHandler = (
  capability: Capability,
  enabled: boolean,
) => Promise<void>;

export function CapabilitySwitch({
  capability,
  pending,
  onChange,
  describedBy,
  compact = false,
}: {
  capability: Capability;
  pending: boolean;
  onChange: CapabilityChangeHandler;
  describedBy?: string;
  compact?: boolean;
}) {
  const action = capability.configured_enabled ? "Disable" : "Enable";
  return (
    <label className={`capability-toggle ${compact ? "compact" : ""}`}>
      <span>
        {pending
          ? "Saving…"
          : capability.configured_enabled
            ? "On"
            : "Off"}
      </span>
      <span className="toggle">
        <input
          type="checkbox"
          role="switch"
          aria-label={`${action} ${capability.name}`}
          aria-describedby={describedBy}
          aria-checked={capability.configured_enabled}
          checked={capability.configured_enabled}
          disabled={pending}
          onChange={(event) =>
            void onChange(capability, event.currentTarget.checked)
          }
        />
        <span className="track">
          <span className="thumb"></span>
        </span>
      </span>
    </label>
  );
}

export function CapabilityRow({
  capability,
  pending,
  onChange,
}: {
  capability: Capability;
  pending: boolean;
  onChange: CapabilityChangeHandler;
}) {
  const rowId = capabilityDomId(capability.key);
  const titleId = `${rowId}-title`;
  const blockerId =
    capability.blocked_by.length > 0 ? `${rowId}-blockers` : undefined;
  const needsReauthorization =
    capability.configured_enabled &&
    capability.blocked_by.includes("resource_changed");
  return (
    <article
      className="capability-row"
      aria-labelledby={titleId}
      aria-busy={pending}
    >
      <div className="capability-row-copy">
        <div className="capability-row-title">
          <strong id={titleId}>{capability.name}</strong>
          <code>{capability.id}</code>
        </div>
        <p>{capability.description}</p>
        <InlineMeta
          items={[
            capability.source,
            capability.parent_key,
            capability.enablement_flag,
            capability.status,
          ]}
        />
        {blockerId && (
          <p className="capability-blockers" id={blockerId}>
            <strong>Blocked by:</strong>{" "}
            {capability.blocked_by.map(formatCapabilityBlocker).join(", ")}
          </p>
        )}
      </div>
      <div className="capability-row-status">
        <div
          className="capability-badges"
          aria-label={`${capability.name} policy`}
        >
          <StatusBadge value={capability.risk} />
          <StatusBadge
            value={capability.requires_approval ? "approval required" : "direct"}
          />
          <StatusBadge
            value={capability.effective_enabled ? "effective on" : "effective off"}
          />
        </div>
        <CapabilitySwitch
          capability={capability}
          pending={pending}
          onChange={onChange}
          describedBy={blockerId}
        />
        {needsReauthorization && (
          <button
            type="button"
            className="btn subtle"
            disabled={pending}
            onClick={() => void onChange(capability, true)}
          >
            Reauthorize
          </button>
        )}
      </div>
    </article>
  );
}
