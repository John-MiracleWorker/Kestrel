import type { ProjectedSetting, SettingsProjection } from "./types";

export const blockedWebSearchSetting: ProjectedSetting = {
  id: "tools.web_search.enabled",
  key: "allow_web",
  category: "Safety and permissions",
  type: "boolean",
  configured_value: true,
  effective_value: false,
  blockers: ["capability:network_disabled"],
  authority_impact: "grants_authority",
  privacy_impact: "network_egress",
  applies: "new_runs",
  revision: "rev-1",
  provenance: "runtime",
  undo_available: true,
  allowed_values: null,
  allowed_range: null,
  restart_required: false,
  writable: true,
  requires_approval: true,
};

export const temperatureSetting: ProjectedSetting = {
  id: "models.temperature",
  key: "temperature",
  category: "Models and providers",
  type: "number",
  configured_value: 0.2,
  effective_value: 0.2,
  blockers: [],
  authority_impact: "none",
  privacy_impact: "none",
  applies: "new_runs",
  revision: "rev-1",
  provenance: "runtime",
  undo_available: true,
  allowed_values: null,
  allowed_range: [0, 2],
  restart_required: false,
  writable: true,
  requires_approval: false,
};

export const settingsProjectionFixture: SettingsProjection = {
  schema: "kestrel.effective_settings.v1",
  revision: "rev-1",
  categories: ["Safety and permissions", "Models and providers"],
  items: [blockedWebSearchSetting, temperatureSetting],
  items_by_id: {
    "tools.web_search.enabled": blockedWebSearchSetting,
    "models.temperature": temperatureSetting,
  },
  counts: { total: 2, blocked: 1, restart_required: 0 },
};

export const jsonResponse = (payload: unknown, status = 200): Response =>
  new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
