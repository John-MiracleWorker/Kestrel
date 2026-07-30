import {
  chmod,
  mkdir,
  mkdtemp,
  readFile,
  realpath,
  rename,
  rm,
  symlink,
  writeFile,
} from "node:fs/promises";
import { createHash } from "node:crypto";
import { tmpdir } from "node:os";
import { join, sep } from "node:path";
import { PassThrough } from "node:stream";
import { describe, expect, it } from "vitest";
import {
  assertExecutableEvidence,
  assertListenerOwnerBinding,
  assertReadinessProcessBinding,
  assertSafeCycleCompletion,
  canonicalSmokeJson,
  captureBoundedDiagnosticStream,
  inspectCanonicalOwnerControl,
  parseListenerOwnerPids,
  parseDirectoryBuildReceipt,
  removeCapturedControl,
  safeSmokeEnvironment,
  validateDirectorySmokeReceipt,
  validateMemorySnapshotEntries,
  validateSmokePlatform,
  verifyExecutableForLaunch,
  verifyManifestInventory,
  verifyPackagedApplicationDist,
} from "./smoke-dir.mjs";

const sha256 = (character) => character.repeat(64);
const sourceCommit = "a".repeat(40);

function buildReceipt(overrides = {}) {
  const privateRoot =
    process.platform === "win32" ? "C:\\private" : "/private";
  const applicationRoot =
    process.platform === "darwin"
      ? join(
          privateRoot,
          "output",
          "Kestrel Developer.app",
        )
      : join(privateRoot, "output", "kestrel-desktop-dir");
  const applicationResourcesRoot =
    process.platform === "darwin"
      ? join(applicationRoot, "Contents", "Resources")
      : join(applicationRoot, "resources");
  const resourceRoot = join(
    applicationResourcesRoot,
    "kestrel",
  );
  const packagedApplicationRoot = join(
    applicationResourcesRoot,
    "app",
  );
  return {
    schema: "kestrel.desktop.directory-build.v1",
    build_mode: "developer",
    key_id: "developer",
    signed: false,
    publishable: false,
    directory_only: true,
    source_commit: sourceCommit,
    app_name: "Kestrel Developer",
    app_version: "0.5.0",
    platform: process.platform,
    architecture: process.arch,
    electron_version: "43.2.0",
    electron_builder_version: "26.15.3",
    production_dependency_count: 2,
    builder_config_sha256: sha256("1"),
    effective_builder_config_sha256: sha256("2"),
    stage_receipt_path: join(
      privateRoot,
      "evidence",
      "stage-receipt.json",
    ),
    stage_receipt_sha256: sha256("3"),
    application_root: applicationRoot,
    resource_root: resourceRoot,
    executable_path:
      process.platform === "darwin"
        ? join(
            applicationRoot,
            "Contents",
            "MacOS",
            "Kestrel Developer",
          )
        : process.platform === "win32"
          ? join(applicationRoot, "Kestrel Developer.exe")
          : join(applicationRoot, "kestrel-desktop"),
    executable_sha256: sha256("4"),
    executable_size: 1024,
    packaged_package_json_path: join(
      packagedApplicationRoot,
      "package.json",
    ),
    packaged_package_json_sha256: sha256("5"),
    packaged_dist_path: join(
      packagedApplicationRoot,
      "dist",
    ),
    packaged_dist_inventory_sha256: sha256("b"),
    packaged_dist_file_count: 2,
    packaged_dist_total_bytes: 2048,
    packaged_public_key_path: join(
      packagedApplicationRoot,
      "config",
      "desktop-developer-public-key.pem",
    ),
    packaged_public_key_sha256: sha256("6"),
    manifest_path: join(
      resourceRoot,
      "kestrel-resource-manifest.json",
    ),
    manifest_sha256: sha256("7"),
    signature_path: join(
      resourceRoot,
      "kestrel-resource-manifest.sig",
    ),
    signature_sha256: sha256("8"),
    ...overrides,
  };
}

function smokeReceipt(overrides = {}) {
  return {
    schema: "kestrel.desktop.directory-smoke.v1",
    build_mode: "developer",
    source_commit: sourceCommit,
    platform: process.platform,
    architecture: process.arch,
    qualified: true,
    cycle_count: 2,
    memory_layer_count: 6,
    captured_process_count: 8,
    authenticated_readiness: true,
    authenticated_recovery: true,
    authenticated_shutdown: true,
    mission_command_loaded: true,
    memory_identity_reused: true,
    processes_exited: true,
    listeners_closed: true,
    owner_data_removed: true,
    signed: false,
    publishable: false,
    native_keyring: false,
    build_receipt_sha256: sha256("9"),
    executable_sha256: sha256("4"),
    manifest_sha256: sha256("7"),
    packaged_dist_inventory_sha256: sha256("b"),
    memory_identity_sha256: sha256("a"),
    ...overrides,
  };
}

describe("external developer directory smoke contracts", () => {
  it("accepts only the exact canonical developer directory receipt", () => {
    const receipt = buildReceipt();
    expect(
      parseDirectoryBuildReceipt(canonicalSmokeJson(receipt), {
        platform: process.platform,
        architecture: process.arch,
      }),
    ).toEqual(receipt);

    expect(() =>
      parseDirectoryBuildReceipt(
        Buffer.from(`${JSON.stringify(receipt, null, 2)}\n`),
        {
          platform: process.platform,
          architecture: process.arch,
        },
      ),
    ).toThrow("directory_smoke_build_receipt_noncanonical");
    expect(() =>
      parseDirectoryBuildReceipt(
        canonicalSmokeJson({
          ...receipt,
          api_token: "must-never-cross",
        }),
        {
          platform: process.platform,
          architecture: process.arch,
        },
      ),
    ).toThrow("directory_smoke_build_receipt_invalid");
  });

  it("rejects noncanonical roots and release or host identity mismatches", () => {
    const baseline = buildReceipt();
    const applicationRoot = baseline.application_root;
    const resourceRoot = baseline.resource_root;
    for (const mutation of [
      {
        application_root:
          `${applicationRoot}${sep}..${sep}escape`,
      },
      {
        resource_root: join(
          process.platform === "win32"
            ? "C:\\private"
            : "/private",
          "unrelated",
          "resources",
        ),
      },
      {
        resource_root: join(
          resourceRoot,
          "..",
          "other",
        ),
      },
      {
        executable_path: join(
          process.platform === "win32"
            ? "C:\\private"
            : "/private",
          "unrelated",
          "Kestrel",
        ),
      },
      {
        executable_path: join(
          applicationRoot,
          process.platform === "darwin"
            ? "Contents/MacOS/Electron"
            : "OtherExecutable",
        ),
      },
      {
        packaged_package_json_path: join(
          applicationRoot,
          "other",
          "package.json",
        ),
      },
      {
        packaged_dist_path: join(
          applicationRoot,
          "other",
          "dist",
        ),
      },
      {
        manifest_path: join(
          resourceRoot,
          "other-manifest.json",
        ),
      },
      { build_mode: "release", key_id: "release" },
      { platform: process.platform === "darwin" ? "linux" : "darwin" },
      { architecture: process.arch === "arm64" ? "x64" : "arm64" },
    ]) {
      expect(() =>
        parseDirectoryBuildReceipt(
          canonicalSmokeJson(buildReceipt(mutation)),
          {
            platform: process.platform,
            architecture: process.arch,
          },
        ),
      ).toThrow();
    }
  });

  it("strips ambient provider and API authority while retaining bounded OS state", () => {
    expect(
      safeSmokeEnvironment({
        LANG: "en_US.UTF-8",
        PATH: "/usr/bin:/bin",
        TMPDIR: "/private/tmp",
        OPENAI_API_KEY: "secret",
        ANTHROPIC_API_KEY: "secret",
        KESTREL_API_TOKEN: "secret",
        AWS_SECRET_ACCESS_KEY: "secret",
        SOME_PROVIDER_TOKEN: "secret",
      }),
    ).toEqual({
      LANG: "en_US.UTF-8",
      PATH: "/usr/bin:/bin",
      TMPDIR: "/private/tmp",
    });
  });

  it("makes current macOS qualification and unsupported hosts explicit", () => {
    expect(validateSmokePlatform("darwin")).toBe("darwin");
    for (const unsupported of ["linux", "win32", "freebsd"]) {
      expect(() => validateSmokePlatform(unsupported)).toThrow(
        "directory_smoke_platform_unsupported",
      );
    }
  });

  it("accepts only a fixed secret-free, non-publishable result receipt", () => {
    const receipt = smokeReceipt();
    expect(
      validateDirectorySmokeReceipt(canonicalSmokeJson(receipt)),
    ).toEqual(receipt);
    expect(() =>
      validateDirectorySmokeReceipt(
        canonicalSmokeJson({
          ...receipt,
          api_token: "must-never-cross",
        }),
      ),
    ).toThrow("directory_smoke_receipt_invalid");
    expect(() =>
      validateDirectorySmokeReceipt(
        canonicalSmokeJson({
          ...receipt,
          signed: true,
        }),
      ),
    ).toThrow("directory_smoke_receipt_invalid");
  });

  it("fails closed on orphan processes, listeners, and pre-exit cleanup", () => {
    expect(() =>
      assertSafeCycleCompletion({
        appExited: false,
        cleanupStarted: true,
        identitiesGone: true,
        listenerClosed: true,
        readinessRemoved: true,
      }),
    ).toThrow("directory_smoke_pre_exit_cleanup");
    expect(() =>
      assertSafeCycleCompletion({
        appExited: true,
        cleanupStarted: false,
        identitiesGone: false,
        listenerClosed: true,
        readinessRemoved: true,
      }),
    ).toThrow("directory_smoke_orphan_process");
    expect(() =>
      assertSafeCycleCompletion({
        appExited: true,
        cleanupStarted: false,
        identitiesGone: true,
        listenerClosed: false,
        readinessRemoved: true,
      }),
    ).toThrow("directory_smoke_orphan_listener");
  });

  it("rejects symlinked, oversized, and replaced control files", async () => {
    const root = await mkdtemp(join(tmpdir(), "kestrel-smoke-control-"));
    const controlRoot = join(root, "directory-smoke-v1");
    const readyPath = join(controlRoot, "ready.json");
    const displacedPath = join(controlRoot, "ready-original.json");
    const outsidePath = join(root, "outside.json");
    const ready = {
      schema: "kestrel.desktop.directory-smoke-ready.v1",
      ready: true,
    };
    await chmod(root, 0o700);
    await mkdir(controlRoot, { mode: 0o700 });
    try {
      const bytes = canonicalSmokeJson(ready);
      await writeFile(readyPath, bytes, { mode: 0o600 });
      await chmod(readyPath, 0o600);
      const captured = await inspectCanonicalOwnerControl(
        readyPath,
        ready.schema,
        new Set(["ready", "schema"]),
      );

      await rename(readyPath, displacedPath);
      await writeFile(readyPath, bytes, { mode: 0o600 });
      await chmod(readyPath, 0o600);
      await expect(
        removeCapturedControl(
          readyPath,
          controlRoot,
          captured.identity,
        ),
      ).rejects.toThrow("directory_smoke_control_changed");
      await expect(readFile(readyPath)).resolves.toEqual(bytes);

      await rm(readyPath);
      await writeFile(outsidePath, bytes, { mode: 0o600 });
      await chmod(outsidePath, 0o600);
      await symlink(outsidePath, readyPath);
      await expect(
        inspectCanonicalOwnerControl(
          readyPath,
          ready.schema,
          new Set(["ready", "schema"]),
        ),
      ).rejects.toThrow();

      await rm(readyPath);
      await writeFile(readyPath, "x".repeat(4 * 1024 + 1), {
        mode: 0o600,
      });
      await chmod(readyPath, 0o600);
      await expect(
        inspectCanonicalOwnerControl(
          readyPath,
          ready.schema,
          new Set(["ready", "schema"]),
        ),
      ).rejects.toThrow();
    } finally {
      await rm(root, { recursive: true, force: true });
    }
  });

  it("bounds child diagnostics without retaining their content", async () => {
    const stream = new PassThrough();
    const diagnostics = captureBoundedDiagnosticStream(stream);
    stream.end(Buffer.alloc(64 * 1024, 0x73));
    await new Promise((resolve) => stream.once("end", resolve));
    expect(diagnostics()).toEqual({
      byteLength: 4 * 1024,
      overflow: true,
    });
  });

  it("binds readiness to the exact captured sidecar birth marker", () => {
    const birth = "Thu Jul 30 12:00:00 2026";
    const sidecar = {
      pid: 4242,
      ppid: 4000,
      birth,
      command: "/private/app/kestrel-desktop-sidecar",
    };
    const readiness = {
      pid: sidecar.pid,
      process_birth_marker:
        `developer-ps-lstart-ms:${Date.parse(birth)}`,
    };
    expect(
      assertReadinessProcessBinding(readiness, [
        {
          pid: 4000,
          ppid: 1,
          birth,
          command: "/private/app/Kestrel Developer",
        },
        sidecar,
      ]),
    ).toEqual(sidecar);
    expect(() =>
      assertReadinessProcessBinding(
        {
          ...readiness,
          process_birth_marker:
            `developer-ps-lstart-ms:${Date.parse(birth) + 1}`,
        },
        [sidecar],
      ),
    ).toThrow("directory_smoke_readiness_identity_mismatch");
  });

  it("requires the listening socket to belong to the same sidecar identity", () => {
    const sidecar = {
      pid: 4242,
      ppid: 4000,
      birth: "Thu Jul 30 12:00:00 2026",
      command: "/private/app/kestrel-desktop-sidecar",
    };
    expect(parseListenerOwnerPids("4242\n")).toEqual([4242]);
    expect(
      assertListenerOwnerBinding([4242], sidecar, {
        ...sidecar,
      }),
    ).toBe(true);
    expect(() =>
      assertListenerOwnerBinding([9001], sidecar, {
        ...sidecar,
      }),
    ).toThrow("directory_smoke_listener_identity_mismatch");
    expect(() =>
      assertListenerOwnerBinding([4242], sidecar, {
        ...sidecar,
        birth: "Thu Jul 30 12:00:01 2026",
      }),
    ).toThrow("directory_smoke_listener_identity_mismatch");
    expect(() => parseListenerOwnerPids("4242\nsecret\n")).toThrow(
      "directory_smoke_listener_identity_ambiguous",
    );
  });

  it("refuses executable replacement evidence before launch", () => {
    const receipt = buildReceipt();
    const trustedMetadata = {
      isFile: () => true,
      isSymbolicLink: () => false,
      mode: 0o100755,
      nlink: 1,
    };
    expect(
      assertExecutableEvidence(
        receipt,
        {
          sha256: receipt.executable_sha256,
          size: receipt.executable_size,
        },
        trustedMetadata,
      ),
    ).toBe(true);
    expect(() =>
      assertExecutableEvidence(
        receipt,
        {
          sha256: sha256("f"),
          size: receipt.executable_size,
        },
        trustedMetadata,
      ),
    ).toThrow("directory_smoke_executable_invalid");
  });

  it.runIf(process.platform === "darwin")(
    "rejects executable ancestor-link drift immediately before launch",
    async () => {
      const root = await realpath(
        await mkdtemp(
          join(tmpdir(), "kestrel-smoke-executable-"),
        ),
      );
      const applicationRoot = join(
        root,
        "Kestrel Developer.app",
      );
      const macOsRoot = join(
        applicationRoot,
        "Contents",
        "MacOS",
      );
      const executablePath = join(
        macOsRoot,
        "Kestrel Developer",
      );
      const outsideMacOsRoot = join(root, "outside-macos");
      await mkdir(macOsRoot, { recursive: true });
      await writeFile(executablePath, "trusted executable");
      await chmod(executablePath, 0o755);
      const executableBytes = await readFile(executablePath);
      const receipt = buildReceipt({
        application_root: applicationRoot,
        executable_path: executablePath,
        executable_sha256: createHash("sha256")
          .update(executableBytes)
          .digest("hex"),
        executable_size: executableBytes.byteLength,
      });
      try {
        await expect(
          verifyExecutableForLaunch(receipt),
        ).resolves.toMatchObject({
          path: executablePath,
        });
        await rename(macOsRoot, outsideMacOsRoot);
        await symlink(outsideMacOsRoot, macOsRoot);
        await expect(
          verifyExecutableForLaunch(receipt),
        ).rejects.toThrow(
          "directory_smoke_executable_invalid",
        );
      } finally {
        await rm(root, { recursive: true, force: true });
      }
    },
  );

  it("rejects packaged main-code tampering before either launch", async () => {
    const root = await realpath(
      await mkdtemp(
        join(tmpdir(), "kestrel-smoke-app-dist-"),
      ),
    );
    const applicationRoot =
      process.platform === "darwin"
        ? join(root, "Kestrel Developer.app")
        : join(root, "kestrel-desktop-dir");
    const packagedApplicationRoot =
      process.platform === "darwin"
        ? join(
            applicationRoot,
            "Contents",
            "Resources",
            "app",
          )
        : join(applicationRoot, "resources", "app");
    const distPath = join(packagedApplicationRoot, "dist");
    const mainBytes = Buffer.from("export {};\n");
    const declarationBytes = Buffer.from("export {};\n");
    const files = {
      "main.d.ts": {
        sha256: createHash("sha256")
          .update(declarationBytes)
          .digest("hex"),
        size: declarationBytes.byteLength,
      },
      "main.js": {
        sha256: createHash("sha256")
          .update(mainBytes)
          .digest("hex"),
        size: mainBytes.byteLength,
      },
    };
    const inventoryBytes = canonicalSmokeJson({
      schema: "kestrel.desktop.directory-inventory.v1",
      files,
    });
    await mkdir(distPath, { recursive: true });
    await writeFile(join(distPath, "main.js"), mainBytes);
    await writeFile(
      join(distPath, "main.d.ts"),
      declarationBytes,
    );
    const receipt = buildReceipt({
      application_root: applicationRoot,
      packaged_dist_path: distPath,
      packaged_dist_inventory_sha256:
        createHash("sha256")
          .update(inventoryBytes)
          .digest("hex"),
      packaged_dist_file_count: 2,
      packaged_dist_total_bytes:
        mainBytes.byteLength + declarationBytes.byteLength,
    });
    try {
      await expect(
        verifyPackagedApplicationDist(receipt),
      ).resolves.toMatchObject({
        fileCount: 2,
        totalBytes:
          mainBytes.byteLength + declarationBytes.byteLength,
      });
      await writeFile(
        join(distPath, "main.js"),
        "throw new Error('tampered');\n",
      );
      await expect(
        verifyPackagedApplicationDist(receipt),
      ).rejects.toThrow(
        "directory_smoke_packaged_dist_invalid",
      );
    } finally {
      await rm(root, { recursive: true, force: true });
    }
  });

  it("rejects a second-cycle extra Memvid v2 file anywhere in the profile", () => {
    const snapshot = [
      "episodic.mv2",
      "policy.mv2",
      "procedural.mv2",
      "self.mv2",
      "semantic.mv2",
      "working.mv2",
    ].map((name, index) => ({
      relative: `memory/${name}`,
      dev: 1,
      ino: index + 1,
    }));
    expect(
      validateMemorySnapshotEntries(snapshot),
    ).toEqual(snapshot);
    expect(() =>
      validateMemorySnapshotEntries([
        ...snapshot,
        {
          relative: "other/extra.mv2",
          dev: 1,
          ino: 99,
        },
      ]),
    ).toThrow("directory_smoke_memory_set_invalid");
  });

  it("independently verifies full staged resource coverage and digests", async () => {
    const root = await realpath(
      await mkdtemp(
        join(tmpdir(), "kestrel-smoke-resources-"),
      ),
    );
    const sidecarRoot = join(root, "sidecar");
    const sidecarPath = join(
      sidecarRoot,
      "kestrel-desktop-sidecar",
    );
    const sbomPath = join(root, "sbom.cdx.json");
    const extraPath = join(root, "unexpected.txt");
    const digest = (value) =>
      createHash("sha256").update(value).digest("hex");
    const sidecarBytes = Buffer.from("sidecar");
    const sbomBytes = Buffer.from("{}");
    const manifest = {
      sbom_sha256: digest(sbomBytes),
      files: {
        "sbom.cdx.json": {
          sha256: digest(sbomBytes),
          size: sbomBytes.byteLength,
        },
        "sidecar/kestrel-desktop-sidecar": {
          sha256: digest(sidecarBytes),
          size: sidecarBytes.byteLength,
        },
      },
    };
    await chmod(root, 0o700);
    await mkdir(sidecarRoot);
    try {
      await writeFile(sidecarPath, sidecarBytes);
      await writeFile(sbomPath, sbomBytes);
      await expect(
        verifyManifestInventory(root, manifest),
      ).resolves.toEqual(manifest.files);

      await writeFile(extraPath, "extra");
      await expect(
        verifyManifestInventory(root, manifest),
      ).rejects.toThrow(
        "directory_smoke_resource_coverage_mismatch",
      );
      await rm(extraPath);

      await writeFile(sidecarPath, "tampered");
      await expect(
        verifyManifestInventory(root, manifest),
      ).rejects.toThrow(
        "directory_smoke_resource_digest_mismatch",
      );
      await rm(sidecarPath);
      await symlink(sbomPath, sidecarPath);
      await expect(
        verifyManifestInventory(root, manifest),
      ).rejects.toThrow("directory_smoke_resource_untrusted");
    } finally {
      await rm(root, { recursive: true, force: true });
    }
  });
});
