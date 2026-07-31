import { getJson, putJson } from "../api";
import type { ProjectedSetting, SettingMutationResult, SettingsProjection } from "./types";

export function fetchSettingsProjection(): Promise<SettingsProjection> {
  return getJson<SettingsProjection>("/api/settings");
}

export function commitSettingValue(
  setting: ProjectedSetting,
  value: unknown,
): Promise<SettingMutationResult> {
  if (!setting.revision) {
    return Promise.reject(new Error("setting_revision_unavailable"));
  }
  return putJson<SettingMutationResult>(
    `/api/settings/${encodeURIComponent(setting.id)}`,
    {
      value,
      expected_revision: setting.revision,
    },
  );
}
