import { afterEach, describe, expect, it } from "vitest";
import {
  hasVisitedSetupCenter,
  readSetupPresentation,
  updateSetupPresentation,
} from "./presentation";

const nativeStorageDescriptor = Object.getOwnPropertyDescriptor(
  globalThis,
  "localStorage",
);

describe("Setup presentation hints", () => {
  afterEach(() => {
    if (nativeStorageDescriptor) {
      Object.defineProperty(
        globalThis,
        "localStorage",
        nativeStorageDescriptor,
      );
      localStorage.clear();
    }
  });

  it("remains non-authoritative when storage access is denied", () => {
    Object.defineProperty(globalThis, "localStorage", {
      configurable: true,
      get() {
        throw new DOMException("denied", "SecurityError");
      },
    });

    expect(readSetupPresentation()).toEqual({
      seen: false,
    });
    expect(() =>
      updateSetupPresentation({
        seen: true,
      }),
    ).not.toThrow();
    expect(hasVisitedSetupCenter()).toBe(false);
  });

  it("stores only presentation progress under the versioned key", () => {
    updateSetupPresentation({
      seen: true,
    });

    expect(readSetupPresentation()).toEqual({
      seen: true,
    });
    expect(localStorage.getItem("kestrel.setup.dismissed")).toBeNull();
  });
});
