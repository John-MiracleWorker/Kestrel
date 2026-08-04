import type { ComponentType } from "react";
import { EmptyState, Metric } from "../components";
import type { Capability, CapabilitySnapshot } from "../types";
import { capabilityKindLabel, capabilityKindOrder } from "./extendUtils";

export type CapabilityRowComponent = ComponentType<{
  capability: Capability;
  pending: boolean;
  onChange: (capability: Capability, enabled: boolean) => Promise<void>;
}>;

export function CapabilityOverview({
  capabilitySnapshot,
  filteredCapabilities,
  capabilityPending,
  capabilitySearch,
  onCapabilitySearchChange,
  capabilityKindFilter,
  onCapabilityKindFilterChange,
  capabilityStateFilter,
  onCapabilityStateFilterChange,
  onCapabilityEnabledChange,
  capabilityRow: CapabilityRow,
}: {
  capabilitySnapshot: CapabilitySnapshot;
  filteredCapabilities: Capability[];
  capabilityPending: Set<string>;
  capabilitySearch: string;
  onCapabilitySearchChange: (value: string) => void;
  capabilityKindFilter: "all" | Capability["kind"];
  onCapabilityKindFilterChange: (value: "all" | Capability["kind"]) => void;
  capabilityStateFilter: string;
  onCapabilityStateFilterChange: (value: string) => void;
  onCapabilityEnabledChange: (
    capability: Capability,
    enabled: boolean,
  ) => Promise<void>;
  capabilityRow: CapabilityRowComponent;
}) {
  return (
    <section
      className="section capability-overview"
      id="capabilities"
      aria-labelledby="capabilities-title"
    >
      <div className="section-head">
        <h2 id="capabilities-title">Capabilities</h2>
        <p>
          Turn individual tools, MCP servers and their tools, and skills on or
          off. Changes persist immediately.
        </p>
        <span className="anchor">/api/capabilities · future invocations</span>
      </div>
      <div className="section-body">
        <div
          className="metric-grid settings-metrics capability-metrics"
          aria-label="Capability counts"
        >
          <Metric label="Total" value={capabilitySnapshot.counts.total} />
          <Metric
            label="Configured on"
            value={capabilitySnapshot.counts.configured_enabled}
          />
          <Metric
            label="Effective on"
            value={capabilitySnapshot.counts.effective_enabled}
          />
          <Metric label="Blocked" value={capabilitySnapshot.counts.blocked} />
        </div>
        <div className="section-row-group capability-toolbar">
          <label>
            Search capabilities
            <input
              className="input"
              type="search"
              value={capabilitySearch}
              onChange={(event) => onCapabilitySearchChange(event.target.value)}
              placeholder="Name, ID, source, or parent"
            />
          </label>
          <label>
            Kind
            <select
              className="select"
              value={capabilityKindFilter}
              onChange={(event) =>
                onCapabilityKindFilterChange(
                  event.target.value as "all" | Capability["kind"],
                )
              }
            >
              <option value="all">All kinds</option>
              <option value="tool">Tools</option>
              <option value="mcp_server">MCP servers</option>
              <option value="skill">Skills</option>
            </select>
          </label>
          <label>
            State
            <select
              className="select"
              value={capabilityStateFilter}
              onChange={(event) =>
                onCapabilityStateFilterChange(event.target.value)
              }
            >
              <option value="all">All states</option>
              <option value="active">Effective on</option>
              <option value="off">Configured off</option>
              <option value="blocked">Blocked</option>
            </select>
          </label>
        </div>
        <div className="capability-groups" aria-live="polite">
          {filteredCapabilities.length === 0 ? (
            <EmptyState>
              No capabilities match the current filters.
            </EmptyState>
          ) : (
            capabilityKindOrder().map((kind) => {
              const rows = filteredCapabilities.filter(
                (capability) => capability.kind === kind,
              );
              if (rows.length === 0) return null;
              const groupId = `capability-group-${kind}`;
              return (
                <section
                  className="capability-group"
                  key={kind}
                  aria-labelledby={groupId}
                >
                  <div className="capability-group-head">
                    <h3 id={groupId}>{capabilityKindLabel(kind)}</h3>
                    <span>{rows.length}</span>
                  </div>
                  <div className="capability-list">
                    {rows.map((capability) => (
                      <CapabilityRow
                        key={capability.key}
                        capability={capability}
                        pending={capabilityPending.has(capability.key)}
                        onChange={onCapabilityEnabledChange}
                      />
                    ))}
                  </div>
                </section>
              );
            })
          )}
        </div>
      </div>
    </section>
  );
}
