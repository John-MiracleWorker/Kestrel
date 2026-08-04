import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import {
  applyTheme,
  installTheme,
  MOTION_CHANGE_EVENT,
  MOTION_STORAGE_KEY,
  readThemePreference,
  resolveTheme,
  setMotionPreference,
  setThemePreference,
  THEME_CHANGE_EVENT,
  THEME_STORAGE_KEY,
} from "./theme";

const REQUIRED_TOKENS = [
  "canvas",
  "surface",
  "ink",
  "muted-ink",
  "structural",
  "action",
  "on-action",
  "success",
  "attention",
  "caution",
  "info",
  "danger",
  "focus",
  "border",
  "border-strong",
  "shadow",
  "shadow-offset",
] as const;
const ACCESSIBLE_FOREGROUNDS = [
  "ink",
  "muted-ink",
  "structural",
  "action",
  "success",
  "attention",
  "caution",
  "info",
  "danger",
  "focus",
] as const;

describe("Wildflower theme", () => {
  it("defines every semantic token in light and dark themes", () => {
    const css = readFileSync(
      `${process.cwd()}/src/design/tokens.css`,
      "utf8",
    );
    const light = themeBlock(css, "light");
    const dark = themeBlock(css, "dark");

    for (const token of REQUIRED_TOKENS) {
      expect(light, `light --${token}`).toContain(`--${token}:`);
      expect(dark, `dark --${token}`).toContain(`--${token}:`);
    }
  });

  it("resolves system preference without overriding the OS", () => {
    expect(resolveTheme("system", false)).toBe("light");
    expect(resolveTheme("system", true)).toBe("dark");
    expect(resolveTheme("light", true)).toBe("light");
    expect(resolveTheme("dark", false)).toBe("dark");
  });

  it("applies the resolved theme and color-scheme semantics", () => {
    const attributes = new Map<string, string>();
    const root = {
      dataset: {} as Record<string, string>,
      style: {
        colorScheme: "",
      },
      setAttribute(name: string, value: string) {
        attributes.set(name, value);
      },
    };

    applyTheme(root, "system", true);

    expect(root.dataset.theme).toBe("dark");
    expect(root.dataset.themePreference).toBe("system");
    expect(root.style.colorScheme).toBe("dark");
    expect(attributes.get("data-theme")).toBe("dark");
  });

  it("falls back safely when persisted preference is invalid", () => {
    const storage = {
      getItem(key: string) {
        expect(key).toBe(THEME_STORAGE_KEY);
        return "neon";
      },
    };

    expect(readThemePreference(storage)).toBe("system");
  });

  it("keeps semantic foregrounds AA against canvas and surface", () => {
    const css = readFileSync(
      `${process.cwd()}/src/design/tokens.css`,
      "utf8",
    );

    for (const theme of ["light", "dark"] as const) {
      const block = themeBlock(css, theme);
      for (const background of ["canvas", "surface"] as const) {
        for (const foreground of ACCESSIBLE_FOREGROUNDS) {
          expect(
            contrastRatio(tokenHex(block, foreground), tokenHex(block, background)),
            `${theme} --${foreground} on --${background}`,
          ).toBeGreaterThanOrEqual(4.5);
        }
      }
      expect(
        contrastRatio(tokenHex(block, "on-action"), tokenHex(block, "action")),
        `${theme} --on-action on --action`,
      ).toBeGreaterThanOrEqual(4.5);
    }
  });

  it("tracks media, storage, and local preference changes, then removes listeners", () => {
    const root = createThemeRoot();
    const listeners = new Map<string, Set<EventListener>>();
    const mediaListeners = new Set<(event: MediaQueryListEvent) => void>();
    const media = {
      matches: false,
      addEventListener(_name: string, listener: (event: MediaQueryListEvent) => void) {
        mediaListeners.add(listener);
      },
      removeEventListener(_name: string, listener: (event: MediaQueryListEvent) => void) {
        mediaListeners.delete(listener);
      },
    };
    const host = {
      localStorage: {
        getItem: () => "system",
      },
      matchMedia: () => media,
      addEventListener(name: string, listener: EventListener) {
        const bucket = listeners.get(name) ?? new Set<EventListener>();
        bucket.add(listener);
        listeners.set(name, bucket);
      },
      removeEventListener(name: string, listener: EventListener) {
        listeners.get(name)?.delete(listener);
      },
    } as unknown as Window;

    const cleanup = installTheme(root, host);
    expect(root.dataset.theme).toBe("light");

    media.matches = true;
    for (const listener of mediaListeners) {
      listener({ matches: true } as MediaQueryListEvent);
    }
    expect(root.dataset.theme).toBe("dark");

    for (const listener of listeners.get("storage") ?? []) {
      listener(
        new StorageEvent("storage", {
          key: THEME_STORAGE_KEY,
          newValue: "light",
        }),
      );
    }
    expect(root.dataset.theme).toBe("light");

    for (const listener of listeners.get(THEME_CHANGE_EVENT) ?? []) {
      listener(new CustomEvent(THEME_CHANGE_EVENT, { detail: "dark" }));
    }
    expect(root.dataset.theme).toBe("dark");

    for (const listener of listeners.get(MOTION_CHANGE_EVENT) ?? []) {
      listener(new CustomEvent(MOTION_CHANGE_EVENT, { detail: "reduce" }));
    }
    expect(root.dataset.reducedMotion).toBe("reduce");

    for (const listener of listeners.get("storage") ?? []) {
      listener(
        new StorageEvent("storage", {
          key: MOTION_STORAGE_KEY,
          newValue: "system",
        }),
      );
    }
    expect(root.dataset.reducedMotion).toBeUndefined();

    cleanup();
    expect(mediaListeners.size).toBe(0);
    expect(listeners.get("storage")?.size).toBe(0);
    expect(listeners.get(THEME_CHANGE_EVENT)?.size).toBe(0);
    expect(listeners.get(MOTION_CHANGE_EVENT)?.size).toBe(0);
  });

  it("publishes local theme and reduction settings after attempting persistence", () => {
    const writes: Array<[string, string]> = [];
    const events: Event[] = [];
    const host = {
      localStorage: {
        setItem(key: string, value: string) {
          writes.push([key, value]);
        },
      },
      dispatchEvent(event: Event) {
        events.push(event);
        return true;
      },
    } as Pick<Window, "localStorage" | "dispatchEvent">;

    setThemePreference("light", host);
    setMotionPreference("reduce", host);

    expect(writes).toEqual([
      [THEME_STORAGE_KEY, "light"],
      [MOTION_STORAGE_KEY, "reduce"],
    ]);
    expect(
      events.map((event) => [
        event.type,
        (event as CustomEvent<string>).detail,
      ]),
    ).toEqual([
      [THEME_CHANGE_EVENT, "light"],
      [MOTION_CHANGE_EVENT, "reduce"],
    ]);
  });

  it("mounts with system appearance when storage access is denied", () => {
    const root = createThemeRoot();
    const listeners = new Map<string, Set<EventListener>>();
    const mediaListeners = new Set<(event: MediaQueryListEvent) => void>();
    const media = {
      matches: true,
      addEventListener(_name: string, listener: (event: MediaQueryListEvent) => void) {
        mediaListeners.add(listener);
      },
      removeEventListener(_name: string, listener: (event: MediaQueryListEvent) => void) {
        mediaListeners.delete(listener);
      },
    };
    const host = {
      get localStorage(): Storage {
        throw new DOMException("storage denied", "SecurityError");
      },
      matchMedia: () => media,
      addEventListener(name: string, listener: EventListener) {
        const bucket = listeners.get(name) ?? new Set<EventListener>();
        bucket.add(listener);
        listeners.set(name, bucket);
      },
      removeEventListener(name: string, listener: EventListener) {
        listeners.get(name)?.delete(listener);
      },
    } as unknown as Window;

    let cleanup: (() => void) | undefined;
    expect(() => {
      cleanup = installTheme(root, host);
    }).not.toThrow();
    expect(root.dataset.theme).toBe("dark");
    expect(root.dataset.themePreference).toBe("system");
    expect(root.dataset.reducedMotion).toBeUndefined();

    cleanup?.();
    expect(mediaListeners.size).toBe(0);
    expect(listeners.get("storage")?.size).toBe(0);
  });
});

function themeBlock(css: string, theme: "light" | "dark"): string {
  const marker = `/* theme:${theme} */`;
  const start = css.indexOf(marker);
  const next = css.indexOf("/* theme:", start + marker.length);
  expect(start).toBeGreaterThanOrEqual(0);
  return css.slice(start, next === -1 ? undefined : next);
}

function tokenHex(block: string, token: string): string {
  const match = block.match(new RegExp(`--${token}:\\s*(#[0-9a-f]{6})`, "i"));
  expect(match, `missing hex token --${token}`).not.toBeNull();
  return match?.[1] ?? "#000000";
}

function contrastRatio(left: string, right: string): number {
  const [lighter, darker] = [luminance(left), luminance(right)].sort(
    (first, second) => second - first,
  );
  return (lighter + 0.05) / (darker + 0.05);
}

function luminance(hex: string): number {
  const channels = hex
    .slice(1)
    .match(/../g)
    ?.map((value) => Number.parseInt(value, 16) / 255)
    .map((value) =>
      value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4,
    ) ?? [0, 0, 0];
  return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
}

function createThemeRoot() {
  const attributes = new Map<string, string>();
  return {
    dataset: {} as Record<string, string>,
    style: {
      colorScheme: "",
    },
    setAttribute(name: string, value: string) {
      attributes.set(name, value);
    },
    removeAttribute(name: string) {
      attributes.delete(name);
      if (name === "data-reduced-motion") {
        delete this.dataset.reducedMotion;
      }
    },
  };
}
