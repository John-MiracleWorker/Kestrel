import { describe, expect, it, vi } from "vitest";
import {
  DESKTOP_CREDENTIAL_IPC_CHANNELS,
  type DesktopCredentialContext
} from "../contracts";
import {
  createCredentialPreload,
  type CredentialPreloadIpc
} from "./preload";

const context: DesktopCredentialContext = {
  schema: "kestrel.credential.context.v1",
  providerId: "openai",
  providerLabel: "OpenAI",
  inputLabel: "OpenAI API key",
  maxUtf8Bytes: 16_384
};

function success(value: unknown): unknown {
  return { ok: true, value };
}

function harness(
  invoke: (
    channel: string,
    request: unknown
  ) => unknown = (channel) => {
    if (channel === DESKTOP_CREDENTIAL_IPC_CHANNELS.bootstrap) {
      return success(context);
    }
    if (channel === DESKTOP_CREDENTIAL_IPC_CHANNELS.submit) {
      return success({ status: "stored" });
    }
    return success({ status: "cancelled" });
  }
): {
  bridge: ReturnType<typeof createCredentialPreload>;
  ipc: CredentialPreloadIpc;
} {
  const ipc: CredentialPreloadIpc = {
    invoke: vi.fn(async (channel, request) =>
      invoke(channel, request)
    )
  };
  return {
    bridge: createCredentialPreload(ipc),
    ipc
  };
}

describe("isolated credential preload", () => {
  it("exposes only three frozen methods and no generic authority", () => {
    const { bridge } = harness();

    expect(Reflect.ownKeys(bridge).sort()).toEqual([
      "cancel",
      "getContext",
      "submit"
    ]);
    expect(Object.isFrozen(bridge)).toBe(true);
    const descriptors = Object.values(
      Object.getOwnPropertyDescriptors(bridge)
    ) as PropertyDescriptor[];
    expect(
      descriptors.every(
        (descriptor) =>
          "value" in descriptor &&
          typeof descriptor.value === "function" &&
          descriptor.writable === false &&
          descriptor.configurable === false
      )
    ).toBe(true);
    for (const forbidden of [
      "invoke",
      "send",
      "on",
      "ipcRenderer",
      "process",
      "env",
      "apiUrl",
      "apiToken",
      "credentialCapability",
      "kestrelDesktop"
    ]) {
      expect(bridge).not.toHaveProperty(forbidden);
    }
  });

  it("uses only the exact private channels and metadata schemas", async () => {
    const { bridge, ipc } = harness();

    await expect(bridge.getContext()).resolves.toEqual(context);
    await expect(bridge.cancel()).resolves.toEqual({
      status: "cancelled"
    });

    expect(ipc.invoke).toHaveBeenNthCalledWith(
      1,
      "kestrel:credential:bootstrap",
      { schema: "kestrel.credential.bootstrap.v1" }
    );
    expect(ipc.invoke).toHaveBeenNthCalledWith(
      2,
      "kestrel:credential:cancel",
      { schema: "kestrel.credential.cancel.v1" }
    );
  });

  it("encodes one bounded byte copy and clears it after success", async () => {
    const sentinel = "credential-private-sentinel-83c2";
    let submitted: Uint8Array | undefined;
    const { bridge, ipc } = harness((channel, request) => {
      if (channel !== DESKTOP_CREDENTIAL_IPC_CHANNELS.submit) {
        return success(context);
      }
      const payload = request as {
        schema: string;
        valueBytes: Uint8Array;
      };
      expect(payload.schema).toBe(
        "kestrel.credential.submit.v1"
      );
      expect(payload.valueBytes).toBeInstanceOf(Uint8Array);
      expect(new TextDecoder().decode(payload.valueBytes)).toBe(
        sentinel
      );
      expect(JSON.stringify(payload)).not.toContain(sentinel);
      submitted = payload.valueBytes;
      return success({ status: "stored" });
    });

    await expect(bridge.submit(sentinel)).resolves.toEqual({
      status: "stored"
    });

    expect(ipc.invoke).toHaveBeenCalledTimes(1);
    expect(submitted).toBeDefined();
    expect([...submitted!]).toEqual(
      Array.from({ length: submitted!.byteLength }, () => 0)
    );
  });

  it("clears its byte copy after a rejected or malformed IPC result", async () => {
    const captured: Uint8Array[] = [];
    for (const response of [
      Promise.reject(new Error("native-secret-error")),
      Promise.reject({
        code: "native-private-sentinel"
      }),
      {
        ok: true,
        value: {
          status: "stored",
          value: "must-not-cross"
        }
      }
    ]) {
      const { bridge } = harness((_channel, request) => {
        captured.push(
          (request as { valueBytes: Uint8Array }).valueBytes
        );
        return response;
      });
      await expect(
        bridge.submit("clear-me-after-terminal-path")
      ).rejects.toMatchObject({
        code: "invalid_desktop_response"
      });
    }

    for (const bytes of captured) {
      expect([...bytes].every((value) => value === 0)).toBe(true);
    }
  });

  it("rejects non-strings and UTF-8 payloads over 16 KiB before IPC", async () => {
    const { bridge, ipc } = harness();

    for (const value of [
      "",
      7,
      new String("boxed"),
      "é".repeat(8_193),
      "x".repeat(16_385)
    ]) {
      await expect(
        bridge.submit(value as never)
      ).rejects.toMatchObject({
        code: "invalid_desktop_request"
      });
    }
    expect(ipc.invoke).not.toHaveBeenCalled();
  });

  it("rejects oversized, accessor-bearing, and secret-bearing context responses", async () => {
    const invalid = [
      success({ ...context, apiToken: "must-not-cross" }),
      success({ ...context, providerLabel: "x".repeat(2_100) }),
      success(
        Object.defineProperty(
          { ...context },
          "providerLabel",
          {
            enumerable: true,
            get: () => "OpenAI"
          }
        )
      )
    ];

    for (const result of invalid) {
      const { bridge } = harness(() => result);
      await expect(bridge.getContext()).rejects.toMatchObject({
        code: "invalid_desktop_response"
      });
    }
  });
});
