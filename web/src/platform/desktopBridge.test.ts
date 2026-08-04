import { afterEach, describe, expect, it, vi } from "vitest";
import {
  DESKTOP_BRIDGE_KEY,
  readDesktopBridge,
  type DesktopBridge
} from "./desktopBridge";

const methodNames = [
  "chooseProjectFolder",
  "chooseStorageFolder",
  "connection",
  "exportSupportBundle",
  "getAppVersion",
  "getUpdateStatus",
  "openCredentialDialog",
  "openExternalUrl",
  "performRecoveryAction",
  "subscribeLifecycle",
  "subscribeUpdateStatus"
] as const;

function validBridge(): DesktopBridge {
  return Object.freeze(
    Object.fromEntries(
      methodNames.map((name) => [
        name,
        name.startsWith("subscribe")
          ? () => () => undefined
          : async () => ({})
      ])
    )
  ) as unknown as DesktopBridge;
}

function install(value: unknown): void {
  Object.defineProperty(globalThis, DESKTOP_BRIDGE_KEY, {
    configurable: true,
    enumerable: false,
    writable: false,
    value
  });
}

afterEach(() => {
  Reflect.deleteProperty(globalThis, DESKTOP_BRIDGE_KEY);
  vi.restoreAllMocks();
});

describe("Web Desktop bridge adapter", () => {
  it("returns null in browser mode with neither Desktop global", () => {
    expect(readDesktopBridge()).toBeNull();
  });

  it("snapshots exactly the reviewed own frozen method set once", async () => {
    const source = validBridge();
    install(source);

    const snapshot = readDesktopBridge();
    expect(snapshot).not.toBe(source);
    expect(Reflect.ownKeys(snapshot ?? {}).sort()).toEqual(
      [...methodNames].sort()
    );
    expect(Object.isFrozen(snapshot)).toBe(true);
    await snapshot?.connection();
  });

  it.each([
    {
      name: "extra key",
      value: Object.freeze({ ...validBridge(), invoke: vi.fn() })
    },
    {
      name: "symbol key",
      value: Object.freeze({
        ...validBridge(),
        [Symbol("secret")]: vi.fn()
      })
    },
    {
      name: "partial bridge",
      value: Object.freeze({ connection: vi.fn() })
    },
    {
      name: "inherited authority",
      value: Object.freeze(
        Object.assign(
          Object.create({ invoke: vi.fn() }),
          Object.fromEntries(
            methodNames.map((name) => [name, vi.fn()])
          )
        )
      )
    },
    {
      name: "accessor",
      value: Object.freeze(
        Object.defineProperty(
          { ...validBridge() },
          "connection",
          { enumerable: true, get: () => vi.fn() }
        )
      )
    },
    {
      name: "non-function",
      value: Object.freeze({ ...validBridge(), connection: 7 })
    },
    {
      name: "unfrozen bridge",
      value: { ...validBridge() }
    },
    {
      name: "throwing proxy",
      value: new Proxy(validBridge(), {
        ownKeys() {
          throw new Error("proxy-secret");
        }
      })
    }
  ])("fails closed with a fixed error for a $name", ({ value }) => {
    install(value);

    expect(() => readDesktopBridge()).toThrow(
      "desktop_bridge_invalid"
    );
  });
});
