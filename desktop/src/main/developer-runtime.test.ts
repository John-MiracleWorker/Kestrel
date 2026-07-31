import { createHash } from "node:crypto";
import { spawn } from "node:child_process";
import {
  chmod,
  copyFile,
  lstat,
  mkdtemp,
  mkdir,
  readFile,
  realpath,
  rename,
  rm,
  symlink,
  writeFile
} from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { FakeSidecarChild } from "../testing/fake-sidecar";
import {
  acquireDeveloperRuntimeExecutable,
  createDeveloperRuntimeSupervisorDependencies,
  createDeveloperRuntimePrivateFileAdapter,
  createMacOSDeveloperRetainedChildQualifier,
  readMacOSDeveloperProcess,
  selectPackagedSupervisorRuntime
} from "./developer-runtime";
import { resolvePrivateProfile } from "./private-files";
import type { VerifiedResourceSet } from "./resource-manifest";

async function sha256File(path: string): Promise<string> {
  return createHash("sha256")
    .update(await readFile(path))
    .digest("hex");
}

describe("developer-runtime executable launch", () => {
  let root: string;

  beforeEach(async () => {
    root = await mkdtemp(join(tmpdir(), "kestrel-developer-executable-"));
  });

  afterEach(async () => {
    await rm(root, { force: true, recursive: true });
  });

  it.skipIf(process.platform === "win32")(
    "opens no-follow, reports residual path race, and retains the handle until confirmed exit",
    async () => {
      const executable = join(root, "retained-sidecar");
      await writeFile(
        executable,
        "#!/bin/sh\ntrap 'exit 0' TERM\nwhile :; do sleep 1; done\n",
        { mode: 0o700 }
      );
      await chmod(executable, 0o700);
      const metadata = await lstat(executable);
      const capability = await acquireDeveloperRuntimeExecutable({
        path: await realpath(executable),
        size: metadata.size,
        sha256: await sha256File(executable)
      });

      expect(capability.mechanism).toBe("developer_reverified_path");
      expect(capability.residualRisk).toBe(
        "path_to_exec_race_not_native_sealed"
      );
      const child = capability.spawn({
        args: ["ignored-bootstrap"],
        options: {
          shell: false,
          detached: false,
          stdio: ["ignore", "pipe", "pipe"],
          env: { PATH: process.env.PATH ?? "/usr/bin:/bin" }
        }
      });
      expect(child.pid).toEqual(expect.any(Number));
      await expect(capability.close()).rejects.toThrow(
        "developer_executable_child_exit_unconfirmed"
      );

      expect(child.kill("SIGTERM")).toBe(true);
      await new Promise<void>((resolvePromise) => {
        child.once("exit", () => resolvePromise());
      });
      await expect(capability.close()).resolves.toBeUndefined();
    }
  );

  it.skipIf(process.platform === "win32")(
    "detects a pathname replacement immediately before spawn without launching it",
    async () => {
      const executable = join(root, "sidecar");
      await copyFile(process.execPath, executable);
      await chmod(executable, 0o700);
      const metadata = await lstat(executable);
      let spawnCalls = 0;
      const capability = await acquireDeveloperRuntimeExecutable(
        {
          path: await realpath(executable),
          size: metadata.size,
          sha256: await sha256File(executable)
        },
        {
          spawnChild: () => {
            spawnCalls += 1;
            throw new Error("spawn must not be reached");
          }
        }
      );
      await rename(executable, `${executable}.captured`);
      await writeFile(executable, "#!/bin/sh\nexit 0\n", { mode: 0o700 });
      await chmod(executable, 0o700);

      expect(() =>
        capability.spawn({
          args: ["bootstrap"],
          options: {
            shell: false,
            detached: false,
            stdio: ["ignore", "pipe", "pipe"],
            env: {}
          }
        })
      ).toThrow("developer_executable_changed_before_spawn");
      expect(spawnCalls).toBe(0);
      await expect(capability.close()).resolves.toBeUndefined();
    }
  );

  it.skipIf(process.platform === "win32")(
    "detects a same-inode same-size rewrite through the retained opened handle",
    async () => {
      const executable = join(root, "sidecar");
      await writeFile(executable, "#!/bin/sh\nexit 0\n", { mode: 0o700 });
      await chmod(executable, 0o700);
      const metadata = await lstat(executable);
      let spawnCalls = 0;
      const capability = await acquireDeveloperRuntimeExecutable(
        {
          path: await realpath(executable),
          size: metadata.size,
          sha256: await sha256File(executable)
        },
        {
          spawnChild: () => {
            spawnCalls += 1;
            throw new Error("spawn must not be reached");
          }
        }
      );
      await writeFile(executable, "#!/bin/sh\nexit 9\n", { mode: 0o700 });
      await chmod(executable, 0o700);

      expect(() =>
        capability.spawn({
          args: ["bootstrap"],
          options: {
            shell: false,
            detached: false,
            stdio: ["ignore", "pipe", "pipe"],
            env: {}
          }
        })
      ).toThrow("developer_executable_changed_before_spawn");
      expect(spawnCalls).toBe(0);
      await expect(capability.close()).resolves.toBeUndefined();
    }
  );

  it.skipIf(process.platform === "win32")(
    "rejects symlinks and digest mismatches during acquisition",
    async () => {
      const executable = join(root, "sidecar");
      const link = join(root, "sidecar-link");
      await copyFile(process.execPath, executable);
      await chmod(executable, 0o700);
      await symlink(executable, link);
      const metadata = await lstat(executable);

      await expect(
        acquireDeveloperRuntimeExecutable({
          path: link,
          size: metadata.size,
          sha256: await sha256File(executable)
        })
      ).rejects.toThrow("developer_executable_path_untrusted");
      await expect(
        acquireDeveloperRuntimeExecutable({
          path: await realpath(executable),
          size: metadata.size,
          sha256: "0".repeat(64)
        })
      ).rejects.toThrow("developer_executable_digest_mismatch");
    }
  );
});

describe("developer-runtime private profile mutation", () => {
  let userData: string;

  beforeEach(async () => {
    userData = await mkdtemp(join(tmpdir(), "kestrel-developer-user-data-"));
    if (process.platform !== "win32") {
      await chmod(userData, 0o700);
    }
  });

  afterEach(async () => {
    await rm(userData, { force: true, recursive: true });
  });

  it.skipIf(process.platform === "win32")(
    "creates and revalidates owner-only profile segments beneath canonical userData",
    async () => {
      const adapter = await createDeveloperRuntimePrivateFileAdapter(userData);
      const profileRoot = join(userData, "profiles", "default");
      const profile = await resolvePrivateProfile(
        {
          profileId: "default",
          trustedAnchor: userData,
          profileRoot,
          statePath: join(profileRoot, "state", "agent.db"),
          memoryDir: join(profileRoot, "memory"),
          runtimeSettingsPath: join(
            profileRoot,
            "config",
            "runtime_settings.json"
          )
        },
        adapter
      );

      expect(adapter.mutationMechanism).toBe(
        "developer_identity_revalidation"
      );
      expect(adapter.deleteMechanism).toBe(
        "developer_identity_revalidation"
      );
      expect(profile.profileRoot).toBe(await realpath(profileRoot));
      for (const path of [
        join(userData, "profiles"),
        profile.profileRoot,
        profile.memoryDir,
        profile.runtimeDirectory
      ]) {
        expect((await lstat(path)).mode & 0o777).toBe(0o700);
      }
    }
  );

  it.skipIf(process.platform === "win32")(
    "rejects wrong-mode and linked ancestors without mutating outside userData",
    async () => {
      const adapter = await createDeveloperRuntimePrivateFileAdapter(userData);
      const wrongMode = join(userData, "wrong-mode");
      await mkdir(wrongMode, { mode: 0o755 });
      await chmod(wrongMode, 0o755);
      await expect(
        adapter.preparePrivateDirectory(
          userData,
          join(wrongMode, "must-not-exist")
        )
      ).rejects.toThrow("private_artifact_permissions_untrusted");
      await expect(lstat(join(wrongMode, "must-not-exist"))).rejects.toMatchObject({
        code: "ENOENT"
      });

      const outside = await mkdtemp(join(tmpdir(), "kestrel-outside-"));
      try {
        await chmod(outside, 0o755);
        const linked = join(userData, "linked");
        await symlink(outside, linked);
        await expect(
          adapter.preparePrivateDirectory(userData, join(linked, "escaped"))
        ).rejects.toThrow("private_profile_symlink_untrusted");
        expect((await lstat(outside)).mode & 0o777).toBe(0o755);
        await expect(lstat(join(outside, "escaped"))).rejects.toMatchObject({
          code: "ENOENT"
        });
      } finally {
        await rm(outside, { force: true, recursive: true });
      }
    }
  );

  it.skipIf(process.platform === "win32")(
    "rejects replacement of the captured canonical userData identity",
    async () => {
      const adapter = await createDeveloperRuntimePrivateFileAdapter(userData);
      const captured = `${userData}-captured`;
      await rename(userData, captured);
      await mkdir(userData, { mode: 0o700 });
      await chmod(userData, 0o700);

      await expect(
        adapter.preparePrivateDirectory(
          userData,
          join(userData, "profiles", "default")
        )
      ).rejects.toThrow("developer_user_data_identity_changed");
      await expect(lstat(join(userData, "profiles"))).rejects.toMatchObject({
        code: "ENOENT"
      });
      await expect(lstat(join(captured, "profiles"))).rejects.toMatchObject({
        code: "ENOENT"
      });
      await rm(captured, { force: true, recursive: true });
    }
  );

  it.skipIf(process.platform === "win32")(
    "deletes only the matching captured identity and preserves replacements",
    async () => {
      const adapter = await createDeveloperRuntimePrivateFileAdapter(userData);
      const runtime = await adapter.preparePrivateDirectory(
        userData,
        join(userData, "profiles", "default", "runtime")
      );
      const artifact = join(runtime, "bootstrap.json");
      await writeFile(artifact, "captured", { mode: 0o600 });
      await chmod(artifact, 0o600);
      const captured = await lstat(artifact);
      await rename(artifact, join(runtime, "captured.json"));
      await writeFile(artifact, "replacement", { mode: 0o600 });
      await chmod(artifact, 0o600);

      await adapter.deleteCapturedFile(artifact, {
        dev: captured.dev,
        ino: captured.ino
      });
      await expect(readFile(artifact, "utf8")).resolves.toBe("replacement");

      const replacement = await lstat(artifact);
      await adapter.deleteCapturedFile(artifact, {
        dev: replacement.dev,
        ino: replacement.ino
      });
      await expect(lstat(artifact)).rejects.toMatchObject({ code: "ENOENT" });
    }
  );
});

describe("macOS developer retained-child qualification", () => {
  it("filters lsof to the exact ps command path before parsing mappings", async () => {
    const root = await mkdtemp(
      join(tmpdir(), "kestrel-developer-process-reader-")
    );
    try {
      const executable = join(root, "Kestrel Developer");
      await copyFile("/bin/sleep", executable);
      await chmod(executable, 0o700);
      const metadata = await lstat(executable);
      const pid = 4242;
      const calls: Array<{
        executable: string;
        argumentsValue: readonly string[];
      }> = [];
      const oversizedMappings = [
        `p${pid}`,
        ...Array.from({ length: 33 }, (_value, index) => [
          "ftxt",
          "tREG",
          `D0x${metadata.dev.toString(16)}`,
          `s${metadata.size}`,
          `i${metadata.ino + index}`,
          `n${executable}.${index}`
        ]).flat()
      ].join("\n");
      const exactMapping = [
        `p${pid}`,
        "ftxt",
        "tREG",
        `D0x${metadata.dev.toString(16)}`,
        `s${metadata.size}`,
        `i${metadata.ino}`,
        `n${executable}`
      ].join("\n");

      const evidence = await readMacOSDeveloperProcess(pid, {
        runCommand: async (
          command,
          argumentsValue
        ): Promise<{ stdout: string }> => {
          calls.push({
            executable: command,
            argumentsValue: [...argumentsValue]
          });
          if (command === "/bin/ps") {
            return {
              stdout:
                `501 ${process.pid} Thu Jul 30 20:35:37 2026     ` +
                `${executable}\n`
            };
          }
          if (command !== "/usr/sbin/lsof") {
            throw new Error("unexpected process evidence command");
          }
          return {
            stdout:
              argumentsValue.at(-2) === "--" &&
              argumentsValue.at(-1) === executable
                ? exactMapping
                : oversizedMappings
          };
        }
      });

      expect(evidence).toMatchObject({
        pid,
        parentPid: process.pid,
        uid: 501,
        executablePath: await realpath(executable),
        executableDigest: await sha256File(executable)
      });
      expect(calls).toHaveLength(2);
      expect(calls[0]?.executable).toBe("/bin/ps");
      expect(calls[1]).toMatchObject({
        executable: "/usr/sbin/lsof",
        argumentsValue: expect.arrayContaining(["--", executable])
      });
    } finally {
      await rm(root, { recursive: true, force: true });
    }
  });

  it.skipIf(process.platform !== "darwin")(
    "observes the canonical mapped executable and hashes its exact backing file",
    async () => {
      const child = spawn("/bin/sleep", ["5"], {
        stdio: "ignore"
      });
      try {
        const pid = child.pid;
        if (pid === undefined) {
          throw new Error("test_child_pid_missing");
        }
        const evidence = await readMacOSDeveloperProcess(pid);
        const executable = await realpath("/bin/sleep");
        expect(evidence).toMatchObject({
          pid,
          parentPid: process.pid,
          executablePath: executable,
          executableDigest: await sha256File(executable)
        });
      } finally {
        child.kill("SIGKILL");
      }
    }
  );

  it("binds tagged ps birth milliseconds only to the retained child object and PID", async () => {
    const child = new FakeSidecarChild(41001);
    const qualifier = createMacOSDeveloperRetainedChildQualifier({
      platform: "darwin",
      readProcess: async (pid) => ({
        pid,
        uid: 501,
        birthMilliseconds: 1_753_886_400_000,
        executablePath: "/bundle/sidecar",
        executableDigest: "b".repeat(64)
      })
    });

    expect(await qualifier.inspectProcess(child.pid!)).toBeNull();
    const identity = await qualifier.inspectRetainedChild(
      child,
      "b".repeat(64)
    );
    expect(identity).toEqual({
      pid: 41001,
      ownerDigest: createHash("sha256").update("uid:501").digest("hex"),
      processBirthMarker: "developer-ps-lstart-ms:1753886400000",
      executablePath: "/bundle/sidecar",
      executableDigest: "b".repeat(64)
    });
    expect(await qualifier.inspectProcess(41002)).toBeNull();
    expect(await qualifier.inspectProcess(41001)).toEqual(identity);

    const unrelated = new FakeSidecarChild(41001);
    await expect(
      qualifier.inspectRetainedChild(unrelated, "b".repeat(64))
    ).rejects.toThrow("developer_existing_process_attach_forbidden");
    child.exit(0);
    expect(await qualifier.inspectProcess(41001)).toBeNull();
  });

  it("qualifies exactly one same-owner onefile payload directly below the retained bootloader", async () => {
    const child = new FakeSidecarChild(5151);
    const digest = "b".repeat(64);
    const evidence = new Map([
      [
        5151,
        {
          pid: 5151,
          parentPid: 4242,
          uid: 501,
          birthMilliseconds: 1_753_886_400_000,
          executablePath: "/bundle/sidecar",
          executableDigest: digest
        }
      ],
      [
        9001,
        {
          pid: 9001,
          parentPid: 5151,
          uid: 501,
          birthMilliseconds: 1_753_886_401_000,
          executablePath: "/bundle/sidecar",
          executableDigest: digest
        }
      ]
    ]);
    const qualifier = createMacOSDeveloperRetainedChildQualifier({
      platform: "darwin",
      readProcess: async (pid) => evidence.get(pid) ?? null
    });
    await qualifier.inspectRetainedChild(child, digest);
    await expect(
      qualifier.qualifyDeveloperOneFilePayload(
        child,
        9001,
        digest
      )
    ).resolves.toMatchObject({
      pid: 9001,
      parentPid: 5151,
      processBirthMarker: "developer-ps-lstart-ms:1753886401000",
      executableDigest: digest
    });
    expect(await qualifier.inspectProcess(9001)).toMatchObject({
      pid: 9001,
      parentPid: 5151
    });
    expect(await qualifier.inspectProcess(31337)).toBeNull();

    child.exit(0);
    evidence.set(9001, {
      pid: 9001,
      parentPid: 1,
      uid: 501,
      birthMilliseconds: 1_753_886_401_000,
      executablePath: "/bundle/sidecar",
      executableDigest: digest
    });
    expect(await qualifier.inspectProcess(9001)).toMatchObject({
      pid: 9001,
      parentPid: 1
    });
    evidence.delete(9001);
    expect(await qualifier.inspectProcess(9001)).toBeNull();
  });

  it.each([
    {
      name: "an unrelated parent",
      parentPid: 31337,
      uid: 501
    },
    {
      name: "a different owner",
      parentPid: 5151,
      uid: 502
    }
  ])("rejects $name for a onefile payload", async ({ parentPid, uid }) => {
    const child = new FakeSidecarChild(5151);
    const qualifier = createMacOSDeveloperRetainedChildQualifier({
      platform: "darwin",
      readProcess: async (pid) =>
        pid === 5151
          ? {
              pid,
              parentPid: 4242,
              uid: 501,
              birthMilliseconds: 1_753_886_400_000,
              executablePath: "/bundle/sidecar",
              executableDigest: "b".repeat(64)
            }
          : {
              pid,
              parentPid,
              uid,
              birthMilliseconds: 1_753_886_401_000,
              executablePath: "/bundle/sidecar",
              executableDigest: "b".repeat(64)
            }
    });
    const digest = "b".repeat(64);

    await qualifier.inspectRetainedChild(child, digest);
    await expect(
      qualifier.qualifyDeveloperOneFilePayload(
        child,
        9001,
        digest
      )
    ).rejects.toThrow(
      "developer_onefile_payload_identity_unavailable"
    );
  });

  it("rejects a payload whose independently observed executable digest differs from the retained bootloader", async () => {
    const child = new FakeSidecarChild(5151);
    const expectedDigest = "b".repeat(64);
    const qualifier = createMacOSDeveloperRetainedChildQualifier({
      platform: "darwin",
      readProcess: async (pid) => ({
        pid,
        parentPid: pid === 5151 ? 4242 : 5151,
        uid: 501,
        birthMilliseconds:
          pid === 5151
            ? 1_753_886_400_000
            : 1_753_886_401_000,
        executablePath: "/bundle/sidecar",
        executableDigest:
          pid === 5151 ? expectedDigest : "c".repeat(64)
      })
    });

    await qualifier.inspectRetainedChild(child, expectedDigest);
    await expect(
      qualifier.qualifyDeveloperOneFilePayload(
        child,
        9001,
        expectedDigest
      )
    ).rejects.toThrow(
      "developer_onefile_payload_identity_unavailable"
    );
  });
});

describe("immutable packaged runtime selection", () => {
  it("ignores environment and argv hints and selects only signed build identity", () => {
    const previousEnvironment = process.env.KESTREL_DEVELOPER_RUNTIME;
    const originalArgv = process.argv;
    process.env.KESTREL_DEVELOPER_RUNTIME = "1";
    process.argv = [...originalArgv, "--developer-runtime"];
    try {
      expect(
        selectPackagedSupervisorRuntime({
          buildMode: "release",
          keyId: "release"
        })
      ).toBe("production-runtime");
      expect(
        selectPackagedSupervisorRuntime({
          buildMode: "developer",
          keyId: "developer"
        })
      ).toBe("developer-runtime");
      expect(() =>
        selectPackagedSupervisorRuntime({
          buildMode: "release",
          keyId: "developer"
        })
      ).toThrow("desktop_build_mode_key_mismatch");
    } finally {
      process.argv = originalArgv;
      if (previousEnvironment === undefined) {
        delete process.env.KESTREL_DEVELOPER_RUNTIME;
      } else {
        process.env.KESTREL_DEVELOPER_RUNTIME = previousEnvironment;
      }
    }
  });

  it.skipIf(process.platform === "win32")(
    "composes the named developer adapters without changing production defaults",
    async () => {
      const userData = await mkdtemp(
        join(tmpdir(), "kestrel-developer-dependencies-")
      );
      try {
        await chmod(userData, 0o700);
        const profileRoot = join(userData, "profiles", "default");
        const executable = join(userData, "sidecar");
        await copyFile(process.execPath, executable);
        await chmod(executable, 0o700);
        const metadata = await lstat(executable);
        const resource = {
          path: await realpath(executable),
          size: metadata.size,
          sha256: await sha256File(executable)
        };
        const dependencies =
          await createDeveloperRuntimeSupervisorDependencies({
            apiSession: {
              activate: () => undefined,
              deactivate: () => undefined
            },
            userDataPath: userData,
            platform: "darwin",
            readProcess: async (pid) => ({
              pid,
              uid: 501,
              birthMilliseconds: 1_753_886_400_000,
              executablePath: "/bundle/electron",
              executableDigest: "a".repeat(64)
            }),
            resourceVerification: {
              resourceRoot: userData,
              manifestPath: join(userData, "manifest.json"),
              signaturePath: join(userData, "manifest.sig"),
              trustedKeys: new Map(),
              requiredFiles: ["sidecar"],
              expectedIdentity: {
                buildMode: "developer",
                keyId: "developer",
                sourceCommit: "a".repeat(40),
                appVersion: "0.5.0",
                platform: "darwin",
                architecture: "arm64",
                pythonLockSha256: "1".repeat(64),
                desktopNpmLockSha256: "2".repeat(64),
                webNpmLockSha256: "3".repeat(64),
                sbomSha256: "4".repeat(64)
              }
            },
            resourceVerifier: async () => {
              throw new Error("not reached");
            },
            profile: {
              profileId: "default",
              trustedAnchor: userData,
              profileRoot,
              statePath: join(profileRoot, "state", "agent.db"),
              memoryDir: join(profileRoot, "memory"),
              runtimeSettingsPath: join(
                profileRoot,
                "config",
                "runtime_settings.json"
              )
            },
            sidecarVersion: "0.5.0"
          });

        await expect(dependencies.parentIdentity()).resolves.toEqual({
          pid: process.pid,
          processBirthMarker: "developer-ps-lstart-ms:1753886400000"
        });
        const capability =
          await dependencies.acquireVerifiedExecutable(
            {
              files: new Map([["sidecar", resource]])
            } as unknown as VerifiedResourceSet,
            "sidecar"
          );
        expect(capability.mechanism).toBe("developer_reverified_path");
        expect(dependencies.inspectRetainedChild).toEqual(
          expect.any(Function)
        );
        await capability.close();
      } finally {
        await rm(userData, { recursive: true, force: true });
      }
    }
  );
});
