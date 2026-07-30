import { describe, expect, it, vi } from "vitest";
import {
  DESKTOP_CREDENTIAL_IPC_CHANNELS,
  type DesktopCredentialContext
} from "../contracts";
import {
  installCredentialIpc,
  type CredentialIpcEvent,
  type CredentialIpcMain,
  type CredentialIpcWebContents
} from "./credential-ipc";

const context: DesktopCredentialContext = {
  schema: "kestrel.credential.context.v1",
  providerId: "openai",
  providerLabel: "OpenAI",
  inputLabel: "OpenAI API key",
  maxUtf8Bytes: 16_384
};

class FakeWebContents implements CredentialIpcWebContents {
  destroyed = false;
  mainFrame: {
    url: string;
    processId: number;
    routingId: number;
    isMainFrame: boolean;
  };

  constructor(readonly id: number) {
    this.mainFrame = {
      url: "kestrel://credential/index.html",
      processId: 1_000 + id,
      routingId: 2_000 + id,
      isMainFrame: true
    };
  }

  isDestroyed(): boolean {
    return this.destroyed;
  }
}

function eventFor(
  webContents: FakeWebContents,
  overrides: Partial<CredentialIpcEvent> = {}
): CredentialIpcEvent {
  return {
    sender: webContents,
    senderFrame: { ...webContents.mainFrame },
    ...overrides
  };
}

function deferred<T>(): {
  promise: Promise<T>;
  resolve(value: T): void;
} {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((nextResolve) => {
    resolve = nextResolve;
  });
  return { promise, resolve };
}

function harness(
  overrides: Partial<{
    submit(valueBytes: Uint8Array): Promise<void>;
    cancel(): void;
  }> = {}
): {
  webContents: FakeWebContents;
  handlers: Map<
    string,
    (
      event: CredentialIpcEvent,
      request: unknown
    ) => Promise<unknown>
  >;
  removed: string[];
  submit: ReturnType<typeof vi.fn>;
  cancel: ReturnType<typeof vi.fn>;
  dispose(): void;
} {
  const handlers = new Map<
    string,
    (
      event: CredentialIpcEvent,
      request: unknown
    ) => Promise<unknown>
  >();
  const removed: string[] = [];
  const ipcMain: CredentialIpcMain = {
    handle(
      channel: string,
      listener: (
        event: CredentialIpcEvent,
        request: unknown
      ) => Promise<unknown>
    ) {
      handlers.set(channel, listener);
    },
    removeHandler(channel: string) {
      removed.push(channel);
      handlers.delete(channel);
    }
  };
  const webContents = new FakeWebContents(71);
  const submit = vi.fn(
    overrides.submit ?? (async () => undefined)
  );
  const cancel = vi.fn(overrides.cancel ?? (() => undefined));
  const authority = installCredentialIpc(ipcMain, {
    webContents,
    context,
    submit,
    cancel
  });
  return {
    webContents,
    handlers,
    removed,
    submit,
    cancel,
    dispose: authority.dispose
  };
}

describe("credential-only main IPC", () => {
  it("registers and disposes only the exact three private channels", () => {
    const { handlers, removed, dispose } = harness();

    expect([...handlers.keys()].sort()).toEqual(
      Object.values(DESKTOP_CREDENTIAL_IPC_CHANNELS).sort()
    );
    expect(handlers).not.toHaveProperty(
      "kestrel:desktop:credential-dialog"
    );

    dispose();
    dispose();
    expect(removed.sort()).toEqual(
      Object.values(DESKTOP_CREDENTIAL_IPC_CHANNELS).sort()
    );
    expect(handlers.size).toBe(0);
  });

  it("returns only bounded metadata and normal cancel acknowledgement to the exact live main frame", async () => {
    const {
      webContents,
      handlers,
      cancel
    } = harness();

    await expect(
      handlers.get(DESKTOP_CREDENTIAL_IPC_CHANNELS.bootstrap)!(
        eventFor(webContents),
        { schema: "kestrel.credential.bootstrap.v1" }
      )
    ).resolves.toEqual({
      ok: true,
      value: context
    });
    await expect(
      handlers.get(DESKTOP_CREDENTIAL_IPC_CHANNELS.cancel)!(
        eventFor(webContents),
        { schema: "kestrel.credential.cancel.v1" }
      )
    ).resolves.toEqual({
      ok: true,
      value: { status: "cancelled" }
    });
    expect(cancel).toHaveBeenCalledOnce();
    expect(JSON.stringify(context)).not.toMatch(
      /token|capability|secretRef|value/i
    );
  });

  it("rejects substituted, subframe, stale, non-credential, and destroyed senders", async () => {
    const { webContents, handlers } = harness();
    const handler = handlers.get(
      DESKTOP_CREDENTIAL_IPC_CHANNELS.bootstrap
    )!;
    const substituted = new FakeWebContents(webContents.id);
    const staleFrame = { ...webContents.mainFrame };
    webContents.mainFrame = {
      ...webContents.mainFrame,
      routingId: webContents.mainFrame.routingId + 1
    };
    const events = [
      eventFor(substituted),
      eventFor(webContents, {
        senderFrame: {
          ...webContents.mainFrame,
          processId: webContents.mainFrame.processId + 1
        }
      }),
      eventFor(webContents, {
        senderFrame: {
          ...webContents.mainFrame,
          routingId: webContents.mainFrame.routingId + 1
        }
      }),
      eventFor(webContents, {
        senderFrame: {
          ...webContents.mainFrame,
          isMainFrame: false
        }
      }),
      eventFor(webContents, {
        senderFrame: {
          ...webContents.mainFrame,
          url: "kestrel://app/index.html"
        }
      }),
      eventFor(webContents, {
        senderFrame: {
          ...webContents.mainFrame,
          url: "kestrel://credential.evil/index.html"
        }
      }),
      eventFor(webContents, { senderFrame: staleFrame }),
      eventFor(webContents, { senderFrame: null })
    ];

    for (const event of events) {
      await expect(
        handler(event, {
          schema: "kestrel.credential.bootstrap.v1"
        })
      ).resolves.toEqual({
        ok: false,
        error: { code: "desktop_sender_untrusted" }
      });
    }

    webContents.destroyed = true;
    await expect(
      handler(eventFor(webContents), {
        schema: "kestrel.credential.bootstrap.v1"
      })
    ).resolves.toEqual({
      ok: false,
      error: { code: "desktop_sender_untrusted" }
    });
  });

  it("rejects an in-place mutation of the bound main-frame identity", async () => {
    const { webContents, handlers } = harness();
    const handler = handlers.get(
      DESKTOP_CREDENTIAL_IPC_CHANNELS.bootstrap
    )!;

    await expect(
      handler(eventFor(webContents), {
        schema: "kestrel.credential.bootstrap.v1"
      })
    ).resolves.toEqual({
      ok: true,
      value: context
    });
    webContents.mainFrame.routingId += 1;

    await expect(
      handler(eventFor(webContents), {
        schema: "kestrel.credential.bootstrap.v1"
      })
    ).resolves.toEqual({
      ok: false,
      error: { code: "desktop_sender_untrusted" }
    });
  });

  it("accepts only an exact plain submit object with an exact Uint8Array", async () => {
    const {
      webContents,
      handlers,
      submit
    } = harness();
    const handler = handlers.get(
      DESKTOP_CREDENTIAL_IPC_CHANNELS.submit
    )!;
    const accessor = Object.defineProperty(
      {
        schema: "kestrel.credential.submit.v1"
      },
      "valueBytes",
      {
        enumerable: true,
        get: () => new Uint8Array([1])
      }
    );
    const customPrototype = Object.assign(
      Object.create({ inherited: true }),
      {
        schema: "kestrel.credential.submit.v1",
        valueBytes: new Uint8Array([1])
      }
    );
    const invalid = [
      null,
      [],
      {
        schema: "kestrel.credential.submit.v0",
        valueBytes: new Uint8Array([1])
      },
      {
        schema: "kestrel.credential.submit.v1",
        valueBytes: []
      },
      {
        schema: "kestrel.credential.submit.v1",
        valueBytes: Buffer.from("buffer-subclass")
      },
      {
        schema: "kestrel.credential.submit.v1",
        valueBytes: new DataView(new ArrayBuffer(1))
      },
      {
        schema: "kestrel.credential.submit.v1",
        valueBytes: new Uint8Array()
      },
      {
        schema: "kestrel.credential.submit.v1",
        valueBytes: new Uint8Array(16_385)
      },
      {
        schema: "kestrel.credential.submit.v1",
        valueBytes: new Uint8Array([1]),
        extra: true
      },
      {
        schema: "kestrel.credential.submit.v1",
        valueBytes: new Uint8Array([1]),
        [Symbol("extra")]: true
      },
      accessor,
      customPrototype
    ];

    for (const request of invalid) {
      await expect(
        handler(eventFor(webContents), request)
      ).resolves.toEqual({
        ok: false,
        error: { code: "invalid_desktop_request" }
      });
    }
    expect(submit).not.toHaveBeenCalled();
  });

  it("scrubs accessible rejected submit views without getters or recursive mutation", async () => {
    const { webContents, handlers, submit } = harness();
    const handler = handlers.get(
      DESKTOP_CREDENTIAL_IPC_CHANNELS.submit
    )!;
    const cases: Array<{
      name: string;
      event: CredentialIpcEvent;
      request: unknown;
      scrubbed: Uint8Array;
      untouched?: Uint8Array;
      getter?: ReturnType<typeof vi.fn>;
    }> = [];
    const add = (
      name: string,
      request: unknown,
      scrubbed: Uint8Array,
      event = eventFor(webContents),
      untouched?: Uint8Array
    ): void => {
      cases.push({
        name,
        event,
        request,
        scrubbed,
        untouched
      });
    };

    const wrongSenderBytes = new Uint8Array([11, 12]);
    add(
      "wrong sender",
      {
        schema: "kestrel.credential.submit.v1",
        valueBytes: wrongSenderBytes
      },
      wrongSenderBytes,
      eventFor(new FakeWebContents(webContents.id))
    );
    const wrongSchemaBytes = new Uint8Array([21, 22]);
    add(
      "wrong schema",
      {
        schema: "kestrel.credential.submit.v0",
        valueBytes: wrongSchemaBytes
      },
      wrongSchemaBytes
    );
    const unrelated = new Uint8Array([31, 32]);
    const extraBytes = new Uint8Array([33, 34]);
    add(
      "extra key",
      {
        schema: "kestrel.credential.submit.v1",
        valueBytes: extraBytes,
        extra: { nested: unrelated }
      },
      extraBytes,
      eventFor(webContents),
      unrelated
    );
    const symbolBytes = new Uint8Array([41, 42]);
    add(
      "symbol key",
      {
        schema: "kestrel.credential.submit.v1",
        valueBytes: symbolBytes,
        [Symbol("extra")]: true
      },
      symbolBytes
    );
    const prototypeBytes = new Uint8Array([51, 52]);
    add(
      "custom prototype",
      Object.assign(Object.create({ inherited: true }), {
        schema: "kestrel.credential.submit.v1",
        valueBytes: prototypeBytes
      }),
      prototypeBytes
    );
    const oversizedBytes = new Uint8Array(16_385);
    oversizedBytes.fill(61);
    add(
      "oversized",
      {
        schema: "kestrel.credential.submit.v1",
        valueBytes: oversizedBytes
      },
      oversizedBytes
    );
    const buffer = Buffer.from([71, 72]);
    add(
      "Buffer",
      {
        schema: "kestrel.credential.submit.v1",
        valueBytes: buffer
      },
      buffer
    );
    const dataViewBacking = new Uint8Array([81, 82]);
    add(
      "DataView",
      {
        schema: "kestrel.credential.submit.v1",
        valueBytes: new DataView(dataViewBacking.buffer)
      },
      dataViewBacking
    );
    const getterBytes = new Uint8Array([91, 92]);
    const getter = vi.fn(() => getterBytes);
    const accessor = Object.defineProperty(
      {
        schema: "kestrel.credential.submit.v1"
      },
      "valueBytes",
      {
        enumerable: true,
        get: getter
      }
    );
    cases.push({
      name: "accessor",
      event: eventFor(webContents),
      request: accessor,
      scrubbed: getterBytes,
      untouched: getterBytes,
      getter
    });

    for (const rejected of cases) {
      const response = await handler(
        rejected.event,
        rejected.request
      );
      expect(
        response,
        rejected.name
      ).toMatchObject({ ok: false });
      if (rejected.name === "accessor") {
        expect(rejected.getter).not.toHaveBeenCalled();
      } else {
        expect(
          [...rejected.scrubbed].every(
            (value) => value === 0
          ),
          rejected.name
        ).toBe(true);
      }
      if (rejected.untouched !== undefined) {
        expect(
          [...rejected.untouched].some(
            (value) => value !== 0
          ),
          `${rejected.name} unrelated data`
        ).toBe(true);
      }
    }
    expect(submit).not.toHaveBeenCalled();
  });

  it("scrubs the structured-clone input immediately after making the owned copy", async () => {
    const pending = deferred<void>();
    let received: Uint8Array | undefined;
    const { webContents, handlers } = harness({
      submit: async (valueBytes) => {
        received = valueBytes;
        await pending.promise;
      }
    });
    const sent = new TextEncoder().encode(
      "ipc-immediate-private"
    );
    const response = handlers.get(
      DESKTOP_CREDENTIAL_IPC_CHANNELS.submit
    )!(eventFor(webContents), {
      schema: "kestrel.credential.submit.v1",
      valueBytes: sent
    });

    await Promise.resolve();
    expect([...sent].every((value) => value === 0)).toBe(true);
    expect(new TextDecoder().decode(received)).toBe(
      "ipc-immediate-private"
    );

    pending.resolve();
    await expect(response).resolves.toEqual({
      ok: true,
      value: { status: "stored" }
    });
    expect(
      [...(received ?? [])].every((value) => value === 0)
    ).toBe(true);
  });

  it("copies and scrubs an accepted view without invoking own shadowed typed-array accessors, methods, or iterator", async () => {
    let submittedText = "";
    const { webContents, handlers } = harness({
      submit: async (valueBytes) => {
        submittedText = new TextDecoder().decode(valueBytes);
      }
    });
    const backing = new ArrayBuffer(3);
    const inspection = new Uint8Array(backing);
    inspection.set([107, 101, 121]);
    const rendererBytes = new Uint8Array(backing);
    const shadowed = {
      buffer: vi.fn(() => {
        throw new Error("shadowed_buffer_called");
      }),
      byteOffset: vi.fn(() => {
        throw new Error("shadowed_byte_offset_called");
      }),
      byteLength: vi.fn(() => {
        throw new Error("shadowed_byte_length_called");
      }),
      iterator: vi.fn(() => {
        throw new Error("shadowed_iterator_called");
      }),
      fill: vi.fn(() => {
        throw new Error("shadowed_fill_called");
      }),
      set: vi.fn(() => {
        throw new Error("shadowed_set_called");
      })
    };
    Object.defineProperties(rendererBytes, {
      buffer: { get: shadowed.buffer },
      byteOffset: { get: shadowed.byteOffset },
      byteLength: { get: shadowed.byteLength },
      [Symbol.iterator]: { value: shadowed.iterator },
      fill: { value: shadowed.fill },
      set: { value: shadowed.set }
    });

    await expect(
      handlers.get(DESKTOP_CREDENTIAL_IPC_CHANNELS.submit)!(
        eventFor(webContents),
        {
          schema: "kestrel.credential.submit.v1",
          valueBytes: rendererBytes
        }
      )
    ).resolves.toEqual({
      ok: true,
      value: { status: "stored" }
    });

    expect(submittedText).toBe("key");
    for (const shadow of Object.values(shadowed)) {
      expect(shadow).not.toHaveBeenCalled();
    }
    expect(Array.from(inspection)).toEqual([0, 0, 0]);
  });

  it("uses module-captured typed-array intrinsics when the accepted view prototype is shadowed later", async () => {
    const originalDescriptors = new Map<
      PropertyKey,
      PropertyDescriptor | undefined
    >();
    const prototype = Uint8Array.prototype;
    const shadowed = new Map<PropertyKey, ReturnType<typeof vi.fn>>();
    for (const key of [
      "buffer",
      "byteOffset",
      "byteLength",
      Symbol.iterator,
      "fill",
      "set"
    ] as const) {
      originalDescriptors.set(
        key,
        Object.getOwnPropertyDescriptor(prototype, key)
      );
      const spy = vi.fn(() => {
        throw new Error(`shadowed_${String(key)}_called`);
      });
      shadowed.set(key, spy);
      Object.defineProperty(
        prototype,
        key,
        typeof key === "string" &&
          ["buffer", "byteOffset", "byteLength"].includes(key)
          ? { configurable: true, get: spy }
          : { configurable: true, value: spy }
      );
    }
    const backing = new ArrayBuffer(3);
    const inspection = new Uint8Array(backing);
    const rendererBytes = new Uint8Array(backing);
    let submittedText = "";
    const { webContents, handlers } = harness({
      submit: async (valueBytes) => {
        submittedText = new TextDecoder().decode(valueBytes);
      }
    });

    try {
      new DataView(backing).setUint8(0, 107);
      new DataView(backing).setUint8(1, 101);
      new DataView(backing).setUint8(2, 121);
      await expect(
        handlers.get(DESKTOP_CREDENTIAL_IPC_CHANNELS.submit)!(
          eventFor(webContents),
          {
            schema: "kestrel.credential.submit.v1",
            valueBytes: rendererBytes
          }
        )
      ).resolves.toEqual({
        ok: true,
        value: { status: "stored" }
      });
    } finally {
      for (const [key, descriptor] of originalDescriptors) {
        if (descriptor === undefined) {
          Reflect.deleteProperty(prototype, key);
        } else {
          Object.defineProperty(prototype, key, descriptor);
        }
      }
    }

    expect(submittedText).toBe("key");
    for (const spy of shadowed.values()) {
      expect(spy).not.toHaveBeenCalled();
    }
    expect(Array.from(inspection)).toEqual([0, 0, 0]);
  });

  it("rejects and scrubs custom-prototype typed arrays without invoking their shadowed properties", async () => {
    const backing = new ArrayBuffer(4);
    const inspection = new Uint8Array(backing);
    inspection.set([9, 8, 7, 6]);
    const rendererBytes = new Uint8Array(backing);
    const shadowed = {
      buffer: vi.fn(() => {
        throw new Error("prototype_buffer_called");
      }),
      byteOffset: vi.fn(() => {
        throw new Error("prototype_byte_offset_called");
      }),
      byteLength: vi.fn(() => {
        throw new Error("prototype_byte_length_called");
      }),
      iterator: vi.fn(() => {
        throw new Error("prototype_iterator_called");
      }),
      fill: vi.fn(() => {
        throw new Error("prototype_fill_called");
      }),
      set: vi.fn(() => {
        throw new Error("prototype_set_called");
      })
    };
    const poisonedPrototype = Object.create(
      Uint8Array.prototype,
      {
        buffer: { get: shadowed.buffer },
        byteOffset: { get: shadowed.byteOffset },
        byteLength: { get: shadowed.byteLength },
        [Symbol.iterator]: { value: shadowed.iterator },
        fill: { value: shadowed.fill },
        set: { value: shadowed.set }
      }
    );
    Object.setPrototypeOf(rendererBytes, poisonedPrototype);
    const { webContents, handlers, submit } = harness();

    await expect(
      handlers.get(DESKTOP_CREDENTIAL_IPC_CHANNELS.submit)!(
        eventFor(webContents),
        {
          schema: "kestrel.credential.submit.v1",
          valueBytes: rendererBytes
        }
      )
    ).resolves.toEqual({
      ok: false,
      error: { code: "invalid_desktop_request" }
    });

    expect(submit).not.toHaveBeenCalled();
    for (const shadow of Object.values(shadowed)) {
      expect(shadow).not.toHaveBeenCalled();
    }
    expect(Array.from(inspection)).toEqual([0, 0, 0, 0]);
  });

  it("scrubs rejected DataView and non-byte typed arrays through captured view intrinsics", async () => {
    const { webContents, handlers, submit } = harness();
    const cases: Array<{
      name: string;
      view: ArrayBufferView;
      inspection: Uint8Array;
      shadows: Array<ReturnType<typeof vi.fn>>;
    }> = [];
    const addCase = (
      name: string,
      makeView: (backing: ArrayBuffer) => ArrayBufferView
    ): void => {
      const backing = new ArrayBuffer(4);
      const inspection = new Uint8Array(backing);
      inspection.set([5, 4, 3, 2]);
      const view = makeView(backing);
      const shadows = [
        vi.fn(() => {
          throw new Error(`${name}_buffer_called`);
        }),
        vi.fn(() => {
          throw new Error(`${name}_byte_offset_called`);
        }),
        vi.fn(() => {
          throw new Error(`${name}_byte_length_called`);
        }),
        vi.fn(() => {
          throw new Error(`${name}_iterator_called`);
        }),
        vi.fn(() => {
          throw new Error(`${name}_fill_called`);
        })
      ];
      Object.defineProperties(view, {
        buffer: { get: shadows[0] },
        byteOffset: { get: shadows[1] },
        byteLength: { get: shadows[2] },
        [Symbol.iterator]: { value: shadows[3] },
        fill: { value: shadows[4] }
      });
      cases.push({ name, view, inspection, shadows });
    };
    addCase("DataView", (backing) => new DataView(backing));
    addCase("Int16Array", (backing) => new Int16Array(backing));

    for (const testCase of cases) {
      await expect(
        handlers.get(DESKTOP_CREDENTIAL_IPC_CHANNELS.submit)!(
          eventFor(webContents),
          {
            schema: "kestrel.credential.submit.v1",
            valueBytes: testCase.view
          }
        ),
        testCase.name
      ).resolves.toEqual({
        ok: false,
        error: { code: "invalid_desktop_request" }
      });
      for (const shadow of testCase.shadows) {
        expect(
          shadow,
          `${testCase.name} shadow`
        ).not.toHaveBeenCalled();
      }
      expect(
        Array.from(testCase.inspection),
        testCase.name
      ).toEqual([0, 0, 0, 0]);
    }
    expect(submit).not.toHaveBeenCalled();
  });

  it("passes a distinct main-owned byte copy and clears both copies after success", async () => {
    let received: Uint8Array | undefined;
    const {
      webContents,
      handlers,
      submit
    } = harness({
      submit: async (valueBytes) => {
        received = valueBytes;
        expect(
          new TextDecoder().decode(valueBytes)
        ).toBe("ipc-private-sentinel");
      }
    });
    const sent = new TextEncoder().encode(
      "ipc-private-sentinel"
    );

    await expect(
      handlers.get(DESKTOP_CREDENTIAL_IPC_CHANNELS.submit)!(
        eventFor(webContents),
        {
          schema: "kestrel.credential.submit.v1",
          valueBytes: sent
        }
      )
    ).resolves.toEqual({
      ok: true,
      value: { status: "stored" }
    });

    expect(submit).toHaveBeenCalledOnce();
    expect(received).toBeDefined();
    expect(received).not.toBe(sent);
    expect([...sent].every((value) => value === 0)).toBe(true);
    expect(
      [...received!].every((value) => value === 0)
    ).toBe(true);
  });

  it("clears both byte copies and returns a fixed error after submit failure", async () => {
    let received: Uint8Array | undefined;
    const { webContents, handlers } = harness({
      submit: async (valueBytes) => {
        received = valueBytes;
        throw new Error("native-ipc-private-sentinel");
      }
    });
    const sent = new TextEncoder().encode(
      "ipc-failure-private-sentinel"
    );

    const response = await handlers.get(
      DESKTOP_CREDENTIAL_IPC_CHANNELS.submit
    )!(eventFor(webContents), {
      schema: "kestrel.credential.submit.v1",
      valueBytes: sent
    });

    expect(response).toEqual({
      ok: false,
      error: { code: "desktop_operation_failed" }
    });
    expect(JSON.stringify(response)).not.toContain("sentinel");
    expect([...sent].every((value) => value === 0)).toBe(true);
    expect(
      [...(received ?? [])].every((value) => value === 0)
    ).toBe(true);
  });
});
