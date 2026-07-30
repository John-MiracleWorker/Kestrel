import type {
  ContextBridge,
  IpcRenderer,
  IpcRendererEvent
} from "electron";
import type { z } from "zod";
import {
  DESKTOP_EVENT_BYTES,
  DESKTOP_IPC_CHANNELS,
  DESKTOP_REQUEST_BYTES,
  DESKTOP_RESPONSE_BYTES,
  assertDesktopPlainData,
  desktopAppVersionRequestSchema,
  desktopAppVersionSchema,
  desktopChooseProjectFolderRequestSchema,
  desktopChooseStorageFolderRequestSchema,
  desktopConnectionRequestSchema,
  desktopConnectionSchema,
  desktopCredentialDialogRequestSchema,
  desktopCredentialIntentSchema,
  desktopCredentialStateSchema,
  desktopEnvelopeSchema,
  desktopExportSupportBundleRequestSchema,
  desktopExternalUrlRequestSchema,
  desktopExternalUrlResultSchema,
  desktopFolderChoiceSchema,
  desktopOpenExternalUrlRequestSchema,
  desktopRecoveryActionIpcRequestSchema,
  desktopRecoveryActionRequestSchema,
  desktopRecoveryActionResultSchema,
  desktopRuntimeBootstrapRequestSchema,
  desktopRuntimeBootstrapResultSchema,
  desktopSupportBundleResultSchema,
  desktopUpdateStatusRequestSchema,
  desktopUpdateStatusSchema,
  type DesktopBridge,
  type DesktopErrorCode,
  type DesktopRuntimeMarker
} from "./contracts.js";

export interface DesktopPreloadIpc {
  invoke(channel: string, request: unknown): Promise<unknown>;
  sendSync(channel: string, request: unknown): unknown;
  on(
    channel: string,
    listener: (event: unknown, payload: unknown) => void
  ): this;
  removeListener(
    channel: string,
    listener: (event: unknown, payload: unknown) => void
  ): this;
}

interface DesktopContextBridge {
  exposeInMainWorld(name: string, value: unknown): void;
}

function fixedError(code: DesktopErrorCode): Readonly<{
  code: DesktopErrorCode;
}> {
  return Object.freeze({ code });
}

function deepFreeze<T>(value: T): T {
  if (value !== null && typeof value === "object") {
    for (const nested of Object.values(value)) {
      deepFreeze(nested);
    }
    Object.freeze(value);
  }
  return value;
}

function jsonBytes(value: unknown): number {
  const serialized = JSON.stringify(value);
  if (serialized === undefined) {
    throw new Error("not_json");
  }
  return new TextEncoder().encode(serialized).byteLength;
}

function parsedWithin<T>(
  schema: z.ZodType<T>,
  value: unknown,
  limit: number,
  errorCode: DesktopErrorCode
): T {
  try {
    assertDesktopPlainData(value);
    const parsed = schema.parse(value);
    if (jsonBytes(parsed) > limit) {
      throw new Error("oversized");
    }
    return deepFreeze(parsed);
  } catch {
    throw fixedError(errorCode);
  }
}

async function invokeValidated<TRequest, TValue>(
  ipc: DesktopPreloadIpc,
  channel: string,
  requestSchema: z.ZodType<TRequest>,
  request: unknown,
  valueSchema: z.ZodType<TValue>
): Promise<TValue> {
  const validatedRequest = parsedWithin(
    requestSchema,
    request,
    DESKTOP_REQUEST_BYTES,
    "invalid_desktop_request"
  );
  let raw: unknown;
  try {
    raw = await ipc.invoke(channel, validatedRequest);
  } catch {
    throw fixedError("invalid_desktop_response");
  }
  const envelope = parsedWithin(
    desktopEnvelopeSchema(valueSchema),
    raw,
    DESKTOP_RESPONSE_BYTES,
    "invalid_desktop_response"
  );
  if (!envelope.ok) {
    throw fixedError(envelope.error.code);
  }
  return deepFreeze(envelope.value);
}

function subscribeValidated<T>(
  ipc: DesktopPreloadIpc,
  channel: string,
  schema: z.ZodType<T>,
  listener: (value: T) => void
): () => void {
  if (typeof listener !== "function") {
    throw fixedError("invalid_desktop_request");
  }
  const wrapped = (_event: unknown, payload: unknown): void => {
    let validated: T;
    try {
      validated = parsedWithin(
        schema,
        payload,
        DESKTOP_EVENT_BYTES,
        "invalid_desktop_response"
      );
    } catch {
      return;
    }
    try {
      listener(validated);
    } catch {
      // One renderer listener cannot damage the preload or its peers.
    }
  };
  ipc.on(channel, wrapped);
  let subscribed = true;
  return (): void => {
    if (!subscribed) {
      return;
    }
    subscribed = false;
    ipc.removeListener(channel, wrapped);
  };
}

function bootstrapRuntimeMarker(
  ipc: DesktopPreloadIpc
): DesktopRuntimeMarker | null {
  const request = parsedWithin(
    desktopRuntimeBootstrapRequestSchema,
    {
      schema: "kestrel.desktop.runtime-bootstrap.request.v1"
    },
    DESKTOP_REQUEST_BYTES,
    "invalid_desktop_request"
  );
  try {
    const raw = ipc.sendSync(
      DESKTOP_IPC_CHANNELS.runtimeBootstrap,
      request
    );
    const envelope = parsedWithin(
      desktopEnvelopeSchema(desktopRuntimeBootstrapResultSchema),
      raw,
      DESKTOP_RESPONSE_BYTES,
      "invalid_desktop_response"
    );
    if (!envelope.ok || envelope.value.marker === null) {
      return null;
    }
    return deepFreeze(envelope.value.marker);
  } catch {
    return null;
  }
}

export function createDesktopPreload(
  ipc: DesktopPreloadIpc
): Readonly<{
  bridge: DesktopBridge;
  runtimeMarker: DesktopRuntimeMarker | null;
}> {
  const bridge: DesktopBridge = {
    connection: () =>
      invokeValidated(
        ipc,
        DESKTOP_IPC_CHANNELS.connection,
        desktopConnectionRequestSchema,
        { schema: "kestrel.desktop.connection.request.v1" },
        desktopConnectionSchema
      ),
    chooseProjectFolder: () =>
      invokeValidated(
        ipc,
        DESKTOP_IPC_CHANNELS.chooseProjectFolder,
        desktopChooseProjectFolderRequestSchema,
        {
          schema:
            "kestrel.desktop.choose-project-folder.request.v1"
        },
        desktopFolderChoiceSchema
      ),
    chooseStorageFolder: () =>
      invokeValidated(
        ipc,
        DESKTOP_IPC_CHANNELS.chooseStorageFolder,
        desktopChooseStorageFolderRequestSchema,
        {
          schema:
            "kestrel.desktop.choose-storage-folder.request.v1"
        },
        desktopFolderChoiceSchema
      ),
    exportSupportBundle: () =>
      invokeValidated(
        ipc,
        DESKTOP_IPC_CHANNELS.exportSupportBundle,
        desktopExportSupportBundleRequestSchema,
        {
          schema:
            "kestrel.desktop.export-support-bundle.request.v1"
        },
        desktopSupportBundleResultSchema
      ),
    getAppVersion: () =>
      invokeValidated(
        ipc,
        DESKTOP_IPC_CHANNELS.appVersion,
        desktopAppVersionRequestSchema,
        { schema: "kestrel.desktop.app-version.request.v1" },
        desktopAppVersionSchema
      ),
    getUpdateStatus: () =>
      invokeValidated(
        ipc,
        DESKTOP_IPC_CHANNELS.updateStatus,
        desktopUpdateStatusRequestSchema,
        {
          schema: "kestrel.desktop.update-status.request.v1"
        },
        desktopUpdateStatusSchema
      ),
    openCredentialDialog: async (intent) => {
      const validatedIntent = parsedWithin(
        desktopCredentialIntentSchema,
        intent,
        DESKTOP_REQUEST_BYTES,
        "invalid_desktop_request"
      );
      return await invokeValidated(
        ipc,
        DESKTOP_IPC_CHANNELS.credentialDialog,
        desktopCredentialDialogRequestSchema,
        {
          schema:
            "kestrel.desktop.credential-dialog.request.v1",
          intent: validatedIntent
        },
        desktopCredentialStateSchema
      );
    },
    openExternalUrl: async (request) => {
      const validatedRequest = parsedWithin(
        desktopExternalUrlRequestSchema,
        request,
        DESKTOP_REQUEST_BYTES,
        "invalid_desktop_request"
      );
      return await invokeValidated(
        ipc,
        DESKTOP_IPC_CHANNELS.openExternalUrl,
        desktopOpenExternalUrlRequestSchema,
        {
          schema:
            "kestrel.desktop.open-external-url.request.v1",
          request: validatedRequest
        },
        desktopExternalUrlResultSchema
      );
    },
    performRecoveryAction: async (request) => {
      const validatedRequest = parsedWithin(
        desktopRecoveryActionRequestSchema,
        request,
        DESKTOP_REQUEST_BYTES,
        "invalid_desktop_request"
      );
      return await invokeValidated(
        ipc,
        DESKTOP_IPC_CHANNELS.recoveryAction,
        desktopRecoveryActionIpcRequestSchema,
        {
          schema:
            "kestrel.desktop.recovery-action.request.v1",
          request: validatedRequest
        },
        desktopRecoveryActionResultSchema
      );
    },
    subscribeLifecycle: (listener) =>
      subscribeValidated(
        ipc,
        DESKTOP_IPC_CHANNELS.lifecycleEvent,
        desktopConnectionSchema,
        listener
      ),
    subscribeUpdateStatus: (listener) =>
      subscribeValidated(
        ipc,
        DESKTOP_IPC_CHANNELS.updateStatusEvent,
        desktopUpdateStatusSchema,
        listener
      )
  };
  return Object.freeze({
    bridge: Object.freeze(bridge),
    runtimeMarker: bootstrapRuntimeMarker(ipc)
  });
}

export function installDesktopPreload(
  contextBridge: DesktopContextBridge,
  ipc: DesktopPreloadIpc
): void {
  const preload = createDesktopPreload(ipc);
  contextBridge.exposeInMainWorld("kestrelDesktop", preload.bridge);
  if (preload.runtimeMarker !== null) {
    contextBridge.exposeInMainWorld(
      "kestrelDesktopRuntime",
      preload.runtimeMarker
    );
  }
}

declare const require:
  | ((name: "electron") => {
      contextBridge?: ContextBridge;
      ipcRenderer?: IpcRenderer;
    })
  | undefined;

if (typeof require === "function") {
  const electron = require("electron");
  if (
    electron.contextBridge !== undefined &&
    electron.ipcRenderer !== undefined
  ) {
    installDesktopPreload(
      electron.contextBridge,
      electron.ipcRenderer as IpcRenderer & {
        on(
          channel: string,
          listener: (
            event: IpcRendererEvent,
            payload: unknown
          ) => void
        ): IpcRenderer;
      }
    );
  }
}
