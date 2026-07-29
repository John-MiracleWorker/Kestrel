import { createHash } from "node:crypto";
import { PassThrough } from "node:stream";
import { describe, expect, it } from "vitest";
import type { VerifiedResourceSet } from "./resource-manifest";
import {
  SidecarSupervisor,
  type AuthenticatedDesktopReadiness,
  type ProfileLeaseEvidence,
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
      key_id: "ephemeral-test",
      files: {
        "sidecar/kestrel-desktop-sidecar": {
          size: 16,
          sha256: executableDigest
        },
        "web/dist/index.html": {
          size: 16,
          sha256: "c".repeat(64)
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
      ]
    ]),
    rendererAssets: {
      totalBytes: 16,
      read: (relativePath) =>
        relativePath === "index.html"
          ? Buffer.from("<h1>Kestrel</h1>")
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

function harness(overrides: Partial<SidecarSupervisorDependencies> = {}): {
  supervisor: SidecarSupervisor;
  spawner: FakeSidecarSpawner;
  logs: string[];
  shutdownRequests: Array<{ baseUrl: string; apiToken: string }>;
} {
  const spawner = new FakeSidecarSpawner();
  const logs: string[] = [];
  const shutdownRequests: Array<{ baseUrl: string; apiToken: string }> = [];
  let leaseInspection = 0;
  const dependencies: SidecarSupervisorDependencies = {
    verifyResources: async () => verifiedResources(),
    acquireVerifiedExecutable: async (resources, relativePath) => {
      const resource = resources.files.get(relativePath);
      if (resource === undefined) {
        throw new Error("missing verified executable");
      }
      return {
        resource,
        mechanism: "test_verified_handle",
        spawn: (request) =>
          spawner.spawn({
            executable: resource.path,
            ...request
          }),
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
      const phase = leaseInspection;
      leaseInspection += 1;
      if (phase === 0) {
        return { status: "available" };
      }
      const child = spawner.children.at(-1);
      if (child === undefined) {
        throw new Error("missing spawned child");
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
    requestShutdown: async (request) => {
      shutdownRequests.push(request);
      spawner.children.at(-1)?.exit(0);
    },
    waitForExit: async (child) => child.exitCode !== null,
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
        environment: {
          PATH: "/safe/bin",
          AWS_SECRET_ACCESS_KEY: "must-not-inherit",
          KESTREL_API_TOKEN: "must-not-inherit"
        }
      },
      dependencies
    ),
    spawner,
    logs,
    shutdownRequests
  };
}

describe("verified sidecar supervisor", () => {
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
      reason: "sidecar_unverified"
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

    await supervisor.start();

    expect(supervisor.state).toMatchObject({
      kind: "ready",
      profileId: "default"
    });
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

  it("preserves the asynchronous rejection contract for duplicate start", async () => {
    const { supervisor } = harness();
    await supervisor.start();

    await expect(supervisor.start()).rejects.toThrow(
      "sidecar_already_started"
    );
  });

  it("closes the exact executable capability as soon as the spawned identity is retained", async () => {
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

    expect(closeCalls).toBe(1);
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
    const { supervisor, spawner } = harness({
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
  });

  it("aborts readiness and awaits launch cleanup before stop resolves", async () => {
    let cleanupFinished = false;
    const cleanupGate = deferred<void>();
    const { supervisor, spawner } = harness({
      createLaunchFiles: async (input) => ({
        bootstrapPath: "/profile/runtime/bootstrap.json",
        readinessPath: "/profile/runtime/desktop-readiness.json",
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
