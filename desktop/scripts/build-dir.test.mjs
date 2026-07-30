import { generateKeyPairSync, sign, createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
import {
  chmod,
  cp,
  lstat,
  mkdir,
  mkdtemp,
  readFile,
  readdir,
  realpath,
  rename,
  rm,
  symlink,
  writeFile,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  buildDeveloperDirectory,
  builderArgumentsForPlatform,
} from "./build-dir.mjs";

const roots = [];
const actualDesktopRoot = fileURLToPath(
  new URL("..", import.meta.url),
);

function canonicalJsonBytes(value) {
  function serialize(current) {
    if (current === null || typeof current !== "object") {
      return JSON.stringify(current);
    }
    if (Array.isArray(current)) {
      return `[${current.map((item) => serialize(item)).join(",")}]`;
    }
    return `{${Object.keys(current)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${serialize(current[key])}`)
      .join(",")}}`;
  }
  return Buffer.from(`${serialize(value)}\n`, "utf8");
}

function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}

async function writeCanonicalJson(path, value) {
  const bytes = canonicalJsonBytes(value);
  await mkdir(dirname(path), { recursive: true });
  await writeFile(path, bytes);
  return sha256(bytes);
}

async function initializeGitRepository(root) {
  const run = (...args) =>
    spawnSync("git", args, {
      cwd: root,
      encoding: "utf8",
      env: {
        ...process.env,
        GIT_AUTHOR_NAME: "Kestrel Test",
        GIT_AUTHOR_EMAIL: "kestrel@example.invalid",
        GIT_COMMITTER_NAME: "Kestrel Test",
        GIT_COMMITTER_EMAIL: "kestrel@example.invalid",
      },
    });
  expect(run("init", "-q").status).toBe(0);
  expect(run("add", "-f", ".").status).toBe(0);
  expect(run("commit", "-qm", "fixture").status).toBe(0);
  const head = run("rev-parse", "HEAD");
  expect(head.status).toBe(0);
  return head.stdout.trim();
}

function runGit(root, ...args) {
  return spawnSync("git", args, {
    cwd: root,
    encoding: "utf8",
    env: {
      ...process.env,
      GIT_AUTHOR_NAME: "Kestrel Test",
      GIT_AUTHOR_EMAIL: "kestrel@example.invalid",
      GIT_COMMITTER_NAME: "Kestrel Test",
      GIT_COMMITTER_EMAIL: "kestrel@example.invalid",
    },
  });
}

function productionPackageRoots(lockMetadata) {
  const candidates = Object.entries(lockMetadata.packages)
    .filter(
      ([pathValue, metadata]) =>
        pathValue.startsWith("node_modules/") &&
        metadata !== null &&
        typeof metadata === "object" &&
        metadata.dev !== true &&
        metadata.link !== true,
    )
    .map(([pathValue]) => pathValue)
    .sort((left, right) => left.length - right.length);
  const selected = [];
  for (const pathValue of candidates) {
    if (
      !selected.some((ancestor) =>
        pathValue.startsWith(`${ancestor}/node_modules/`),
      )
    ) {
      selected.push(pathValue);
    }
  }
  return selected;
}

async function createFixture(
  { useActualProductionClosure = false } = {},
) {
  const root = await realpath(
    await mkdtemp(join(tmpdir(), "kestrel-build-dir-test-")),
  );
  roots.push(root);
  const repositoryRoot = join(root, "repository");
  const desktopRoot = join(repositoryRoot, "desktop");
  const stageRoot = join(root, "stage");
  const evidenceRoot = join(root, "evidence");
  const outputRoot = join(root, "directory-output");
  const buildReceiptPath = join(evidenceRoot, "build-receipt.json");
  await mkdir(join(desktopRoot, "dist"), { recursive: true });
  await mkdir(
    join(desktopRoot, "node_modules", "electron-builder", "out", "cli"),
    { recursive: true },
  );
  await mkdir(join(desktopRoot, "node_modules", "electron"), {
    recursive: true,
  });
  await mkdir(join(desktopRoot, "node_modules", "electron-updater"), {
    recursive: true,
  });
  await mkdir(
    join(
      desktopRoot,
      "node_modules",
      "electron-updater",
      "node_modules",
      ".bin",
    ),
    { recursive: true },
  );
  await mkdir(join(desktopRoot, "node_modules", "zod"), {
    recursive: true,
  });
  await writeFile(join(desktopRoot, "dist", "main.js"), "export {};\n");
  await writeFile(
    join(desktopRoot, "dist", "runtime-proof.js"),
    "export const runtimeProof = true;\n",
  );
  const desktopPackage = {
    name: "kestrel-desktop",
    version: "0.5.0",
    private: true,
    type: "module",
    main: "dist/main.js",
    dependencies: {
      "electron-updater": "6.8.9",
      zod: "4.4.3",
    },
    devDependencies: {
      electron: "43.2.0",
      "electron-builder": "26.15.3",
    },
  };
  await writeFile(
    join(desktopRoot, "package.json"),
    `${JSON.stringify(desktopPackage)}\n`,
  );
  await writeFile(
    join(desktopRoot, "package-lock.json"),
    `${JSON.stringify({
      lockfileVersion: 3,
      packages: {
        "": {
          dependencies: {
            "electron-updater": "6.8.9",
            zod: "4.4.3",
          },
          devDependencies: {
            electron: "43.2.0",
            "electron-builder": "26.15.3",
          },
        },
        "node_modules/electron": {
          version: "43.2.0",
          dev: true,
        },
        "node_modules/electron-builder": {
          version: "26.15.3",
          dev: true,
        },
        "node_modules/electron-updater": { version: "6.8.9" },
        "node_modules/zod": { version: "4.4.3" },
      },
    })}\n`,
  );
  await writeFile(
    join(
      desktopRoot,
      "node_modules",
      "electron-builder",
      "package.json",
    ),
    '{"version":"26.15.3"}\n',
  );
  await writeFile(
    join(
      desktopRoot,
      "node_modules",
      "electron-builder",
      "out",
      "cli",
      "cli.js",
    ),
    "export {};\n",
  );
  await writeFile(
    join(desktopRoot, "node_modules", "electron", "package.json"),
    '{"version":"43.2.0"}\n',
  );
  await writeFile(
    join(
      desktopRoot,
      "node_modules",
      "electron-updater",
      "package.json",
    ),
    JSON.stringify({
      version: "6.8.9",
      scripts: {
        test: "node test.js",
      },
      keywords: ["electron", "updates"],
      bugs: {
        url: "https://example.invalid/electron-updater/issues",
      },
    }) + "\n",
  );
  await symlink(
    "../semver/bin/semver.js",
    join(
      desktopRoot,
      "node_modules",
      "electron-updater",
      "node_modules",
      ".bin",
      "semver",
    ),
  );
  await writeFile(
    join(desktopRoot, "node_modules", "zod", "package.json"),
    JSON.stringify({
      version: "4.4.3",
      scripts: {
        test: "node test.js",
      },
      keywords: ["schema", "validation"],
      contributors: ["Kestrel fixture"],
    }) + "\n",
  );
  let productionPackagePaths = [
    "node_modules/electron-updater",
    "node_modules/zod",
  ];
  if (useActualProductionClosure) {
    const actualPackage = await readFile(
      join(actualDesktopRoot, "package.json"),
    );
    const actualLock = await readFile(
      join(actualDesktopRoot, "package-lock.json"),
    );
    const lockMetadata = JSON.parse(actualLock.toString("utf8"));
    productionPackagePaths = productionPackageRoots(lockMetadata);
    await writeFile(
      join(desktopRoot, "package.json"),
      actualPackage,
    );
    await writeFile(
      join(desktopRoot, "package-lock.json"),
      actualLock,
    );
    await rm(
      join(desktopRoot, "node_modules", "electron-updater"),
      { recursive: true, force: true },
    );
    await rm(
      join(desktopRoot, "node_modules", "zod"),
      { recursive: true, force: true },
    );
    for (const packagePath of productionPackagePaths) {
      await cp(
        join(
          actualDesktopRoot,
          ...packagePath.split("/"),
        ),
        join(desktopRoot, ...packagePath.split("/")),
        { recursive: true },
      );
    }
  }
  const config = {
    appId: "dev.kestrel.desktop",
    productName: "Kestrel Developer",
    asar: false,
    npmRebuild: false,
    removePackageKeywords: false,
    removePackageScripts: false,
    directories: {
      output: "__VERIFIED_DIRECTORY_OUTPUT__",
    },
    electronVersion: "43.2.0",
    files: [
      "dist/**/*",
      "package.json",
      "config/desktop-developer-public-key.pem",
    ],
    extraResources: [
      {
        from: "__VERIFIED_STAGE_RESOURCE_ROOT__",
        to: "kestrel",
      },
    ],
    mac: {
      target: ["dir"],
      identity: null,
      hardenedRuntime: false,
      gatekeeperAssess: false,
    },
    win: {
      target: ["dir"],
      signAndEditExecutable: false,
    },
    linux: { target: ["dir"] },
  };
  await writeFile(
    join(desktopRoot, "electron-builder.developer.yml"),
    canonicalJsonBytes(config),
  );
  const sourceCommit = await initializeGitRepository(repositoryRoot);
  const packageLockBytes = await readFile(
    join(desktopRoot, "package-lock.json"),
  );

  await mkdir(join(stageRoot, "sidecar"), { recursive: true });
  await writeFile(
    join(stageRoot, "sidecar", "kestrel-desktop-sidecar"),
    "sidecar\n",
  );
  const { privateKey, publicKey } = generateKeyPairSync("ed25519");
  const publicKeyBytes = Buffer.from(
    publicKey.export({ type: "spki", format: "pem" }),
  );
  await writeFile(
    join(stageRoot, "desktop-developer-public-key.pem"),
    publicKeyBytes,
  );
  const manifest = {
    schema: "kestrel.desktop.resources.v1",
    build_mode: "developer",
    key_id: "developer",
    source_commit: sourceCommit,
    app_version: "0.5.0",
    platform: process.platform,
    architecture: process.arch,
    python_lock_sha256: "1".repeat(64),
    desktop_npm_lock_sha256: sha256(packageLockBytes),
    web_npm_lock_sha256: "2".repeat(64),
    sbom_sha256: "3".repeat(64),
    files: {
      "desktop-developer-public-key.pem": {
        size: publicKeyBytes.length,
        sha256: sha256(publicKeyBytes),
      },
      "sidecar/kestrel-desktop-sidecar": {
        size: 8,
        sha256: sha256(Buffer.from("sidecar\n")),
      },
    },
  };
  const manifestPath = join(stageRoot, "kestrel-resource-manifest.json");
  const manifestBytes = canonicalJsonBytes(manifest);
  await writeFile(manifestPath, manifestBytes);
  const signatureBytes = sign(null, manifestBytes, privateKey);
  const signaturePath = join(stageRoot, "kestrel-resource-manifest.sig");
  await writeFile(signaturePath, signatureBytes);
  const publicKeyPath = join(
    stageRoot,
    "desktop-developer-public-key.pem",
  );
  const stageReceipt = {
    schema: "kestrel.desktop.stage.v1",
    build_mode: "developer",
    key_id: "developer",
    source_commit: sourceCommit,
    app_version: "0.5.0",
    platform: process.platform,
    architecture: process.arch,
    resource_root: stageRoot,
    sidecar_relative_path: "sidecar/kestrel-desktop-sidecar",
    manifest_path: manifestPath,
    manifest_sha256: sha256(manifestBytes),
    signature_path: signaturePath,
    signature_sha256: sha256(signatureBytes),
    public_key_path: publicKeyPath,
    public_key_sha256: sha256(publicKeyBytes),
    sbom_sha256: "3".repeat(64),
    input_receipt_sha256: {
      desktop: "4".repeat(64),
      notices: "5".repeat(64),
      sbom: "6".repeat(64),
      sidecar: "7".repeat(64),
      web: "8".repeat(64),
    },
  };
  await mkdir(evidenceRoot, { recursive: true });
  const stageReceiptPath = join(evidenceRoot, "stage-receipt.json");
  await writeCanonicalJson(stageReceiptPath, stageReceipt);
  return {
    repositoryRoot,
    desktopRoot,
    stageRoot,
    stageReceipt,
    stageReceiptPath,
    outputRoot,
    buildReceiptPath,
    productionPackagePaths,
  };
}

async function copyDirectorySourceToApplication(invocation, mutate) {
  const outputRoot = invocation.outputRoot;
  const appSource = invocation.appSource;
  const stageRoot = invocation.resourceRoot;
  const applicationRoot =
    process.platform === "darwin"
      ? join(outputRoot, "opaque", "Kestrel Developer.app")
      : join(outputRoot, "opaque");
  const resourcesRoot =
    process.platform === "darwin"
      ? join(applicationRoot, "Contents", "Resources")
      : join(applicationRoot, "resources");
  await mkdir(resourcesRoot, { recursive: true });
  await cp(appSource, join(resourcesRoot, "app"), { recursive: true });
  await cp(stageRoot, join(resourcesRoot, "kestrel"), {
    recursive: true,
  });
  const executablePath =
    process.platform === "darwin"
      ? join(
          applicationRoot,
          "Contents",
          "MacOS",
          "Kestrel Developer",
        )
      : process.platform === "win32"
        ? join(applicationRoot, "Kestrel Developer.exe")
        : join(applicationRoot, "kestrel-desktop");
  await mkdir(dirname(executablePath), { recursive: true });
  await writeFile(executablePath, "developer executable fixture\n");
  if (process.platform !== "win32") {
    await chmod(executablePath, 0o755);
  }
  if (mutate !== undefined) {
    await mutate({ applicationRoot, resourcesRoot });
  }
}

async function runPinnedRealBuilder(invocation, builderOutput) {
  const completed = spawnSync(
    process.execPath,
    [
      join(
        actualDesktopRoot,
        "node_modules",
        "electron-builder",
        "out",
        "cli",
        "cli.js",
      ),
      ...invocation.arguments,
    ],
    {
      cwd: invocation.cwd,
      env: invocation.environment,
      encoding: "utf8",
      maxBuffer: 1024 * 1024,
    },
  );
  builderOutput.push(completed.stdout, completed.stderr);
  expect(
    completed.status,
    builderOutput.join("\n").slice(-16 * 1024),
  ).toBe(0);
}

describe("developer directory bundle", () => {
  beforeEach(() => {
    delete process.env.KESTREL_DESKTOP_BUILD_MODE;
  });

  afterEach(async () => {
    delete process.env.KESTREL_DESKTOP_BUILD_MODE;
    await Promise.all(
      roots.splice(0).map((root) =>
        rm(root, { recursive: true, force: true })
      ),
    );
  });

  it("emits exact receipt roots and only a current-platform dir invocation", async () => {
    const fixture = await createFixture();
    let capturedInvocation;
    let capturedEffectiveConfig;
    const receipt = await buildDeveloperDirectory(
      {
        stageReceiptPath: fixture.stageReceiptPath,
        outputRoot: fixture.outputRoot,
        receiptPath: fixture.buildReceiptPath,
      },
      {
        repositoryRoot: fixture.repositoryRoot,
        desktopRoot: fixture.desktopRoot,
        executeBuilder: async (invocation) => {
          capturedInvocation = invocation;
          capturedEffectiveConfig = JSON.parse(
            await readFile(invocation.effectiveConfigPath, "utf8"),
          );
          await copyDirectorySourceToApplication(invocation);
        },
      },
    );

    expect(capturedInvocation.arguments).toEqual(
      builderArgumentsForPlatform(
        process.platform,
        process.arch,
        capturedInvocation.effectiveConfigPath,
        capturedInvocation.appSource,
      ),
    );
    expect(capturedInvocation.arguments.join(" ")).not.toMatch(
      /publish|dmg|zip|nsis|appimage|notar|sign/i,
    );
    expect(capturedEffectiveConfig.removePackageScripts).toBe(false);
    expect(capturedEffectiveConfig.removePackageKeywords).toBe(false);
    expect(capturedEffectiveConfig.extraResources).toEqual([
      {
        from: fixture.stageRoot,
        to: "kestrel",
      },
    ]);
    expect(receipt.schema).toBe(
      "kestrel.desktop.directory-build.v1",
    );
    expect(receipt.build_mode).toBe("developer");
    expect(receipt.source_commit).toBe(
      fixture.stageReceipt.source_commit,
    );
    expect(receipt.application_root.startsWith(fixture.outputRoot)).toBe(
      true,
    );
    expect(receipt.resource_root).toBe(
      process.platform === "darwin"
        ? join(
            receipt.application_root,
            "Contents",
            "Resources",
            "kestrel",
          )
        : join(receipt.application_root, "resources", "kestrel"),
    );
    expect(receipt.executable_path.startsWith(receipt.application_root)).toBe(
      true,
    );
    const packagedAppRoot = dirname(
      dirname(receipt.packaged_public_key_path),
    );
    expect(receipt.executable_sha256).toMatch(/^[0-9a-f]{64}$/);
    expect(receipt.executable_size).toBeGreaterThan(0);
    expect(receipt.packaged_dist_path).toBe(
      join(packagedAppRoot, "dist"),
    );
    expect(receipt.packaged_dist_inventory_sha256).toMatch(
      /^[0-9a-f]{64}$/,
    );
    expect(receipt.packaged_dist_file_count).toBe(2);
    expect(receipt.packaged_dist_total_bytes).toBeGreaterThan(0);
    expect(receipt.stage_receipt_path).toBe(
      fixture.stageReceiptPath,
    );
    expect(receipt.electron_version).toBe("43.2.0");
    expect(receipt.electron_builder_version).toBe("26.15.3");
    const packagedPackage = JSON.parse(
      await readFile(receipt.packaged_package_json_path, "utf8"),
    );
    expect(
      packagedPackage.kestrelDesktopBuild.resource_root_relative,
    ).toBe("kestrel");
    expect(
      packagedPackage.kestrelDesktopBuild.smoke_authority,
    ).toBe("developer_directory_smoke_v1");
    await expect(
      lstat(
        join(
          packagedAppRoot,
          "node_modules",
          "electron-updater",
          "package.json",
        ),
      ),
    ).resolves.toBeDefined();
    await expect(
      lstat(
        join(packagedAppRoot, "node_modules", "zod", "package.json"),
      ),
    ).resolves.toBeDefined();
    await expect(
      lstat(
        join(
          packagedAppRoot,
          "node_modules",
          "electron-builder",
        ),
      ),
    ).rejects.toMatchObject({ code: "ENOENT" });
    expect(JSON.parse(await readFile(
      fixture.buildReceiptPath,
      "utf8",
    ))).toEqual(receipt);
    expect(
      (await readdir(receipt.application_root, {
        recursive: true,
      })).some((name) => /private.*key|\\.key$/i.test(name)),
    ).toBe(false);
    const privateKeySearch = spawnSync(
      "grep",
      ["-R", "-l", "BEGIN PRIVATE KEY", receipt.application_root],
      { encoding: "utf8" },
    );
    expect(privateKeySearch.status).toBe(1);
  });

  it.runIf(
    process.env.RUN_DESKTOP_BUILDER_INTEGRATION === "1",
  )(
    "runs the pinned real electron-builder directory target",
    async () => {
      const fixture = await createFixture();
      const builderOutput = [];
      const receipt = await buildDeveloperDirectory(
        {
          stageReceiptPath: fixture.stageReceiptPath,
          outputRoot: fixture.outputRoot,
          receiptPath: fixture.buildReceiptPath,
        },
        {
          repositoryRoot: fixture.repositoryRoot,
          desktopRoot: fixture.desktopRoot,
          executeBuilder: async (invocation) => {
            await runPinnedRealBuilder(invocation, builderOutput);
          },
        },
      );

      expect(receipt.directory_only).toBe(true);
      expect(receipt.signed).toBe(false);
      expect(builderOutput.join("\n")).not.toMatch(
        /publishing|notarizing|signing identity/i,
      );
      const packagedAppRoot = dirname(
        dirname(receipt.packaged_public_key_path),
      );
      for (const dependency of ["electron-updater", "zod"]) {
        const sourcePackage = await readFile(
          join(
            fixture.desktopRoot,
            "node_modules",
            dependency,
            "package.json",
          ),
        );
        const packagedDependency = await readFile(
          join(
            packagedAppRoot,
            "node_modules",
            dependency,
            "package.json",
          ),
        );
        expect(packagedDependency.equals(sourcePackage)).toBe(true);
      }
    },
    30_000,
  );

  it.runIf(
    process.env.RUN_DESKTOP_BUILDER_INTEGRATION === "1",
  )(
    "preserves the complete installed production dependency closure",
    async () => {
      const fixture = await createFixture({
        useActualProductionClosure: true,
      });
      const builderOutput = [];
      const receipt = await buildDeveloperDirectory(
        {
          stageReceiptPath: fixture.stageReceiptPath,
          outputRoot: fixture.outputRoot,
          receiptPath: fixture.buildReceiptPath,
        },
        {
          repositoryRoot: fixture.repositoryRoot,
          desktopRoot: fixture.desktopRoot,
          executeBuilder: async (invocation) => {
            await runPinnedRealBuilder(invocation, builderOutput);
          },
        },
      );

      expect(receipt.production_dependency_count).toBe(
        fixture.productionPackagePaths.length,
      );
      const packagedAppRoot = dirname(
        dirname(receipt.packaged_public_key_path),
      );
      for (const packagePath of fixture.productionPackagePaths) {
        const sourcePackage = await readFile(
          join(
            fixture.desktopRoot,
            ...packagePath.split("/"),
            "package.json",
          ),
        );
        const packagedPackage = await readFile(
          join(
            packagedAppRoot,
            ...packagePath.split("/"),
            "package.json",
          ),
        );
        expect(
          packagedPackage.equals(sourcePackage),
          packagePath,
        ).toBe(true);
      }
    },
    30_000,
  );

  it("ignores environment hints, rejects release identity and unknown identity argv", async () => {
    const fixture = await createFixture();
    process.env.KESTREL_DESKTOP_BUILD_MODE = "release";
    await expect(
      buildDeveloperDirectory(
        {
          stageReceiptPath: fixture.stageReceiptPath,
          outputRoot: fixture.outputRoot,
          receiptPath: fixture.buildReceiptPath,
        },
        {
          repositoryRoot: fixture.repositoryRoot,
          desktopRoot: fixture.desktopRoot,
          executeBuilder: async (invocation) => {
            await copyDirectorySourceToApplication(invocation);
          },
        },
      ),
    ).resolves.toMatchObject({ build_mode: "developer" });

    const releaseReceipt = {
      ...fixture.stageReceipt,
      build_mode: "release",
      key_id: "release",
    };
    await writeCanonicalJson(
      fixture.stageReceiptPath,
      releaseReceipt,
    );
    await rm(fixture.outputRoot, { recursive: true, force: true });
    await rm(fixture.buildReceiptPath, { force: true });
    await expect(
      buildDeveloperDirectory(
        {
          stageReceiptPath: fixture.stageReceiptPath,
          outputRoot: fixture.outputRoot,
          receiptPath: fixture.buildReceiptPath,
        },
        {
          repositoryRoot: fixture.repositoryRoot,
          desktopRoot: fixture.desktopRoot,
          executeBuilder: async () => {
            throw new Error("builder must not run");
          },
        },
      ),
    ).rejects.toThrow("developer");

    const unknownIdentityArgument = spawnSync(
      process.execPath,
      [
        fileURLToPath(new URL("./build-dir.mjs", import.meta.url)),
        "--build-mode",
        "release",
      ],
      { encoding: "utf8" },
    );
    expect(unknownIdentityArgument.status).not.toBe(0);
    expect(unknownIdentityArgument.stderr).toContain(
      "unknown build-dir option",
    );
  });

  it("refuses preexisting output and linked staged payload", async () => {
    const fixture = await createFixture();
    await mkdir(fixture.outputRoot);
    await expect(
      buildDeveloperDirectory(
        {
          stageReceiptPath: fixture.stageReceiptPath,
          outputRoot: fixture.outputRoot,
          receiptPath: fixture.buildReceiptPath,
        },
        {
          repositoryRoot: fixture.repositoryRoot,
          desktopRoot: fixture.desktopRoot,
          executeBuilder: async () => {
            throw new Error("builder must not run");
          },
        },
      ),
    ).rejects.toThrow("must not already exist");

    await rm(fixture.outputRoot, { recursive: true });
    const external = join(dirname(fixture.stageRoot), "external");
    await writeFile(external, "outside\n");
    await symlink(external, join(fixture.stageRoot, "linked"));
    await expect(
      buildDeveloperDirectory(
        {
          stageReceiptPath: fixture.stageReceiptPath,
          outputRoot: fixture.outputRoot,
          receiptPath: fixture.buildReceiptPath,
        },
        {
          repositoryRoot: fixture.repositoryRoot,
          desktopRoot: fixture.desktopRoot,
          executeBuilder: async () => {
            throw new Error("builder must not run");
          },
        },
      ),
    ).rejects.toThrow(/link|regular/i);
  });

  it("rejects preexisting receipt, dirty source, source drift, config drift, and toolchain drift", async () => {
    const attempts = [
      async (fixture) => {
        await writeFile(fixture.buildReceiptPath, "{}\n");
        return "build receipt must not already exist";
      },
      async (fixture) => {
        fixture.outputRoot = join(
          fixture.repositoryRoot,
          "derived-directory",
        );
        return "directory output path is unsafe";
      },
      async (fixture) => {
        fixture.buildReceiptPath = join(
          fixture.repositoryRoot,
          "derived-receipt.json",
        );
        return "build receipt path is unsafe";
      },
      async (fixture) => {
        await writeFile(
          join(fixture.repositoryRoot, "untracked.txt"),
          "dirty\n",
        );
        return "source checkout must be exactly clean";
      },
      async (fixture) => {
        expect(
          runGit(
            fixture.repositoryRoot,
            "commit",
            "--allow-empty",
            "-qm",
            "drift",
          ).status,
        ).toBe(0);
        return "source checkout commit mismatch";
      },
      async (fixture) => {
        const configPath = join(
          fixture.desktopRoot,
          "electron-builder.developer.yml",
        );
        const config = JSON.parse(await readFile(configPath, "utf8"));
        config.publish = "always";
        await writeFile(configPath, canonicalJsonBytes(config));
        return "unreviewed target";
      },
      async (fixture) => {
        await writeFile(
          join(
            fixture.desktopRoot,
            "node_modules",
            "electron-builder",
            "package.json",
          ),
          '{"version":"26.15.2"}\n',
        );
        return "toolchain pin mismatch";
      },
    ];
    for (const arrange of attempts) {
      const fixture = await createFixture();
      const expected = await arrange(fixture);
      await expect(
        buildDeveloperDirectory(
          {
            stageReceiptPath: fixture.stageReceiptPath,
            outputRoot: fixture.outputRoot,
            receiptPath: fixture.buildReceiptPath,
          },
          {
            repositoryRoot: fixture.repositoryRoot,
            desktopRoot: fixture.desktopRoot,
            executeBuilder: async () => {
              throw new Error("builder must not run");
            },
          },
        ),
      ).rejects.toThrow(expected);
    }
  });

  it("rejects packaged metadata, compiled app, key, and staged-resource identity drift", async () => {
    for (const mutation of ["package", "compiled-app", "key", "resource"]) {
      const fixture = await createFixture();
      await expect(
        buildDeveloperDirectory(
          {
            stageReceiptPath: fixture.stageReceiptPath,
            outputRoot: fixture.outputRoot,
            receiptPath: fixture.buildReceiptPath,
          },
          {
            repositoryRoot: fixture.repositoryRoot,
            desktopRoot: fixture.desktopRoot,
            executeBuilder: async (invocation) => {
              await copyDirectorySourceToApplication(
                invocation,
                async ({ resourcesRoot }) => {
                  if (mutation === "package") {
                    await writeFile(
                      join(resourcesRoot, "app", "package.json"),
                      '{"version":"9.9.9"}\n',
                    );
                  } else if (mutation === "compiled-app") {
                    await writeFile(
                      join(
                        resourcesRoot,
                        "app",
                        "dist",
                        "main.js",
                      ),
                      "throw new Error('tampered');\n",
                    );
                  } else if (mutation === "key") {
                    await writeFile(
                      join(
                        resourcesRoot,
                        "app",
                        "config",
                        "desktop-developer-public-key.pem",
                      ),
                      "replacement\n",
                    );
                  } else {
                    await writeFile(
                      join(
                        resourcesRoot,
                        "kestrel",
                        "sidecar",
                        "kestrel-desktop-sidecar",
                      ),
                      "replacement\n",
                    );
                  }
                },
              );
            },
          },
        ),
      ).rejects.toThrow(/mismatch|changed/i);
      await expect(lstat(fixture.outputRoot)).rejects.toMatchObject({
        code: "ENOENT",
      });
    }
  });

  it.runIf(process.platform === "darwin")(
    "rejects an executable reached through a linked ancestor",
    async () => {
      const fixture = await createFixture();
      const outsideMacOsRoot = join(
        dirname(fixture.outputRoot),
        "outside-macos",
      );
      await expect(
        buildDeveloperDirectory(
          {
            stageReceiptPath: fixture.stageReceiptPath,
            outputRoot: fixture.outputRoot,
            receiptPath: fixture.buildReceiptPath,
          },
          {
            repositoryRoot: fixture.repositoryRoot,
            desktopRoot: fixture.desktopRoot,
            executeBuilder: async (invocation) => {
              await copyDirectorySourceToApplication(
                invocation,
                async ({ applicationRoot }) => {
                  const macOsRoot = join(
                    applicationRoot,
                    "Contents",
                    "MacOS",
                  );
                  await rename(macOsRoot, outsideMacOsRoot);
                  await symlink(outsideMacOsRoot, macOsRoot);
                },
              );
            },
          },
        ),
      ).rejects.toThrow(/canonical|link|escape/i);
      await expect(
        readFile(
          join(
            outsideMacOsRoot,
            "Kestrel Developer",
          ),
          "utf8",
        ),
      ).resolves.toContain("developer executable fixture");
    },
  );

  it("rejects a linked generated dependency root before replacement", async () => {
    const fixture = await createFixture();
    const externalDependencies = join(
      dirname(fixture.repositoryRoot),
      "external-dependencies",
    );
    await mkdir(externalDependencies);
    const markerPath = join(externalDependencies, "marker.txt");
    await writeFile(markerPath, "preserve\n");

    await expect(
      buildDeveloperDirectory(
        {
          stageReceiptPath: fixture.stageReceiptPath,
          outputRoot: fixture.outputRoot,
          receiptPath: fixture.buildReceiptPath,
        },
        {
          repositoryRoot: fixture.repositoryRoot,
          desktopRoot: fixture.desktopRoot,
          executeBuilder: async (invocation) => {
            await copyDirectorySourceToApplication(
              invocation,
              async ({ resourcesRoot }) => {
                const dependencyRoot = join(
                  resourcesRoot,
                  "app",
                  "node_modules",
                );
                await rm(dependencyRoot, {
                  recursive: true,
                  force: true,
                });
                await symlink(externalDependencies, dependencyRoot);
              },
            );
          },
        },
      ),
    ).rejects.toThrow(/linked directory|canonical/i);
    await expect(readFile(markerPath, "utf8")).resolves.toBe(
      "preserve\n",
    );
    await expect(lstat(fixture.outputRoot)).rejects.toMatchObject({
      code: "ENOENT",
    });
  });
});
