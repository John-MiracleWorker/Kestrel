import { createHash } from "node:crypto";
import {
  chmod,
  mkdtemp,
  mkdir,
  rm,
  writeFile
} from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { PassThrough } from "node:stream";
import { describe, expect, it } from "vitest";
import { resolvePrivateProfile } from "./private-files";
import type { VerifiedResourceSet } from "./resource-manifest";
import {
  createNodeSupervisorDependencies,
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
    ])
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
      if (phase % 2 === 0) {
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
    spawnSidecar: spawner.spawn,
    waitForReadiness: async ({ child }) => {
      const pid = child.pid;
      if (pid === undefined) {
        throw new Error("missing child PID");
      }
      return {
        schema: "kestrel.desktop.sidecar_readiness.v1",
        pid,
        process_birth_marker: `birth-${pid}`,
        port: 43000 + pid,
        profile_id: "default",
        sidecar_version: "0.5.0",
        executable_digest: executableDigest,
        resource_manifest_digest: manifestDigest,
        launch_nonce_digest: launchNonceDigest
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
    reconcileUnexpectedExit: async () => ({ status: "safe_to_restart" }),
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

  it("restarts once after explicit safe reconciliation and then enters recovery", async () => {
    const { supervisor, spawner } = harness();
    await supervisor.start();

    spawner.children[0]?.exit(17);
    await eventually(() => {
      expect(spawner.children).toHaveLength(2);
      expect(supervisor.state.kind).toBe("ready");
    });

    spawner.children[1]?.exit(17);
    await eventually(() => {
      expect(supervisor.state).toMatchObject({
        kind: "recovery",
        reason: "sidecar_unavailable"
      });
    });
    expect(spawner.children).toHaveLength(2);
  });

  it("does not restart when a high-risk call is ambiguous", async () => {
    const { supervisor, spawner } = harness();
    await supervisor.start();
    supervisor.markHighRiskCallAmbiguous();

    spawner.children[0]?.exit(17);

    await eventually(() => {
      expect(supervisor.state).toMatchObject({
        kind: "recovery",
        reason: "reconciliation_required"
      });
    });
    expect(spawner.children).toHaveLength(1);
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
        schema: "kestrel.desktop.sidecar_readiness.v1",
        pid: child.pid ?? 0,
        process_birth_marker: `birth-${child.pid}`,
        port: 43000 + (child.pid ?? 0),
        profile_id: "default",
        sidecar_version: "0.5.0",
        executable_digest: executableDigest,
        resource_manifest_digest: manifestDigest,
        launch_nonce_digest: "0".repeat(64)
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

  it("allows production restart reconciliation only after lease evidence is absent", async () => {
    const testRoot = await mkdtemp(join(tmpdir(), "kestrel-reconcile-"));
    const profileInput = {
      profileId: "default",
      profileRoot: join(testRoot, "profile"),
      statePath: join(testRoot, "profile", "state", "agent.db"),
      memoryDir: join(testRoot, "profile", "memory"),
      runtimeSettingsPath: join(
        testRoot,
        "profile",
        "config",
        "runtime_settings.json"
      )
    };
    try {
      const profile = await resolvePrivateProfile(profileInput);
      const dependencies = createNodeSupervisorDependencies({
        resourceVerification: {
          resourceRoot: testRoot,
          manifestPath: join(testRoot, "resources.json"),
          signaturePath: join(testRoot, "resources.sig"),
          trustedKeys: new Map(),
          requiredFiles: []
        },
        profile: profileInput,
        sidecarVersion: "0.5.0"
      });

      await expect(
        dependencies.reconcileUnexpectedExit({
          profile,
          exitCode: 17,
          signal: null
        })
      ).resolves.toEqual({ status: "safe_to_restart" });

      await mkdir(profile.leaseControlRoot, {
        recursive: true,
        mode: 0o700
      });
      await chmod(profile.leaseControlRoot, 0o700);
      const metadataPath = join(
        profile.leaseControlRoot,
        "runtime-profile.json"
      );
      await writeFile(metadataPath, '{"schema":"untrusted"}', {
        mode: 0o600
      });
      await chmod(metadataPath, 0o600);

      await expect(
        dependencies.reconcileUnexpectedExit({
          profile,
          exitCode: 17,
          signal: null
        })
      ).resolves.toEqual({ status: "reconciliation_required" });
    } finally {
      await rm(testRoot, { force: true, recursive: true });
    }
  });
});
