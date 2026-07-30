import { createHash, createHmac } from "node:crypto";
import { spawnSync } from "node:child_process";
import { createServer } from "node:http";
import { PassThrough } from "node:stream";
import { describe, expect, it } from "vitest";
import type { VerifiedResourceSet } from "./resource-manifest";
import {
  createNodeSupervisorDependencies,
  SidecarSupervisor,
  SidecarSupervisorError,
  type AuthenticatedDesktopReadiness,
  type AuthenticatedDesktopRecoveryReport,
  type ProfileLeaseEvidence,
  type SidecarSpawnRequest,
  type SidecarSupervisorConfig,
  type SidecarSupervisorDependencies
} from "./sidecar-supervisor";
import {
  FakeSidecarChild,
  FakeSidecarSpawner
} from "../testing/fake-sidecar";

const manifestDigest = `sha256:${"a".repeat(64)}` as `sha256:${string}`;
const executableDigest = "b".repeat(64);
const launchNonce = "11".repeat(32);
const apiToken = "22".repeat(32);
const launchNonceDigest = createHash("sha256")
  .update(launchNonce)
  .digest("hex");

async function eventually(assertion: () => void): Promise<void> {
  let lastError: unknown;
  for (let attempt = 0; attempt < 100; attempt += 1) {
    try {
      assertion();
      return;
    } catch (error) {
      lastError = error;
      await new Promise<void>((resolve) => setImmediate(resolve));
    }
  }
  throw lastError;
}

function deferred<T>(): {
  promise: Promise<T>;
  resolve(value: T): void;
  reject(error: unknown): void;
} {
  let resolvePromise!: (value: T) => void;
  let rejectPromise!: (error: unknown) => void;
  const promise = new Promise<T>((resolve, reject) => {
    resolvePromise = resolve;
    rejectPromise = reject;
  });
  return {
    promise,
    resolve: resolvePromise,
    reject: rejectPromise
  };
}

function verifiedResources(): VerifiedResourceSet {
  return {
    resourceRoot: "/bundle/resources",
    manifestDigest,
    manifest: {
      schema: "kestrel.desktop.resources.v1",
      build_mode: "release",
      key_id: "release",
      source_commit: "a".repeat(40),
      app_version: "0.5.0",
      platform: "darwin",
      architecture: "arm64",
      python_lock_sha256: "2".repeat(64),
      desktop_npm_lock_sha256: "3".repeat(64),
      web_npm_lock_sha256: "4".repeat(64),
      sbom_sha256: "5".repeat(64),
      files: {
        "sidecar/kestrel-desktop-sidecar": {
          size: 16,
          sha256: executableDigest
        },
        "web/dist/index.html": {
          size: 16,
          sha256: "c".repeat(64)
        },
        "desktop/dist/credential/index.html": {
          size: 8,
          sha256: "d".repeat(64)
        },
        "desktop/dist/credential/form.js": {
          size: 8,
          sha256: "e".repeat(64)
        },
        "desktop/dist/credential/styles.css": {
          size: 8,
          sha256: "f".repeat(64)
        },
        "desktop/dist/credential/preload.js": {
          size: 8,
          sha256: "1".repeat(64)
        },
        "sbom.cdx.json": {
          size: 8,
          sha256: "5".repeat(64)
        }
      }
    },
    files: new Map([
      [
        "sidecar/kestrel-desktop-sidecar",
        {
          path: "/bundle/resources/sidecar/kestrel-desktop-sidecar",
          size: 16,
          sha256: executableDigest
        }
      ],
      [
        "web/dist/index.html",
        {
          path: "/bundle/resources/web/dist/index.html",
          size: 16,
          sha256: "c".repeat(64)
        }
      ],
      [
        "desktop/dist/credential/index.html",
        {
          path: "/bundle/resources/desktop/dist/credential/index.html",
          size: 8,
          sha256: "d".repeat(64)
        }
      ],
      [
        "desktop/dist/credential/form.js",
        {
          path: "/bundle/resources/desktop/dist/credential/form.js",
          size: 8,
          sha256: "e".repeat(64)
        }
      ],
      [
        "desktop/dist/credential/styles.css",
        {
          path: "/bundle/resources/desktop/dist/credential/styles.css",
          size: 8,
          sha256: "f".repeat(64)
        }
      ],
      [
        "desktop/dist/credential/preload.js",
        {
          path: "/bundle/resources/desktop/dist/credential/preload.js",
          size: 8,
          sha256: "1".repeat(64)
        }
      ],
      [
        "sbom.cdx.json",
        {
          path: "/bundle/resources/sbom.cdx.json",
          size: 8,
          sha256: "5".repeat(64)
        }
      ]
    ]),
    rendererAssets: {
      totalBytes: 16,
      read: (relativePath) =>
        relativePath === "index.html"
          ? Buffer.from("<h1>Kestrel</h1>")
          : undefined
    },
    credentialAssets: {
      totalBytes: 32,
      read: (relativePath) =>
        [
          "index.html",
          "form.js",
          "styles.css",
          "preload.js"
        ].includes(relativePath)
          ? Buffer.from(relativePath)
          : undefined
    }
  };
}

function leaseFor(child: FakeSidecarChild): ProfileLeaseEvidence {
  return {
    status: "attach_desktop",
    current: {
      profileId: "default",
      management: "desktop",
      pid: child.pid,
      processBirthMarker: `birth-${child.pid}`,
      executableDigest,
      launchNonceDigest,
      baseUrl: `http://127.0.0.1:${43000 + child.pid}/`,
      version: "0.5.0"
    }
  };
}

function apiReadiness(): AuthenticatedDesktopReadiness {
  return {
    schema: "kestrel.desktop.readiness.v1",
    ready: true,
    profile_id: "default",
    launch_nonce_digest: launchNonceDigest,
    sidecar_version: "0.5.0",
    state_schema_version: 21,
    routing_schema_version: 2,
    memory_layers: [
      "working",
      "episodic",
      "semantic",
      "procedural",
      "self",
      "policy"
    ]
  };
}

function recoveryReport(
  overrides: Partial<AuthenticatedDesktopRecoveryReport> = {}
): AuthenticatedDesktopRecoveryReport {
  return {
    schema: "kestrel.desktop.recovery.v1" as const,
    can_auto_resume: overrides.can_auto_resume ?? true,
    reasons: overrides.reasons ?? [],
    blockers: overrides.blockers ?? [],
    actions: [
      "inspect",
      "export_support_bundle",
      "retry_readiness"
    ],
    state:
      overrides.state ?? {
        integrity: "ok" as const,
        schema_version: 21,
        writable: true
      },
    memory: overrides.memory ?? { ready: true },
    approvals:
      overrides.approvals ?? { pending_high_risk: 0 },
    routing:
      overrides.routing ?? { ambiguous_provider_attempts: 0 },
    credential_storage:
      overrides.credential_storage ?? {
        state: "available" as const
      }
  };
}

function harness(
  overrides: Partial<SidecarSupervisorDependencies> = {},
  configOverrides: Partial<SidecarSupervisorConfig> & {
    platform?: NodeJS.Platform;
  } = {},
  observeSpawn?: (request: SidecarSpawnRequest) => void
): {
  supervisor: SidecarSupervisor;
  spawner: FakeSidecarSpawner;
  logs: string[];
  shutdownRequests: Array<{ baseUrl: string; apiToken: string }>;
  apiSessionEvents: Array<
    | {
        kind: "activate";
        baseUrl: string;
        apiToken: string;
        credentialCapability: string;
        generation: number;
      }
    | { kind: "deactivate"; generation?: number }
  >;
} {
  const spawner = new FakeSidecarSpawner();
  const logs: string[] = [];
  const shutdownRequests: Array<{ baseUrl: string; apiToken: string }> = [];
  const apiSessionEvents: Array<
    | {
        kind: "activate";
        baseUrl: string;
        apiToken: string;
        credentialCapability: string;
        generation: number;
      }
    | { kind: "deactivate"; generation?: number }
  > = [];
  const dependencies: SidecarSupervisorDependencies = {
    apiSession: {
      activate(input: {
        baseUrl: string;
        apiToken: string;
        credentialCapability: string;
        generation: number;
      }): void {
        apiSessionEvents.push({ kind: "activate", ...input });
      },
      deactivate(generation?: number): void {
        apiSessionEvents.push({ kind: "deactivate", generation });
      }
    },
    verifyResources: async () => verifiedResources(),
    acquireVerifiedExecutable: async (resources, relativePath) => {
      const resource = resources.files.get(relativePath);
      if (resource === undefined) {
        throw new Error("missing verified executable");
      }
      return {
        resource,
        mechanism: "test_verified_handle",
        spawn: (request) => {
          observeSpawn?.({
            executable: resource.path,
            ...request
          });
          return spawner.spawn({
            executable: resource.path,
            ...request
          });
        },
        close: async () => undefined
      };
    },
    resolveProfile: async () => ({
      profileId: "default",
      profileRoot: "/profile",
      statePath: "/profile/state/agent.db",
      memoryDir: "/profile/memory",
      runtimeSettingsPath: "/profile/config/runtime_settings.json",
      runtimeDirectory: "/profile/runtime",
      readinessPath: "/profile/runtime/desktop-readiness.json",
      leaseControlRoot: "/profile/state/.kestrel-runtime-profiles/profile"
    }),
    inspectLease: async () => {
      const child = spawner.children.at(-1);
      if (
        child === undefined ||
        child.exitCode !== null ||
        child.signalCode !== null
      ) {
        return { status: "available" };
      }
      return leaseFor(child);
    },
    parentIdentity: async () => ({
      pid: process.pid,
      processBirthMarker: "desktop-parent-birth"
    }),
    createLaunchFiles: async (input) => ({
      bootstrapPath: "/profile/runtime/bootstrap.json",
      readinessPath: "/profile/runtime/desktop-readiness.json",
      failurePath: "/profile/runtime/desktop-failure.json",
      launchNonce,
      launchNonceDigest,
      apiToken,
      profile: input.profile,
      cleanup: async () => undefined
    }),
    waitForReadiness: async ({ child }) => {
      const pid = child.pid;
      if (pid === undefined) {
        throw new Error("missing child PID");
      }
      return {
        identity: { dev: 1, ino: pid },
        readiness: {
          schema: "kestrel.desktop.sidecar_readiness.v1",
          pid,
          process_birth_marker: `birth-${pid}`,
          port: 43000 + pid,
          profile_id: "default",
          sidecar_version: "0.5.0",
          executable_digest: executableDigest,
          resource_manifest_digest: manifestDigest,
          launch_nonce_digest: launchNonceDigest
        }
      };
    },
    inspectProcess: async (pid) => ({
      pid,
      processBirthMarker: `birth-${pid}`,
      executableDigest
    }),
    requestReadiness: async () => apiReadiness(),
    requestRecovery: async () => recoveryReport(),
    requestRecoveryRetry: async () => ({
      schema: "kestrel.desktop.recovery-retry-result.v1",
      accepted: true,
      report: recoveryReport()
    }),
    requestShutdown: async (request) => {
      shutdownRequests.push(request);
      spawner.children.at(-1)?.exit(0);
    },
    waitForExit: async (child) => {
      const fake = child as FakeSidecarChild;
      if (fake.exitCode !== null || fake.signalCode !== null) {
        return true;
      }
      if (fake.killSignals.length > 0) {
        fake.exit(0, "SIGTERM");
        return true;
      }
      return false;
    },
    now: () => Date.now(),
    log: (line) => logs.push(line),
    ...overrides
  };
  return {
    supervisor: new SidecarSupervisor(
      {
        sidecarRelativePath: "sidecar/kestrel-desktop-sidecar",
        sidecarVersion: "0.5.0",
        readinessTimeoutMs: 1_000,
        shutdownTimeoutMs: 1_000,
        platform: "darwin",
        environment: {
          PATH: "/safe/bin",
          AWS_SECRET_ACCESS_KEY: "must-not-inherit",
          KESTREL_API_TOKEN: "must-not-inherit"
        },
        ...configOverrides
      } as SidecarSupervisorConfig & { platform: NodeJS.Platform },
      dependencies
    ),
    spawner,
    logs,
    shutdownRequests,
    apiSessionEvents
  };
}

function environmentObservedByActualChild(
  request: SidecarSpawnRequest
): Record<string, string> {
  const result = spawnSync(
    process.execPath,
    [
      "-e",
      "process.stdout.write(JSON.stringify(process.env))"
    ],
    {
      encoding: "utf8",
      env: request.options.env
    }
  );
  if (
    result.error !== undefined ||
    result.status !== 0 ||
    result.signal !== null
  ) {
    throw new Error("test_environment_probe_failed");
  }
  return JSON.parse(result.stdout) as Record<string, string>;
}

function failedStartHarness(
  termination:
    | "unconfirmed"
    | "identity_unverified"
    | "signal_rejected",
  lifecycle: {
    cleanup?: () => Promise<void>;
    close?: () => Promise<void>;
  } = {}
): ReturnType<typeof harness> & {
  counts: { cleanup: number; close: number };
} {
  const counts = { cleanup: 0, close: 0 };
  let processInspections = 0;
  const context = harness({
    acquireVerifiedExecutable: async (resources, relativePath) => {
      const resource = resources.files.get(relativePath);
      if (resource === undefined) {
        throw new Error("missing verified executable");
      }
      return {
        resource,
        mechanism: "test_verified_handle",
        spawn: (request) => {
          const child = context.spawner.spawn({
            executable: resource.path,
            ...request
          });
          if (termination === "signal_rejected") {
            child.kill = (signal?: NodeJS.Signals | number): boolean => {
              child.killSignals.push(signal);
              return false;
            };
          }
          return child;
        },
        close: async () => {
          counts.close += 1;
          await lifecycle.close?.();
        }
      };
    },
    createLaunchFiles: async (input) => ({
      bootstrapPath: "/profile/runtime/bootstrap.json",
      readinessPath: "/profile/runtime/desktop-readiness.json",
      failurePath: "/profile/runtime/desktop-failure.json",
      launchNonce,
      launchNonceDigest,
      apiToken,
      profile: input.profile,
      cleanup: async () => {
        counts.cleanup += 1;
        await lifecycle.cleanup?.();
      }
    }),
    waitForReadiness: async () => {
      throw new SidecarSupervisorError(
        "startup_probe_failed",
        "sidecar_unverified"
      );
    },
    inspectProcess: async (pid) => {
      processInspections += 1;
      if (
        termination === "identity_unverified" &&
        processInspections > 1
      ) {
        return null;
      }
      return {
        pid,
        processBirthMarker: `birth-${pid}`,
        executableDigest
      };
    },
    inspectLease: async () => ({ status: "available" }),
    waitForExit: async () => false
  });
  return { ...context, counts };
}

describe("verified sidecar supervisor", () => {
  it("pushes frozen lifecycle transitions and isolates observer failures", async () => {
    const { supervisor } = harness();
    const received: unknown[] = [];
    const unsubscribeThrowing = supervisor.subscribe(() => {
      throw new Error("observer-secret");
    });
    const unsubscribe = supervisor.subscribe((state) => {
      received.push(state);
    });

    await supervisor.start();

    expect(received).toEqual([
      { kind: "verifying" },
      { kind: "starting" },
      {
        kind: "ready",
        profileId: "default",
        baseUrl: "http://127.0.0.1:52100/",
        sidecarVersion: "0.5.0"
      }
    ]);
    expect(received.every(Object.isFrozen)).toBe(true);
    unsubscribeThrowing();
    unsubscribeThrowing();
    unsubscribe();
    await supervisor.stop();
    expect(received).toHaveLength(3);
  });

  it("fails before spawn when resource verification rejects tampering", async () => {
    const error = Object.assign(new Error("resource_digest_mismatch"), {
      code: "resource_digest_mismatch"
    });
    const { supervisor, spawner } = harness({
      verifyResources: async () => {
        throw error;
      }
    });

    await expect(supervisor.start()).rejects.toMatchObject({
      code: "resource_digest_mismatch"
    });
    expect(spawner.children).toHaveLength(0);
    expect(supervisor.state).toMatchObject({
      kind: "recovery",
      reason: "payload_verification_failed"
    });
  });

  it("rejects unknown or contradictory recovery reports and bounds retry responses", async () => {
    let recoveryMode: "unknown" | "contradictory" = "unknown";
    let retryBody = "";
    const authorizations: Array<string | undefined> = [];
    const server = createServer((request, response) => {
      authorizations.push(request.headers.authorization);
      if (request.url === "/api/desktop/recovery/retry") {
        request.setEncoding("utf8");
        request.on("data", (chunk: string) => {
          retryBody += chunk;
        });
        request.on("end", () => {
          response.writeHead(200, {
            "Content-Type": "application/json"
          });
          response.end(
            JSON.stringify({ padding: "x".repeat(20 * 1024) })
          );
        });
        return;
      }
      const report =
        recoveryMode === "unknown"
          ? {
              ...recoveryReport(),
              reasons: ["provider-secret-code"]
            }
          : {
              ...recoveryReport(),
              can_auto_resume: true,
              reasons: ["pending_high_risk_approval"],
              blockers: [],
              approvals: { pending_high_risk: 1 }
            };
      response.writeHead(200, {
        "Content-Type": "application/json"
      });
      response.end(JSON.stringify(report));
    });
    await new Promise<void>((resolve, reject) => {
      server.once("error", reject);
      server.listen(0, "127.0.0.1", resolve);
    });
    const address = server.address();
    if (address === null || typeof address === "string") {
      server.close();
      throw new Error("test_http_server_unavailable");
    }
    const dependencies = createNodeSupervisorDependencies({
      apiSession: {
        activate: () => undefined,
        deactivate: () => undefined
      },
      resourceVerification: {
        resourceRoot: "/unused",
        manifestPath: "/unused/manifest.json",
        signaturePath: "/unused/manifest.sig",
        trustedKeys: new Map(),
        requiredFiles: [],
        expectedIdentity: {
          buildMode: "release",
          keyId: "release",
          sourceCommit: "a".repeat(40),
          appVersion: "0.5.0",
          platform: "darwin",
          architecture: "arm64",
          pythonLockSha256: "2".repeat(64),
          desktopNpmLockSha256: "3".repeat(64),
          webNpmLockSha256: "4".repeat(64),
          sbomSha256: "5".repeat(64)
        }
      },
      profile: {
        profileId: "default",
        trustedAnchor: "/unused",
        profileRoot: "/unused/profile",
        statePath: "/unused/profile/state/agent.db",
        memoryDir: "/unused/profile/memory",
        runtimeSettingsPath:
          "/unused/profile/config/runtime_settings.json"
      },
      sidecarVersion: "0.5.0"
    });
    const baseUrl = `http://127.0.0.1:${address.port}/`;
    try {
      await expect(
        dependencies.requestRecovery({ baseUrl, apiToken })
      ).rejects.toMatchObject({
        code: "authenticated_recovery_invalid"
      });
      recoveryMode = "contradictory";
      await expect(
        dependencies.requestRecovery({ baseUrl, apiToken })
      ).rejects.toMatchObject({
        code: "authenticated_recovery_invalid"
      });
      await expect(
        dependencies.requestRecoveryRetry({ baseUrl, apiToken })
      ).rejects.toThrow("desktop_http_response_too_large");
    } finally {
      await new Promise<void>((resolve, reject) => {
        server.close((error) => {
          if (error === undefined) {
            resolve();
          } else {
            reject(error);
          }
        });
      });
    }

    expect(authorizations).toEqual([
      `Bearer ${apiToken}`,
      `Bearer ${apiToken}`,
      `Bearer ${apiToken}`
    ]);
    expect(JSON.parse(retryBody)).toEqual({
      schema: "kestrel.desktop.recovery-retry.v1",
      action: "retry_readiness"
    });
  });

  it("acquires an exact executable capability before profile or secret mutation", async () => {
    let profileResolved = false;
    let launchFilesCreated = false;
    let capabilityRequested = false;
    const { supervisor, spawner } = harness({
      acquireVerifiedExecutable: async () => {
        capabilityRequested = true;
        throw Object.assign(
          new Error("verified_executable_launch_unqualified"),
          { code: "verified_executable_launch_unqualified" }
        );
      },
      resolveProfile: async () => {
        profileResolved = true;
        throw new Error("profile_must_not_be_resolved");
      },
      createLaunchFiles: async () => {
        launchFilesCreated = true;
        throw new Error("launch_files_must_not_be_created");
      }
    });

    await expect(supervisor.start()).rejects.toMatchObject({
      code: "verified_executable_launch_unqualified"
    });
    expect(capabilityRequested).toBe(true);
    expect(profileResolved).toBe(false);
    expect(launchFilesCreated).toBe(false);
    expect(spawner.children).toHaveLength(0);
  });

  it("uses the exact executable and sole bootstrap argv with a secret-free environment", async () => {
    const { supervisor, spawner } = harness();

    const verified = await supervisor.start();

    expect(supervisor.state).toMatchObject({
      kind: "ready",
      profileId: "default"
    });
    expect(verified.rendererAssets.read("index.html")).toEqual(
      Buffer.from("<h1>Kestrel</h1>")
    );
    expect(verified.credentialAssets.read("form.js")).toEqual(
      Buffer.from("form.js")
    );
    expect(verified.credentialPreloadPath).toBe(
      "/bundle/resources/desktop/dist/credential/preload.js"
    );
    expect(Object.isFrozen(verified)).toBe(true);
    expect(spawner.requests).toEqual([
      {
        executable: "/bundle/resources/sidecar/kestrel-desktop-sidecar",
        args: ["/profile/runtime/bootstrap.json"],
        options: {
          shell: false,
          detached: false,
          stdio: ["ignore", "pipe", "pipe"],
          env: { PATH: "/safe/bin" }
        }
      }
    ]);
    expect(JSON.stringify(spawner.requests)).not.toContain(apiToken);
    expect(JSON.stringify(supervisor.state)).not.toContain(apiToken);
  });

  it("passes only bounded local Secret Service environment through an actual Linux child spawn", async () => {
    const dbusAddressAtLimit =
      `unix:path=/${"d".repeat(2_037)}`;
    const runtimeDirectoryAtLimit = `/${"x".repeat(1_023)}`;
    let actualEnvironment: Record<string, string> | undefined;
    const sourceEnvironment = {
      PATH: "/safe/bin",
      DBUS_SESSION_BUS_ADDRESS: dbusAddressAtLimit,
      XDG_RUNTIME_DIR: runtimeDirectoryAtLimit,
      AWS_SECRET_ACCESS_KEY: "must-not-inherit",
      KESTREL_API_TOKEN: "must-not-inherit",
      OPENAI_API_KEY: "must-not-inherit",
      SSH_AUTH_SOCK: "/must/not/inherit"
    };
    const { supervisor, spawner } = harness(
      {},
      {
        platform: "linux",
        environment: sourceEnvironment
      },
      (request) => {
        actualEnvironment =
          environmentObservedByActualChild(request);
      }
    );

    await supervisor.start();

    expect(dbusAddressAtLimit).toHaveLength(2_048);
    expect(runtimeDirectoryAtLimit).toHaveLength(1_024);
    expect(spawner.requests[0]?.options.env).toEqual({
      PATH: "/safe/bin",
      DBUS_SESSION_BUS_ADDRESS: dbusAddressAtLimit,
      XDG_RUNTIME_DIR: runtimeDirectoryAtLimit
    });
    expect(actualEnvironment).toMatchObject({
      PATH: "/safe/bin",
      DBUS_SESSION_BUS_ADDRESS: dbusAddressAtLimit,
      XDG_RUNTIME_DIR: runtimeDirectoryAtLimit
    });
    for (const forbidden of [
      "AWS_SECRET_ACCESS_KEY",
      "KESTREL_API_TOKEN",
      "OPENAI_API_KEY",
      "SSH_AUTH_SOCK"
    ]) {
      expect(spawner.requests[0]?.options.env).not.toHaveProperty(
        forbidden
      );
      expect(actualEnvironment).not.toHaveProperty(forbidden);
    }
  });

  it("accepts bounded path, abstract, and all-unix DBus session addresses", async () => {
    const addresses = [
      "unix:path=/run/user/1000/bus",
      "unix:abstract=/tmp/dbus-kestrel",
      `unix:path=/run/user/1000/dbus-%2Csocket,guid=${"a".repeat(32)}`,
      "unix:path=/run/user/1000/bus;unix:abstract=/tmp/dbus-kestrel"
    ];

    for (const address of addresses) {
      let actualEnvironment: Record<string, string> | undefined;
      const { supervisor, spawner } = harness(
        {},
        {
          platform: "linux",
          environment: {
            DBUS_SESSION_BUS_ADDRESS: address,
            XDG_RUNTIME_DIR: "/run/user/1000"
          }
        },
        (request) => {
          actualEnvironment =
            environmentObservedByActualChild(request);
        }
      );

      await supervisor.start();

      expect(
        spawner.requests[0]?.options.env
          .DBUS_SESSION_BUS_ADDRESS,
        address
      ).toBe(address);
      expect(
        actualEnvironment?.DBUS_SESSION_BUS_ADDRESS,
        address
      ).toBe(address);
    }
  });

  it("omits malformed Linux Secret Service variables from the actual spawned environment", async () => {
    const validDbus = "unix:path=/run/user/1000/bus";
    const validRuntime = "/run/user/1000";
    const cases = [
      {
        name: "non-unix transport",
        variable: "DBUS_SESSION_BUS_ADDRESS",
        value: "tcp:host=127.0.0.1,port=4444"
      },
      {
        name: "relative unix path",
        variable: "DBUS_SESSION_BUS_ADDRESS",
        value: "unix:path=run/user/1000/bus"
      },
      {
        name: "multiple transports",
        variable: "DBUS_SESSION_BUS_ADDRESS",
        value:
          "unix:path=/run/user/1000/bus;tcp:host=127.0.0.1"
      },
      {
        name: "malformed percent escape",
        variable: "DBUS_SESSION_BUS_ADDRESS",
        value: "unix:path=/run/user/1000/dbus-%zz"
      },
      {
        name: "unescaped backslash",
        variable: "DBUS_SESSION_BUS_ADDRESS",
        value: "unix:path=/run/user/1000/dbus\\socket"
      },
      {
        name: "DBus control character",
        variable: "DBUS_SESSION_BUS_ADDRESS",
        value: "unix:path=/run/user/1000/\nbus"
      },
      {
        name: "oversized DBus address",
        variable: "DBUS_SESSION_BUS_ADDRESS",
        value: `unix:path=/${"d".repeat(2_038)}`
      },
      {
        name: "relative runtime directory",
        variable: "XDG_RUNTIME_DIR",
        value: "run/user/1000"
      },
      {
        name: "non-normal runtime directory",
        variable: "XDG_RUNTIME_DIR",
        value: "/run/user/1000/../2000"
      },
      {
        name: "runtime control character",
        variable: "XDG_RUNTIME_DIR",
        value: "/run/user/\u007f1000"
      },
      {
        name: "oversized runtime directory",
        variable: "XDG_RUNTIME_DIR",
        value: `/${"x".repeat(1_024)}`
      }
    ] as const;

    for (const testCase of cases) {
      let actualEnvironment: Record<string, string> | undefined;
      const environment = {
        PATH: "/safe/bin",
        DBUS_SESSION_BUS_ADDRESS: validDbus,
        XDG_RUNTIME_DIR: validRuntime,
        [testCase.variable]: testCase.value
      };
      const { supervisor, spawner } = harness(
        {},
        { platform: "linux", environment },
        (request) => {
          actualEnvironment =
            environmentObservedByActualChild(request);
        }
      );

      await supervisor.start();

      expect(
        spawner.requests[0]?.options.env,
        testCase.name
      ).not.toHaveProperty(testCase.variable);
      expect(
        actualEnvironment,
        testCase.name
      ).not.toHaveProperty(testCase.variable);
      const retainedVariable =
        testCase.variable === "DBUS_SESSION_BUS_ADDRESS"
          ? "XDG_RUNTIME_DIR"
          : "DBUS_SESSION_BUS_ADDRESS";
      expect(
        spawner.requests[0]?.options.env,
        `${testCase.name} preserves the other bounded variable`
      ).toHaveProperty(
        retainedVariable,
        environment[retainedVariable]
      );
    }
  });

  it("does not inherit Secret Service environment on non-Linux platforms", async () => {
    for (const platform of ["darwin", "win32"] as const) {
      let actualEnvironment: Record<string, string> | undefined;
      const { supervisor, spawner } = harness(
        {},
        {
          platform,
          environment: {
            PATH: "/safe/bin",
            DBUS_SESSION_BUS_ADDRESS:
              "unix:path=/run/user/1000/bus",
            XDG_RUNTIME_DIR: "/run/user/1000"
          }
        },
        (request) => {
          actualEnvironment =
            environmentObservedByActualChild(request);
        }
      );

      await supervisor.start();

      expect(spawner.requests[0]?.options.env).toEqual({
        PATH: "/safe/bin"
      });
      expect(actualEnvironment).not.toHaveProperty(
        "DBUS_SESSION_BUS_ADDRESS"
      );
      expect(actualEnvironment).not.toHaveProperty(
        "XDG_RUNTIME_DIR"
      );
    }
  });

  it("activates the main-process API authority only after authenticated readiness", async () => {
    const lifecycle: string[] = [];
    const { supervisor } = harness({
      apiSession: {
        activate({
          baseUrl,
          apiToken: receivedToken,
          credentialCapability,
          generation
        }): void {
          expect(baseUrl).toBe(`http://127.0.0.1:${43000 + 9100}/`);
          expect(receivedToken).toBe(apiToken);
          expect(credentialCapability).toBe(
            createHmac("sha256", Buffer.from(apiToken, "utf8"))
              .update(
                Buffer.from(
                  `kestrel.desktop.credential.write.v1\0${launchNonce}`,
                  "utf8"
                )
              )
              .digest("hex")
          );
          expect(generation).toBe(1);
          lifecycle.push("activate");
        },
        deactivate(generation?: number): void {
          lifecycle.push(`deactivate:${generation ?? "all"}`);
        }
      },
      requestReadiness: async () => {
        expect(lifecycle).toEqual(["deactivate:all"]);
        lifecycle.push("authenticated-readiness");
        return apiReadiness();
      }
    });

    await supervisor.start();

    expect(lifecycle).toEqual([
      "deactivate:all",
      "authenticated-readiness",
      "activate"
    ]);
  });

  it("surfaces and exactly cleans an authenticated startup failure", async () => {
    const failureIdentity = { dev: 7, ino: 11 };
    const cleanupCalls: Array<
      [
        { dev: number; ino: number } | undefined,
        { dev: number; ino: number } | undefined
      ]
    > = [];
    const { supervisor } = harness({
      createLaunchFiles: async (input) => ({
        bootstrapPath: "/profile/runtime/bootstrap.json",
        readinessPath: "/profile/runtime/desktop-readiness.json",
        failurePath: "/profile/runtime/desktop-failure.json",
        launchNonce,
        launchNonceDigest,
        apiToken,
        profile: input.profile,
        cleanup: async (readinessIdentity, startupFailureIdentity) => {
          cleanupCalls.push([
            readinessIdentity,
            startupFailureIdentity
          ]);
        }
      }),
      waitForReadiness: async () => {
        throw new SidecarSupervisorError(
          "sidecar_exited_before_readiness",
          "sidecar_unavailable"
        );
      },
      readStartupFailure: async () => ({
        identity: failureIdentity,
        failure: {
          schema: "kestrel.desktop.sidecar-failure.v1",
          launch_nonce_digest: launchNonceDigest,
          profile_id: "default",
          reason: "state_corrupt",
          resource_manifest_digest: manifestDigest,
          sidecar_version: "0.5.0",
          authentication_tag: "c".repeat(64)
        }
      })
    });

    await expect(supervisor.start()).rejects.toMatchObject({
      code: "state_corrupt",
      recoveryReason: "state_corrupt"
    });

    expect(supervisor.state).toEqual({
      kind: "recovery",
      reason: "state_corrupt",
      detail: "state_corrupt"
    });
    expect(cleanupCalls).toContainEqual([
      undefined,
      failureIdentity
    ]);
  });

  it("captures startup failure evidence before exit finalization cleanup", async () => {
    const readiness = deferred<never>();
    const failureIdentity = { dev: 13, ino: 17 };
    const cleanupCalls: Array<
      [
        { dev: number; ino: number } | undefined,
        { dev: number; ino: number } | undefined
      ]
    > = [];
    const { supervisor, spawner } = harness({
      createLaunchFiles: async (input) => ({
        bootstrapPath: "/profile/runtime/bootstrap.json",
        readinessPath: "/profile/runtime/desktop-readiness.json",
        failurePath: "/profile/runtime/desktop-failure.json",
        launchNonce,
        launchNonceDigest,
        apiToken,
        profile: input.profile,
        cleanup: async (readinessIdentity, startupFailureIdentity) => {
          cleanupCalls.push([
            readinessIdentity,
            startupFailureIdentity
          ]);
        }
      }),
      waitForReadiness: () => readiness.promise,
      readStartupFailure: async () => ({
        identity: failureIdentity,
        failure: {
          schema: "kestrel.desktop.sidecar-failure.v1",
          launch_nonce_digest: launchNonceDigest,
          profile_id: "default",
          reason: "memvid_reopen_failed",
          resource_manifest_digest: manifestDigest,
          sidecar_version: "0.5.0",
          authentication_tag: "c".repeat(64)
        }
      })
    });

    const start = supervisor.start();
    await eventually(() => {
      expect(spawner.children).toHaveLength(1);
    });
    spawner.children[0]!.exit(1);

    await expect(start).rejects.toMatchObject({
      code: "memvid_reopen_failed",
      recoveryReason: "memvid_reopen_failed"
    });
    await eventually(() => {
      expect(cleanupCalls).toContainEqual([
        undefined,
        failureIdentity
      ]);
    });
  });

  it("keeps API authority inactive when live recovery inspection is blocked", async () => {
    const { supervisor, apiSessionEvents } = harness({
      requestRecovery: async () =>
        recoveryReport({
          can_auto_resume: false,
          reasons: ["pending_high_risk_approval"],
          blockers: ["pending_high_risk_approval"]
        })
    });

    await expect(supervisor.start()).resolves.toBeDefined();

    expect(supervisor.state).toEqual({
      kind: "recovery",
      reason: "reconciliation_required",
      detail: "recovery_blocked_pending_high_risk_approval"
    });
    expect(
      apiSessionEvents.some((event) => event.kind === "activate")
    ).toBe(false);
  });

  it("coalesces one explicit recovery retry without automatic replay", async () => {
    const secondVerification = deferred<VerifiedResourceSet>();
    let verificationCalls = 0;
    const { supervisor, spawner } = harness({
      verifyResources: async () => {
        verificationCalls += 1;
        return verificationCalls === 1
          ? verifiedResources()
          : secondVerification.promise;
      }
    });
    await supervisor.start();
    supervisor.enterReconciliationRequired();

    const first = supervisor.retryReadiness();
    const duplicate = supervisor.retryReadiness();
    await eventually(() => {
      expect(verificationCalls).toBe(2);
    });
    expect(spawner.children).toHaveLength(1);

    secondVerification.resolve(verifiedResources());
    await expect(first).resolves.toEqual({ accepted: true });
    await expect(duplicate).resolves.toEqual({ accepted: true });
    expect(spawner.children).toHaveLength(2);
    expect(supervisor.state.kind).toBe("ready");
  });

  it("never activates a stale generation after a live recovery retry", async () => {
    const retried = deferred<{
      schema: "kestrel.desktop.recovery-retry-result.v1";
      accepted: true;
      report: AuthenticatedDesktopRecoveryReport;
    }>();
    let retryCalls = 0;
    const { supervisor, apiSessionEvents } = harness({
      requestRecovery: async () =>
        recoveryReport({
          can_auto_resume: false,
          reasons: ["ambiguous_provider_attempt"],
          blockers: ["ambiguous_provider_attempt"],
          routing: { ambiguous_provider_attempts: 1 }
        }),
      requestRecoveryRetry: async () => {
        retryCalls += 1;
        return retried.promise;
      }
    });
    await supervisor.start();

    const retry = supervisor.retryReadiness();
    await eventually(() => {
      expect(retryCalls).toBe(1);
    });
    await supervisor.stop();
    retried.resolve({
      schema: "kestrel.desktop.recovery-retry-result.v1",
      accepted: true,
      report: recoveryReport()
    });

    await expect(retry).resolves.toEqual({
      accepted: false,
      reason: "retry_failed"
    });
    expect(
      apiSessionEvents.some((event) => event.kind === "activate")
    ).toBe(false);
    expect(supervisor.state).toEqual({ kind: "stopping" });
  });

  it("enters a bounded crash loop after two failed explicit retries", async () => {
    let now = 1_000;
    let verificationCalls = 0;
    const { supervisor } = harness({
      now: () => now,
      verifyResources: async () => {
        verificationCalls += 1;
        if (verificationCalls === 1) {
          return verifiedResources();
        }
        throw new Error("verification_failed");
      }
    });
    await supervisor.start();
    supervisor.enterReconciliationRequired();

    await expect(supervisor.retryReadiness()).resolves.toEqual({
      accepted: false,
      reason: "retry_failed"
    });
    await expect(supervisor.retryReadiness()).resolves.toEqual({
      accepted: false,
      reason: "retry_failed"
    });
    await expect(supervisor.retryReadiness()).resolves.toEqual({
      accepted: false,
      reason: "retry_rate_limited"
    });
    expect(verificationCalls).toBe(3);
    expect(supervisor.state).toEqual({
      kind: "recovery",
      reason: "sidecar_crash_loop",
      detail: "recovery_retry_rate_limited"
    });

    now += 60_001;
    await expect(supervisor.retryReadiness()).resolves.toEqual({
      accepted: false,
      reason: "retry_failed"
    });
    expect(verificationCalls).toBe(4);
  });

  it("deactivates synchronously before stop waits for sidecar shutdown", async () => {
    let authorityActive = false;
    const shutdownGate = deferred<void>();
    const { supervisor, spawner } = harness({
      apiSession: {
        activate(): void {
          authorityActive = true;
        },
        deactivate(): void {
          authorityActive = false;
        }
      },
      requestShutdown: async () => {
        await shutdownGate.promise;
        spawner.children.at(-1)?.exit(0);
      }
    });
    await supervisor.start();
    expect(authorityActive).toBe(true);

    const stopping = supervisor.stop();
    expect(authorityActive).toBe(false);
    shutdownGate.resolve();
    await stopping;
  });

  it("enters conservative reconciliation without retrying or terminating the retained sidecar", async () => {
    const { supervisor, spawner, apiSessionEvents } = harness();
    await supervisor.start();

    supervisor.enterReconciliationRequired();
    supervisor.enterReconciliationRequired();

    expect(supervisor.state).toEqual({
      kind: "recovery",
      reason: "reconciliation_required",
      detail: "credential_mutation_reconciliation_required"
    });
    expect(apiSessionEvents.filter((event) => event.kind === "deactivate"))
      .toContainEqual({
        kind: "deactivate",
        generation: 1
      });
    expect(spawner.children.at(-1)?.killSignals).toEqual([]);
  });

  it("deactivates immediately on confirmed unexpected exit before cleanup settles", async () => {
    let authorityActive = false;
    const projected: unknown[] = [];
    const cleanupGate = deferred<void>();
    const { supervisor, spawner } = harness({
      apiSession: {
        activate(): void {
          authorityActive = true;
        },
        deactivate(): void {
          authorityActive = false;
        }
      },
      createLaunchFiles: async (input) => ({
        bootstrapPath: "/profile/runtime/bootstrap.json",
        readinessPath: "/profile/runtime/desktop-readiness.json",
        failurePath: "/profile/runtime/desktop-failure.json",
        launchNonce,
        launchNonceDigest,
        apiToken,
        profile: input.profile,
        cleanup: () => cleanupGate.promise
      })
    });
    supervisor.subscribe((state) => projected.push(state));
    await supervisor.start();
    expect(authorityActive).toBe(true);

    spawner.children[0]?.exit(17);
    expect(authorityActive).toBe(false);
    expect(projected.at(-1)).toMatchObject({
      kind: "recovery",
      reason: "reconciliation_required"
    });

    cleanupGate.resolve();
    await eventually(() => {
      expect(supervisor.state).toMatchObject({
        kind: "recovery",
        reason: "reconciliation_required"
      });
    });
  });

  it("keeps authority inactive when a start fails after generation rotation", async () => {
    const { supervisor, apiSessionEvents } = harness({
      requestReadiness: async () => {
        throw new SidecarSupervisorError(
          "authenticated_readiness_invalid",
          "sidecar_unverified"
        );
      }
    });

    await expect(supervisor.start()).rejects.toThrow(
      "authenticated_readiness_invalid"
    );

    expect(
      apiSessionEvents.some((event) => event.kind === "activate")
    ).toBe(false);
    expect(apiSessionEvents).toContainEqual({
      kind: "deactivate",
      generation: 1
    });
  });

  it("preserves the asynchronous rejection contract for duplicate start", async () => {
    const { supervisor } = harness();
    await supervisor.start();

    await expect(supervisor.start()).rejects.toThrow(
      "sidecar_already_started"
    );
  });

  it("retains the exact executable capability until confirmed child exit", async () => {
    let closeCalls = 0;
    const { supervisor, spawner } = harness({
      acquireVerifiedExecutable: async (resources, relativePath) => {
        const resource = resources.files.get(relativePath);
        if (resource === undefined) {
          throw new Error("missing verified executable");
        }
        return {
          resource,
          mechanism: "test_verified_handle",
          spawn: (request) =>
            spawner.spawn({ executable: resource.path, ...request }),
          close: async () => {
            closeCalls += 1;
          }
        };
      }
    });

    await supervisor.start();

    expect(closeCalls).toBe(0);
    await supervisor.stop();
    expect(closeCalls).toBe(1);
  });

  it("never auto-restarts after an unexpected exit without a unified durable reconciliation authority", async () => {
    const { supervisor, spawner } = harness();
    await supervisor.start();

    spawner.children[0]?.exit(17);
    await eventually(() => {
      expect(supervisor.state).toMatchObject({
        kind: "recovery",
        reason: "reconciliation_required",
        detail: "unexpected_exit_reconciliation_required"
      });
    });
    expect(spawner.children).toHaveLength(1);
  });

  it("shares one exit finalization with stop and never projects stale reconciliation", async () => {
    const cleanupGate = deferred<void>();
    let cleanupCalls = 0;
    let closeCalls = 0;
    const { supervisor, spawner } = harness({
      acquireVerifiedExecutable: async (resources, relativePath) => {
        const resource = resources.files.get(relativePath);
        if (resource === undefined) {
          throw new Error("missing verified executable");
        }
        return {
          resource,
          mechanism: "test_verified_handle",
          spawn: (request) =>
            spawner.spawn({ executable: resource.path, ...request }),
          close: async () => {
            closeCalls += 1;
          }
        };
      },
      createLaunchFiles: async (input) => ({
        bootstrapPath: "/profile/runtime/bootstrap.json",
        readinessPath: "/profile/runtime/desktop-readiness.json",
        failurePath: "/profile/runtime/desktop-failure.json",
        launchNonce,
        launchNonceDigest,
        apiToken,
        profile: input.profile,
        cleanup: async () => {
          cleanupCalls += 1;
          await cleanupGate.promise;
        }
      })
    });
    await supervisor.start();

    spawner.children[0]?.exit(17);
    await eventually(() => expect(cleanupCalls).toBe(1));
    const stop = supervisor.stop();
    await new Promise<void>((resolve) => setImmediate(resolve));
    const cleanupCallsBeforeRelease = cleanupCalls;
    cleanupGate.resolve();
    await stop;
    await new Promise<void>((resolve) => setImmediate(resolve));

    expect(cleanupCallsBeforeRelease).toBe(1);
    expect(cleanupCalls).toBe(1);
    expect(closeCalls).toBe(1);
    expect(supervisor.state).toEqual({ kind: "stopping" });
  });

  it("serializes stop against in-flight verification and prevents later profile or bootstrap mutation", async () => {
    const verification = deferred<VerifiedResourceSet>();
    let profileResolved = false;
    let launchFilesCreated = false;
    const { supervisor, spawner } = harness({
      verifyResources: () => verification.promise,
      resolveProfile: async () => {
        profileResolved = true;
        throw new Error("profile_must_not_be_resolved");
      },
      createLaunchFiles: async () => {
        launchFilesCreated = true;
        throw new Error("launch_files_must_not_be_created");
      }
    });

    const start = supervisor.start();
    const stop = supervisor.stop();
    let stopSettled = false;
    void stop.finally(() => {
      stopSettled = true;
    });
    await new Promise<void>((resolve) => setImmediate(resolve));
    expect(stopSettled).toBe(false);

    verification.resolve(verifiedResources());
    await expect(start).rejects.toThrow("sidecar_start_cancelled");
    await expect(stop).resolves.toBeUndefined();
    expect(profileResolved).toBe(false);
    expect(launchFilesCreated).toBe(false);
    expect(spawner.children).toHaveLength(0);
    expect(supervisor.state.kind).toBe("stopping");
  });

  it("attaches spawn error and exit handlers before awaiting readiness", async () => {
    const readiness = deferred<never>();
    const { supervisor, spawner } = harness({
      acquireVerifiedExecutable: async (resources, relativePath) => {
        const resource = resources.files.get(relativePath);
        if (resource === undefined) {
          throw new Error("missing verified executable");
        }
        return {
          resource,
          mechanism: "test_verified_handle",
          spawn: (request) => {
            const child = spawner.spawn({
              executable: resource.path,
              ...request
            });
            queueMicrotask(() => {
              child.fail(Object.assign(new Error("spawn ENOENT"), {
                code: "ENOENT"
              }));
            });
            return child;
          },
          close: async () => undefined
        };
      },
      waitForReadiness: () => readiness.promise
    });

    await expect(supervisor.start()).rejects.toThrow("sidecar_spawn_failed");
    expect(supervisor.state).toMatchObject({
      kind: "recovery",
      detail: "sidecar_spawn_failed"
    });
  });

  it("buffers an exit before readiness and rejects without authenticating", async () => {
    const readiness = deferred<never>();
    let authenticated = false;
    let cleanupCalls = 0;
    let closeCalls = 0;
    const { supervisor, spawner } = harness({
      acquireVerifiedExecutable: async (resources, relativePath) => {
        const resource = resources.files.get(relativePath);
        if (resource === undefined) {
          throw new Error("missing verified executable");
        }
        return {
          resource,
          mechanism: "test_verified_handle",
          spawn: (request) =>
            spawner.spawn({ executable: resource.path, ...request }),
          close: async () => {
            closeCalls += 1;
          }
        };
      },
      createLaunchFiles: async (input) => ({
        bootstrapPath: "/profile/runtime/bootstrap.json",
        readinessPath: "/profile/runtime/desktop-readiness.json",
        failurePath: "/profile/runtime/desktop-failure.json",
        launchNonce,
        launchNonceDigest,
        apiToken,
        profile: input.profile,
        cleanup: async () => {
          cleanupCalls += 1;
        }
      }),
      waitForReadiness: () => readiness.promise,
      requestReadiness: async () => {
        authenticated = true;
        return apiReadiness();
      }
    });

    const start = supervisor.start();
    await eventually(() => expect(spawner.children).toHaveLength(1));
    spawner.children[0]?.exit(127);

    await expect(start).rejects.toThrow("sidecar_exited_before_readiness");
    expect(authenticated).toBe(false);
    expect(cleanupCalls).toBe(1);
    expect(closeCalls).toBe(1);
  });

  it("aborts readiness and awaits launch cleanup before stop resolves", async () => {
    let cleanupFinished = false;
    const cleanupGate = deferred<void>();
    const { supervisor, spawner } = harness({
      createLaunchFiles: async (input) => ({
        bootstrapPath: "/profile/runtime/bootstrap.json",
        readinessPath: "/profile/runtime/desktop-readiness.json",
        failurePath: "/profile/runtime/desktop-failure.json",
        launchNonce,
        launchNonceDigest,
        apiToken,
        profile: input.profile,
        cleanup: async () => {
          await cleanupGate.promise;
          cleanupFinished = true;
        }
      }),
      waitForReadiness: ({ signal }) =>
        new Promise((_, reject) => {
          signal.addEventListener(
            "abort",
            () => reject(new Error("readiness_aborted")),
            { once: true }
          );
        }),
      waitForExit: async (child) => {
        const fake = child as FakeSidecarChild;
        if (fake.killSignals.length > 0) {
          fake.exit(0, "SIGTERM");
          return true;
        }
        return false;
      }
    });

    const start = supervisor.start();
    await eventually(() => expect(spawner.children).toHaveLength(1));
    const stop = supervisor.stop();
    let stopped = false;
    void stop.finally(() => {
      stopped = true;
    });
    await new Promise<void>((resolve) => setImmediate(resolve));
    expect(stopped).toBe(false);
    expect(cleanupFinished).toBe(false);

    cleanupGate.resolve();
    await expect(start).rejects.toThrow("sidecar_start_cancelled");
    await expect(stop).resolves.toBeUndefined();
    expect(cleanupFinished).toBe(true);
    expect(supervisor.state.kind).toBe("stopping");
  });

  it.each([
    {
      termination: "unconfirmed" as const,
      expected: "sidecar_termination_unconfirmed",
      signals: ["SIGTERM", "SIGKILL"]
    },
    {
      termination: "identity_unverified" as const,
      expected: "sidecar_termination_identity_unverified",
      signals: []
    },
    {
      termination: "signal_rejected" as const,
      expected: "sidecar_termination_signal_rejected",
      signals: ["SIGTERM"]
    }
  ])(
    "surfaces and retains failed-start $termination authority",
    async ({ termination, expected, signals }) => {
      const { supervisor, spawner, counts } =
        failedStartHarness(termination);

      await expect(supervisor.start()).rejects.toThrow(expected);

      expect(supervisor.state).toMatchObject({
        kind: "recovery",
        reason: "sidecar_unavailable",
        detail: expected
      });
      expect(spawner.children).toHaveLength(1);
      expect(spawner.children[0]?.exitCode).toBeNull();
      expect(spawner.children[0]?.killSignals).toEqual(signals);
      expect(counts).toEqual({ cleanup: 0, close: 0 });
      await expect(supervisor.start()).rejects.toThrow(
        "sidecar_already_started"
      );
      expect(spawner.children).toHaveLength(1);
    }
  );

  it("finalizes and releases a retained failed-start child only after confirmed exit", async () => {
    const { supervisor, spawner, counts } =
      failedStartHarness("unconfirmed");

    await expect(supervisor.start()).rejects.toThrow(
      "sidecar_termination_unconfirmed"
    );
    const retainedRecovery = supervisor.state;
    const child = spawner.children[0];
    if (child === undefined) {
      throw new Error("missing retained child");
    }

    child.fail(new Error("process_error_without_exit"));
    await new Promise<void>((resolve) => setImmediate(resolve));
    expect(counts).toEqual({ cleanup: 0, close: 0 });

    child.exit(0);
    await eventually(() => {
      expect(counts).toEqual({ cleanup: 1, close: 1 });
    });
    expect(supervisor.state).toEqual(retainedRecovery);

    await expect(supervisor.start()).rejects.toThrow(
      "sidecar_termination_unconfirmed"
    );
    expect(spawner.children).toHaveLength(2);
  });

  it("shares retained failed-start exit finalization with a racing stop", async () => {
    const cleanupGate = deferred<void>();
    const { supervisor, spawner, counts } = failedStartHarness(
      "unconfirmed",
      { cleanup: () => cleanupGate.promise }
    );
    await expect(supervisor.start()).rejects.toThrow(
      "sidecar_termination_unconfirmed"
    );
    const child = spawner.children[0];
    if (child === undefined) {
      throw new Error("missing retained child");
    }

    child.exit(0);
    await eventually(() => expect(counts.cleanup).toBe(1));
    const stop = supervisor.stop();
    let stopSettled = false;
    void stop.finally(() => {
      stopSettled = true;
    });
    await new Promise<void>((resolve) => setImmediate(resolve));
    expect(stopSettled).toBe(false);

    cleanupGate.resolve();
    await expect(stop).resolves.toBeUndefined();
    await new Promise<void>((resolve) => setImmediate(resolve));
    expect(counts).toEqual({ cleanup: 1, close: 1 });
    expect(supervisor.state).toEqual({ kind: "stopping" });
  });

  it("retains failed-start ownership when confirmed-exit cleanup fails", async () => {
    const { supervisor, spawner, counts, logs } = failedStartHarness(
      "unconfirmed",
      {
        cleanup: async () => {
          throw new Error("cleanup_io_failure");
        }
      }
    );
    await expect(supervisor.start()).rejects.toThrow(
      "sidecar_termination_unconfirmed"
    );

    spawner.children[0]?.exit(0);
    await eventually(() => {
      expect(supervisor.state).toMatchObject({
        kind: "recovery",
        reason: "sidecar_unavailable",
        detail: "sidecar_cleanup_failed"
      });
    });
    expect(counts).toEqual({ cleanup: 1, close: 1 });
    expect(logs).toContain(
      "[sidecar:supervisor] sidecar_cleanup_failed"
    );
    await expect(supervisor.start()).rejects.toThrow(
      "sidecar_already_started"
    );
  });

  it("never kills a conflicting listener it did not spawn", async () => {
    const { supervisor, spawner } = harness({
      inspectLease: async () => ({
        status: "foreign_or_unrelated",
        detail: "conflicting_listener"
      })
    });

    await expect(supervisor.start()).rejects.toThrow("foreign_or_unrelated");
    expect(spawner.children).toHaveLength(0);
  });

  it("does not send its token until readiness and lease identity are locally verified", async () => {
    let inspection = 0;
    let tokenWasSent = false;
    const { supervisor } = harness({
      inspectLease: async () => {
        inspection += 1;
        return inspection === 1
          ? { status: "available" }
          : {
              status: "foreign_or_unrelated",
              detail: "listener_not_owned_by_child"
            };
      },
      requestReadiness: async () => {
        tokenWasSent = true;
        return apiReadiness();
      }
    });

    await expect(supervisor.start()).rejects.toThrow(
      "profile_lease_readiness_mismatch"
    );
    expect(tokenWasSent).toBe(false);
  });

  it("rejects a substituted launch nonce before sending the API token", async () => {
    let tokenWasSent = false;
    const { supervisor } = harness({
      waitForReadiness: async ({ child }) => ({
        identity: { dev: 1, ino: child.pid ?? 0 },
        readiness: {
          schema: "kestrel.desktop.sidecar_readiness.v1",
          pid: child.pid ?? 0,
          process_birth_marker: `birth-${child.pid}`,
          port: 43000 + (child.pid ?? 0),
          profile_id: "default",
          sidecar_version: "0.5.0",
          executable_digest: executableDigest,
          resource_manifest_digest: manifestDigest,
          launch_nonce_digest: "0".repeat(64)
        }
      }),
      requestReadiness: async () => {
        tokenWasSent = true;
        return apiReadiness();
      }
    });

    await expect(supervisor.start()).rejects.toThrow(
      "sidecar_readiness_identity_mismatch"
    );
    expect(tokenWasSent).toBe(false);
    expect(supervisor.state).toMatchObject({
      kind: "recovery",
      reason: "sidecar_unverified"
    });
  });

  it("rejects PID reuse when the process birth marker changes before authentication", async () => {
    let tokenWasSent = false;
    const { supervisor } = harness({
      inspectProcess: async (pid) => ({
        pid,
        processBirthMarker: `reused-birth-${pid}`,
        executableDigest
      }),
      requestReadiness: async () => {
        tokenWasSent = true;
        return apiReadiness();
      }
    });

    await expect(supervisor.start()).rejects.toThrow(
      "sidecar_readiness_identity_mismatch"
    );
    expect(tokenWasSent).toBe(false);
  });

  it("authenticates graceful shutdown to the verified URL before any handle signal", async () => {
    const { supervisor, spawner, shutdownRequests } = harness();
    await supervisor.start();

    await supervisor.stop();

    expect(shutdownRequests).toEqual([
      {
        baseUrl: `http://127.0.0.1:${43000 + 9100}/`,
        apiToken
      }
    ]);
    expect(spawner.children[0]?.killSignals).toEqual([]);
    expect(supervisor.state.kind).toBe("stopping");
  });

  it("signals only its retained verified child and fails closed if termination stays unconfirmed", async () => {
    const { supervisor, spawner } = harness({
      requestShutdown: async () => {
        throw new Error("shutdown_unavailable");
      },
      waitForExit: async () => false
    });
    await supervisor.start();

    await expect(supervisor.stop()).rejects.toThrow(
      "sidecar_termination_unconfirmed"
    );

    expect(spawner.children[0]?.killSignals).toEqual(["SIGTERM", "SIGKILL"]);
    expect(supervisor.state).toMatchObject({
      kind: "recovery",
      reason: "sidecar_unavailable",
      detail: "sidecar_termination_unconfirmed"
    });
  });

  it("waits for graceful exit even when the authenticated shutdown request fails", async () => {
    const waitsWithSignals: number[] = [];
    const { supervisor, spawner } = harness({
      requestShutdown: async () => {
        throw new Error("shutdown_unavailable");
      },
      waitForExit: async (child) => {
        waitsWithSignals.push(
          (child as FakeSidecarChild).killSignals.length
        );
        return false;
      }
    });
    await supervisor.start();

    await expect(supervisor.stop()).rejects.toThrow(
      "sidecar_termination_unconfirmed"
    );
    expect(waitsWithSignals).toEqual([0, 1, 2]);
  });

  it("refuses TERM when fresh process or lease identity cannot be reattested", async () => {
    let drifted = false;
    const { supervisor, spawner } = harness({
      requestShutdown: async () => {
        drifted = true;
        throw new Error("shutdown_unavailable");
      },
      waitForExit: async () => false,
      inspectProcess: async (pid) => ({
        pid,
        processBirthMarker: drifted
          ? `substituted-${pid}`
          : `birth-${pid}`,
        executableDigest
      })
    });
    await supervisor.start();

    await expect(supervisor.stop()).rejects.toThrow(
      "sidecar_termination_identity_unverified"
    );
    expect(spawner.children[0]?.killSignals).toEqual([]);
  });

  it("reattests again and refuses KILL when identity changes after TERM", async () => {
    const { supervisor, spawner } = harness({
      requestShutdown: async () => {
        throw new Error("shutdown_unavailable");
      },
      waitForExit: async () => false,
      inspectProcess: async (pid) => ({
        pid,
        processBirthMarker:
          (spawner.children[0]?.killSignals.length ?? 0) > 0
            ? `substituted-${pid}`
            : `birth-${pid}`,
        executableDigest
      })
    });
    await supervisor.start();

    await expect(supervisor.stop()).rejects.toThrow(
      "sidecar_termination_identity_unverified"
    );
    expect(spawner.children[0]?.killSignals).toEqual(["SIGTERM"]);
  });

  it("checks a rejected retained-handle signal and reports a fixed error", async () => {
    const { supervisor, spawner } = harness({
      requestShutdown: async () => {
        throw new Error("shutdown_unavailable");
      },
      waitForExit: async () => false
    });
    await supervisor.start();
    const child = spawner.children[0];
    if (child === undefined) {
      throw new Error("missing child");
    }
    child.kill = (signal?: NodeJS.Signals | number): boolean => {
      child.killSignals.push(signal);
      return false;
    };

    await expect(supervisor.stop()).rejects.toThrow(
      "sidecar_termination_signal_rejected"
    );
    expect(child.killSignals).toEqual(["SIGTERM"]);
  });

  it("surfaces launch-artifact cleanup failure after confirmed exit", async () => {
    const { supervisor } = harness({
      createLaunchFiles: async (input) => ({
        bootstrapPath: "/profile/runtime/bootstrap.json",
        readinessPath: "/profile/runtime/desktop-readiness.json",
        failurePath: "/profile/runtime/desktop-failure.json",
        launchNonce,
        launchNonceDigest,
        apiToken,
        profile: input.profile,
        cleanup: async () => {
          throw new Error("cleanup_io_failure");
        }
      })
    });
    await supervisor.start();

    await expect(supervisor.stop()).rejects.toThrow(
      "sidecar_cleanup_failed"
    );
    expect(supervisor.state).toMatchObject({
      kind: "recovery",
      detail: "sidecar_cleanup_failed"
    });
  });

  it("bounds and redacts child logs without retaining split secrets", async () => {
    const { supervisor, spawner, logs } = harness();
    await supervisor.start();
    const child = spawner.children[0];
    if (child === undefined) {
      throw new Error("missing child");
    }
    (child.stdout as PassThrough).write(
      `Bearer ${apiToken}\n${"x".repeat(10_000)}\n`
    );
    (child.stderr as PassThrough).write(`launch_nonce=${launchNonce}\n`);
    await eventually(() => expect(logs.length).toBeGreaterThanOrEqual(3));

    expect(logs.join("\n")).not.toContain(apiToken);
    expect(logs.join("\n")).not.toContain(launchNonce);
    expect(Math.max(...logs.map((line) => Buffer.byteLength(line)))).toBeLessThanOrEqual(
      1_024
    );
  });

});
