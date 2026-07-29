import { join } from "node:path";
import { app, BrowserWindow, protocol, session } from "electron";
import {
  registerAppProtocol,
  registerKestrelScheme
} from "./main/protocol.js";
import {
  installSessionBoundary,
  installWebContentsBoundary,
  type RestrictedSession,
  type RestrictedWebContents
} from "./main/security.js";
import {
  createAppWindow,
  createSingleWindowController,
  type AppWindow
} from "./main/window.js";

registerKestrelScheme(protocol);

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  const windows = createSingleWindowController(() => {
    const created = createAppWindow({
      createWindow: (options) =>
        new BrowserWindow(options) as BrowserWindow & AppWindow,
      installSecurity: (webContents) => {
        installWebContentsBoundary(
          webContents as RestrictedWebContents
        );
      }
    });
    void created.loaded.catch(() => {
      if (!created.window.isDestroyed()) {
        created.window.destroy();
      }
      app.quit();
    });
    return created.window;
  });
  let desktopReady = false;

  app.on("second-instance", () => {
    if (desktopReady) {
      windows.openOrFocus();
    }
  });

  app.on("activate", () => {
    if (desktopReady) {
      windows.openOrFocus();
    }
  });

  app.on("window-all-closed", () => {
    if (process.platform !== "darwin") {
      app.quit();
    }
  });

  void app.whenReady().then(() => {
    installSessionBoundary(
      session.defaultSession as unknown as RestrictedSession
    );
    registerAppProtocol(
      protocol,
      join(process.resourcesPath, "web", "dist")
    );
    desktopReady = true;
    windows.openOrFocus();
  });
}
