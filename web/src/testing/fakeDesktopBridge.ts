import type {
  DesktopBridge,
  DesktopConnection,
  DesktopUpdateStatus,
} from "../platform/desktopBridge";
import type { DesktopRuntimeMarker } from "../types";

const readyConnection: DesktopConnection = Object.freeze({
  schema: "kestrel.desktop.connection.v1",
  state: "ready",
  generation: 4,
  baseUrl: "http://127.0.0.1:43123/",
  profileId: "default",
  sidecarVersion: "0.5.0",
  recovery: null,
});

const unavailableUpdate: DesktopUpdateStatus = Object.freeze({
  schema: "kestrel.desktop.update.v1",
  state: "unavailable",
  reason: "not_configured",
});

export type FakeDesktopBridgeOverrides = Partial<DesktopBridge>;

export function createFakeDesktopBridge(
  overrides: FakeDesktopBridgeOverrides = {},
): DesktopBridge {
  const defaults: DesktopBridge = {
    connection: async () => readyConnection,
    chooseProjectFolder: async () => ({ status: "cancelled" }),
    chooseStorageFolder: async () => ({ status: "cancelled" }),
    exportSupportBundle: async () => ({ status: "cancelled" }),
    getAppVersion: async () => ({ version: "0.5.0" }),
    getUpdateStatus: async () => unavailableUpdate,
    openCredentialDialog: async () => ({
      status: "stored",
      secretRef: "secret://fixture_provider_key",
      validation: "unverified",
      fingerprint: "sha256:0123456789ab",
    }),
    openExternalUrl: async () => ({ opened: true }),
    performRecoveryAction: async () => ({ accepted: true }),
    subscribeLifecycle: () => () => undefined,
    subscribeUpdateStatus: () => () => undefined,
  };
  return Object.freeze({
    ...defaults,
    ...overrides,
  });
}

export function installFakeDesktopBridge(
  overrides: FakeDesktopBridgeOverrides = {},
): DesktopBridge {
  const bridge = createFakeDesktopBridge(overrides);
  Object.defineProperty(globalThis, "kestrelDesktop", {
    configurable: true,
    enumerable: false,
    writable: false,
    value: bridge,
  });
  return bridge;
}

export function installFakeDesktopRuntimeMarker(
  overrides: Partial<DesktopRuntimeMarker> = {},
): DesktopRuntimeMarker {
  const marker: DesktopRuntimeMarker = Object.freeze({
    schema: "kestrel.desktop.runtime.v1",
    baseUrl: "http://127.0.0.1:43123/",
    generation: 4,
    ...overrides,
  });
  Object.defineProperty(globalThis, "kestrelDesktopRuntime", {
    configurable: true,
    enumerable: false,
    writable: false,
    value: marker,
  });
  return marker;
}

export function removeFakeDesktopEnvironment(): void {
  Reflect.deleteProperty(globalThis, "kestrelDesktop");
  Reflect.deleteProperty(globalThis, "kestrelDesktopRuntime");
}
