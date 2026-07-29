import { createPublicKey } from "node:crypto";
import { readFile } from "node:fs/promises";
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
  startVerifiedDesktopSession,
  type AppWindow
} from "./main/window.js";
import {
  createNodeSupervisorDependencies,
  SidecarSupervisor
} from "./main/sidecar-supervisor.js";

const MAX_PUBLIC_KEY_BYTES = 16 * 1024;
const RELEASE_MANIFEST_KEY_ID = "release";

async function createPackagedSidecarSupervisor(): Promise<SidecarSupervisor> {
  const resourceRoot = process.resourcesPath;
  const sidecarName =
    process.platform === "win32"
      ? "kestrel-desktop-sidecar.exe"
      : "kestrel-desktop-sidecar";
  const sidecarRelativePath = `sidecar/${sidecarName}`;
  const publicKeyBytes = await readFile(
    join(app.getAppPath(), "config", "desktop-release-public-key.pem")
  );
  if (
    publicKeyBytes.byteLength === 0 ||
    publicKeyBytes.byteLength > MAX_PUBLIC_KEY_BYTES
  ) {
    throw new Error("desktop_resource_key_unavailable");
  }
  const publicKey = createPublicKey(publicKeyBytes);
  const profileRoot = join(app.getPath("userData"), "profiles", "default");
  const sidecarVersion = app.getVersion();
  const dependencies = createNodeSupervisorDependencies({
    resourceVerification: {
      resourceRoot,
      manifestPath: join(resourceRoot, "kestrel-resource-manifest.json"),
      signaturePath: join(resourceRoot, "kestrel-resource-manifest.sig"),
      trustedKeys: new Map([[RELEASE_MANIFEST_KEY_ID, publicKey]]),
      requiredFiles: [sidecarRelativePath, "web/dist/index.html"]
    },
    profile: {
      profileId: "default",
      trustedAnchor: app.getPath("userData"),
      profileRoot,
      statePath: join(profileRoot, "state", "agent.db"),
      memoryDir: join(profileRoot, "memory"),
      runtimeSettingsPath: join(
        profileRoot,
        "config",
        "runtime_settings.json"
      )
    },
    sidecarVersion
  });
  return new SidecarSupervisor(
    {
      sidecarRelativePath,
      sidecarVersion,
      readinessTimeoutMs: 15_000,
      shutdownTimeoutMs: 10_000,
      environment: process.env
    },
    dependencies
  );
}

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
  let supervisor: SidecarSupervisor | null = null;
  let stoppingForQuit = false;
  let quitAfterStop = false;

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

  app.on("before-quit", (event) => {
    if (quitAfterStop || supervisor === null) {
      return;
    }
    event.preventDefault();
    if (stoppingForQuit) {
      return;
    }
    stoppingForQuit = true;
    void supervisor
      .stop()
      .then(() => {
        quitAfterStop = true;
        app.quit();
      })
      .catch(() => {
        stoppingForQuit = false;
      });
  });

  void app
    .whenReady()
    .then(async () => {
      installSessionBoundary(
        session.defaultSession as unknown as RestrictedSession
      );
      await startVerifiedDesktopSession({
        async startSupervisor() {
          supervisor = await createPackagedSidecarSupervisor();
          return supervisor.start();
        },
        registerVerifiedProtocol(rendererAssets) {
          registerAppProtocol(protocol, rendererAssets);
        },
        openWindow() {
          desktopReady = true;
          windows.openOrFocus();
        },
        quit() {
          app.quit();
        }
      });
    })
    .catch(() => {
      app.quit();
    });
}
