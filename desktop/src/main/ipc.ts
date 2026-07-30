import type { Stats } from "node:fs";
import type { z } from "zod";
import {
  DESKTOP_EVENT_BYTES,
  DESKTOP_IPC_CHANNELS,
  DESKTOP_PATH_CHARACTERS,
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
  desktopCredentialStateSchema,
  desktopEnvelopeSchema,
  desktopExportSupportBundleRequestSchema,
  desktopExternalUrlRequestSchema,
  desktopExternalUrlResultSchema,
  desktopFolderChoiceSchema,
  desktopOpenExternalUrlRequestSchema,
  desktopRecoveryActionIpcRequestSchema,
  desktopRecoveryActionResultSchema,
  desktopRuntimeBootstrapRequestSchema,
  desktopRuntimeBootstrapResultSchema,
  desktopRuntimeMarkerSchema,
  desktopSupportBundleResultSchema,
  desktopUpdateStatusRequestSchema,
  desktopUpdateStatusSchema,
  type DesktopConnection,
  type DesktopCredentialIntent,
  type DesktopCredentialState,
  type DesktopErrorCode,
  type DesktopExternalUrlRequest,
  type DesktopExternalUrlResult,
  type DesktopFolderChoice,
  type DesktopRecoveryActionRequest,
  type DesktopRecoveryActionResult,
  type DesktopRuntimeMarker,
  type DesktopSupportBundleResult,
  type DesktopUpdateStatus
} from "../contracts.js";

export interface DesktopIpcFrame {
  readonly url: string;
}

export interface DesktopIpcWebContents {
  readonly id: number;
  readonly mainFrame: DesktopIpcFrame;
  isDestroyed(): boolean;
  once(event: "destroyed", listener: () => void): unknown;
  send(channel: string, payload: unknown): void;
}

export interface DesktopIpcEvent {
  readonly sender: DesktopIpcWebContents;
  readonly senderFrame: DesktopIpcFrame | null;
  returnValue?: unknown;
}

export interface DesktopIpcMain {
  handle(
    channel: string,
    listener: (
      event: DesktopIpcEvent,
      request: unknown
    ) => Promise<unknown>
  ): void;
  on(
    channel: string,
    listener: (event: DesktopIpcEvent, request: unknown) => void
  ): void;
}

export interface DesktopIpcAdapters {
  readConnection(): DesktopConnection;
  subscribeLifecycle(
    listener: (connection: DesktopConnection) => void
  ): () => void;
  readUpdateStatus(): DesktopUpdateStatus;
  subscribeUpdateStatus(
    listener: (status: DesktopUpdateStatus) => void
  ): () => void;
  chooseProjectFolder(): Promise<DesktopFolderChoice>;
  chooseStorageFolder(): Promise<DesktopFolderChoice>;
  exportSupportBundle(): Promise<DesktopSupportBundleResult>;
  getAppVersion(): string;
  openCredentialDialog(
    intent: DesktopCredentialIntent
  ): Promise<DesktopCredentialState>;
  openExternalUrl(
    request: DesktopExternalUrlRequest
  ): Promise<void>;
  performRecoveryAction(
    request: DesktopRecoveryActionRequest
  ): Promise<DesktopRecoveryActionResult>;
  runtimeMarker(): DesktopRuntimeMarker | null;
}

export interface DesktopIpcAuthority {
  bindRenderer(webContents: DesktopIpcWebContents): () => void;
}

interface RendererBinding {
  webContents: DesktopIpcWebContents;
}

export class DesktopAdapterError extends Error {
  constructor(readonly code: DesktopErrorCode) {
    super(code);
    this.name = "DesktopAdapterError";
  }
}

export type DesktopConnectionSourceState =
  | { kind: "verifying" | "starting" | "stopping" }
  | {
      kind: "ready";
      profileId: string;
      baseUrl: string;
      sidecarVersion: string;
    }
  | {
      kind: "recovery";
      reason:
        | "sidecar_unavailable"
        | "sidecar_unverified"
        | "profile_conflict"
        | "version_incompatible"
        | "reconciliation_required";
    };

function errorEnvelope(code: DesktopErrorCode): Readonly<{
  ok: false;
  error: Readonly<{ code: DesktopErrorCode }>;
}> {
  return Object.freeze({
    ok: false,
    error: Object.freeze({ code })
  });
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

export function projectDesktopConnection(
  state: DesktopConnectionSourceState,
  marker: DesktopRuntimeMarker | null
): DesktopConnection {
  if (state.kind === "ready") {
    if (
      marker !== null &&
      marker.baseUrl === state.baseUrl
    ) {
      return deepFreeze({
        schema: "kestrel.desktop.connection.v1",
        state: "ready",
        generation: marker.generation,
        baseUrl: marker.baseUrl,
        profileId: state.profileId,
        sidecarVersion: state.sidecarVersion,
        recovery: null
      });
    }
    return deepFreeze({
      schema: "kestrel.desktop.connection.v1",
      state: "recovery",
      generation: null,
      baseUrl: null,
      profileId: null,
      sidecarVersion: null,
      recovery: { reason: "sidecar_unverified" }
    });
  }
  if (state.kind === "recovery") {
    return deepFreeze({
      schema: "kestrel.desktop.connection.v1",
      state: "recovery",
      generation: null,
      baseUrl: null,
      profileId: null,
      sidecarVersion: null,
      recovery: { reason: state.reason }
    });
  }
  return deepFreeze({
    schema: "kestrel.desktop.connection.v1",
    state: state.kind,
    generation: null,
    baseUrl: null,
    profileId: null,
    sidecarVersion: null,
    recovery: null
  });
}

function jsonBytes(value: unknown): number {
  const serialized = JSON.stringify(value);
  if (serialized === undefined) {
    throw new Error("not_json");
  }
  return Buffer.byteLength(serialized, "utf8");
}

function parseWithin<T>(
  schema: z.ZodType<T>,
  value: unknown,
  limit: number
): T {
  assertDesktopPlainData(value);
  const parsed = schema.parse(value);
  if (jsonBytes(parsed) > limit) {
    throw new Error("oversized");
  }
  return deepFreeze(parsed);
}

function validAppFrameUrl(value: string): boolean {
  if (
    value.includes("\\") ||
    value.includes("\0") ||
    /%(?:00|2e|2f|5c)/i.test(value)
  ) {
    return false;
  }
  try {
    const parsed = new URL(value);
    return (
      parsed.protocol === "kestrel:" &&
      parsed.hostname === "app" &&
      parsed.username === "" &&
      parsed.password === "" &&
      parsed.port === "" &&
      parsed.search === "" &&
      parsed.hash === "" &&
      parsed.pathname.startsWith("/") &&
      parsed.href === value
    );
  } catch {
    return false;
  }
}

function successfulEnvelope<T>(
  valueSchema: z.ZodType<T>,
  value: unknown,
  limit: number
): Readonly<{ ok: true; value: T }> {
  const parsedValue = parseWithin(valueSchema, value, limit);
  const envelope = parseWithin(
    desktopEnvelopeSchema(valueSchema),
    { ok: true, value: parsedValue },
    limit
  );
  if (!envelope.ok) {
    throw new Error("unexpected_error_envelope");
  }
  return deepFreeze(envelope);
}

function operationError(error: unknown): ReturnType<typeof errorEnvelope> {
  return error instanceof DesktopAdapterError
    ? errorEnvelope(error.code)
    : errorEnvelope("desktop_operation_failed");
}

function exactRuntimeAgreement(
  connection: DesktopConnection,
  marker: DesktopRuntimeMarker | null
): DesktopRuntimeMarker | null {
  if (
    connection.state !== "ready" ||
    marker === null ||
    connection.baseUrl !== marker.baseUrl ||
    connection.generation !== marker.generation
  ) {
    return null;
  }
  return marker;
}

export function installDesktopIpc(
  ipcMain: DesktopIpcMain,
  adapters: DesktopIpcAdapters
): DesktopIpcAuthority {
  const bindings = new Map<number, RendererBinding>();

  const trustedSender = (event: DesktopIpcEvent): boolean => {
    const binding = bindings.get(event.sender.id);
    return (
      binding !== undefined &&
      binding.webContents === event.sender &&
      !binding.webContents.isDestroyed() &&
      event.senderFrame !== null &&
      event.senderFrame === event.sender.mainFrame &&
      validAppFrameUrl(event.senderFrame.url)
    );
  };

  const register = <TRequest, TValue>(
    channel: string,
    requestSchema: z.ZodType<TRequest>,
    valueSchema: z.ZodType<TValue>,
    operation: (request: TRequest) => TValue | Promise<TValue>
  ): void => {
    ipcMain.handle(channel, async (event, rawRequest) => {
      if (!trustedSender(event)) {
        return errorEnvelope("desktop_sender_untrusted");
      }
      let request: TRequest;
      try {
        request = parseWithin(
          requestSchema,
          rawRequest,
          DESKTOP_REQUEST_BYTES
        );
      } catch {
        return errorEnvelope("invalid_desktop_request");
      }
      let value: unknown;
      try {
        value = await operation(request);
      } catch (error) {
        return operationError(error);
      }
      try {
        return successfulEnvelope(
          valueSchema,
          value,
          DESKTOP_RESPONSE_BYTES
        );
      } catch {
        return errorEnvelope("invalid_desktop_response");
      }
    });
  };

  register(
    DESKTOP_IPC_CHANNELS.connection,
    desktopConnectionRequestSchema,
    desktopConnectionSchema,
    () => adapters.readConnection()
  );
  register(
    DESKTOP_IPC_CHANNELS.chooseProjectFolder,
    desktopChooseProjectFolderRequestSchema,
    desktopFolderChoiceSchema,
    () => adapters.chooseProjectFolder()
  );
  register(
    DESKTOP_IPC_CHANNELS.chooseStorageFolder,
    desktopChooseStorageFolderRequestSchema,
    desktopFolderChoiceSchema,
    () => adapters.chooseStorageFolder()
  );
  register(
    DESKTOP_IPC_CHANNELS.exportSupportBundle,
    desktopExportSupportBundleRequestSchema,
    desktopSupportBundleResultSchema,
    () => adapters.exportSupportBundle()
  );
  register(
    DESKTOP_IPC_CHANNELS.appVersion,
    desktopAppVersionRequestSchema,
    desktopAppVersionSchema,
    () => ({ version: adapters.getAppVersion() })
  );
  register(
    DESKTOP_IPC_CHANNELS.updateStatus,
    desktopUpdateStatusRequestSchema,
    desktopUpdateStatusSchema,
    () => adapters.readUpdateStatus()
  );
  register(
    DESKTOP_IPC_CHANNELS.credentialDialog,
    desktopCredentialDialogRequestSchema,
    desktopCredentialStateSchema,
    (request) => adapters.openCredentialDialog(request.intent)
  );
  register(
    DESKTOP_IPC_CHANNELS.openExternalUrl,
    desktopOpenExternalUrlRequestSchema,
    desktopExternalUrlResultSchema,
    async (request): Promise<DesktopExternalUrlResult> => {
      await adapters.openExternalUrl(request.request);
      return { opened: true };
    }
  );
  register(
    DESKTOP_IPC_CHANNELS.recoveryAction,
    desktopRecoveryActionIpcRequestSchema,
    desktopRecoveryActionResultSchema,
    (request) => adapters.performRecoveryAction(request.request)
  );

  ipcMain.on(
    DESKTOP_IPC_CHANNELS.runtimeBootstrap,
    (event, rawRequest) => {
      if (!trustedSender(event)) {
        event.returnValue = errorEnvelope(
          "desktop_sender_untrusted"
        );
        return;
      }
      try {
        parseWithin(
          desktopRuntimeBootstrapRequestSchema,
          rawRequest,
          DESKTOP_REQUEST_BYTES
        );
      } catch {
        event.returnValue = errorEnvelope(
          "invalid_desktop_request"
        );
        return;
      }
      let connection: DesktopConnection;
      let marker: DesktopRuntimeMarker | null;
      try {
        connection = parseWithin(
          desktopConnectionSchema,
          adapters.readConnection(),
          DESKTOP_RESPONSE_BYTES
        );
        const rawMarker = adapters.runtimeMarker();
        marker =
          rawMarker === null
            ? null
            : parseWithin(
                desktopRuntimeMarkerSchema,
                rawMarker,
                DESKTOP_RESPONSE_BYTES
              );
      } catch {
        event.returnValue = errorEnvelope(
          "invalid_desktop_response"
        );
        return;
      }
      try {
        event.returnValue = successfulEnvelope(
          desktopRuntimeBootstrapResultSchema,
          {
            marker: exactRuntimeAgreement(connection, marker)
          },
          DESKTOP_RESPONSE_BYTES
        );
      } catch {
        event.returnValue = errorEnvelope(
          "invalid_desktop_response"
        );
      }
    }
  );

  const broadcast = <T>(
    channel: string,
    schema: z.ZodType<T>,
    rawValue: unknown
  ): void => {
    let value: T;
    try {
      value = parseWithin(
        schema,
        rawValue,
        DESKTOP_EVENT_BYTES
      );
    } catch {
      return;
    }
    for (const binding of [...bindings.values()]) {
      if (binding.webContents.isDestroyed()) {
        continue;
      }
      try {
        binding.webContents.send(channel, deepFreeze(value));
      } catch {
        // One renderer cannot prevent delivery to another.
      }
    }
  };

  adapters.subscribeLifecycle((connection) => {
    broadcast(
      DESKTOP_IPC_CHANNELS.lifecycleEvent,
      desktopConnectionSchema,
      connection
    );
  });
  adapters.subscribeUpdateStatus((status) => {
    broadcast(
      DESKTOP_IPC_CHANNELS.updateStatusEvent,
      desktopUpdateStatusSchema,
      status
    );
  });

  const authority: DesktopIpcAuthority = {
    bindRenderer(webContents: DesktopIpcWebContents): () => void {
      if (
        !Number.isSafeInteger(webContents.id) ||
        webContents.id <= 0 ||
        webContents.isDestroyed()
      ) {
        return () => undefined;
      }
      const binding: RendererBinding = { webContents };
      bindings.set(webContents.id, binding);
      let unbound = false;
      const unbind = (): void => {
        if (unbound) {
          return;
        }
        unbound = true;
        if (bindings.get(webContents.id) === binding) {
          bindings.delete(webContents.id);
        }
      };
      webContents.once("destroyed", unbind);
      return unbind;
    }
  };
  return Object.freeze(authority);
}

export interface DirectoryPickerDependencies {
  showOpenDialog(): Promise<{
    canceled: boolean;
    filePaths: string[];
  }>;
  realpath(path: string): Promise<string>;
  stat(path: string): Promise<Pick<Stats, "isDirectory">>;
  isAbsolute(path: string): boolean;
  basename(path: string): string;
}

export async function chooseCanonicalDirectory(
  dependencies: DirectoryPickerDependencies
): Promise<DesktopFolderChoice> {
  let result: Awaited<
    ReturnType<DirectoryPickerDependencies["showOpenDialog"]>
  >;
  try {
    result = await dependencies.showOpenDialog();
  } catch {
    throw new DesktopAdapterError("desktop_operation_failed");
  }
  if (result.canceled) {
    return deepFreeze({ status: "cancelled" });
  }
  if (result.filePaths.length !== 1) {
    throw new DesktopAdapterError("desktop_operation_failed");
  }
  const selected = result.filePaths[0];
  if (
    selected === undefined ||
    selected.length === 0 ||
    selected.length > DESKTOP_PATH_CHARACTERS ||
    selected.includes("\0") ||
    !dependencies.isAbsolute(selected)
  ) {
    throw new DesktopAdapterError("desktop_operation_failed");
  }
  let canonical: string;
  let stats: Pick<Stats, "isDirectory">;
  try {
    canonical = await dependencies.realpath(selected);
    if (
      canonical.length === 0 ||
      canonical.length > DESKTOP_PATH_CHARACTERS ||
      canonical.includes("\0") ||
      !dependencies.isAbsolute(canonical)
    ) {
      throw new Error("invalid_path");
    }
    stats = await dependencies.stat(canonical);
  } catch {
    throw new DesktopAdapterError("desktop_operation_failed");
  }
  const displayLabel = dependencies.basename(canonical);
  if (
    !stats.isDirectory() ||
    displayLabel.trim().length === 0 ||
    displayLabel.length > 256
  ) {
    throw new DesktopAdapterError("desktop_operation_failed");
  }
  return deepFreeze({
    status: "selected",
    path: canonical,
    displayLabel
  });
}

const EXTERNAL_HOSTS = Object.freeze({
  documentation: "github.com",
  issues: "github.com",
  security: "github.com",
  release_notes: "github.com"
} as const);

export async function openReviewedExternalUrl(
  rawRequest: DesktopExternalUrlRequest,
  open: (url: string) => Promise<void>
): Promise<DesktopExternalUrlResult> {
  let request: DesktopExternalUrlRequest;
  try {
    request = parseWithin(
      desktopExternalUrlRequestSchema,
      rawRequest,
      DESKTOP_REQUEST_BYTES
    );
  } catch {
    throw new DesktopAdapterError("invalid_desktop_request");
  }
  const parsed = new URL(request.url);
  if (parsed.hostname !== EXTERNAL_HOSTS[request.purpose]) {
    throw new DesktopAdapterError("invalid_desktop_request");
  }
  try {
    await open(request.url);
  } catch {
    throw new DesktopAdapterError("desktop_operation_failed");
  }
  return deepFreeze({ opened: true });
}

export function unavailableDesktopFeature(): never {
  throw new DesktopAdapterError("desktop_feature_unavailable");
}
