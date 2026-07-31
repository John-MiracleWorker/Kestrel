import type {
  Capability,
  CapabilitySnapshot,
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
