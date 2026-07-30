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
import type {
  BrowserWindowConstructorOptions,
  Event as ElectronEvent
} from "electron";
import {
  DESKTOP_CREDENTIAL_VALUE_BYTES,
  type DesktopUpdateStatus
} from "./contracts.js";
import {
  registerKestrelProtocol,
  registerKestrelScheme
} from "./main/protocol.js";
import {
  installCredentialWebContentsBoundary,
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
  installCredentialIpc,
  type CredentialIpcBinding,
  type CredentialIpcMain,
  type CredentialIpcWebContents,
  type DesktopIpcAuthority,
  type DesktopIpcMain,
  type DesktopIpcWebContents
} from "./main/ipc.js";
import {
  createDesktopCredentialApiClient,
  credentialProviderAuthority
} from "./main/credential-api.js";
import {
  createCredentialDialogController,
  createCredentialWindow,
  type CredentialDialogController
} from "./main/credential-window.js";
import type { VerifiedDesktopSessionResources } from "./main/sidecar-supervisor.js";

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
      requiredFiles: [
        sidecarRelativePath,
        "web/dist/index.html",
        "desktop/dist/credential/index.html",
        "desktop/dist/credential/form.js",
        "desktop/dist/credential/styles.css",
        "desktop/dist/credential/preload.js"
      ]
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
      platform: process.platform,
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
  let credentialDialog: CredentialDialogController | null = null;
  let verifiedDesktopResources: VerifiedDesktopSessionResources | null =
    null;
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
    credentialDialog?.abort();
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
      const credentialClient = createDesktopCredentialApiClient({
        readAuthority: () =>
          apiSession?.credentialAuthority() ?? null,
        fetch: (url, init) => fetch(url, init)
      });
      credentialDialog = createCredentialDialogController({
        currentGeneration: () =>
          apiSession?.runtimeMarker()?.generation ?? null,
        openModal(intent, callbacks) {
          const parent = windows.current();
          const resources = verifiedDesktopResources;
          if (
            parent === null ||
            parent.isDestroyed() ||
            resources === null
          ) {
            throw new Error("credential_window_unavailable");
          }
          const provider = credentialProviderAuthority(
            intent.providerId
          );
          let credentialBinding: CredentialIpcBinding | null =
            null;
          let ownerCloseBlocked = false;
          let cleaned = false;
          const preventOwnerClose = (
            event: ElectronEvent
          ): void => {
            event.preventDefault();
          };
          const ownerClosed = (): void => {
            credentialDialog?.abort();
          };
          const cleanup = (): void => {
            if (cleaned) {
              return;
            }
            cleaned = true;
            credentialBinding?.dispose();
            credentialBinding = null;
            if (ownerCloseBlocked) {
              parent.removeListener(
                "close",
                preventOwnerClose
              );
              ownerCloseBlocked = false;
            }
            parent.removeListener("closed", ownerClosed);
          };
          const opened = createCredentialWindow({
            parent,
            preloadPath: resources.credentialPreloadPath,
            createWindow: (options) => {
              const created = new BrowserWindow(
                options as BrowserWindowConstructorOptions
              );
              return created;
            },
            installCredentialSecurity: (webContents) => {
              installCredentialWebContentsBoundary(
                webContents as RestrictedWebContents
              );
            },
            bindCredentialIpc: (webContents) => {
              credentialBinding = installCredentialIpc(
                ipcMain as unknown as CredentialIpcMain,
                {
                  webContents:
                    webContents as unknown as CredentialIpcWebContents,
                  context: Object.freeze({
                    schema: "kestrel.credential.context.v1",
                    providerId: provider.providerId,
                    providerLabel: provider.label,
                    inputLabel: `${provider.label} API key`,
                    maxUtf8Bytes:
                      DESKTOP_CREDENTIAL_VALUE_BYTES
                  }),
                  submit: callbacks.onSubmit,
                  cancel: callbacks.onCancel
                }
              );
              return () => {
                credentialBinding?.dispose();
              };
            },
            onFailure: callbacks.onFailure
          });
          const modal =
            opened.window as unknown as BrowserWindow;
          parent.once("closed", ownerClosed);
          modal.once("closed", () => {
            cleanup();
            callbacks.onClose();
          });
          void opened.loaded.catch(() => undefined);
          return Object.freeze({
            close(): void {
              cleanup();
              if (!modal.isDestroyed()) {
                modal.close();
              }
            },
            preventOwnerClose(): void {
              if (ownerCloseBlocked) {
                return;
              }
              ownerCloseBlocked = true;
              parent.on("close", preventOwnerClose);
            }
          });
        },
        storeProviderCredential: (request) =>
          credentialClient.storeProviderCredential(request),
        enterReconciliationRequired: () => {
          supervisor?.enterReconciliationRequired();
        },
        subscribeDeactivation: (listener) =>
          apiSession?.subscribeDeactivation(listener) ??
          (() => undefined)
      });
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
          openCredentialDialog: (intent) => {
            if (credentialDialog === null) {
              return Promise.reject(
                new Error("credential_dialog_unavailable")
              );
            }
            return credentialDialog.open(intent);
          },
          openExternalUrl: async (request) => {
            await openReviewedExternalUrl(
              request,
              async (url) => {
                await shell.openExternal(url);
              }
            );
          },
          performRecoveryAction: async () => {
            if (supervisor === null) {
              return unavailableDesktopFeature();
            }
            return supervisor.retryReadiness();
          },
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
        registerVerifiedProtocol(resources) {
          verifiedDesktopResources = resources;
          registerKestrelProtocol(protocol, resources);
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
