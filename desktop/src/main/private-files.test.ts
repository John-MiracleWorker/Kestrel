import { createHash } from "node:crypto";
import {
  chmod,
  lstat,
  mkdtemp,
  mkdir,
  readFile,
  rm,
  symlink,
  writeFile
} from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  createPrivateLaunchFiles,
  readSidecarReadiness,
  resolvePrivateProfile
} from "./private-files";

const memoryLayers = [
  "working",
  "episodic",
  "semantic",
  "procedural",
  "self",
  "policy"
];

describe("private desktop launch files", () => {
  let testRoot: string;
  let profileRoot: string;

  beforeEach(async () => {
    testRoot = await mkdtemp(join(tmpdir(), "kestrel-private-"));
    profileRoot = join(testRoot, "profile");
  });

  afterEach(async () => {
    await rm(testRoot, { force: true, recursive: true });
  });

  it("creates the strict bootstrap atomically with real POSIX owner-only mode", async () => {
    const profile = await resolvePrivateProfile({
      profileId: "default",
      profileRoot,
      statePath: join(profileRoot, "state", "agent.db"),
      memoryDir: join(profileRoot, "memory"),
      runtimeSettingsPath: join(profileRoot, "config", "runtime_settings.json")
    });
    const bytes = [Buffer.alloc(32, 0x11), Buffer.alloc(32, 0x22)];
    const launch = await createPrivateLaunchFiles({
      profile,
      parentPid: process.pid,
      parentBirthMarker: "parent-birth-marker",
      resourceManifestDigest: `sha256:${"a".repeat(64)}`,
      randomBytes: () => {
        const value = bytes.shift();
        if (value === undefined) {
          throw new Error("unexpected randomness request");
        }
        return value;
      }
    });

    const payload = JSON.parse(
      await readFile(launch.bootstrapPath, "utf8")
    ) as Record<string, unknown>;
    expect(payload).toEqual({
      schema: "kestrel.desktop.bootstrap.v1",
      profile_id: "default",
      profile_root: profile.profileRoot,
      state_path: profile.statePath,
      memory_dir: profile.memoryDir,
      runtime_settings_path: profile.runtimeSettingsPath,
      launch_nonce: "11".repeat(32),
      api_token: "22".repeat(32),
      parent_pid: process.pid,
      parent_birth_marker: "parent-birth-marker",
      resource_manifest_digest: `sha256:${"a".repeat(64)}`,
      memory_layers: memoryLayers
    });
    expect(launch.launchNonceDigest).toBe(
      createHash("sha256").update("11".repeat(32)).digest("hex")
    );
    if (process.platform !== "win32") {
      expect((await lstat(launch.bootstrapPath)).mode & 0o777).toBe(0o600);
      expect((await lstat(profile.profileRoot)).mode & 0o777).toBe(0o700);
    }
  });

  it("removes partial launch artifacts when secure creation fails", async () => {
    const profile = await resolvePrivateProfile({
      profileId: "default",
      profileRoot,
      statePath: join(profileRoot, "state", "agent.db"),
      memoryDir: join(profileRoot, "memory"),
      runtimeSettingsPath: join(profileRoot, "config", "runtime_settings.json")
    });

    await expect(
      createPrivateLaunchFiles({
        profile,
        parentPid: process.pid,
        parentBirthMarker: "parent-birth-marker",
        resourceManifestDigest: `sha256:${"a".repeat(64)}`,
        randomBytes: () => Buffer.alloc(31)
      })
    ).rejects.toThrow("256-bit");
    expect(
      (await import("node:fs")).readdirSync(join(profileRoot, "runtime"))
    ).toEqual([]);
  });

  it("reads only bounded owner-only readiness and rejects symlinks", async () => {
    const profile = await resolvePrivateProfile({
      profileId: "default",
      profileRoot,
      statePath: join(profileRoot, "state", "agent.db"),
      memoryDir: join(profileRoot, "memory"),
      runtimeSettingsPath: join(profileRoot, "config", "runtime_settings.json")
    });
    const readiness = {
      schema: "kestrel.desktop.sidecar_readiness.v1",
      pid: process.pid,
      process_birth_marker: "sidecar-birth",
      port: 43123,
      profile_id: "default",
      sidecar_version: "0.5.0",
      executable_digest: "b".repeat(64),
      resource_manifest_digest: `sha256:${"a".repeat(64)}`,
      launch_nonce_digest: "c".repeat(64)
    };
    await writeFile(
      profile.readinessPath,
      JSON.stringify(readiness),
      { mode: 0o600 }
    );
    await chmod(profile.readinessPath, 0o600);

    await expect(readSidecarReadiness(profile.readinessPath)).resolves.toEqual(
      readiness
    );

    const target = join(testRoot, "outside-readiness.json");
    await writeFile(target, JSON.stringify(readiness), { mode: 0o600 });
    await rm(profile.readinessPath);
    await symlink(target, profile.readinessPath);
    await expect(readSidecarReadiness(profile.readinessPath)).rejects.toThrow(
      "symlink"
    );

    await rm(profile.readinessPath);
    await writeFile(profile.readinessPath, " ".repeat(16 * 1024 + 1), {
      mode: 0o600
    });
    await expect(readSidecarReadiness(profile.readinessPath)).rejects.toThrow(
      "16 KiB"
    );
  });

  it("fails closed instead of claiming chmod qualifies a Windows owner ACL", async () => {
    await mkdir(profileRoot);
    await expect(
      resolvePrivateProfile(
        {
          profileId: "default",
          profileRoot,
          statePath: join(profileRoot, "state", "agent.db"),
          memoryDir: join(profileRoot, "memory"),
          runtimeSettingsPath: join(
            profileRoot,
            "config",
            "runtime_settings.json"
          )
        },
        {
          platform: "win32",
          currentOwnerId: () => null,
          qualifyOwnerOnly: async () => {
            throw new Error("windows_owner_acl_unqualified");
          }
        }
      )
    ).rejects.toThrow("windows_owner_acl_unqualified");
  });

  it("rejects a symlinked profile without changing the target permissions", async () => {
    const outside = join(testRoot, "outside-profile");
    await mkdir(outside, { mode: 0o755 });
    await chmod(outside, 0o755);
    await symlink(outside, profileRoot);

    await expect(
      resolvePrivateProfile({
        profileId: "default",
        profileRoot,
        statePath: join(profileRoot, "state", "agent.db"),
        memoryDir: join(profileRoot, "memory"),
        runtimeSettingsPath: join(
          profileRoot,
          "config",
          "runtime_settings.json"
        )
      })
    ).rejects.toThrow("symlink");
    if (process.platform !== "win32") {
      expect((await lstat(outside)).mode & 0o777).toBe(0o755);
    }
  });

  it("rejects an existing state file that resolves outside the profile", async () => {
    const stateDirectory = join(profileRoot, "state");
    await mkdir(stateDirectory, { recursive: true, mode: 0o700 });
    const outsideState = join(testRoot, "outside.db");
    await writeFile(outsideState, "outside", { mode: 0o600 });
    await symlink(outsideState, join(stateDirectory, "agent.db"));

    await expect(
      resolvePrivateProfile({
        profileId: "default",
        profileRoot,
        statePath: join(stateDirectory, "agent.db"),
        memoryDir: join(profileRoot, "memory"),
        runtimeSettingsPath: join(
          profileRoot,
          "config",
          "runtime_settings.json"
        )
      })
    ).rejects.toThrow("state_path");
  });
});
