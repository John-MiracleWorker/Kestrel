import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { Cpu, GitBranch, RefreshCw, Route, ServerCog } from "lucide-react";
import { EmptyState, Field, InlineMeta, JsonBlock, Panel, StatusBadge } from "../components";
import {
  getModelTargets,
  getProviderProfiles,
  getRoutePolicies,
  getRoutingStatus,
  getRunRouting,
  previewTaskRoute,
  putModelTarget,
  putProviderProfile
} from "./api";
import type {
  ModelTarget,
  ModelTargetDraft,
  ProviderProfile,
  ProviderProfileDraft,
  RoutePolicy,
  RoutingHealth,
  RoutingLocality,
  RoutingRunReport,
  RoutingStatus,
  TaskRoutePreview
} from "./types";

const emptyProviderDraft: ProviderProfileDraft = {
  profile_id: "",
  display_name: "",
  adapter: "openai-compatible",
  base_url: "",
  secret_ref: "",
  enabled: true,
  locality: "local",
  trust_class: "standard",
  max_concurrency: 1,
  metadata: {}
};

const emptyTargetDraft: ModelTargetDraft = {
  target_id: "",
  provider_profile_id: "",
  provider: "openai-compatible",
  model: "",
  enabled: true,
  locality: "local",
  trust_class: "standard",
  capability_tags: ["worker"],
  role_affinities: ["worker"],
  task_family_affinities: [],
  max_context_tokens: 32_000,
  supports_tools: true,
  supports_json: false,
  supports_vision: false,
  supports_reasoning: false,
  supports_streaming: true,
  quality_tier: 2,
  latency_tier: 2,
  operator_priority: 0,
  estimated_cost_usd: 0,
  health: "unknown",
  recent_failure_rate: 0,
  predicted_success: null,
  metadata: {}
};

type RoutingCenterProps = {
  activeRunId?: string | null;
  activeTaskId?: string | null;
  onError?: (message: string) => void;
  onNotice?: (message: string) => void;
};

export function RoutingCenter({
  activeRunId = null,
  activeTaskId = null,
  onError,
  onNotice
}: RoutingCenterProps) {
  const [status, setStatus] = useState<RoutingStatus | null>(null);
  const [providers, setProviders] = useState<ProviderProfile[]>([]);
  const [targets, setTargets] = useState<ModelTarget[]>([]);
  const [policies, setPolicies] = useState<RoutePolicy[]>([]);
  const [runReport, setRunReport] = useState<RoutingRunReport | null>(null);
  const [preview, setPreview] = useState<TaskRoutePreview | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [providerDraft, setProviderDraft] = useState<ProviderProfileDraft>(emptyProviderDraft);
  const [targetDraft, setTargetDraft] = useState<ModelTargetDraft>(emptyTargetDraft);
  const [providerMetadata, setProviderMetadata] = useState("{}");
  const [targetMetadata, setTargetMetadata] = useState("{}");
  const [targetCapabilityTags, setTargetCapabilityTags] = useState("worker");
  const [targetRoleAffinities, setTargetRoleAffinities] = useState("worker");
  const [targetTaskAffinities, setTargetTaskAffinities] = useState("");
  const [previewTaskId, setPreviewTaskId] = useState(activeTaskId ?? "");
  const [previewPolicyId, setPreviewPolicyId] = useState("");
  const [previewDirectTargetId, setPreviewDirectTargetId] = useState("");
  const [previewLocalRequired, setPreviewLocalRequired] = useState(false);
  const [previewBudget, setPreviewBudget] = useState("");

  const reportError = useCallback(
    (error: unknown) => {
      const message = error instanceof Error ? error.message : String(error);
      onError?.(message);
    },
    [onError]
  );

  const refresh = useCallback(async () => {
    setLoading(true);
    const controller = new AbortController();
    try {
      const [nextStatus, nextProviders, nextTargets, nextPolicies] = await Promise.all([
        getRoutingStatus(controller.signal),
        getProviderProfiles(controller.signal),
        getModelTargets(controller.signal),
        getRoutePolicies(controller.signal)
      ]);
      setStatus(nextStatus);
      setProviders(nextProviders);
      setTargets(nextTargets);
      setPolicies(nextPolicies);
      if (activeRunId) {
        setRunReport(await getRunRouting(activeRunId, activeTaskId ?? undefined, controller.signal));
      } else {
        setRunReport(null);
      }
    } catch (error) {
      reportError(error);
    } finally {
      setLoading(false);
    }
    return () => controller.abort();
  }, [activeRunId, activeTaskId, reportError]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (activeTaskId) setPreviewTaskId(activeTaskId);
  }, [activeTaskId]);

  const enabledTargetsByLocality = useMemo(
    () => ({
      local: targets.filter((target) => target.enabled && target.locality === "local").length,
      cloud: targets.filter((target) => target.enabled && target.locality === "cloud").length,
      hybrid: targets.filter((target) => target.enabled && target.locality === "hybrid").length
    }),
    [targets]
  );

  async function saveProvider(event: FormEvent) {
    event.preventDefault();
    setSaving(true);
    try {
      const saved = await putProviderProfile({
        ...providerDraft,
        profile_id: providerDraft.profile_id.trim(),
        display_name: providerDraft.display_name.trim(),
        adapter: providerDraft.adapter.trim(),
        metadata: parseObject(providerMetadata, "Provider metadata")
      });
      setProviderDraft(emptyProviderDraft);
      setProviderMetadata("{}");
      onNotice?.(`Saved provider profile ${saved.display_name}.`);
      await refresh();
    } catch (error) {
      reportError(error);
    } finally {
      setSaving(false);
    }
  }

  async function saveTarget(event: FormEvent) {
    event.preventDefault();
    setSaving(true);
    try {
      const saved = await putModelTarget({
        ...targetDraft,
        target_id: targetDraft.target_id.trim(),
        provider_profile_id: targetDraft.provider_profile_id.trim(),
        provider: targetDraft.provider.trim(),
        model: targetDraft.model.trim(),
        capability_tags: parseCsv(targetCapabilityTags),
        role_affinities: parseCsv(targetRoleAffinities),
        task_family_affinities: parseCsv(targetTaskAffinities),
        metadata: parseObject(targetMetadata, "Target metadata")
      });
      setTargetDraft(emptyTargetDraft);
      setTargetMetadata("{}");
      setTargetCapabilityTags("worker");
      setTargetRoleAffinities("worker");
      setTargetTaskAffinities("");
      onNotice?.(`Saved model target ${saved.target_id}.`);
      await refresh();
    } catch (error) {
      reportError(error);
    } finally {
      setSaving(false);
    }
  }

  async function runPreview(event: FormEvent) {
    event.preventDefault();
    setSaving(true);
    try {
      const budget = optionalNumber(previewBudget, "Maximum cost");
      const result = await previewTaskRoute(previewTaskId.trim(), {
        policyId: previewPolicyId,
        directTargetId: previewDirectTargetId,
        localRequired: previewLocalRequired,
        maximumCostUsd: budget
      });
      setPreview(result);
      onNotice?.(`Previewed route for ${result.task.title}.`);
    } catch (error) {
      reportError(error);
    } finally {
      setSaving(false);
    }
  }

  function editProvider(profile: ProviderProfile) {
    setProviderDraft({
      profile_id: profile.profile_id,
      display_name: profile.display_name,
      adapter: profile.adapter,
      base_url: "",
      secret_ref: "",
      enabled: profile.enabled,
      locality: profile.locality,
      trust_class: profile.trust_class,
      max_concurrency: profile.max_concurrency,
      metadata: profile.metadata,
      expected_revision: profile.revision
    });
    setProviderMetadata(JSON.stringify(profile.metadata, null, 2));
  }

  function editTarget(target: ModelTarget) {
    setTargetDraft({
      target_id: target.target_id,
      provider_profile_id: target.provider_profile_id,
      provider: target.provider,
      model: target.model,
      enabled: target.enabled,
      locality: target.locality,
      trust_class: target.trust_class,
      capability_tags: target.capability_tags,
      role_affinities: target.role_affinities,
      task_family_affinities: target.task_family_affinities,
      max_context_tokens: target.max_context_tokens,
      supports_tools: target.supports_tools,
      supports_json: target.supports_json,
      supports_vision: target.supports_vision,
      supports_reasoning: target.supports_reasoning,
      supports_streaming: target.supports_streaming,
      quality_tier: target.quality_tier,
      latency_tier: target.latency_tier,
      operator_priority: target.operator_priority,
      estimated_cost_usd: target.estimated_cost_usd,
      health: target.health,
      recent_failure_rate: target.recent_failure_rate,
      predicted_success: target.predicted_success,
      metadata: target.metadata,
      expected_revision: target.revision
    });
    setTargetCapabilityTags(target.capability_tags.join(", "));
    setTargetRoleAffinities(target.role_affinities.join(", "));
    setTargetTaskAffinities(target.task_family_affinities.join(", "));
    setTargetMetadata(JSON.stringify(target.metadata, null, 2));
  }

  return (
    <section id="routing" className="content-grid wide-left" aria-label="Adaptive Flock Routing Center">
      <Panel
        title="Adaptive Flock"
        icon={<Route size={19} />}
        actions={
          <button type="button" onClick={() => void refresh()} disabled={loading}>
            <RefreshCw size={15} /> Refresh
          </button>
        }
      >
        {status ? (
          <>
            <div className="metric-grid">
              <RoutingMetric label="Runtime" value={status.runtime.enabled ? status.runtime.mode : "off"} />
              <RoutingMetric label="Policy" value={status.runtime.policy_id} />
              <RoutingMetric label="Providers" value={status.counts.provider_profiles} />
              <RoutingMetric label="Targets" value={status.counts.model_targets} />
            </div>
            <InlineMeta
              items={[
                `schema v${status.routing_schema_version}`,
                `${enabledTargetsByLocality.local} local`,
                `${enabledTargetsByLocality.cloud} cloud`,
                `${enabledTargetsByLocality.hybrid} hybrid`
              ]}
            />
            <p className="muted">
              Routing is launch-controlled. Inventory changes affect future attempts and never expand a task&apos;s tools,
              workspace, approvals, or privacy policy.
            </p>
          </>
        ) : (
          <EmptyState>{loading ? "Loading routing status…" : "Routing status unavailable."}</EmptyState>
        )}
      </Panel>

      <Panel title="Route Preview" icon={<GitBranch size={19} />}>
        <form className="stack-form" onSubmit={runPreview}>
          <div className="field-row">
            <Field label="Task ID">
              <input value={previewTaskId} onChange={(event) => setPreviewTaskId(event.target.value)} required />
            </Field>
            <Field label="Policy">
              <select value={previewPolicyId} onChange={(event) => setPreviewPolicyId(event.target.value)}>
                <option value="">Runtime policy</option>
                {policies.map((policy) => (
                  <option key={policy.policy_id} value={policy.policy_id}>
                    {policy.policy_id}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Direct target">
              <select
                value={previewDirectTargetId}
                onChange={(event) => setPreviewDirectTargetId(event.target.value)}
              >
                <option value="">Automatic</option>
                {targets.map((target) => (
                  <option key={target.target_id} value={target.target_id}>
                    {target.target_id}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Maximum cost (USD)">
              <input
                value={previewBudget}
                onChange={(event) => setPreviewBudget(event.target.value)}
                inputMode="decimal"
                placeholder="No hard budget"
              />
            </Field>
          </div>
          <label className="check-row">
            <input
              type="checkbox"
              checked={previewLocalRequired}
              onChange={(event) => setPreviewLocalRequired(event.target.checked)}
            />
            <span>Require a local target</span>
          </label>
          <button type="submit" disabled={saving || !previewTaskId.trim()}>
            Preview decision
          </button>
        </form>
        {preview ? <PreviewResult preview={preview} /> : <EmptyState>No task preview calculated yet.</EmptyState>}
      </Panel>

      <Panel title="Provider Profiles" icon={<ServerCog size={19} />}>
        <form className="stack-form" onSubmit={saveProvider}>
          <div className="field-row">
            <Field label="Profile ID">
              <input
                value={providerDraft.profile_id}
                onChange={(event) => setProviderDraft({ ...providerDraft, profile_id: event.target.value })}
                required
              />
            </Field>
            <Field label="Display name">
              <input
                value={providerDraft.display_name}
                onChange={(event) => setProviderDraft({ ...providerDraft, display_name: event.target.value })}
                required
              />
            </Field>
            <Field label="Adapter">
              <input
                value={providerDraft.adapter}
                onChange={(event) => setProviderDraft({ ...providerDraft, adapter: event.target.value })}
                required
              />
            </Field>
            <LocalityField
              value={providerDraft.locality}
              onChange={(locality) => setProviderDraft({ ...providerDraft, locality })}
            />
          </div>
          <div className="field-row">
            <Field label="Base URL" hint="Leave blank to use the adapter default.">
              <input
                value={providerDraft.base_url}
                onChange={(event) => setProviderDraft({ ...providerDraft, base_url: event.target.value })}
              />
            </Field>
            <Field label="Secret reference" hint="Use secret://name. The backend never returns it.">
              <input
                type="password"
                autoComplete="new-password"
                value={providerDraft.secret_ref}
                onChange={(event) => setProviderDraft({ ...providerDraft, secret_ref: event.target.value })}
              />
            </Field>
            <Field label="Trust class">
              <input
                value={providerDraft.trust_class}
                onChange={(event) => setProviderDraft({ ...providerDraft, trust_class: event.target.value })}
              />
            </Field>
            <Field label="Maximum concurrency">
              <input
                type="number"
                min={1}
                max={1024}
                value={providerDraft.max_concurrency}
                onChange={(event) =>
                  setProviderDraft({ ...providerDraft, max_concurrency: Number(event.target.value) })
                }
              />
            </Field>
          </div>
          <Field label="Metadata JSON">
            <textarea value={providerMetadata} onChange={(event) => setProviderMetadata(event.target.value)} rows={3} />
          </Field>
          <label className="check-row">
            <input
              type="checkbox"
              checked={providerDraft.enabled}
              onChange={(event) => setProviderDraft({ ...providerDraft, enabled: event.target.checked })}
            />
            <span>Provider profile enabled</span>
          </label>
          <button type="submit" disabled={saving || !providerDraft.profile_id.trim()}>
            Save provider
          </button>
        </form>
        <div className="list separated">
          {providers.length === 0 ? (
            <EmptyState>No provider profiles configured.</EmptyState>
          ) : (
            providers.map((profile) => (
              <button
                type="button"
                className="data-row"
                key={profile.profile_id}
                onClick={() => editProvider(profile)}
              >
                <strong>{profile.display_name}</strong>
                <InlineMeta
                  items={[
                    profile.profile_id,
                    profile.adapter,
                    profile.locality,
                    `revision ${profile.revision}`
                  ]}
                />
                <StatusBadge value={profile.enabled ? "enabled" : "disabled"} />
                <span>{profile.secret_configured ? "Credential configured" : "No credential configured"}</span>
              </button>
            ))
          )}
        </div>
      </Panel>

      <Panel title="Model Targets" icon={<Cpu size={19} />}>
        <form className="stack-form" onSubmit={saveTarget}>
          <div className="field-row">
            <Field label="Target ID">
              <input
                value={targetDraft.target_id}
                onChange={(event) => setTargetDraft({ ...targetDraft, target_id: event.target.value })}
                required
              />
            </Field>
            <Field label="Provider profile">
              <select
                value={targetDraft.provider_profile_id}
                onChange={(event) => {
                  const profile = providers.find((item) => item.profile_id === event.target.value);
                  setTargetDraft({
                    ...targetDraft,
                    provider_profile_id: event.target.value,
                    provider: profile?.adapter ?? targetDraft.provider,
                    locality: profile?.locality ?? targetDraft.locality
                  });
                }}
                required
              >
                <option value="">Select profile</option>
                {providers.map((profile) => (
                  <option key={profile.profile_id} value={profile.profile_id}>
                    {profile.display_name}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Model">
              <input
                value={targetDraft.model}
                onChange={(event) => setTargetDraft({ ...targetDraft, model: event.target.value })}
                required
              />
            </Field>
            <LocalityField
              value={targetDraft.locality}
              onChange={(locality) => setTargetDraft({ ...targetDraft, locality })}
            />
          </div>
          <div className="field-row">
            <Field label="Context tokens">
              <input
                type="number"
                min={1}
                value={targetDraft.max_context_tokens ?? ""}
                onChange={(event) =>
                  setTargetDraft({ ...targetDraft, max_context_tokens: nullableNumber(event.target.value) })
                }
              />
            </Field>
            <Field label="Quality tier">
              <input
                type="number"
                min={1}
                max={5}
                value={targetDraft.quality_tier}
                onChange={(event) => setTargetDraft({ ...targetDraft, quality_tier: Number(event.target.value) })}
              />
            </Field>
            <Field label="Latency tier">
              <input
                type="number"
                min={1}
                max={5}
                value={targetDraft.latency_tier}
                onChange={(event) => setTargetDraft({ ...targetDraft, latency_tier: Number(event.target.value) })}
              />
            </Field>
            <HealthField
              value={targetDraft.health}
              onChange={(health) => setTargetDraft({ ...targetDraft, health })}
            />
          </div>
          <div className="field-row">
            <Field label="Capability tags">
              <input value={targetCapabilityTags} onChange={(event) => setTargetCapabilityTags(event.target.value)} />
            </Field>
            <Field label="Role affinities">
              <input value={targetRoleAffinities} onChange={(event) => setTargetRoleAffinities(event.target.value)} />
            </Field>
            <Field label="Task-family affinities">
              <input value={targetTaskAffinities} onChange={(event) => setTargetTaskAffinities(event.target.value)} />
            </Field>
          </div>
          <div className="check-row">
            {([
              ["supports_tools", "Tools"],
              ["supports_json", "Structured output"],
              ["supports_vision", "Vision"],
              ["supports_reasoning", "Reasoning"],
              ["supports_streaming", "Streaming"]
            ] as const).map(([key, label]) => (
              <label key={key}>
                <input
                  type="checkbox"
                  checked={targetDraft[key]}
                  onChange={(event) => setTargetDraft({ ...targetDraft, [key]: event.target.checked })}
                />
                <span>{label}</span>
              </label>
            ))}
          </div>
          <Field label="Metadata JSON">
            <textarea value={targetMetadata} onChange={(event) => setTargetMetadata(event.target.value)} rows={3} />
          </Field>
          <label className="check-row">
            <input
              type="checkbox"
              checked={targetDraft.enabled}
              onChange={(event) => setTargetDraft({ ...targetDraft, enabled: event.target.checked })}
            />
            <span>Model target enabled</span>
          </label>
          <button
            type="submit"
            disabled={saving || !targetDraft.target_id.trim() || !targetDraft.provider_profile_id}
          >
            Save target
          </button>
        </form>
        <div className="list separated">
          {targets.length === 0 ? (
            <EmptyState>No model targets configured.</EmptyState>
          ) : (
            targets.map((target) => (
              <button type="button" className="data-row" key={target.target_id} onClick={() => editTarget(target)}>
                <strong>{target.target_id}</strong>
                <InlineMeta
                  items={[
                    target.model,
                    target.provider_profile_id,
                    target.locality,
                    `quality ${target.quality_tier}`,
                    `revision ${target.revision}`
                  ]}
                />
                <StatusBadge value={target.health} />
                <span>{target.capability_tags.join(" · ") || "No capability tags"}</span>
              </button>
            ))
          )}
        </div>
      </Panel>

      <Panel title="Policies & Recent Routes" icon={<GitBranch size={19} />}>
        <div className="list separated">
          {policies.map((policy) => (
            <div className="data-row" key={policy.policy_id}>
              <strong>{policy.policy_id}</strong>
              <InlineMeta
                items={[
                  `revision ${policy.revision}`,
                  `quality ${policy.quality_weight}`,
                  `cost ${policy.cost_weight}`,
                  `failure ${policy.failure_weight}`
                ]}
              />
              <StatusBadge value={policy.enabled ? "enabled" : "disabled"} />
            </div>
          ))}
          {policies.length === 0 && <EmptyState>No route policies configured.</EmptyState>}
        </div>
        {runReport ? (
          <>
            <InlineMeta
              items={[
                runReport.run_id,
                runReport.task_id,
                `${runReport.decisions.length} decisions`,
                `${runReport.outcomes.length} outcomes`
              ]}
            />
            <JsonBlock value={runReport} maxHeight="420px" />
          </>
        ) : (
          <EmptyState>Select a run to inspect its route history.</EmptyState>
        )}
      </Panel>
    </section>
  );
}

function PreviewResult({ preview }: { preview: TaskRoutePreview }) {
  return (
    <div className="run-detail">
      <h3>{preview.task.title}</h3>
      <InlineMeta
        items={[
          preview.decision.selected_target_id,
          preview.decision.selected_provider,
          preview.decision.selected_model,
          `score ${preview.decision.score.toFixed(3)}`,
          preview.decision.actionable ? "actionable" : "shadow"
        ]}
      />
      <div className="list compact-list">
        {preview.decision.candidates.map((candidate) => (
          <div className="data-row" key={candidate.target_id}>
            <strong>{candidate.target_id}</strong>
            <InlineMeta items={[candidate.provider, candidate.model, candidate.score?.toFixed(3)]} />
            <StatusBadge value={candidate.eligible ? "eligible" : "rejected"} />
            <span>{candidate.reason_codes.join(" · ")}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function RoutingMetric({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="metric">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function LocalityField({
  value,
  onChange
}: {
  value: RoutingLocality;
  onChange: (value: RoutingLocality) => void;
}) {
  return (
    <Field label="Locality">
      <select value={value} onChange={(event) => onChange(event.target.value as RoutingLocality)}>
        <option value="local">local</option>
        <option value="cloud">cloud</option>
        <option value="hybrid">hybrid</option>
      </select>
    </Field>
  );
}

function HealthField({ value, onChange }: { value: RoutingHealth; onChange: (value: RoutingHealth) => void }) {
  return (
    <Field label="Health">
      <select value={value} onChange={(event) => onChange(event.target.value as RoutingHealth)}>
        <option value="unknown">unknown</option>
        <option value="healthy">healthy</option>
        <option value="degraded">degraded</option>
        <option value="open">circuit open</option>
        <option value="unavailable">unavailable</option>
      </select>
    </Field>
  );
}

function parseCsv(value: string): string[] {
  return [...new Set(value.split(",").map((item) => item.trim()).filter(Boolean))];
}

function parseObject(value: string, label: string): Record<string, unknown> {
  const parsed = JSON.parse(value || "{}");
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new Error(`${label} must be a JSON object.`);
  }
  return parsed as Record<string, unknown>;
}

function nullableNumber(value: string): number | null {
  const normalized = value.trim();
  return normalized ? Number(normalized) : null;
}

function optionalNumber(value: string, label: string): number | null {
  const normalized = value.trim();
  if (!normalized) return null;
  const parsed = Number(normalized);
  if (!Number.isFinite(parsed) || parsed < 0) throw new Error(`${label} must be a non-negative number.`);
  return parsed;
}
