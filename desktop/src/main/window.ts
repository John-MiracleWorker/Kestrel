import type { BrowserWindowConstructorOptions } from "electron";
import { fileURLToPath } from "node:url";
import { DESKTOP_APP_ENTRY_URL } from "../contracts.js";
import { isTrustedAppFrameUrl } from "./app-route.js";

export interface AppWindow {
  readonly webContents: unknown;
  loadURL(url: string): Promise<void>;
  once(event: string, listener: () => void): unknown;
  isDestroyed(): boolean;
  isMinimized(): boolean;
  restore(): void;
  focus(): void;
  show(): void;
}

export interface AppWindowResult<TWindow extends AppWindow> {
  window: TWindow;
  loaded: Promise<void>;
}

export async function startVerifiedDesktopSession<TVerified>(
  dependencies: {
    startSupervisor(): Promise<TVerified>;
    registerVerifiedProtocol(resources: TVerified): void;
    openWindow(): void;
    quit(): void;
  }
): Promise<boolean> {
  try {
    const verified = await dependencies.startSupervisor();
    dependencies.registerVerifiedProtocol(verified);
    dependencies.openWindow();
    return true;
  } catch {
    dependencies.quit();
    return false;
  }
}

export function windowOptions(): BrowserWindowConstructorOptions {
  return {
    width: 1320,
    height: 860,
    minWidth: 980,
    minHeight: 680,
    show: false,
    backgroundColor: "#fffaf0",
    webPreferences: {
      preload: fileURLToPath(
        new URL("../../dist/preload.js", import.meta.url)
      ),
      nodeIntegration: false,
      nodeIntegrationInWorker: false,
      nodeIntegrationInSubFrames: false,
      contextIsolation: true,
      sandbox: true,
      webSecurity: true,
      webviewTag: false,
      allowRunningInsecureContent: false
    }
  };
}

export function createAppWindow<TWindow extends AppWindow>(dependencies: {
  entryUrl?: string;
  showWhenReady?: boolean;
  createWindow(options: BrowserWindowConstructorOptions): TWindow;
  installSecurity(webContents: unknown): void;
  bindApiSession(webContents: unknown): void;
  bindDesktopIpc(webContents: unknown): void;
}): AppWindowResult<TWindow> {
  const entryUrl =
    dependencies.entryUrl ?? DESKTOP_APP_ENTRY_URL;
  if (!isTrustedAppFrameUrl(entryUrl)) {
    throw new Error("desktop_app_route_untrusted");
  }
  const window = dependencies.createWindow(windowOptions());
  dependencies.installSecurity(window.webContents);
  dependencies.bindApiSession(window.webContents);
  dependencies.bindDesktopIpc(window.webContents);
  if (dependencies.showWhenReady !== false) {
    window.once("ready-to-show", () => {
      if (!window.isDestroyed()) {
        window.show();
      }
    });
  }

  return {
    window,
    loaded: window.loadURL(entryUrl)
  };
}

export function createSingleWindowController<TWindow extends AppWindow>(
  createWindow: (entryUrl: string) => TWindow
): {
  openOrFocus(entryUrl?: string): TWindow;
  current(): TWindow | null;
} {
  let currentWindow: TWindow | null = null;

  return {
    openOrFocus(entryUrl?: string): TWindow {
      const reviewedEntryUrl =
        entryUrl ?? DESKTOP_APP_ENTRY_URL;
      if (!isTrustedAppFrameUrl(reviewedEntryUrl)) {
        throw new Error("desktop_app_route_untrusted");
      }
      if (currentWindow !== null && !currentWindow.isDestroyed()) {
        if (currentWindow.isMinimized()) {
          currentWindow.restore();
        }
        currentWindow.focus();
        if (entryUrl !== undefined) {
          void currentWindow
            .loadURL(reviewedEntryUrl)
            .catch(() => undefined);
        }
        return currentWindow;
      }

      const created = createWindow(reviewedEntryUrl);
      currentWindow = created;
      created.once("closed", () => {
        if (currentWindow === created) {
          currentWindow = null;
        }
      });
      return created;
    },
    current(): TWindow | null {
      return currentWindow;
    }
  };
}
