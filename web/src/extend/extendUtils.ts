import type {
  Capability,
  CapabilityKind,
  CapabilitySnapshot,
  Plugin,
  PluginReviewReport,
  Tool,
} from "../types";

export function readJson<T>(text: string, fallback: T): T {
  if (!text.trim()) return fallback;
  return JSON.parse(text) as T;
}

export function uniqueStrings(values: string[]): string[] {
  return [...new Set(values)].sort((left, right) =>
    left.localeCompare(right),
  );
}

export function capabilityForTool(
  capabilities: Capability[],
  toolName: string,
): Capability | undefined {
  return capabilities.find(
    (capability) =>
      capability.kind === "tool" && capability.id === toolName,
  );
}

export function capabilityForMcpServer(
  capabilities: Capability[],
  serverId: string,
): Capability | undefined {
  return capabilities.find(
    (capability) =>
      capability.kind === "mcp_server" && capability.id === serverId,
  );
}

export function capabilityForMcpTool(
  capabilities: Capability[],
  serverId: string,
  tool: Tool & { remote_name?: string },
): Capability | undefined {
  const remoteName = tool.remote_name ?? tool.name;
  const registeredName = tool.name.startsWith("mcp.")
    ? tool.name
    : `mcp.${serverId}.${remoteName}`;
  return (
    capabilityForTool(capabilities, registeredName) ??
    capabilityForTool(capabilities, tool.name) ??
    capabilities.find(
      (capability) =>
        capability.kind === "tool" &&
        capability.parent_key === `mcp_server:${serverId}` &&
        [remoteName, registeredName].includes(capability.id),
    )
  );
}

export function capabilityForSkill(
  capabilities: Capability[],
  skillId: string,
): Capability | undefined {
  return capabilities.find(
    (capability) =>
      capability.kind === "skill" && capability.id === skillId,
  );
}

export function isToolEffectivelyEnabled(
  tool: Tool,
  toolPermissions: Record<string, boolean>,
  capabilities: Capability[],
): boolean {
  const capability = capabilityForTool(capabilities, tool.name);
  if (capability) return capability.effective_enabled;
  const flag = tool.enablement_flag;
  if (!flag) {
    return typeof tool.enabled === "boolean" ? tool.enabled : true;
  }
  if (flag in toolPermissions) return toolPermissions[flag];
  return typeof tool.enabled === "boolean" ? tool.enabled : false;
}

export function replaceCapability(
  snapshot: CapabilitySnapshot,
  capability: Capability,
): CapabilitySnapshot {
  const found = snapshot.items.some(
    (item) => item.key === capability.key,
  );
  const items = found
    ? snapshot.items.map((item) =>
        item.key === capability.key ? capability : item,
      )
    : [...snapshot.items, capability];
  return {
    ...snapshot,
    items,
    counts: {
      total: items.length,
      configured_enabled: items.filter(
        (item) => item.configured_enabled,
      ).length,
      effective_enabled: items.filter(
        (item) => item.effective_enabled,
      ).length,
      blocked: items.filter((item) => item.blocked_by.length > 0).length,
    },
  };
}

export function formatCapabilityBlocker(value: string): string {
  return value.replaceAll("_", " ");
}

export function schemaDefault(
  schema?: Record<string, unknown>,
): Record<string, unknown> {
  const properties = schema?.properties;
  if (!properties || typeof properties !== "object") return {};
  return Object.fromEntries(Object.keys(properties).map((key) => [key, ""]));
}

export function stringArray(value: unknown): string[] {
  return Array.isArray(value)
    ? value.map((item) => String(item)).filter(Boolean)
    : [];
}

export function capabilityKindOrder(): CapabilityKind[] {
  return ["mcp_server", "tool", "skill"];
}

export function capabilityKindLabel(kind: CapabilityKind): string {
  if (kind === "mcp_server") return "MCP Servers";
  if (kind === "skill") return "Skills";
  return "Tools";
}

export function capabilityDomId(key: string): string {
  return `capability-${key.replace(/[^a-zA-Z0-9_-]+/g, "-")}`;
}

export function pluginReviewName(review: PluginReviewReport): string {
  return String(review.manifest.id ?? review.source_url);
}

export function pluginDependencySummary(review: PluginReviewReport): string {
  const declared = review.dependency_review.declared;
  if (!declared || typeof declared !== "object" || Array.isArray(declared)) {
    return "none";
  }
  const parts = Object.entries(declared).flatMap(([kind, value]) =>
    stringArray(value).map((item) => `${kind}:${item}`),
  );
  return parts.length ? parts.join(", ") : "none";
}

export function pluginIsolationSummary(review: PluginReviewReport): string {
  const mode = String(review.isolation_review.mode ?? "shared");
  const required = Boolean(review.isolation_review.required);
  const available = Boolean(review.isolation_review.available);
  return `${mode}${required ? " required" : ""}${available ? "" : " unavailable"}`;
}

export function pluginBlockers(plugin: Plugin): string[] {
  return stringArray(plugin.risk_report.enable_blockers);
}
