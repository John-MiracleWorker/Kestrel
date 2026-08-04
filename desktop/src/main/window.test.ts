import { describe, expect, it, vi } from "vitest";
import { DESKTOP_APP_ENTRY_URL } from "../contracts";
import {
  createAppWindow,
  createSingleWindowController,
  startVerifiedDesktopSession,
  windowOptions
} from "./window";

class FakeWindow {
  readonly webContents = {};
  readonly loadedUrls: string[] = [];
  readonly listeners = new Map<string, () => void>();
  minimized = false;
  destroyed = false;
  focusCount = 0;
  restoreCount = 0;
  showCount = 0;

  constructor(readonly options: ReturnType<typeof windowOptions>) {}

  async loadURL(url: string): Promise<void> {
    this.loadedUrls.push(url);
  }

  once(event: string, listener: () => void): this {
    this.listeners.set(event, listener);
    return this;
  }

  emit(event: string): void {
    this.listeners.get(event)?.();
  }

  isDestroyed(): boolean {
    return this.destroyed;
  }

  isMinimized(): boolean {
    return this.minimized;
  }

  restore(): void {
    this.minimized = false;
    this.restoreCount += 1;
  }

  focus(): void {
    this.focusCount += 1;
  }

  show(): void {
    this.showCount += 1;
  }
}

describe("desktop renderer window", () => {
  it("constructs every renderer with the mandatory boundary", () => {
    expect(windowOptions().webPreferences).toMatchObject({
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true,
      webSecurity: true
    });
    expect(windowOptions().webPreferences?.preload).toMatch(
      /[/\\]desktop[/\\]dist[/\\]preload\.js$/
    );
    expect(windowOptions().webPreferences).not.toHaveProperty("webviewTag", true);
  });

  it("loads only the private app entry URL and waits to show", async () => {
    const windows: FakeWindow[] = [];
    const installSecurity = vi.fn();
    const bindApiSession = vi.fn();
    const bindDesktopIpc = vi.fn();

    const created = createAppWindow({
      createWindow: (options) => {
        const window = new FakeWindow(options);
        windows.push(window);
        return window;
      },
      installSecurity,
      bindApiSession,
      bindDesktopIpc
    });

    await created.loaded;

    expect(windows).toHaveLength(1);
    expect(created.window).toBe(windows[0]);
    expect(windows[0]?.loadedUrls).toEqual([DESKTOP_APP_ENTRY_URL]);
    expect(windows[0]?.options.show).toBe(false);
    expect(installSecurity).toHaveBeenCalledWith(windows[0]?.webContents);
    expect(bindApiSession).toHaveBeenCalledWith(windows[0]?.webContents);
    expect(bindDesktopIpc).toHaveBeenCalledWith(windows[0]?.webContents);
    expect(installSecurity.mock.invocationCallOrder[0]).toBeLessThan(
      bindApiSession.mock.invocationCallOrder[0]!
    );
    expect(bindApiSession.mock.invocationCallOrder[0]).toBeLessThan(
      bindDesktopIpc.mock.invocationCallOrder[0]!
    );
    expect(windows[0]?.showCount).toBe(0);

    windows[0]?.emit("ready-to-show");
    expect(windows[0]?.showCount).toBe(1);
  });

  it("loads a reviewed stable route as the initial private entry", async () => {
    const entryUrl =
      "kestrel://app/index.html#/flock/qualification?run_id=run+1";
    const created = createAppWindow({
      entryUrl,
      createWindow: (options) => new FakeWindow(options),
      installSecurity: () => undefined,
      bindApiSession: () => undefined,
      bindDesktopIpc: () => undefined
    });

    await created.loaded;

    expect(created.window.loadedUrls).toEqual([entryUrl]);
  });

  it("rejects an unreviewed initial route before creating a renderer", () => {
    const createWindow = vi.fn(
      (options: ReturnType<typeof windowOptions>) =>
        new FakeWindow(options)
    );

    expect(() =>
      createAppWindow({
        entryUrl:
          "kestrel://app/index.html#/unknown/overview",
        createWindow,
        installSecurity: () => undefined,
        bindApiSession: () => undefined,
        bindDesktopIpc: () => undefined
      })
    ).toThrow("desktop_app_route_untrusted");
    expect(createWindow).not.toHaveBeenCalled();
  });

  it("loads Mission Command without ever showing it for developer directory smoke", async () => {
    const created = createAppWindow({
      showWhenReady: false,
      createWindow: (options) => new FakeWindow(options),
      installSecurity: () => undefined,
      bindApiSession: () => undefined,
      bindDesktopIpc: () => undefined
    });

    await created.loaded;
    created.window.emit("ready-to-show");

    expect(created.window.loadedUrls).toEqual([DESKTOP_APP_ENTRY_URL]);
    expect(created.window.options.show).toBe(false);
    expect(created.window.showCount).toBe(0);
  });

  it("reuses and focuses the sole live window", () => {
    const windows: FakeWindow[] = [];
    const controller = createSingleWindowController(() => {
      const window = new FakeWindow(windowOptions());
      windows.push(window);
      return window;
    });

    const first = controller.openOrFocus();
    first.minimized = true;
    const second = controller.openOrFocus();

    expect(second).toBe(first);
    expect(windows).toHaveLength(1);
    expect(first.restoreCount).toBe(1);
    expect(first.focusCount).toBe(1);

    first.destroyed = true;
    expect(controller.openOrFocus()).not.toBe(first);
    expect(windows).toHaveLength(2);
  });

  it("opens a reviewed deep link in the sole live window without changing routes on ordinary focus", async () => {
    const createdWith: string[] = [];
    const controller = createSingleWindowController(
      (entryUrl) => {
        createdWith.push(entryUrl);
        return new FakeWindow(windowOptions());
      }
    );
    const flockRoute =
      "kestrel://app/index.html#/flock/qualification";
    const settingsRoute =
      "kestrel://app/index.html#/settings/updates";

    const first = controller.openOrFocus(flockRoute);
    controller.openOrFocus(settingsRoute);
    controller.openOrFocus();
    await Promise.resolve();

    expect(createdWith).toEqual([flockRoute]);
    expect(first.loadedUrls).toEqual([settingsRoute]);
    expect(first.focusCount).toBe(2);
  });

  it("refuses to navigate the live renderer to an unreviewed route", () => {
    const controller = createSingleWindowController(
      () => new FakeWindow(windowOptions())
    );
    const first = controller.openOrFocus();

    expect(() =>
      controller.openOrFocus(
        "kestrel://app/index.html#/unknown/overview"
      )
    ).toThrow("desktop_app_route_untrusted");
    expect(first.loadedUrls).toEqual([]);
  });

  it("quits without registering or opening resource UI when verified startup fails", async () => {
    const events: string[] = [];

    const started = await startVerifiedDesktopSession({
      startSupervisor: async () => {
        events.push("supervisor");
        throw new Error("resource_signature_invalid");
      },
      registerVerifiedProtocol: () => events.push("protocol"),
      openWindow: () => events.push("window"),
      quit: () => events.push("quit")
    });

    expect(started).toBe(false);
    expect(events).toEqual(["supervisor", "quit"]);
  });

  it("registers verified snapshots only after supervisor readiness and before opening", async () => {
    const events: string[] = [];
    const resources = { renderer: "verified" };

    const started = await startVerifiedDesktopSession({
      startSupervisor: async () => {
        events.push("ready");
        return resources;
      },
      registerVerifiedProtocol: (received) => {
        expect(received).toBe(resources);
        events.push("protocol");
      },
      openWindow: () => events.push("window"),
      quit: () => events.push("quit")
    });

    expect(started).toBe(true);
    expect(events).toEqual(["ready", "protocol", "window"]);
  });
});
