export type SettingType =
  | "boolean"
  | "enum"
  | "number"
  | "string"
  | "path"
  | "duration";

export type ProjectedSetting = Readonly<{
  id: string;
  key: string | null;
  category: string;
  type: string;
  configured_value: unknown;
  effective_value: unknown;
  blockers: readonly string[];
  authority_impact: string;
  privacy_impact: string;
  applies: string;
  revision: string | null;
  provenance: string;
  undo_available: boolean;
  allowed_values: readonly string[] | null;
  allowed_range: readonly [number, number] | null;
  restart_required: boolean;
  writable: boolean;
  requires_approval: boolean;
}>;

export type SettingsProjection = Readonly<{
  schema: string;
  revision: string | null;
  categories: string[];
  items: ProjectedSetting[];
  items_by_id: Record<string, ProjectedSetting>;
  counts: Readonly<{
    total: number;
    blocked: number;
    restart_required: number;
  }>;
}>;

export type SettingMutationResult = Readonly<{
  schema: string;
  setting: ProjectedSetting;
  projection?: SettingsProjection;
  revision: string | null;
  store_revision: string | null;
  undo_available: boolean;
  undo: Readonly<{
    available: boolean;
    setting_id: string;
    key: string | null;
  }>;
  revoked_approvals: number;
  authority_changes: unknown[];
}>;

export type SettingConflict = Readonly<{
  current: ProjectedSetting;
}>;

export function isSettingConflict(error: unknown): SettingConflict | null {
  if (
    typeof error !== "object" ||
    error === null ||
    !("status" in error) ||
    (error as { status: unknown }).status !== 409
  ) {
    return null;
  }
  const message = "message" in error ? String((error as { message: unknown }).message) : "";
  try {
    const detail = JSON.parse(message) as {
      error?: unknown;
      current?: unknown;
    };
    if (detail.error === "setting_revision_conflict" && detail.current) {
      return { current: detail.current as ProjectedSetting };
    }
  } catch {
    return null;
  }
  return null;
}

export function formatBlocker(blocker: string): string {
  if (blocker.startsWith("capability:")) {
    const capability = blocker.slice("capability:".length);
    if (capability === "network_disabled") {
      return "Network capability is disabled";
    }
    const readable = capability.replace(/_disabled$/, "").replace(/_/g, " ");
    return `${readable} capability is disabled`;
  }
  return blocker.replace(/_/g, " ");
}

export function formatApplies(setting: ProjectedSetting): string {
  if (setting.restart_required || setting.applies === "restart") {
    return "Applies after restart";
  }
  if (setting.applies === "immediate") {
    return "Applies immediately";
  }
  return "Applies to new runs";
}
