import { getJson, postJson, queryString } from "../api";
import type {
  ModelTarget,
  ModelTargetDraft,
  ProviderProfile,
  ProviderProfileDraft,
  RoutePolicy,
  RoutingRunReport,
  RoutingStatus,
  TaskRoutePreview
} from "./types";

export async function getRoutingStatus(signal?: AbortSignal): Promise<RoutingStatus> {
  return getJson<RoutingStatus>("/api/routing/status", { signal });
}

export async function getProviderProfiles(signal?: AbortSignal): Promise<ProviderProfile[]> {
  return getJson<ProviderProfile[]>("/api/routing/providers", { signal });
}

export async function putProviderProfile(draft: ProviderProfileDraft): Promise<ProviderProfile> {
  return postJson<ProviderProfile>("/api/routing/providers", {
    ...draft,
    base_url: optionalText(draft.base_url),
    secret_ref: optionalText(draft.secret_ref),
    expected_revision: draft.expected_revision ?? null
  });
}

export async function getModelTargets(signal?: AbortSignal): Promise<ModelTarget[]> {
  return getJson<ModelTarget[]>("/api/routing/targets", { signal });
}

export async function putModelTarget(draft: ModelTargetDraft): Promise<ModelTarget> {
  return postJson<ModelTarget>("/api/routing/targets", {
    ...draft,
    expected_revision: draft.expected_revision ?? null
  });
}

export async function getRoutePolicies(signal?: AbortSignal): Promise<RoutePolicy[]> {
  return getJson<RoutePolicy[]>("/api/routing/policies", { signal });
}

export async function previewTaskRoute(
  taskId: string,
  options: {
    policyId?: string;
    directTargetId?: string;
    localRequired?: boolean;
    maximumCostUsd?: number | null;
  } = {}
): Promise<TaskRoutePreview> {
  return postJson<TaskRoutePreview>("/api/routing/preview", {
    task_id: taskId,
    policy_id: optionalText(options.policyId ?? ""),
    direct_target_id: optionalText(options.directTargetId ?? ""),
    local_required: options.localRequired ?? false,
    maximum_cost_usd: options.maximumCostUsd ?? null
  });
}

export async function getRunRouting(
  runId: string,
  taskId?: string,
  signal?: AbortSignal
): Promise<RoutingRunReport> {
  return getJson<RoutingRunReport>(
    `/api/runs/${encodeURIComponent(runId)}/routing${queryString({ task_id: taskId })}`,
    { signal }
  );
}

function optionalText(value: string): string | null {
  const normalized = value.trim();
  return normalized ? normalized : null;
}
