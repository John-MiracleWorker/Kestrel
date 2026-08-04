import { Bell } from "lucide-react";
import { EmptyState, InlineMeta, Panel, StatusBadge } from "../components";
import type { Channel } from "../types";

export type AutomateChannelsSlice = {
  channels: Channel[];
  onEditChannel: (channel: Channel) => void;
};

export function ChannelsPanel({ channelsSlice }: { channelsSlice: AutomateChannelsSlice }) {
  const { channels, onEditChannel } = channelsSlice;
  return (
    <Panel
      id="automate-channels"
      title="Delivery channels"
      icon={<Bell size={19} />}
      actions={<StatusBadge value={`${channels.length} configured`} />}
    >
      <p className="muted delivery-truth-note">
        Routine destinations deliver through these connectors with idempotent admission and a
        connector receipt per attempt. Channel configuration and webhook testing live in Settings;
        Edit opens that channel there.
      </p>
      {channels.length === 0 ? (
        <EmptyState>No channels configured. Add one in Settings before selecting a delivery destination.</EmptyState>
      ) : (
        <div className="list compact-list">
          {channels.map((channel) => (
            <article className="data-row" key={channel.id}>
              <div className="run-title">
                <strong>{channel.id}</strong>
                <StatusBadge value={channel.enabled ? "enabled" : "disabled"} />
              </div>
              <InlineMeta items={[
                channel.provider,
                channel.send_enabled ? "send enabled" : "dry-run",
                channel.auto_reply ? "auto reply" : "manual"
              ]} />
              <div className="page-actions">
                <button
                  type="button"
                  aria-label={`Edit ${channel.id} channel in Settings`}
                  onClick={() => onEditChannel(channel)}
                >
                  Edit
                </button>
              </div>
            </article>
          ))}
        </div>
      )}
    </Panel>
  );
}
