import { createPublicKey } from "node:crypto";
import { readFile, realpath, stat } from "node:fs/promises";
import { basename, isAbsolute, join } from "node:path";
import {
  app,
  BrowserWindow,
  dialog,
  ipcMain,
  protocol,
  session,
  shell
} from "electron";
import type { DesktopUpdateStatus } from "./contracts.js";
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
import {
  installDesktopApiSession,
  type ApiSessionWebContents,
  type ApiSessionWebRequest,
  type DesktopApiSessionAuthority
} from "./main/api-session.js";
import {
  chooseCanonicalDirectory,
  installDesktopIpc,
  openReviewedExternalUrl,
  projectDesktopConnection,
  unavailableDesktopFeature,
  type DesktopIpcAuthority,
  type DesktopIpcMain,
  type DesktopIpcWebContents
} from "./main/ipc.js";

const MAX_PUBLIC_KEY_BYTES = 16 * 1024;
const RELEASE_MANIFEST_KEY_ID = "release";
const unavailableUpdateStatus = Object.freeze({
  schema: "kestrel.desktop.update.v1",
  state: "unavailable",
  reason: "not_configured"
} satisfies DesktopUpdateStatus);

async function createPackagedSidecarSupervisor(
  apiSession: DesktopApiSessionAuthority
): Promise<SidecarSupervisor> {
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
    apiSession,
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
  let apiSession: DesktopApiSessionAuthority | null = null;
  let desktopIpc: DesktopIpcAuthority | null = null;
  const windows = createSingleWindowController(() => {
    const created = createAppWindow({
      createWindow: (options) =>
        new BrowserWindow(options) as BrowserWindow & AppWindow,
      installSecurity: (webContents) => {
        installWebContentsBoundary(
          webContents as RestrictedWebContents
        );
      },
      bindApiSession: (webContents) => {
        if (apiSession === null) {
          throw new Error("desktop_api_session_unavailable");
        }
        apiSession.bindRenderer(
          webContents as ApiSessionWebContents
        );
      },
      bindDesktopIpc: (webContents) => {
        if (desktopIpc === null) {
          throw new Error("desktop_ipc_unavailable");
        }
        desktopIpc.bindRenderer(
          webContents as DesktopIpcWebContents
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
      const defaultSession = session.defaultSession;
      installSessionBoundary(
        defaultSession as unknown as RestrictedSession
      );
      apiSession = installDesktopApiSession(
        defaultSession.webRequest as unknown as ApiSessionWebRequest
      );
      supervisor = await createPackagedSidecarSupervisor(apiSession);
      desktopIpc = installDesktopIpc(
        ipcMain as unknown as DesktopIpcMain,
        {
          readConnection: () => {
            if (supervisor === null || apiSession === null) {
              throw new Error("desktop_connection_unavailable");
            }
            return projectDesktopConnection(
              supervisor.state,
              apiSession.runtimeMarker()
            );
          },
          subscribeLifecycle(listener) {
            if (supervisor === null || apiSession === null) {
              return () => undefined;
            }
            return supervisor.subscribe(() => {
              if (supervisor !== null && apiSession !== null) {
                listener(
                  projectDesktopConnection(
                    supervisor.state,
                    apiSession.runtimeMarker()
                  )
                );
              }
            });
          },
          readUpdateStatus: () => unavailableUpdateStatus,
          subscribeUpdateStatus: () => () => undefined,
          chooseProjectFolder: () =>
            chooseCanonicalDirectory({
              showOpenDialog: async () => {
                const result = await dialog.showOpenDialog({
                  title: "Choose a Kestrel project folder",
                  properties: ["openDirectory"]
                });
                return {
                  canceled: result.canceled,
                  filePaths: result.filePaths
                };
              },
              realpath,
              stat,
              isAbsolute,
              basename
            }),
          chooseStorageFolder: () =>
            chooseCanonicalDirectory({
              showOpenDialog: async () => {
                const result = await dialog.showOpenDialog({
                  title: "Choose Kestrel storage",
                  properties: ["openDirectory", "createDirectory"]
                });
                return {
                  canceled: result.canceled,
                  filePaths: result.filePaths
                };
              },
              realpath,
              stat,
              isAbsolute,
              basename
            }),
          exportSupportBundle: async () =>
            unavailableDesktopFeature(),
          getAppVersion: () => app.getVersion(),
          openCredentialDialog: async () =>
            unavailableDesktopFeature(),
          openExternalUrl: async (request) => {
            await openReviewedExternalUrl(
              request,
              async (url) => {
                await shell.openExternal(url);
              }
            );
          },
          performRecoveryAction: async () =>
            unavailableDesktopFeature(),
          runtimeMarker: () => apiSession?.runtimeMarker() ?? null
        }
      );
      await startVerifiedDesktopSession({
        async startSupervisor() {
          if (supervisor === null) {
            throw new Error("desktop_supervisor_unavailable");
          }
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
