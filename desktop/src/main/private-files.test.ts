import { createHash } from "node:crypto";
import {
  chmod,
  lstat,
  mkdtemp,
  mkdir,
  readFile,
  realpath,
  rename,
  rm,
  symlink,
  unlink,
  writeFile
} from "node:fs/promises";
import { tmpdir } from "node:os";
import {
  isAbsolute,
  join,
  relative,
  resolve,
  sep
} from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  createPrivateLaunchFiles,
  createNodePrivateFileAdapter,
  readSidecarReadiness,
  resolvePrivateProfile,
  runtimeProfileControlIdentityBytes,
  type PrivateFilePlatformAdapter
} from "./private-files";

const memoryLayers = [
  "working",
  "episodic",
  "semantic",
  "procedural",
  "self",
  "policy"
];

function qualifiedTestDirectoryAdapter(): PrivateFilePlatformAdapter {
  const base = createNodePrivateFileAdapter();
  return {
    ...base,
    async preparePrivateDirectory(
      trustedAnchor: string,
      candidate: string
    ): Promise<string> {
      const canonicalAnchor = await realpath(trustedAnchor);
      const target = resolve(candidate);
      const fromAnchor = relative(canonicalAnchor, target);
      if (
        fromAnchor === "" ||
        isAbsolute(fromAnchor) ||
        fromAnchor === ".." ||
        fromAnchor.startsWith(`..${sep}`)
      ) {
        throw new Error("private_directory_outside_trusted_anchor");
      }
      let current = canonicalAnchor;
      for (const segment of fromAnchor.split(sep)) {
        current = join(current, segment);
        let metadata;
        try {
          metadata = await lstat(current);
        } catch (error) {
          if ((error as NodeJS.ErrnoException).code !== "ENOENT") {
            throw error;
          }
          await mkdir(current, { mode: 0o700 });
          metadata = await lstat(current);
        }
        if (metadata.isSymbolicLink() || !metadata.isDirectory()) {
          throw new Error("private_profile_symlink_untrusted");
        }
        await base.qualifyOwnerOnly(current, "directory");
      }
      return realpath(current);
    },
    async deleteCapturedFile(
      path: string,
      identity: { dev: number; ino: number }
    ): Promise<void> {
      let metadata;
      try {
        metadata = await lstat(path);
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code === "ENOENT") {
          return;
        }
        throw error;
      }
      if (
        metadata.isSymbolicLink() ||
        !metadata.isFile() ||
        metadata.dev !== identity.dev ||
        metadata.ino !== identity.ino
      ) {
        return;
      }
      await unlink(path);
    }
  };
}

describe("private desktop launch files", () => {
  let testRoot: string;
  let profileRoot: string;
  let profileAdapter: PrivateFilePlatformAdapter;

  beforeEach(async () => {
    testRoot = await mkdtemp(join(tmpdir(), "kestrel-private-"));
    profileRoot = join(testRoot, "profile");
    profileAdapter = qualifiedTestDirectoryAdapter();
  });

  afterEach(async () => {
    await rm(testRoot, { force: true, recursive: true });
  });

  it("creates the strict bootstrap atomically with real POSIX owner-only mode", async () => {
    const profile = await resolvePrivateProfile({
      profileId: "default",
      trustedAnchor: testRoot,
      profileRoot,
      statePath: join(profileRoot, "state", "agent.db"),
      memoryDir: join(profileRoot, "memory"),
      runtimeSettingsPath: join(profileRoot, "config", "runtime_settings.json")
    }, profileAdapter);
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
      trustedAnchor: testRoot,
      profileRoot,
      statePath: join(profileRoot, "state", "agent.db"),
      memoryDir: join(profileRoot, "memory"),
      runtimeSettingsPath: join(profileRoot, "config", "runtime_settings.json")
    }, profileAdapter);

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
      trustedAnchor: testRoot,
      profileRoot,
      statePath: join(profileRoot, "state", "agent.db"),
      memoryDir: join(profileRoot, "memory"),
      runtimeSettingsPath: join(profileRoot, "config", "runtime_settings.json")
    }, profileAdapter);
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

    await expect(
      readSidecarReadiness(profile.readinessPath)
    ).resolves.toMatchObject({ readiness });

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

  it("cleans up only the readiness inode captured by the verified read", async () => {
    const profile = await resolvePrivateProfile({
      profileId: "default",
      trustedAnchor: testRoot,
      profileRoot,
      statePath: join(profileRoot, "state", "agent.db"),
      memoryDir: join(profileRoot, "memory"),
      runtimeSettingsPath: join(
        profileRoot,
        "config",
        "runtime_settings.json"
      )
    }, profileAdapter);
    const secrets = [Buffer.alloc(32, 0x11), Buffer.alloc(32, 0x22)];
    const launch = await createPrivateLaunchFiles({
      profile,
      parentPid: process.pid,
      parentBirthMarker: "parent-birth-marker",
      resourceManifestDigest: `sha256:${"a".repeat(64)}`,
      randomBytes: () => secrets.shift() ?? Buffer.alloc(0),
      platformAdapter: profileAdapter
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
      launch_nonce_digest: launch.launchNonceDigest
    };
    await writeFile(profile.readinessPath, JSON.stringify(readiness), {
      mode: 0o600
    });
    await chmod(profile.readinessPath, 0o600);
    const captured = await readSidecarReadiness(profile.readinessPath);
    expect(captured).toHaveProperty("identity");
    expect(captured).toHaveProperty("readiness", readiness);

    await rename(
      profile.readinessPath,
      join(profile.runtimeDirectory, "captured-readiness.json")
    );
    const replacement = { ...readiness, port: 43124 };
    await writeFile(profile.readinessPath, JSON.stringify(replacement), {
      mode: 0o600
    });
    await chmod(profile.readinessPath, 0o600);

    await launch.cleanup(captured.identity);
    await expect(
      readFile(profile.readinessPath, "utf8").then(JSON.parse)
    ).resolves.toEqual(replacement);
  });

  it("fails closed without unlinking when Node lacks exact-object deletion", async () => {
    const profile = await resolvePrivateProfile({
      profileId: "default",
      trustedAnchor: testRoot,
      profileRoot,
      statePath: join(profileRoot, "state", "agent.db"),
      memoryDir: join(profileRoot, "memory"),
      runtimeSettingsPath: join(
        profileRoot,
        "config",
        "runtime_settings.json"
      )
    }, profileAdapter);
    const secrets = [Buffer.alloc(32, 0x11), Buffer.alloc(32, 0x22)];
    const launch = await createPrivateLaunchFiles({
      profile,
      parentPid: process.pid,
      parentBirthMarker: "parent-birth-marker",
      resourceManifestDigest: `sha256:${"a".repeat(64)}`,
      randomBytes: () => secrets.shift() ?? Buffer.alloc(0)
    });

    await expect(launch.cleanup()).rejects.toThrow(
      "private_exact_delete_unqualified"
    );
    await expect(readFile(launch.bootstrapPath, "utf8")).resolves.toContain(
      "kestrel.desktop.bootstrap.v1"
    );
  });

  it("never falls back to path unlink after an exact-delete identity swap", async () => {
    const profile = await resolvePrivateProfile({
      profileId: "default",
      trustedAnchor: testRoot,
      profileRoot,
      statePath: join(profileRoot, "state", "agent.db"),
      memoryDir: join(profileRoot, "memory"),
      runtimeSettingsPath: join(
        profileRoot,
        "config",
        "runtime_settings.json"
      )
    }, profileAdapter);
    let deleteCalls = 0;
    let replacementBytes = "";
    const capturedOriginal = join(
      profile.runtimeDirectory,
      "captured-readiness.json"
    );
    const adversarialAdapter = {
      ...profileAdapter,
      async deleteCapturedFile(
        path: string,
        identity: { dev: number; ino: number }
      ): Promise<void> {
        deleteCalls += 1;
        const metadata = await lstat(path);
        if (
          metadata.dev !== identity.dev ||
          metadata.ino !== identity.ino
        ) {
          return;
        }
        if (deleteCalls === 1) {
          await unlink(path);
          return;
        }
        await rename(path, capturedOriginal);
        await writeFile(path, replacementBytes, { mode: 0o600 });
        await chmod(path, 0o600);
        throw new Error("private_exact_delete_identity_changed");
      }
    };
    const secrets = [Buffer.alloc(32, 0x11), Buffer.alloc(32, 0x22)];
    const launch = await createPrivateLaunchFiles({
      profile,
      parentPid: process.pid,
      parentBirthMarker: "parent-birth-marker",
      resourceManifestDigest: `sha256:${"a".repeat(64)}`,
      randomBytes: () => secrets.shift() ?? Buffer.alloc(0),
      platformAdapter: adversarialAdapter
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
      launch_nonce_digest: launch.launchNonceDigest
    };
    await writeFile(profile.readinessPath, JSON.stringify(readiness), {
      mode: 0o600
    });
    await chmod(profile.readinessPath, 0o600);
    const captured = await readSidecarReadiness(
      profile.readinessPath,
      adversarialAdapter
    );
    replacementBytes = JSON.stringify({ ...readiness, port: 43124 });

    await expect(launch.cleanup(captured.identity)).rejects.toThrow(
      "private_exact_delete_identity_changed"
    );
    await expect(readFile(profile.readinessPath, "utf8")).resolves.toBe(
      replacementBytes
    );
    expect(deleteCalls).toBe(2);
  });

  it("fails closed instead of claiming chmod qualifies a Windows owner ACL", async () => {
    await mkdir(profileRoot);
    await expect(
      resolvePrivateProfile(
        {
          profileId: "default",
          trustedAnchor: testRoot,
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
          },
          preparePrivateDirectory: async () => {
            throw new Error("windows_owner_acl_unqualified");
          },
          deleteCapturedFile: async () => {
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
        trustedAnchor: testRoot,
        profileRoot,
        statePath: join(profileRoot, "state", "agent.db"),
        memoryDir: join(profileRoot, "memory"),
        runtimeSettingsPath: join(
          profileRoot,
          "config",
          "runtime_settings.json"
        )
      }, profileAdapter)
    ).rejects.toThrow("symlink");
    if (process.platform !== "win32") {
      expect((await lstat(outside)).mode & 0o777).toBe(0o755);
    }
  });

  it("rejects an intermediate symlink below the trusted anchor before any chmod or mkdir", async () => {
    const outside = join(testRoot, "outside");
    const outsideProfile = join(outside, "default");
    const profiles = join(testRoot, "profiles");
    await mkdir(outsideProfile, { recursive: true, mode: 0o755 });
    await chmod(outsideProfile, 0o755);
    await symlink(outside, profiles);
    const requestedProfile = join(profiles, "default");

    await expect(
      resolvePrivateProfile({
        profileId: "default",
        trustedAnchor: testRoot,
        profileRoot: requestedProfile,
        statePath: join(requestedProfile, "state", "agent.db"),
        memoryDir: join(requestedProfile, "memory"),
        runtimeSettingsPath: join(
          requestedProfile,
          "config",
          "runtime_settings.json"
        )
      } as Parameters<typeof resolvePrivateProfile>[0], profileAdapter)
    ).rejects.toThrow("symlink");
    if (process.platform !== "win32") {
      expect((await lstat(outsideProfile)).mode & 0o777).toBe(0o755);
    }
  });

  it("rejects a profile outside the trusted anchor without creating it", async () => {
    const trustedAnchor = join(testRoot, "anchor");
    await mkdir(trustedAnchor, { mode: 0o700 });
    const outsideProfile = join(testRoot, "outside");
    await expect(
      resolvePrivateProfile({
        profileId: "default",
        trustedAnchor,
        profileRoot: outsideProfile,
        statePath: join(outsideProfile, "state", "agent.db"),
        memoryDir: join(outsideProfile, "memory"),
        runtimeSettingsPath: join(
          outsideProfile,
          "config",
          "runtime_settings.json"
        )
      } as Parameters<typeof resolvePrivateProfile>[0], profileAdapter)
    ).rejects.toThrow("trusted_anchor");
    await expect(lstat(outsideProfile)).rejects.toMatchObject({
      code: "ENOENT"
    });
  });

  it("fails closed before mutation when Node lacks a directory-relative native seam", async () => {
    await expect(
      resolvePrivateProfile({
        profileId: "default",
        trustedAnchor: testRoot,
        profileRoot,
        statePath: join(profileRoot, "state", "agent.db"),
        memoryDir: join(profileRoot, "memory"),
        runtimeSettingsPath: join(
          profileRoot,
          "config",
          "runtime_settings.json"
        )
      })
    ).rejects.toThrow("private_directory_mutation_unqualified");
    await expect(lstat(profileRoot)).rejects.toMatchObject({
      code: "ENOENT"
    });
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
        trustedAnchor: testRoot,
        profileRoot,
        statePath: join(stateDirectory, "agent.db"),
        memoryDir: join(profileRoot, "memory"),
        runtimeSettingsPath: join(
          profileRoot,
          "config",
          "runtime_settings.json"
        )
      }, profileAdapter)
    ).rejects.toThrow("state_path");
  });

  it("matches shared UTF-8 lease identity bytes without normalizing Unicode or separators", async () => {
    const fixture = JSON.parse(
      await readFile(
        join(
          import.meta.dirname,
          "../../../tests/fixtures/desktop-canonical-vectors.json"
        ),
        "utf8"
      )
    ) as {
      runtime_profile_identities: Array<{
        profile_id: string;
        state_path: string;
        memory_dir: string;
        canonical_utf8_hex: string;
        sha256: string;
      }>;
    };
    for (const vector of fixture.runtime_profile_identities) {
      const bytes = runtimeProfileControlIdentityBytes(
        vector.state_path,
        vector.memory_dir,
        vector.profile_id
      );
      expect(bytes.toString("hex")).toBe(vector.canonical_utf8_hex);
      expect(createHash("sha256").update(bytes).digest("hex")).toBe(
        vector.sha256
      );
    }
    expect(fixture.runtime_profile_identities[0]?.sha256).not.toBe(
      fixture.runtime_profile_identities[1]?.sha256
    );
  });
});
