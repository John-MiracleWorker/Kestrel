import type { FormEvent } from "react";
import { GitBranch } from "lucide-react";
import { Field, InlineMeta, JsonBlock, Panel } from "../components";
import type { Plugin, PluginReviewReport } from "../types";
import {
  pluginBlockers,
  pluginDependencySummary,
  pluginIsolationSummary,
  pluginReviewName,
} from "./extendUtils";

export function PluginsPanel({
  plugins,
  pluginSource,
  pluginRef,
  pluginEnable,
  pluginResult,
  pluginReview,
  pluginUpdateReviews,
  reviewedCurrentPlugin,
  pluginEnableBlockers,
  onPluginSourceChange,
  onPluginRefChange,
  onPluginEnableChange,
  onReview,
  onInstall,
  onPluginAction,
}: {
  plugins: Plugin[];
  pluginSource: string;
  pluginRef: string;
  pluginEnable: boolean;
  pluginResult: Record<string, unknown> | null;
  pluginReview: PluginReviewReport | null;
  pluginUpdateReviews: Record<string, string>;
  reviewedCurrentPlugin: boolean;
  pluginEnableBlockers: string[];
  onPluginSourceChange: (value: string) => void;
  onPluginRefChange: (value: string) => void;
  onPluginEnableChange: (value: boolean) => void;
  onReview: (event: FormEvent) => Promise<void> | void;
  onInstall: () => Promise<void> | void;
  onPluginAction: (
    plugin: Plugin,
    action: "enable" | "disable" | "update" | "remove",
  ) => Promise<void> | void;
}) {
  return (
    <Panel title="Plugins" icon={<GitBranch size={19} />}>
      <form onSubmit={onReview} className="inline-form">
        <Field label="GitHub source">
          <input
            value={pluginSource}
            onChange={(event) => onPluginSourceChange(event.target.value)}
          />
        </Field>
        <Field label="Ref">
          <input
            value={pluginRef}
            onChange={(event) => onPluginRefChange(event.target.value)}
          />
        </Field>
        <label className="check-row">
          <input
            type="checkbox"
            checked={pluginEnable}
            disabled={!reviewedCurrentPlugin || pluginEnableBlockers.length > 0}
            onChange={(event) => onPluginEnableChange(event.target.checked)}
          />
          <span>Enable after install</span>
        </label>
        <button type="submit" disabled={!pluginSource.trim()}>
          Review
        </button>
        <button
          type="button"
          disabled={
            !pluginSource.trim() ||
            !reviewedCurrentPlugin ||
            (pluginEnable && pluginEnableBlockers.length > 0)
          }
          onClick={() => void onInstall()}
        >
          Install
        </button>
      </form>
      {reviewedCurrentPlugin && pluginReview && (
        <div className="data-row">
          <strong>Review: {pluginReviewName(pluginReview)}</strong>
          <InlineMeta
            items={[
              String(pluginReview.risk_report.risk ?? "medium"),
              pluginReview.commit_sha.slice(0, 12),
            ]}
          />
          <p>Dependencies: {pluginDependencySummary(pluginReview)}</p>
          <p>Isolation: {pluginIsolationSummary(pluginReview)}</p>
          <p>
            Provenance:{" "}
            {String(pluginReview.provenance_review?.status ?? "unverified")}
          </p>
          <p>
            Compatibility:{" "}
            {String(pluginReview.compatibility_review?.status ?? "unknown")}
          </p>
          {pluginReview.authority_delta?.added.length ? (
            <p>Added authority: {pluginReview.authority_delta.added.join(", ")}</p>
          ) : null}
          {pluginEnableBlockers.length > 0 && (
            <InlineMeta items={pluginEnableBlockers} />
          )}
        </div>
      )}
      {plugins.map((plugin) => (
        <div className="data-row" key={plugin.id}>
          <strong>{plugin.name}</strong>
          <InlineMeta
            items={[
              plugin.id,
              plugin.format,
              plugin.install_status,
              plugin.enabled ? "enabled" : "disabled",
            ]}
          />
          <p>{plugin.description}</p>
          {pluginBlockers(plugin).length > 0 && (
            <InlineMeta items={pluginBlockers(plugin)} />
          )}
          <div className="page-actions">
            <button
              type="button"
              disabled={!plugin.enabled && pluginBlockers(plugin).length > 0}
              onClick={() =>
                void onPluginAction(
                  plugin,
                  plugin.enabled ? "disable" : "enable",
                )
              }
            >
              {plugin.enabled ? "Disable" : "Enable"}
            </button>
            <button
              type="button"
              onClick={() => void onPluginAction(plugin, "update")}
            >
              {pluginUpdateReviews[plugin.id]
                ? "Apply reviewed update"
                : "Review update"}
            </button>
            <button
              type="button"
              className="btn danger"
              onClick={() => void onPluginAction(plugin, "remove")}
            >
              Remove
            </button>
          </div>
        </div>
      ))}
      {pluginResult && <JsonBlock value={pluginResult} maxHeight="320px" />}
    </Panel>
  );
}
