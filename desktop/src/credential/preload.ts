import type { ContextBridge, IpcRenderer } from "electron";
import type { z } from "zod";
import {
  DESKTOP_CREDENTIAL_CONTEXT_BYTES,
  DESKTOP_CREDENTIAL_IPC_CHANNELS,
  DESKTOP_CREDENTIAL_VALUE_BYTES,
  assertDesktopPlainData,
  desktopCredentialBootstrapRequestSchema,
  desktopCredentialCancelRequestSchema,
  desktopCredentialCancelledResultSchema,
  desktopCredentialContextSchema,
  desktopCredentialStoredResultSchema,
  desktopEnvelopeSchema,
  type DesktopCredentialContext,
  type DesktopErrorCode
} from "../contracts.js";

const CREDENTIAL_RESPONSE_BYTES = 2 * 1024;

export interface CredentialPreloadIpc {
  invoke(channel: string, request: unknown): Promise<unknown>;
}

export interface CredentialBridge {
  getContext(): Promise<DesktopCredentialContext>;
  submit(value: string): Promise<Readonly<{ status: "stored" }>>;
  cancel(): Promise<Readonly<{ status: "cancelled" }>>;
}

interface CredentialContextBridge {
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

function parsePlainWithin<T>(
  schema: z.ZodType<T>,
  raw: unknown,
  limit: number
): T {
  assertDesktopPlainData(raw);
  const parsed = schema.parse(raw);
  if (jsonBytes(parsed) > limit) {
    throw new Error("oversized");
  }
  return deepFreeze(parsed);
}

async function invokeCredential<T>(
  ipc: CredentialPreloadIpc,
  channel: string,
  request: unknown,
  requestSchema: z.ZodType,
  valueSchema: z.ZodType<T>,
  responseLimit = CREDENTIAL_RESPONSE_BYTES
): Promise<T> {
  let parsedRequest: unknown;
  try {
    parsedRequest = parsePlainWithin(
      requestSchema,
      request,
      DESKTOP_CREDENTIAL_CONTEXT_BYTES
    );
  } catch {
    throw fixedError("invalid_desktop_request");
  }
  let raw: unknown;
  try {
    raw = await ipc.invoke(channel, parsedRequest);
  } catch {
    throw fixedError("invalid_desktop_response");
  }
  let envelope:
    | { ok: true; value: T }
    | { ok: false; error: { code: DesktopErrorCode } };
  try {
    envelope = parsePlainWithin(
      desktopEnvelopeSchema(valueSchema),
      raw,
      responseLimit
    );
  } catch {
    throw fixedError("invalid_desktop_response");
  }
  if (!envelope.ok) {
    throw fixedError(envelope.error.code);
  }
  return deepFreeze(envelope.value);
}

export function createCredentialPreload(
  ipc: CredentialPreloadIpc
): CredentialBridge {
  const bridge: CredentialBridge = {
    getContext: () =>
      invokeCredential(
        ipc,
        DESKTOP_CREDENTIAL_IPC_CHANNELS.bootstrap,
        { schema: "kestrel.credential.bootstrap.v1" },
        desktopCredentialBootstrapRequestSchema,
        desktopCredentialContextSchema,
        DESKTOP_CREDENTIAL_CONTEXT_BYTES
      ),
    submit: async (value) => {
      if (typeof value !== "string") {
        throw fixedError("invalid_desktop_request");
      }
      const valueBytes = new TextEncoder().encode(value);
      if (
        valueBytes.byteLength === 0 ||
        valueBytes.byteLength > DESKTOP_CREDENTIAL_VALUE_BYTES
      ) {
        valueBytes.fill(0);
        throw fixedError("invalid_desktop_request");
      }
      try {
        let raw: unknown;
        try {
          raw = await ipc.invoke(
            DESKTOP_CREDENTIAL_IPC_CHANNELS.submit,
            {
              schema: "kestrel.credential.submit.v1",
              valueBytes
            }
          );
        } catch {
          throw fixedError("invalid_desktop_response");
        }
        let envelope:
          | {
              ok: true;
              value: Readonly<{ status: "stored" }>;
            }
          | {
              ok: false;
              error: { code: DesktopErrorCode };
            };
        try {
          envelope = parsePlainWithin(
            desktopEnvelopeSchema(
              desktopCredentialStoredResultSchema
            ),
            raw,
            CREDENTIAL_RESPONSE_BYTES
          );
        } catch {
          throw fixedError("invalid_desktop_response");
        }
        if (!envelope.ok) {
          throw fixedError(envelope.error.code);
        }
        return deepFreeze(envelope.value);
      } finally {
        valueBytes.fill(0);
      }
    },
    cancel: () =>
      invokeCredential(
        ipc,
        DESKTOP_CREDENTIAL_IPC_CHANNELS.cancel,
        { schema: "kestrel.credential.cancel.v1" },
        desktopCredentialCancelRequestSchema,
        desktopCredentialCancelledResultSchema
      )
  };
  return Object.freeze(bridge);
}

export function installCredentialPreload(
  contextBridge: CredentialContextBridge,
  ipc: CredentialPreloadIpc
): void {
  contextBridge.exposeInMainWorld(
    "kestrelCredential",
    createCredentialPreload(ipc)
  );
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
    installCredentialPreload(
      electron.contextBridge,
      electron.ipcRenderer
    );
  }
}
