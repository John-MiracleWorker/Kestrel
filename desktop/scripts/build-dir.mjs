#!/usr/bin/env node

import { spawnSync } from "node:child_process";
import {
  createHash,
  createPublicKey,
  verify as verifySignature,
} from "node:crypto";
import {
  constants as fsConstants,
  copyFile,
  lstat,
  mkdir,
  mkdtemp,
  open,
  readdir,
  realpath,
  rm,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import {
  dirname,
  isAbsolute,
  join,
  parse,
  relative,
  resolve,
  sep,
} from "node:path";
import { fileURLToPath } from "node:url";

const SCRIPT_DIRECTORY = dirname(fileURLToPath(import.meta.url));
const DEFAULT_DESKTOP_ROOT = resolve(SCRIPT_DIRECTORY, "..");
const DEFAULT_REPOSITORY_ROOT = resolve(DEFAULT_DESKTOP_ROOT, "..");
const CONFIG_NAME = "electron-builder.developer.yml";
const MANIFEST_NAME = "kestrel-resource-manifest.json";
const SIGNATURE_NAME = "kestrel-resource-manifest.sig";
const PUBLIC_KEY_NAME = "desktop-developer-public-key.pem";
const STAGE_RECEIPT_SCHEMA = "kestrel.desktop.stage.v1";
const BUILD_RECEIPT_SCHEMA = "kestrel.desktop.directory-build.v1";
const DIRECTORY_INVENTORY_SCHEMA =
  "kestrel.desktop.directory-inventory.v1";
const RESOURCE_SCHEMA = "kestrel.desktop.resources.v1";
const ELECTRON_VERSION = "43.2.0";
const ELECTRON_BUILDER_VERSION = "26.15.3";
const MAX_CONTROL_BYTES = 64 * 1024;
const MAX_MANIFEST_BYTES = 1024 * 1024;
const MAX_SIGNATURE_BYTES = 4096;
const MAX_PUBLIC_KEY_BYTES = 16 * 1024;
const MAX_INVENTORY_FILES = 100_000;
const MAX_INVENTORY_BYTES = 4 * 1024 * 1024 * 1024;
const SHA256_PATTERN = /^[0-9a-f]{64}$/;
const COMMIT_PATTERN = /^[0-9a-f]{40}$/;
const VERSION_PATTERN = /^[0-9A-Za-z][0-9A-Za-z.+-]{0,63}$/;
const ARGUMENT_NAMES = new Set([
  "--output",
  "--receipt",
  "--stage-receipt",
]);
const STAGE_RECEIPT_KEYS = new Set([
  "app_version",
  "architecture",
  "build_mode",
  "input_receipt_sha256",
  "key_id",
  "manifest_path",
  "manifest_sha256",
  "platform",
  "public_key_path",
  "public_key_sha256",
  "resource_root",
  "sbom_sha256",
  "schema",
  "sidecar_relative_path",
  "signature_path",
  "signature_sha256",
  "source_commit",
]);
const EXPECTED_DEVELOPER_CONFIG = {
  appId: "dev.kestrel.desktop",
  productName: "Kestrel Developer",
  asar: false,
  npmRebuild: false,
  removePackageKeywords: false,
  removePackageScripts: false,
  directories: {
    output: "__VERIFIED_DIRECTORY_OUTPUT__",
  },
  electronVersion: ELECTRON_VERSION,
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
  linux: {
    target: ["dir"],
  },
};

function compareStrings(left, right) {
  const leftPoints = Array.from(
    left,
    (value) => value.codePointAt(0) ?? 0,
  );
  const rightPoints = Array.from(
    right,
    (value) => value.codePointAt(0) ?? 0,
  );
  const length = Math.min(leftPoints.length, rightPoints.length);
  for (let index = 0; index < length; index += 1) {
    const difference = leftPoints[index] - rightPoints[index];
    if (difference !== 0) {
      return difference;
    }
  }
  return leftPoints.length - rightPoints.length;
}

function canonicalJsonBytes(value) {
  function serialize(current) {
    if (current === null || typeof current !== "object") {
      const scalar = JSON.stringify(current);
      if (scalar === undefined) {
        throw new Error("canonical JSON contains an unsupported value");
      }
      return scalar;
    }
    if (Array.isArray(current)) {
      return `[${current.map((item) => serialize(item)).join(",")}]`;
    }
    return `{${Object.keys(current)
      .sort(compareStrings)
      .map((key) => `${JSON.stringify(key)}:${serialize(current[key])}`)
      .join(",")}}`;
  }
  return Buffer.from(`${serialize(value)}\n`, "utf8");
}

function sha256Bytes(value) {
  return createHash("sha256").update(value).digest("hex");
}

function requireExactKeys(value, expected, label) {
  if (value === null || Array.isArray(value) || typeof value !== "object") {
    throw new Error(`${label} must be a JSON object`);
  }
  const actual = Object.keys(value).sort(compareStrings);
  const wanted = [...expected].sort(compareStrings);
  if (
    actual.length !== wanted.length ||
    actual.some((key, index) => key !== wanted[index])
  ) {
    throw new Error(`${label} fields mismatch`);
  }
}

function requireSha256(value, label) {
  if (typeof value !== "string" || !SHA256_PATTERN.test(value)) {
    throw new Error(`${label} must be a lowercase SHA-256 digest`);
  }
  return value;
}

function requireCommit(value, label) {
  if (typeof value !== "string" || !COMMIT_PATTERN.test(value)) {
    throw new Error(`${label} must be an exact lowercase source commit`);
  }
  return value;
}

function isContained(root, candidate) {
  const fromRoot = relative(root, candidate);
  return (
    fromRoot !== "" &&
    fromRoot !== ".." &&
    !fromRoot.startsWith(`..${sep}`) &&
    !isAbsolute(fromRoot)
  );
}

function sameFileIdentity(...entries) {
  return entries.every(
    (entry) =>
      entry.dev === entries[0].dev &&
      entry.ino === entries[0].ino &&
      entry.size === entries[0].size &&
      entry.mode === entries[0].mode &&
      entry.nlink === entries[0].nlink &&
      entry.mtimeNs === entries[0].mtimeNs &&
      entry.ctimeNs === entries[0].ctimeNs,
  );
}

async function inspectRegularFile(
  pathValue,
  label,
  maximumBytes = null,
  collect = false,
) {
  const before = await lstat(pathValue, { bigint: true });
  if (
    before.isSymbolicLink() ||
    !before.isFile() ||
    before.nlink !== 1n
  ) {
    throw new Error(`${label} must be a unique regular file`);
  }
  if (
    before.size > BigInt(Number.MAX_SAFE_INTEGER) ||
    (maximumBytes !== null && before.size > BigInt(maximumBytes))
  ) {
    throw new Error(`${label} exceeds its size limit`);
  }
  const handle = await open(
    pathValue,
    fsConstants.O_RDONLY |
      (fsConstants.O_NOFOLLOW ?? 0) |
      (fsConstants.O_CLOEXEC ?? 0),
  );
  try {
    const opened = await handle.stat({ bigint: true });
    if (!opened.isFile() || opened.nlink !== 1n) {
      throw new Error(`${label} changed during open`);
    }
    const size = Number(opened.size);
    const digest = createHash("sha256");
    const chunks = [];
    const buffer = Buffer.allocUnsafe(1024 * 1024);
    let position = 0;
    while (position < size) {
      const length = Math.min(buffer.length, size - position);
      const { bytesRead } = await handle.read(
        buffer,
        0,
        length,
        position,
      );
      if (bytesRead !== length) {
        throw new Error(`${label} changed during read`);
      }
      const chunk = buffer.subarray(0, bytesRead);
      digest.update(chunk);
      if (collect) {
        chunks.push(Buffer.from(chunk));
      }
      position += bytesRead;
    }
    const openedAfter = await handle.stat({ bigint: true });
    const after = await lstat(pathValue, { bigint: true });
    if (
      after.isSymbolicLink() ||
      !after.isFile() ||
      after.nlink !== 1n ||
      !sameFileIdentity(before, opened, openedAfter, after)
    ) {
      throw new Error(`${label} changed during read`);
    }
    return {
      bytes: collect ? Buffer.concat(chunks) : null,
      sha256: digest.digest("hex"),
      size,
    };
  } finally {
    await handle.close();
  }
}

async function readJsonFile(
  pathValue,
  label,
  maximumBytes,
  canonical = false,
) {
  const inspected = await inspectRegularFile(
    pathValue,
    label,
    maximumBytes,
    true,
  );
  let value;
  try {
    value = JSON.parse(inspected.bytes.toString("utf8"));
  } catch {
    throw new Error(`${label} must be valid UTF-8 JSON`);
  }
  if (
    canonical &&
    !canonicalJsonBytes(value).equals(inspected.bytes)
  ) {
    throw new Error(`${label} must be canonical JSON`);
  }
  return { ...inspected, value };
}

function validateSafeName(name, label) {
  if (
    name.length === 0 ||
    Buffer.byteLength(name, "utf8") > 255 ||
    name === "." ||
    name === ".." ||
    /[\u0000-\u001f\u007f-\u009f]/.test(name) ||
    /^\.env(?:\.|$)/i.test(name) ||
    /private[-_ ]?key|\.key$/i.test(name)
  ) {
    throw new Error(`${label} contains a forbidden path`);
  }
}

async function qualifyDirectory(pathValue, label) {
  const requested = resolve(pathValue);
  const metadata = await lstat(requested);
  if (metadata.isSymbolicLink() || !metadata.isDirectory()) {
    throw new Error(`${label} must be a non-linked directory`);
  }
  const canonical = await realpath(requested);
  if (canonical !== requested) {
    throw new Error(`${label} must be canonical`);
  }
  return canonical;
}

async function inventoryDirectory(
  root,
  label,
  { omitDependencyBins = false } = {},
) {
  const canonicalRoot = await qualifyDirectory(root, label);
  const files = new Map();
  let totalBytes = 0;
  async function walk(directory, relativeDirectory) {
    const entries = await readdir(directory, { withFileTypes: true });
    entries.sort((left, right) =>
      compareStrings(left.name, right.name),
    );
    for (const entry of entries) {
      validateSafeName(entry.name, label);
      const absolutePath = join(directory, entry.name);
      const relativePath =
        relativeDirectory.length === 0
          ? entry.name
          : `${relativeDirectory}/${entry.name}`;
      if (
        omitDependencyBins &&
        entry.isDirectory() &&
        entry.name === ".bin" &&
        (relativeDirectory === "node_modules" ||
          relativeDirectory.endsWith("/node_modules"))
      ) {
        continue;
      }
      const metadata = await lstat(absolutePath);
      if (
        entry.isSymbolicLink() ||
        metadata.isSymbolicLink() ||
        (!entry.isDirectory() && !entry.isFile())
      ) {
        throw new Error(`${label} contains a link or special file`);
      }
      const canonical = await realpath(absolutePath);
      if (!isContained(canonicalRoot, canonical)) {
        throw new Error(`${label} path escapes its root`);
      }
      if (entry.isDirectory()) {
        await walk(absolutePath, relativePath);
      } else {
        const inspected = await inspectRegularFile(
          absolutePath,
          `${label} ${relativePath}`,
        );
        totalBytes += inspected.size;
        if (
          files.size >= MAX_INVENTORY_FILES ||
          totalBytes > MAX_INVENTORY_BYTES
        ) {
          throw new Error(`${label} exceeds its inventory bounds`);
        }
        files.set(relativePath, inspected);
      }
    }
  }
  await walk(canonicalRoot, "");
  return { files, root: canonicalRoot };
}

function inventoriesMatch(left, right) {
  if (left.size !== right.size) {
    return false;
  }
  for (const [name, evidence] of left) {
    const other = right.get(name);
    if (
      other === undefined ||
      evidence.size !== other.size ||
      evidence.sha256 !== other.sha256
    ) {
      return false;
    }
  }
  return true;
}

function inventoryEvidence(inventory) {
  const files = {};
  let totalBytes = 0;
  for (const name of [...inventory.keys()].sort(compareStrings)) {
    const entry = inventory.get(name);
    if (entry === undefined) {
      throw new Error("directory inventory changed");
    }
    files[name] = {
      sha256: entry.sha256,
      size: entry.size,
    };
    totalBytes += entry.size;
    if (
      !Number.isSafeInteger(totalBytes) ||
      totalBytes > MAX_INVENTORY_BYTES
    ) {
      throw new Error("directory inventory exceeds its byte bound");
    }
  }
  const bytes = canonicalJsonBytes({
    schema: DIRECTORY_INVENTORY_SCHEMA,
    files,
  });
  return {
    fileCount: inventory.size,
    sha256: sha256Bytes(bytes),
    totalBytes,
  };
}

function inventoryDifference(left, right) {
  const differences = [];
  for (const [name, evidence] of left) {
    const other = right.get(name);
    if (other === undefined) {
      differences.push(`missing:${name}`);
    } else if (
      evidence.size !== other.size ||
      evidence.sha256 !== other.sha256
    ) {
      differences.push(`changed:${name}`);
    }
  }
  for (const name of right.keys()) {
    if (!left.has(name)) {
      differences.push(`extra:${name}`);
    }
  }
  return differences.sort(compareStrings).slice(0, 12).join(",");
}

function runGit(repositoryRoot, argumentsValue) {
  const completed = spawnSync("git", argumentsValue, {
    cwd: repositoryRoot,
    encoding: "utf8",
    maxBuffer: 1024 * 1024,
    env: {
      LANG: "C",
      LC_ALL: "C",
      PATH: process.env.PATH ?? "/usr/bin:/bin",
    },
    windowsHide: true,
  });
  if (completed.error || completed.status !== 0) {
    throw new Error("source checkout Git identity is unavailable");
  }
  return completed.stdout.trim();
}

function verifyCleanExactSource(repositoryRoot, expectedCommit) {
  const head = runGit(repositoryRoot, ["rev-parse", "HEAD"]);
  if (head !== expectedCommit) {
    throw new Error("source checkout commit mismatch");
  }
  const status = runGit(repositoryRoot, [
    "status",
    "--porcelain=v1",
    "--untracked-files=all",
  ]);
  if (status !== "") {
    throw new Error("source checkout must be exactly clean");
  }
}

function validateDeveloperIdentity(identity, platform, architecture) {
  if (
    identity.build_mode !== "developer" ||
    identity.key_id !== "developer"
  ) {
    throw new Error(
      "directory bundles require immutable developer identity",
    );
  }
  requireCommit(identity.source_commit, "stage source commit");
  if (
    typeof identity.app_version !== "string" ||
    !VERSION_PATTERN.test(identity.app_version)
  ) {
    throw new Error("stage app version is invalid");
  }
  if (
    identity.platform !== platform ||
    identity.architecture !== architecture
  ) {
    throw new Error(
      "stage identity does not match the current build host",
    );
  }
}

function requireExpectedPath(actual, expected, label) {
  if (!isAbsolute(actual) || resolve(actual) !== expected) {
    throw new Error(`${label} path mismatch`);
  }
}

async function verifyStageReceipt(
  stageReceiptPath,
  desktopRoot,
  platform,
  architecture,
) {
  const receiptRecord = await readJsonFile(
    stageReceiptPath,
    "stage receipt",
    MAX_CONTROL_BYTES,
    true,
  );
  const receipt = receiptRecord.value;
  requireExactKeys(receipt, STAGE_RECEIPT_KEYS, "stage receipt");
  if (receipt.schema !== STAGE_RECEIPT_SCHEMA) {
    throw new Error("stage receipt schema mismatch");
  }
  validateDeveloperIdentity(receipt, platform, architecture);
  requireSha256(receipt.manifest_sha256, "manifest digest");
  requireSha256(receipt.signature_sha256, "signature digest");
  requireSha256(receipt.public_key_sha256, "public key digest");
  requireSha256(receipt.sbom_sha256, "SBOM digest");
  requireExactKeys(
    receipt.input_receipt_sha256,
    new Set(["desktop", "notices", "sbom", "sidecar", "web"]),
    "stage input receipt digests",
  );
  for (const [name, digest] of Object.entries(
    receipt.input_receipt_sha256,
  )) {
    requireSha256(digest, `${name} input receipt digest`);
  }

  const inventory = await inventoryDirectory(
    receipt.resource_root,
    "staged resource root",
  );
  const root = inventory.root;
  const manifestPath = join(root, MANIFEST_NAME);
  const signaturePath = join(root, SIGNATURE_NAME);
  const publicKeyPath = join(root, PUBLIC_KEY_NAME);
  requireExpectedPath(
    receipt.resource_root,
    root,
    "resource root",
  );
  requireExpectedPath(
    receipt.manifest_path,
    manifestPath,
    "manifest",
  );
  requireExpectedPath(
    receipt.signature_path,
    signaturePath,
    "signature",
  );
  requireExpectedPath(
    receipt.public_key_path,
    publicKeyPath,
    "public key",
  );

  const manifestRecord = await readJsonFile(
    manifestPath,
    "resource manifest",
    MAX_MANIFEST_BYTES,
    true,
  );
  const signature = await inspectRegularFile(
    signaturePath,
    "resource signature",
    MAX_SIGNATURE_BYTES,
    true,
  );
  const publicKey = await inspectRegularFile(
    publicKeyPath,
    "developer public key",
    MAX_PUBLIC_KEY_BYTES,
    true,
  );
  if (
    manifestRecord.sha256 !== receipt.manifest_sha256 ||
    signature.sha256 !== receipt.signature_sha256 ||
    publicKey.sha256 !== receipt.public_key_sha256
  ) {
    throw new Error("stage receipt artifact digest mismatch");
  }
  if (signature.size !== 64) {
    throw new Error("developer resource signature size mismatch");
  }
  let verified = false;
  try {
    verified = verifySignature(
      null,
      manifestRecord.bytes,
      createPublicKey(publicKey.bytes),
      signature.bytes,
    );
  } catch {
    verified = false;
  }
  if (!verified) {
    throw new Error("developer resource signature mismatch");
  }

  const manifest = manifestRecord.value;
  if (
    manifest === null ||
    Array.isArray(manifest) ||
    typeof manifest !== "object" ||
    manifest.schema !== RESOURCE_SCHEMA ||
    manifest.build_mode !== "developer" ||
    manifest.key_id !== "developer"
  ) {
    throw new Error("developer resource manifest identity mismatch");
  }
  requireExactKeys(
    manifest,
    new Set([
      "app_version",
      "architecture",
      "build_mode",
      "desktop_npm_lock_sha256",
      "files",
      "key_id",
      "platform",
      "python_lock_sha256",
      "sbom_sha256",
      "schema",
      "source_commit",
      "web_npm_lock_sha256",
    ]),
    "resource manifest",
  );
  for (const name of [
    "app_version",
    "architecture",
    "build_mode",
    "key_id",
    "platform",
    "sbom_sha256",
    "source_commit",
  ]) {
    if (manifest[name] !== receipt[name]) {
      throw new Error(`stage manifest ${name} mismatch`);
    }
  }
  requireCommit(manifest.source_commit, "manifest source commit");
  for (const name of [
    "desktop_npm_lock_sha256",
    "python_lock_sha256",
    "sbom_sha256",
    "web_npm_lock_sha256",
  ]) {
    requireSha256(manifest[name], `${name} identity`);
  }
  if (
    manifest.files === null ||
    Array.isArray(manifest.files) ||
    typeof manifest.files !== "object"
  ) {
    throw new Error("resource manifest files are invalid");
  }
  const payloadInventory = new Map(inventory.files);
  payloadInventory.delete(MANIFEST_NAME);
  payloadInventory.delete(SIGNATURE_NAME);
  const manifestNames = Object.keys(manifest.files).sort(compareStrings);
  const payloadNames = [...payloadInventory.keys()].sort(compareStrings);
  if (
    manifestNames.length !== payloadNames.length ||
    manifestNames.some(
      (name, index) =>
        name !== payloadNames[index],
    )
  ) {
    throw new Error("staged resource inventory mismatch");
  }
  for (const name of manifestNames) {
    const declared = manifest.files[name];
    const actual = payloadInventory.get(name);
    if (
      declared === null ||
      Array.isArray(declared) ||
      typeof declared !== "object" ||
      Object.keys(declared).sort().join(",") !== "sha256,size" ||
      !Number.isSafeInteger(declared.size) ||
      declared.size <= 0 ||
      requireSha256(declared.sha256, `${name} digest`) !==
        actual.sha256 ||
      declared.size !== actual.size
    ) {
      throw new Error(`staged resource ${name} mismatch`);
    }
  }

  const lock = await inspectRegularFile(
    join(desktopRoot, "package-lock.json"),
    "desktop package lock",
  );
  if (lock.sha256 !== manifest.desktop_npm_lock_sha256) {
    throw new Error("desktop package lock identity mismatch");
  }
  return {
    inventory: inventory.files,
    manifest,
    manifestRecord,
    publicKey,
    receipt,
    receiptRecord,
    root,
    signature,
  };
}

async function requireAbsent(pathValue, label) {
  try {
    await lstat(pathValue);
  } catch (error) {
    if (error.code === "ENOENT") {
      return;
    }
    throw error;
  }
  throw new Error(`${label} must not already exist`);
}

function assertOutputBoundaries(
  repositoryRoot,
  stageRoot,
  outputRoot,
  receiptPath,
) {
  for (const [pathValue, label] of [
    [outputRoot, "directory output"],
    [receiptPath, "build receipt"],
  ]) {
    if (
      pathValue === parse(pathValue).root ||
      pathValue === repositoryRoot ||
      isContained(repositoryRoot, pathValue) ||
      pathValue === stageRoot ||
      isContained(stageRoot, pathValue)
    ) {
      throw new Error(`${label} path is unsafe`);
    }
  }
  if (
    receiptPath === outputRoot ||
    isContained(outputRoot, receiptPath)
  ) {
    throw new Error(
      "build receipt must be outside the directory output",
    );
  }
}

async function validateToolchain(desktopRoot) {
  const packageRecord = await readJsonFile(
    join(desktopRoot, "package.json"),
    "desktop package metadata",
    MAX_CONTROL_BYTES,
  );
  const lockRecord = await readJsonFile(
    join(desktopRoot, "package-lock.json"),
    "desktop package lock",
    16 * 1024 * 1024,
  );
  const builderRecord = await readJsonFile(
    join(
      desktopRoot,
      "node_modules",
      "electron-builder",
      "package.json",
    ),
    "installed electron-builder metadata",
    MAX_CONTROL_BYTES,
  );
  const electronRecord = await readJsonFile(
    join(
      desktopRoot,
      "node_modules",
      "electron",
      "package.json",
    ),
    "installed Electron metadata",
    MAX_CONTROL_BYTES,
  );
  if (
    packageRecord.value.version === undefined ||
    packageRecord.value.devDependencies?.electron !==
      ELECTRON_VERSION ||
    packageRecord.value.devDependencies?.["electron-builder"] !==
      ELECTRON_BUILDER_VERSION ||
    lockRecord.value.packages?.[""]?.devDependencies?.[
      "electron-builder"
    ] !== ELECTRON_BUILDER_VERSION ||
    lockRecord.value.packages?.[
      "node_modules/electron-builder"
    ]?.version !== ELECTRON_BUILDER_VERSION ||
    builderRecord.value.version !== ELECTRON_BUILDER_VERSION ||
    electronRecord.value.version !== ELECTRON_VERSION
  ) {
    throw new Error("desktop directory toolchain pin mismatch");
  }
  if (
    !canonicalJsonBytes(
      packageRecord.value.dependencies ?? {},
    ).equals(
      canonicalJsonBytes(
        lockRecord.value.packages?.[""]?.dependencies ?? {},
      ),
    )
  ) {
    throw new Error("desktop production dependency lock mismatch");
  }
  return {
    packageMetadata: packageRecord.value,
    lockMetadata: lockRecord.value,
    electronBuilderVersion: builderRecord.value.version,
    electronVersion: electronRecord.value.version,
  };
}

async function validateDeveloperConfig(configPath) {
  const config = await readJsonFile(
    configPath,
    "developer builder config",
    MAX_CONTROL_BYTES,
    true,
  );
  if (
    !canonicalJsonBytes(config.value).equals(
      canonicalJsonBytes(EXPECTED_DEVELOPER_CONFIG),
    )
  ) {
    throw new Error(
      "developer builder config permits an unreviewed target",
    );
  }
  return config;
}

export function builderArgumentsForPlatform(
  platform,
  architecture,
  effectiveConfigPath,
  appSource,
) {
  const platformArgument = {
    darwin: "--mac",
    linux: "--linux",
    win32: "--win",
  }[platform];
  const architectureArgument = {
    arm64: "--arm64",
    x64: "--x64",
  }[architecture];
  if (
    platformArgument === undefined ||
    architectureArgument === undefined
  ) {
    throw new Error("current desktop build host is unsupported");
  }
  return [
    "--config",
    effectiveConfigPath,
    "--projectDir",
    appSource,
    "--dir",
    platformArgument,
    architectureArgument,
  ];
}

async function copyInventory(sourceRoot, destinationRoot, inventory) {
  await mkdir(destinationRoot, { recursive: true });
  for (const [relativePath, expected] of [...inventory].sort(
    ([left], [right]) => compareStrings(left, right),
  )) {
    const destination = join(
      destinationRoot,
      ...relativePath.split("/"),
    );
    await mkdir(dirname(destination), { recursive: true });
    await copyFile(
      join(sourceRoot, ...relativePath.split("/")),
      destination,
      fsConstants.COPYFILE_EXCL,
    );
    const copied = await inspectRegularFile(
      destination,
      `copied ${relativePath}`,
    );
    if (
      copied.size !== expected.size ||
      copied.sha256 !== expected.sha256
    ) {
      throw new Error(`copied ${relativePath} identity mismatch`);
    }
  }
}

async function copyProductionDependencies(
  desktopRoot,
  appSource,
  packageMetadata,
  lockMetadata,
) {
  const declaredDependencies = Object.keys(
    packageMetadata.dependencies ?? {},
  ).sort(compareStrings);
  if (declaredDependencies.length === 0) {
    throw new Error("desktop production dependency closure is empty");
  }
  const packageEntries = Object.entries(
    lockMetadata.packages ?? {},
  )
    .filter(
      ([pathValue, metadata]) =>
        pathValue.startsWith("node_modules/") &&
        metadata !== null &&
        typeof metadata === "object" &&
        metadata.dev !== true &&
        metadata.link !== true,
    )
    .sort(([left], [right]) => {
      const depth = left.split("/node_modules/").length -
        right.split("/node_modules/").length;
      return depth === 0
        ? compareStrings(left, right)
        : depth;
    });
  const selected = [];
  for (const [pathValue, metadata] of packageEntries) {
    if (
      selected.some(
        ([ancestor]) =>
          pathValue.startsWith(`${ancestor}/node_modules/`),
      )
    ) {
      continue;
    }
    const source = join(desktopRoot, ...pathValue.split("/"));
    let inventory;
    try {
      inventory = await inventoryDirectory(
        source,
        `production dependency ${pathValue}`,
        { omitDependencyBins: true },
      );
    } catch (error) {
      if (metadata.optional === true && error.code === "ENOENT") {
        continue;
      }
      throw error;
    }
    selected.push([pathValue, inventory]);
  }
  const selectedNames = new Set(
    selected.map(([pathValue]) => pathValue),
  );
  for (const name of declaredDependencies) {
    if (!selectedNames.has(`node_modules/${name}`)) {
      throw new Error(
        `production dependency ${name} is not lock-qualified`,
      );
    }
  }
  for (const [pathValue, inventory] of selected) {
    await copyInventory(
      inventory.root,
      join(appSource, ...pathValue.split("/")),
      inventory.files,
    );
  }
  const copied = await inventoryDirectory(
    join(appSource, "node_modules"),
    "packaged production dependency source",
  );
  return {
    count: selected.length,
    inventory: copied.files,
  };
}

function packagedBuildMetadata(manifest) {
  return {
    schema: "kestrel.desktop.packaged-build.v1",
    build_mode: "developer",
    key_id: "developer",
    source_commit: manifest.source_commit,
    app_version: manifest.app_version,
    platform: manifest.platform,
    architecture: manifest.architecture,
    resource_root_relative: "kestrel",
    smoke_authority: "developer_directory_smoke_v1",
    python_lock_sha256: manifest.python_lock_sha256,
    desktop_npm_lock_sha256: manifest.desktop_npm_lock_sha256,
    web_npm_lock_sha256: manifest.web_npm_lock_sha256,
    sbom_sha256: manifest.sbom_sha256,
  };
}

async function writeExclusive(pathValue, bytes, mode = 0o644) {
  await mkdir(dirname(pathValue), { recursive: true });
  const handle = await open(
    pathValue,
    fsConstants.O_WRONLY |
      fsConstants.O_CREAT |
      fsConstants.O_EXCL,
    mode,
  );
  try {
    await handle.writeFile(bytes);
    await handle.sync();
  } finally {
    await handle.close();
  }
}

function safeBuilderEnvironment() {
  const environment = {
    LANG: "C",
    LC_ALL: "C",
    PATH: process.env.PATH ?? "/usr/bin:/bin",
    CSC_IDENTITY_AUTO_DISCOVERY: "false",
  };
  for (const name of [
    "HOME",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "WINDIR",
  ]) {
    const value = process.env[name];
    if (
      typeof value === "string" &&
      value.length > 0 &&
      !value.includes("\0")
    ) {
      environment[name] = value;
    }
  }
  return environment;
}

function executeBuilderProcess(invocation) {
  const completed = spawnSync(
    process.execPath,
    [invocation.builderCliPath, ...invocation.arguments],
    {
      cwd: invocation.cwd,
      env: invocation.environment,
      encoding: "utf8",
      maxBuffer: 1024 * 1024,
      windowsHide: true,
    },
  );
  if (completed.error) {
    throw new Error(
      `electron-builder failed to start: ${completed.error.message}`,
    );
  }
  if (completed.status !== 0) {
    const detail = (completed.stderr || completed.stdout || "")
      .trim()
      .slice(0, 4096);
    throw new Error(`electron-builder directory build failed: ${detail}`);
  }
}

async function locateApplicationRoot(
  outputRoot,
  platform,
) {
  const candidates = [];
  async function walk(directory, depth) {
    if (depth > 4) {
      return;
    }
    const entries = await readdir(directory, { withFileTypes: true });
    for (const entry of entries) {
      if (!entry.isDirectory() || entry.isSymbolicLink()) {
        continue;
      }
      const candidate = join(directory, entry.name);
      const resourceRoot =
        platform === "darwin"
          ? join(
              candidate,
              "Contents",
              "Resources",
              "kestrel",
            )
          : join(candidate, "resources", "kestrel");
      try {
        const manifest = await lstat(
          join(resourceRoot, MANIFEST_NAME),
        );
        if (
          manifest.isFile() &&
          !manifest.isSymbolicLink() &&
          (platform !== "darwin" || entry.name.endsWith(".app"))
        ) {
          candidates.push({ applicationRoot: candidate, resourceRoot });
        }
      } catch (error) {
        if (error.code !== "ENOENT") {
          throw error;
        }
      }
      await walk(candidate, depth + 1);
    }
  }
  await walk(outputRoot, 0);
  if (candidates.length !== 1) {
    throw new Error("directory build application root is ambiguous");
  }
  const applicationRoot = await qualifyDirectory(
    candidates[0].applicationRoot,
    "packaged application root",
  );
  const resourceRoot = await qualifyDirectory(
    candidates[0].resourceRoot,
    "packaged resource root",
  );
  if (
    !isContained(outputRoot, applicationRoot) ||
    !isContained(applicationRoot, resourceRoot)
  ) {
    throw new Error("packaged application layout escapes output");
  }
  return { applicationRoot, resourceRoot };
}

async function locateApplicationExecutable(
  applicationRoot,
  platform,
  packageName,
  productName,
) {
  const executableName =
    platform === "darwin"
      ? productName
      : platform === "win32"
        ? `${productName}.exe`
        : packageName;
  const executablePath =
    platform === "darwin"
      ? join(
          applicationRoot,
          "Contents",
          "MacOS",
          executableName,
        )
      : join(applicationRoot, executableName);
  if (
    !isContained(applicationRoot, executablePath) ||
    resolve(executablePath) !== executablePath
  ) {
    throw new Error("packaged executable escapes application root");
  }
  const executableParent = await qualifyDirectory(
    dirname(executablePath),
    "packaged application executable parent",
  );
  if (!isContained(applicationRoot, executableParent)) {
    throw new Error("packaged executable parent escapes application root");
  }
  if (
    (await realpath(executablePath)) !== executablePath
  ) {
    throw new Error("packaged executable path is not canonical");
  }
  const executable = await inspectRegularFile(
    executablePath,
    "packaged application executable",
    null,
  );
  const metadata = await lstat(executablePath);
  if (
    platform !== "win32" &&
    (metadata.mode & 0o111) === 0
  ) {
    throw new Error("packaged application executable is not executable");
  }
  if (
    (await realpath(executableParent)) !== executableParent ||
    (await realpath(executablePath)) !== executablePath
  ) {
    throw new Error("packaged executable path changed");
  }
  return {
    path: executablePath,
    sha256: executable.sha256,
    size: executable.size,
  };
}

function parseArguments(argumentsValue) {
  if (argumentsValue.length % 2 !== 0) {
    throw new Error("every build-dir option requires one value");
  }
  const parsed = {};
  for (let index = 0; index < argumentsValue.length; index += 2) {
    const name = argumentsValue[index];
    const value = argumentsValue[index + 1];
    if (!ARGUMENT_NAMES.has(name)) {
      throw new Error(`unknown build-dir option: ${name}`);
    }
    if (Object.hasOwn(parsed, name)) {
      throw new Error(`duplicate build-dir option: ${name}`);
    }
    if (typeof value !== "string" || value.length === 0) {
      throw new Error(`missing build-dir value for ${name}`);
    }
    parsed[name] = value;
  }
  for (const name of ARGUMENT_NAMES) {
    if (!Object.hasOwn(parsed, name)) {
      throw new Error(`missing required build-dir option: ${name}`);
    }
  }
  return {
    stageReceiptPath: parsed["--stage-receipt"],
    outputRoot: parsed["--output"],
    receiptPath: parsed["--receipt"],
  };
}

export async function buildDeveloperDirectory(
  input,
  runtime = {},
) {
  const repositoryRoot = await qualifyDirectory(
    runtime.repositoryRoot ?? DEFAULT_REPOSITORY_ROOT,
    "source repository root",
  );
  const desktopRoot = await qualifyDirectory(
    runtime.desktopRoot ?? DEFAULT_DESKTOP_ROOT,
    "desktop source root",
  );
  if (!isContained(repositoryRoot, desktopRoot)) {
    throw new Error("desktop source root escapes repository");
  }
  const stageReceiptPath = resolve(input.stageReceiptPath);
  const outputRoot = resolve(input.outputRoot);
  const receiptPath = resolve(input.receiptPath);
  validateSafeName(
    stageReceiptPath.split(sep).at(-1) ?? "",
    "stage receipt",
  );
  validateSafeName(
    outputRoot.split(sep).at(-1) ?? "",
    "directory output",
  );
  validateSafeName(
    receiptPath.split(sep).at(-1) ?? "",
    "build receipt",
  );
  await qualifyDirectory(
    dirname(stageReceiptPath),
    "stage receipt parent",
  );
  await qualifyDirectory(
    dirname(outputRoot),
    "directory output parent",
  );
  await qualifyDirectory(
    dirname(receiptPath),
    "build receipt parent",
  );
  if ((await realpath(stageReceiptPath)) !== stageReceiptPath) {
    throw new Error("stage receipt path must be canonical");
  }
  await requireAbsent(outputRoot, "directory output");
  await requireAbsent(receiptPath, "build receipt");

  const toolchain = await validateToolchain(desktopRoot);
  const configPath = join(
    desktopRoot,
    CONFIG_NAME,
  );
  const config = await validateDeveloperConfig(configPath);
  const stage = await verifyStageReceipt(
    stageReceiptPath,
    desktopRoot,
    process.platform,
    process.arch,
  );
  assertOutputBoundaries(
    repositoryRoot,
    stage.root,
    outputRoot,
    receiptPath,
  );
  verifyCleanExactSource(
    repositoryRoot,
    stage.receipt.source_commit,
  );
  if (toolchain.packageMetadata.version !== stage.receipt.app_version) {
    throw new Error("desktop package app version mismatch");
  }

  const dist = await inventoryDirectory(
    join(desktopRoot, "dist"),
    "desktop compiled application",
  );
  if (!dist.files.has("main.js")) {
    throw new Error("desktop compiled application is incomplete");
  }
  const builderCliPath = join(
    desktopRoot,
    "node_modules",
    "electron-builder",
    "out",
    "cli",
    "cli.js",
  );
  await inspectRegularFile(
    builderCliPath,
    "electron-builder CLI",
    1024 * 1024,
  );

  const temporaryRoot = await realpath(
    await mkdtemp(
      join(tmpdir(), "kestrel-desktop-directory-"),
    ),
  );
  const appSource = join(temporaryRoot, "app");
  let outputCreated = false;
  try {
    await mkdir(appSource);
    await copyInventory(
      dist.root,
      join(appSource, "dist"),
      dist.files,
    );
    const productionDependencies =
      await copyProductionDependencies(
        desktopRoot,
        appSource,
        toolchain.packageMetadata,
        toolchain.lockMetadata,
      );
    const packagedPackage = {
      name: toolchain.packageMetadata.name,
      version: stage.receipt.app_version,
      private: true,
      type: "module",
      main: "dist/main.js",
      dependencies: toolchain.packageMetadata.dependencies ?? {},
      kestrelDesktopBuild: packagedBuildMetadata(stage.manifest),
    };
    const packagedPackageBytes = canonicalJsonBytes(packagedPackage);
    const packagePath = join(appSource, "package.json");
    await writeExclusive(packagePath, packagedPackageBytes);
    const publicKeyPath = join(
      appSource,
      "config",
      PUBLIC_KEY_NAME,
    );
    await mkdir(dirname(publicKeyPath), { recursive: true });
    await copyFile(
      stage.receipt.public_key_path,
      publicKeyPath,
      fsConstants.COPYFILE_EXCL,
    );
    const copiedPublicKey = await inspectRegularFile(
      publicKeyPath,
      "packaged developer public key source",
      MAX_PUBLIC_KEY_BYTES,
    );
    if (
      copiedPublicKey.sha256 !== stage.publicKey.sha256 ||
      copiedPublicKey.size !== stage.publicKey.size
    ) {
      throw new Error("packaged developer public key source mismatch");
    }
    const effectiveConfig = {
      ...config.value,
      directories: {
        output: outputRoot,
      },
      extraResources: [
        {
          from: stage.root,
          to: "kestrel",
        },
      ],
    };
    const effectiveConfigBytes = canonicalJsonBytes(
      effectiveConfig,
    );
    const effectiveConfigPath = join(
      temporaryRoot,
      "electron-builder.effective.json",
    );
    await writeExclusive(
      effectiveConfigPath,
      effectiveConfigBytes,
    );

    const invocation = {
      arguments: builderArgumentsForPlatform(
        process.platform,
        process.arch,
        effectiveConfigPath,
        appSource,
      ),
      appSource,
      builderCliPath,
      cwd: desktopRoot,
      effectiveConfigPath,
      environment: safeBuilderEnvironment(),
      outputRoot,
      resourceRoot: stage.root,
    };
    await (
      runtime.executeBuilder ??
      ((candidate) => executeBuilderProcess(candidate))
    )(invocation);
    outputCreated = true;

    const layout = await locateApplicationRoot(
      await qualifyDirectory(
        outputRoot,
        "directory builder output",
      ),
      process.platform,
    );
    const packagedExecutable = await locateApplicationExecutable(
      layout.applicationRoot,
      process.platform,
      toolchain.packageMetadata.name,
      config.value.productName,
    );
    const packagedResources = await inventoryDirectory(
      layout.resourceRoot,
      "packaged staged resources",
    );
    if (!inventoriesMatch(stage.inventory, packagedResources.files)) {
      throw new Error("packaged staged resource identity mismatch");
    }
    const requestedAppRoot =
      process.platform === "darwin"
        ? join(
            layout.applicationRoot,
            "Contents",
            "Resources",
            "app",
          )
        : join(layout.applicationRoot, "resources", "app");
    const appRoot = await qualifyDirectory(
      requestedAppRoot,
      "packaged application source root",
    );
    if (!isContained(layout.applicationRoot, appRoot)) {
      throw new Error("packaged application source escapes output");
    }
    const generatedDependencyRoot = await qualifyDirectory(
      join(appRoot, "node_modules"),
      "generated packaged dependencies",
    );
    if (!isContained(appRoot, generatedDependencyRoot)) {
      throw new Error("generated packaged dependencies escape app root");
    }
    await inventoryDirectory(
      generatedDependencyRoot,
      "generated packaged dependencies before replacement",
      { omitDependencyBins: true },
    );
    await rm(generatedDependencyRoot, {
      recursive: true,
      force: false,
    });
    await copyInventory(
      join(appSource, "node_modules"),
      generatedDependencyRoot,
      productionDependencies.inventory,
    );
    const restoredDependencies = await inventoryDirectory(
      generatedDependencyRoot,
      "restored packaged production dependencies",
    );
    if (
      !inventoriesMatch(
        productionDependencies.inventory,
        restoredDependencies.files,
      )
    ) {
      throw new Error(
        "restored production dependency closure mismatch:" +
          inventoryDifference(
            productionDependencies.inventory,
            restoredDependencies.files,
          ),
      );
    }
    const packagedPackagePath = join(appRoot, "package.json");
    const packagedDistPath = join(appRoot, "dist");
    const packagedPublicKeyPath = join(
      appRoot,
      "config",
      PUBLIC_KEY_NAME,
    );
    const packagedPackageRecord = await inspectRegularFile(
      packagedPackagePath,
      "packaged package metadata",
      MAX_CONTROL_BYTES,
      true,
    );
    const packagedPublicKeyRecord = await inspectRegularFile(
      packagedPublicKeyPath,
      "packaged developer public key",
      MAX_PUBLIC_KEY_BYTES,
    );
    if (
      !packagedPackageRecord.bytes.equals(packagedPackageBytes) ||
      packagedPublicKeyRecord.sha256 !== stage.publicKey.sha256 ||
      packagedPublicKeyRecord.size !== stage.publicKey.size
    ) {
      throw new Error("packaged app identity mismatch");
    }
    const packagedDependencies = await inventoryDirectory(
      join(appRoot, "node_modules"),
      "packaged production dependencies",
    );
    if (
      !inventoriesMatch(
        productionDependencies.inventory,
        packagedDependencies.files,
      )
    ) {
      throw new Error(
        "packaged production dependency closure mismatch:" +
          inventoryDifference(
            productionDependencies.inventory,
            packagedDependencies.files,
          ),
      );
    }
    const packagedDist = await inventoryDirectory(
      packagedDistPath,
      "packaged compiled application",
    );
    if (
      !packagedDist.files.has("main.js") ||
      !inventoriesMatch(dist.files, packagedDist.files)
    ) {
      throw new Error(
        "packaged compiled application identity mismatch:" +
          inventoryDifference(dist.files, packagedDist.files),
      );
    }
    const packagedDistEvidence =
      inventoryEvidence(packagedDist.files);
    const packagedManifestPath = join(
      layout.resourceRoot,
      MANIFEST_NAME,
    );
    const packagedSignaturePath = join(
      layout.resourceRoot,
      SIGNATURE_NAME,
    );
    const packagedManifest = await inspectRegularFile(
      packagedManifestPath,
      "packaged resource manifest",
      MAX_MANIFEST_BYTES,
    );
    const packagedSignature = await inspectRegularFile(
      packagedSignaturePath,
      "packaged resource signature",
      MAX_SIGNATURE_BYTES,
    );
    if (
      packagedManifest.sha256 !== stage.manifestRecord.sha256 ||
      packagedSignature.sha256 !== stage.signature.sha256
    ) {
      throw new Error("packaged manifest or signature mismatch");
    }

    const receipt = {
      schema: BUILD_RECEIPT_SCHEMA,
      build_mode: "developer",
      key_id: "developer",
      signed: false,
      publishable: false,
      directory_only: true,
      source_commit: stage.receipt.source_commit,
      app_name: "Kestrel Developer",
      app_version: stage.receipt.app_version,
      platform: stage.receipt.platform,
      architecture: stage.receipt.architecture,
      electron_version: toolchain.electronVersion,
      electron_builder_version:
        toolchain.electronBuilderVersion,
      production_dependency_count:
        productionDependencies.count,
      builder_config_sha256: config.sha256,
      effective_builder_config_sha256:
        sha256Bytes(effectiveConfigBytes),
      stage_receipt_path: stageReceiptPath,
      stage_receipt_sha256: stage.receiptRecord.sha256,
      application_root: layout.applicationRoot,
      resource_root: layout.resourceRoot,
      executable_path: packagedExecutable.path,
      executable_sha256: packagedExecutable.sha256,
      executable_size: packagedExecutable.size,
      packaged_package_json_path: packagedPackagePath,
      packaged_package_json_sha256:
        packagedPackageRecord.sha256,
      packaged_dist_path: packagedDist.root,
      packaged_dist_inventory_sha256:
        packagedDistEvidence.sha256,
      packaged_dist_file_count:
        packagedDistEvidence.fileCount,
      packaged_dist_total_bytes:
        packagedDistEvidence.totalBytes,
      packaged_public_key_path: packagedPublicKeyPath,
      packaged_public_key_sha256:
        packagedPublicKeyRecord.sha256,
      manifest_path: packagedManifestPath,
      manifest_sha256: packagedManifest.sha256,
      signature_path: packagedSignaturePath,
      signature_sha256: packagedSignature.sha256,
    };
    const receiptBytes = canonicalJsonBytes(receipt);
    if (receiptBytes.length >= MAX_CONTROL_BYTES) {
      throw new Error("directory build receipt exceeds 64 KiB");
    }
    await writeExclusive(receiptPath, receiptBytes);
    return receipt;
  } catch (error) {
    if (outputCreated) {
      await rm(outputRoot, { recursive: true, force: true });
    } else {
      try {
        await lstat(outputRoot);
        await rm(outputRoot, { recursive: true, force: true });
      } catch (cleanupError) {
        if (cleanupError.code !== "ENOENT") {
          error.addNote?.(
            `directory output cleanup failed: ${cleanupError.message}`,
          );
        }
      }
    }
    throw error;
  } finally {
    await rm(temporaryRoot, { recursive: true, force: true });
  }
}

const invokedPath =
  process.argv[1] === undefined ? null : resolve(process.argv[1]);
if (invokedPath === fileURLToPath(import.meta.url)) {
  try {
    const receipt = await buildDeveloperDirectory(
      parseArguments(process.argv.slice(2)),
    );
    process.stdout.write(`${JSON.stringify(receipt)}\n`);
  } catch (error) {
    const message =
      error instanceof Error ? error.message : String(error);
    process.stderr.write(`build-dir: ${message}\n`);
    process.exitCode = 1;
  }
}
