import { z } from "zod";

const loopbackHosts = new Set(["127.0.0.1", "[::1]"]);

export const DESKTOP_APP_SCHEME = "kestrel";
export const DESKTOP_APP_HOST = "app";
export const DESKTOP_APP_ORIGIN =
  `${DESKTOP_APP_SCHEME}://${DESKTOP_APP_HOST}` as const;
export const DESKTOP_APP_ENTRY_URL =
  `${DESKTOP_APP_ORIGIN}/index.html` as const;

export const desktopLifecycleStateSchema = z.enum([
  "verifying",
  "starting",
  "ready",
  "stopping",
  "restarting",
  "recovery"
]);

export const desktopRecoveryReasonSchema = z.enum([
  "sidecar_unavailable",
  "sidecar_unverified",
  "profile_conflict",
  "version_incompatible",
  "reconciliation_required"
]);

export const desktopRecoverySchema = z
  .object({
    reason: desktopRecoveryReasonSchema
  })
  .strict();

export function isLoopbackHttpUrl(value: string): boolean {
  try {
    const url = new URL(value);
    return url.protocol === "http:" && loopbackHosts.has(url.hostname);
  } catch {
    return false;
  }
}

export const desktopConnectionSchema = z
  .object({
    state: desktopLifecycleStateSchema,
    baseUrl: z.string().url().refine(isLoopbackHttpUrl),
    profileId: z.string().min(1).max(120),
    sidecarVersion: z.string().min(1).max(64),
    recovery: desktopRecoverySchema.nullable()
  })
  .strict();

export type DesktopLifecycleState = z.infer<typeof desktopLifecycleStateSchema>;
export type DesktopRecoveryReason = z.infer<typeof desktopRecoveryReasonSchema>;
export type DesktopConnection = z.infer<typeof desktopConnectionSchema>;

export interface DesktopBridge {
  connection(): Promise<DesktopConnection>;
}
