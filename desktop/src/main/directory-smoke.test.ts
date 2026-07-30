import {
  access,
  mkdir,
  mkdtemp,
  readFile,
  stat,
  symlink,
  writeFile
} from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import type { PackagedBuildTrust } from "./build-trust";
import {
  DIRECTORY_SMOKE_MEMORY_FILES,
  createDirectorySmokeCycle,
  selectDirectorySmokeRequest,
  waitForDirectorySmokeMission
} from "./directory-smoke";

const roots: string[] = [];
const developerTrust: PackagedBuildTrust = Object.freeze({
  buildMode: "developer",
  keyId: "developer",
  sourceCommit: "a".repeat(40),
  appVersion: "0.5.0",
  platform: "darwin",
  architecture: "arm64",
  resourceRootRelative: "kestrel",
  smokeAuthority: "developer_directory_smoke_v1",
  pythonLockSha256: "1".repeat(64),
  desktopNpmLockSha256: "2".repeat(64),
  webNpmLockSha256: "3".repeat(64),
  sbomSha256: "4".repeat(64)
});

async function fixture(): Promise<{
  userDataPath: string;
  profileRoot: string;
}> {
  const root = await mkdtemp(join(tmpdir(), "kestrel-directory-smoke-"));
  roots.push(root);
  const userDataPath = join(root, "owner");
  const profileRoot = join(userDataPath, "profiles", "default");
  const memoryRoot = join(profileRoot, "memory");
  await mkdir(memoryRoot, { recursive: true, mode: 0o700 });
  for (const name of DIRECTORY_SMOKE_MEMORY_FILES) {
    await writeFile(join(memoryRoot, name), `${name}\n`, { mode: 0o600 });
  }
  return { userDataPath, profileRoot };
}

afterEach(async () => {
  const { rm } = await import("node:fs/promises");
  await Promise.all(
    roots.splice(0).map((root) =>
      rm(root, { recursive: true, force: true })
    )
  );
});

describe("packaged developer directory smoke", () => {
  it("allows argv to request, but never grant, immutable smoke authority", () => {
    process.env.KESTREL_DIRECTORY_SMOKE = "1";
    expect(selectDirectorySmokeRequest(developerTrust, [])).toBe(false);
    expect(
      selectDirectorySmokeRequest(developerTrust, [
        "--kestrel-directory-smoke"
      ])
    ).toBe(true);
    expect(() =>
      selectDirectorySmokeRequest(
        {
          ...developerTrust,
          buildMode: "release",
          keyId: "release",
          smokeAuthority: "disabled"
        },
        ["--kestrel-directory-smoke"]
      )
    ).toThrow("desktop_directory_smoke_forbidden");
    expect(() =>
      selectDirectorySmokeRequest(developerTrust, [
        "--kestrel-directory-smoke",
        "--kestrel-directory-smoke"
      ])
    ).toThrow("desktop_directory_smoke_request_invalid");
    delete process.env.KESTREL_DIRECTORY_SMOKE;
  });

  it("proves the exact hidden Mission Command DOM instead of trusting navigation completion", async () => {
    const observations = [false, true];
    const evaluated: string[] = [];

    await waitForDirectorySmokeMission(
      {
        isDestroyed: () => false,
        getURL: () => "kestrel://app/index.html",
        executeJavaScript: async (source) => {
          evaluated.push(source);
          return observations.shift() ?? false;
        }
      },
      { timeoutMs: 100, pollIntervalMs: 1 }
    );

    expect(evaluated).toHaveLength(2);
    expect(evaluated[0]).toContain(
      '.mission-shell[data-active-section="mission"]'
    );
    await expect(
      waitForDirectorySmokeMission(
        {
          isDestroyed: () => false,
          getURL: () => "https://example.com/",
          executeJavaScript: async () => true
        },
        { timeoutMs: 10, pollIntervalMs: 1 }
      )
    ).rejects.toThrow("desktop_directory_smoke_mission_untrusted");
  });

  it("holds a hidden, authenticated ready cycle until the fixed continuation and completes only after shutdown", async () => {
    const { userDataPath, profileRoot } = await fixture();
    const events: string[] = [];
    const cycle = createDirectorySmokeCycle({
      userDataPath,
      profileRoot,
      sourceCommit: developerTrust.sourceCommit,
      supervisorState: () => ({
        kind: "ready",
        profileId: "default",
        baseUrl: "http://127.0.0.1:49152",
        sidecarVersion: "0.5.0"
      }),
      stopSupervisor: async () => {
        events.push("authenticated-shutdown");
      },
      quit: () => {
        events.push("quit");
      }
    });

    const running = cycle.runAfterMissionCommandLoaded();
    const ready = await cycle.waitForReadyForTest();
    expect(ready).toEqual({
      schema: "kestrel.desktop.directory-smoke-ready.v1",
      authenticated_readiness: true,
      authenticated_recovery: true,
      build_mode: "developer",
      hidden: true,
      memory_files: [...DIRECTORY_SMOKE_MEMORY_FILES],
      mission_command_url: "kestrel://app/index.html",
      source_commit: developerTrust.sourceCommit
    });
    expect(events).toEqual([]);

    await cycle.continueForTest();
    await running;

    expect(events).toEqual(["authenticated-shutdown", "quit"]);
    expect(
      JSON.parse(
        await readFile(cycle.paths.completedPath, "utf8")
      )
    ).toEqual({
      schema: "kestrel.desktop.directory-smoke-completed.v1",
      authenticated_shutdown: true,
      child_exited: true
    });
  });

  it("requires exactly the canonical six Memvid v2 files and no second set", async () => {
    const { userDataPath, profileRoot } = await fixture();
    const extraRoot = join(profileRoot, "other-memory");
    await mkdir(extraRoot, { mode: 0o700 });
    await writeFile(join(extraRoot, "working.mv2"), "second\n", {
      mode: 0o600
    });
    const cycle = createDirectorySmokeCycle({
      userDataPath,
      profileRoot,
      sourceCommit: developerTrust.sourceCommit,
      supervisorState: () => ({
        kind: "ready",
        profileId: "default",
        baseUrl: "http://127.0.0.1:49152",
        sidecarVersion: "0.5.0"
      }),
      stopSupervisor: async () => undefined,
      quit: () => undefined
    });

    await expect(
      cycle.runAfterMissionCommandLoaded()
    ).rejects.toThrow("desktop_directory_smoke_memory_set_invalid");
    expect((await stat(userDataPath)).isDirectory()).toBe(true);
  });

  it("rejects a linked control ancestor without writing outside userData", async () => {
    const { userDataPath, profileRoot } = await fixture();
    const outside = join(userDataPath, "..", "outside");
    await mkdir(outside, { mode: 0o700 });
    await symlink(outside, join(userDataPath, "directory-smoke-v1"));
    const cycle = createDirectorySmokeCycle({
      userDataPath,
      profileRoot,
      sourceCommit: developerTrust.sourceCommit,
      supervisorState: () => ({
        kind: "ready",
        profileId: "default",
        baseUrl: "http://127.0.0.1:49152",
        sidecarVersion: "0.5.0"
      }),
      stopSupervisor: async () => undefined,
      quit: () => undefined
    });

    await expect(
      cycle.runAfterMissionCommandLoaded()
    ).rejects.toThrow(/private_profile_symlink_untrusted|control_untrusted/);
    await expect(access(join(outside, "ready.json"))).rejects.toMatchObject({
      code: "ENOENT"
    });
  });

  it("authentically stops and quits on a malformed continuation without claiming completion", async () => {
    const { userDataPath, profileRoot } = await fixture();
    const events: string[] = [];
    const cycle = createDirectorySmokeCycle({
      userDataPath,
      profileRoot,
      sourceCommit: developerTrust.sourceCommit,
      supervisorState: () => ({
        kind: "ready",
        profileId: "default",
        baseUrl: "http://127.0.0.1:49152",
        sidecarVersion: "0.5.0"
      }),
      stopSupervisor: async () => {
        events.push("authenticated-shutdown");
      },
      quit: () => {
        events.push("quit");
      }
    });
    const running = cycle.runAfterMissionCommandLoaded();
    await cycle.waitForReadyForTest();
    await writeFile(
      cycle.paths.continuePath,
      '{"continue":false,"schema":"kestrel.desktop.directory-smoke-continue.v1"}\n',
      { mode: 0o600 }
    );

    await expect(running).rejects.toThrow(
      "desktop_directory_smoke_control_invalid"
    );
    expect(events).toEqual(["authenticated-shutdown", "quit"]);
    await expect(access(cycle.paths.completedPath)).rejects.toMatchObject({
      code: "ENOENT"
    });
  });

  it("does not serialize runtime authority or location into handshake diagnostics", async () => {
    const { userDataPath, profileRoot } = await fixture();
    const cycle = createDirectorySmokeCycle({
      userDataPath,
      profileRoot,
      sourceCommit: developerTrust.sourceCommit,
      supervisorState: () => ({
        kind: "ready",
        profileId: "default",
        baseUrl: "http://127.0.0.1:49152",
        sidecarVersion: "0.5.0"
      }),
      stopSupervisor: async () => undefined,
      quit: () => undefined
    });
    const running = cycle.runAfterMissionCommandLoaded();
    await cycle.waitForReadyForTest();
    await cycle.continueForTest();
    await running;

    const diagnostics = [
      await readFile(cycle.paths.readyPath, "utf8"),
      await readFile(cycle.paths.completedPath, "utf8")
    ].join("\n");
    expect(diagnostics).not.toContain(userDataPath);
    expect(diagnostics).not.toContain("127.0.0.1");
    expect(diagnostics).not.toMatch(
      /api.?token|nonce|bootstrap|base.?url|endpoint|port|pid/i
    );
  });
});
