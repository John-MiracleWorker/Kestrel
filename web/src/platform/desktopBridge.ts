import type { DesktopRuntimeMarker } from "../types";

export const DESKTOP_BRIDGE_KEY = "kestrelDesktop" as const;

export type DesktopConnection =
  | Readonly<{
      schema: "kestrel.desktop.connection.v1";
      state: "ready";
      generation: number;
      baseUrl: string;
      profileId: string;
      sidecarVersion: string;
      recovery: null;
    }>
  | Readonly<{
      schema: "kestrel.desktop.connection.v1";
      state: "verifying" | "starting" | "stopping";
      generation: null;
      baseUrl: null;
      profileId: null;
      sidecarVersion: null;
      recovery: null;
    }>
  | Readonly<{
      schema: "kestrel.desktop.connection.v1";
      state: "recovery";
      generation: null;
      baseUrl: null;
      profileId: null;
      sidecarVersion: null;
      recovery: Readonly<{
        reason:
          | "sidecar_unavailable"
          | "sidecar_unverified"
          | "payload_verification_failed"
          | "profile_conflict"
          | "version_incompatible"
          | "state_incompatible"
          | "state_corrupt"
          | "memvid_reopen_failed"
          | "sidecar_crash_loop"
          | "credential_backend_unavailable"
          | "reconciliation_required";
      }>;
    }>;

export type DesktopUpdateStatus = Readonly<
  Record<string, unknown> & {
    schema: "kestrel.desktop.update.v1";
    state:
      | "unavailable"
      | "idle"
      | "checking"
      | "available"
      | "downloading"
      | "downloaded"
      | "error";
  }
>;

export interface DesktopBridge {
  connection(): Promise<DesktopConnection>;
  chooseProjectFolder(): Promise<unknown>;
  chooseStorageFolder(): Promise<unknown>;
  exportSupportBundle(): Promise<unknown>;
  getAppVersion(): Promise<unknown>;
  getUpdateStatus(): Promise<DesktopUpdateStatus>;
  openCredentialDialog(intent: unknown): Promise<unknown>;
  openExternalUrl(request: unknown): Promise<unknown>;
  performRecoveryAction(request: unknown): Promise<unknown>;
  subscribeLifecycle(
    listener: (connection: DesktopConnection) => void
  ): () => void;
  subscribeUpdateStatus(
    listener: (status: DesktopUpdateStatus) => void
  ): () => void;
}

const METHOD_NAMES = Object.freeze([
  "connection",
  "chooseProjectFolder",
  "chooseStorageFolder",
  "exportSupportBundle",
  "getAppVersion",
  "getUpdateStatus",
  "openCredentialDialog",
  "openExternalUrl",
  "performRecoveryAction",
  "subscribeLifecycle",
  "subscribeUpdateStatus"
] as const);

const METHOD_NAME_SET = new Set<string>(METHOD_NAMES);

function invalidBridge(): Error {
  return new Error("desktop_bridge_invalid");
}

export function hasDesktopBridge(): boolean {
  return Object.prototype.hasOwnProperty.call(
    globalThis,
    DESKTOP_BRIDGE_KEY
  );
}

function bridgeValue(): unknown {
  try {
    return Reflect.get(globalThis, DESKTOP_BRIDGE_KEY);
  } catch {
    throw invalidBridge();
  }
}

export function readDesktopBridge(): DesktopBridge | null {
  if (!hasDesktopBridge()) {
    return null;
  }
  try {
    const source = bridgeValue();
    if (
      typeof source !== "object" ||
      source === null ||
      !Object.isFrozen(source)
    ) {
      throw invalidBridge();
    }
    const prototype = Object.getPrototypeOf(source);
    if (
      prototype !== Object.prototype &&
      prototype !== null
    ) {
      throw invalidBridge();
    }
    const keys = Reflect.ownKeys(source);
    if (
      keys.length !== METHOD_NAMES.length ||
      keys.some(
        (key) =>
          typeof key !== "string" || !METHOD_NAME_SET.has(key)
      )
    ) {
      throw invalidBridge();
    }
    const descriptors = Object.getOwnPropertyDescriptors(source);
    const methods = new Map<string, (...args: unknown[]) => unknown>();
    for (const name of METHOD_NAMES) {
      const descriptor = descriptors[name];
      if (
        descriptor === undefined ||
        !("value" in descriptor) ||
        typeof descriptor.value !== "function"
      ) {
        throw invalidBridge();
      }
      methods.set(name, descriptor.value as (...args: unknown[]) => unknown);
    }
    const call = (name: string, args: unknown[]): unknown =>
      Reflect.apply(methods.get(name)!, undefined, args);
    return Object.freeze({
      connection: () => call("connection", []) as Promise<DesktopConnection>,
      chooseProjectFolder: () =>
        call("chooseProjectFolder", []) as Promise<unknown>,
      chooseStorageFolder: () =>
        call("chooseStorageFolder", []) as Promise<unknown>,
      exportSupportBundle: () =>
        call("exportSupportBundle", []) as Promise<unknown>,
      getAppVersion: () =>
        call("getAppVersion", []) as Promise<unknown>,
      getUpdateStatus: () =>
        call("getUpdateStatus", []) as Promise<DesktopUpdateStatus>,
      openCredentialDialog: (intent: unknown) =>
        call("openCredentialDialog", [intent]) as Promise<unknown>,
      openExternalUrl: (request: unknown) =>
        call("openExternalUrl", [request]) as Promise<unknown>,
      performRecoveryAction: (request: unknown) =>
        call("performRecoveryAction", [request]) as Promise<unknown>,
      subscribeLifecycle: (
        listener: (connection: DesktopConnection) => void
      ) =>
        call("subscribeLifecycle", [listener]) as () => void,
      subscribeUpdateStatus: (
        listener: (status: DesktopUpdateStatus) => void
      ) =>
        call("subscribeUpdateStatus", [listener]) as () => void
    });
  } catch {
    throw invalidBridge();
  }
}

export type { DesktopRuntimeMarker };
