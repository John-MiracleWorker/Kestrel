#!/usr/bin/env node

import {
  spawn,
  spawnSync,
} from "node:child_process";
import {
  createHash,
  createPublicKey,
  verify as verifySignature,
} from "node:crypto";
import {
  constants as fsConstants,
  chmod,
  lstat,
  mkdir,
  mkdtemp,
  open,
  readdir,
  realpath,
  rm,
  unlink,
} from "node:fs/promises";
import { createConnection } from "node:net";
import { tmpdir } from "node:os";
import {
  dirname,
  isAbsolute,
  join,
  relative,
  resolve,
  sep,
} from "node:path";
import { fileURLToPath } from "node:url";

const SCRIPT_DIRECTORY = dirname(fileURLToPath(import.meta.url));
const DEFAULT_REPOSITORY_ROOT = resolve(SCRIPT_DIRECTORY, "..", "..");
const BUILD_RECEIPT_SCHEMA =
  "kestrel.desktop.directory-build.v1";
const SMOKE_RECEIPT_SCHEMA =
  "kestrel.desktop.directory-smoke.v1";
const RESOURCE_SCHEMA = "kestrel.desktop.resources.v1";
const DIRECTORY_INVENTORY_SCHEMA =
  "kestrel.desktop.directory-inventory.v1";
const PACKAGED_BUILD_SCHEMA =
  "kestrel.desktop.packaged-build.v1";
const READY_SCHEMA =
  "kestrel.desktop.directory-smoke-ready.v1";
const CONTINUE_SCHEMA =
  "kestrel.desktop.directory-smoke-continue.v1";
const COMPLETED_SCHEMA =
  "kestrel.desktop.directory-smoke-completed.v1";
const SIDECAR_READINESS_SCHEMA =
  "kestrel.desktop.sidecar_readiness.v1";
const CONTROL_DIRECTORY = "directory-smoke-v1";
const DIRECTORY_SMOKE_ARGUMENT = "--kestrel-directory-smoke";
const MAX_BUILD_RECEIPT_BYTES = 64 * 1024;
const MAX_PACKAGE_BYTES = 64 * 1024;
const MAX_MANIFEST_BYTES = 1024 * 1024;
const MAX_SIGNATURE_BYTES = 4096;
const MAX_PUBLIC_KEY_BYTES = 16 * 1024;
const MAX_CONTROL_BYTES = 4 * 1024;
const MAX_SIDECAR_READINESS_BYTES = 16 * 1024;
const MAX_STDERR_BYTES = 4 * 1024;
const PROCESS_OUTPUT_BYTES = 4 * 1024 * 1024;
const MAX_LISTENER_OUTPUT_BYTES = 64 * 1024;
const MAX_INVENTORY_FILES = 100_000;
const MAX_INVENTORY_BYTES = 4 * 1024 * 1024 * 1024;
const READ_CHUNK_BYTES = 1024 * 1024;
const CYCLE_TIMEOUT_MS = 150_000;
const POLL_INTERVAL_MS = 25;
const SHA256_PATTERN = /^[0-9a-f]{64}$/;
const PREFIXED_SHA256_PATTERN = /^sha256:[0-9a-f]{64}$/;
const SOURCE_COMMIT_PATTERN = /^[0-9a-f]{40}$/;
const APP_VERSION_PATTERN =
  /^[0-9A-Za-z][0-9A-Za-z.+-]{0,63}$/;
const MEMORY_FILES = Object.freeze([
  "working.mv2",
  "episodic.mv2",
  "semantic.mv2",
  "procedural.mv2",
  "self.mv2",
  "policy.mv2",
]);
const RESOURCE_MANIFEST_NAME =
  "kestrel-resource-manifest.json";
const RESOURCE_SIGNATURE_NAME =
  "kestrel-resource-manifest.sig";
const RESOURCE_SBOM_NAME = "sbom.cdx.json";
const RESOURCE_MANIFEST_KEYS = new Set([
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
]);
const BUILD_RECEIPT_KEYS = new Set([
  "app_name",
  "app_version",
  "application_root",
  "architecture",
  "build_mode",
  "builder_config_sha256",
  "directory_only",
  "effective_builder_config_sha256",
  "electron_builder_version",
  "electron_version",
  "executable_path",
  "executable_sha256",
  "executable_size",
  "key_id",
  "manifest_path",
  "manifest_sha256",
  "packaged_package_json_path",
  "packaged_package_json_sha256",
  "packaged_dist_file_count",
  "packaged_dist_inventory_sha256",
  "packaged_dist_path",
  "packaged_dist_total_bytes",
  "packaged_public_key_path",
  "packaged_public_key_sha256",
  "platform",
  "production_dependency_count",
  "publishable",
  "resource_root",
  "schema",
  "signature_path",
  "signature_sha256",
  "signed",
  "source_commit",
  "stage_receipt_path",
  "stage_receipt_sha256",
]);
const SMOKE_RECEIPT_KEYS = new Set([
  "architecture",
  "authenticated_readiness",
  "authenticated_recovery",
  "authenticated_shutdown",
  "build_mode",
  "build_receipt_sha256",
  "captured_process_count",
  "cycle_count",
  "executable_sha256",
  "listeners_closed",
  "manifest_sha256",
  "memory_identity_reused",
  "memory_identity_sha256",
  "memory_layer_count",
  "mission_command_loaded",
  "native_keyring",
  "owner_data_removed",
  "packaged_dist_inventory_sha256",
  "platform",
  "processes_exited",
  "publishable",
  "qualified",
  "schema",
  "signed",
  "source_commit",
]);
const READY_KEYS = new Set([
  "authenticated_readiness",
  "authenticated_recovery",
  "build_mode",
  "hidden",
  "memory_files",
  "mission_command_url",
  "schema",
  "source_commit",
]);
const COMPLETED_KEYS = new Set([
  "authenticated_shutdown",
  "child_exited",
  "schema",
]);
const SIDECAR_READINESS_KEYS = new Set([
  "executable_digest",
  "launch_nonce_digest",
  "pid",
  "port",
  "process_birth_marker",
  "profile_id",
  "resource_manifest_digest",
  "schema",
  "sidecar_version",
]);
const SAFE_ENVIRONMENT_NAMES = Object.freeze([
  "HOME",
  "LANG",
  "LC_ALL",
  "PATH",
  "SYSTEMROOT",
  "TEMP",
  "TMP",
  "TMPDIR",
  "TZ",
  "WINDIR",
]);

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

export function canonicalSmokeJson(value) {
  function serialize(current) {
    if (current === null || typeof current !== "object") {
      const scalar = JSON.stringify(current);
      if (scalar === undefined) {
        throw new Error("directory_smoke_json_invalid");
      }
      return scalar;
    }
    if (Array.isArray(current)) {
      return `[${current
        .map((item) => serialize(item))
        .join(",")}]`;
    }
    return `{${Object.keys(current)
      .sort(compareStrings)
      .map(
        (key) =>
          `${JSON.stringify(key)}:${serialize(current[key])}`,
      )
      .join(",")}}`;
  }
  return Buffer.from(`${serialize(value)}\n`, "utf8");
}

function exactKeys(value, expected) {
  if (
    value === null ||
    Array.isArray(value) ||
    typeof value !== "object"
  ) {
    return false;
  }
  const actual = Object.keys(value).sort(compareStrings);
  const wanted = [...expected].sort(compareStrings);
  return (
    actual.length === wanted.length &&
    actual.every((name, index) => name === wanted[index])
  );
}

function parseCanonicalJson(bytes, maximumBytes, errorPrefix) {
  if (
    !(bytes instanceof Uint8Array) ||
    bytes.byteLength === 0 ||
    bytes.byteLength > maximumBytes
  ) {
    throw new Error(`${errorPrefix}_invalid`);
  }
  let value;
  try {
    value = JSON.parse(
      Buffer.from(bytes).toString("utf8"),
    );
  } catch {
    throw new Error(`${errorPrefix}_invalid`);
  }
  if (!canonicalSmokeJson(value).equals(Buffer.from(bytes))) {
    throw new Error(`${errorPrefix}_noncanonical`);
  }
  return value;
}

function isCanonicalAbsolutePath(value) {
  return (
    typeof value === "string" &&
    value.length > 1 &&
    value.length <= 4096 &&
    !value.includes("\0") &&
    isAbsolute(value) &&
    resolve(value) === value
  );
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

function validDigest(value) {
  return (
    typeof value === "string" &&
    SHA256_PATTERN.test(value)
  );
}

function validCount(value, minimum = 0) {
  return (
    Number.isSafeInteger(value) &&
    value >= minimum
  );
}

export function parseDirectoryBuildReceipt(
  bytes,
  host = {
    platform: process.platform,
    architecture: process.arch,
  },
) {
  const value = parseCanonicalJson(
    bytes,
    MAX_BUILD_RECEIPT_BYTES,
    "directory_smoke_build_receipt",
  );
  const pathNames = [
    "application_root",
    "resource_root",
    "executable_path",
    "stage_receipt_path",
    "packaged_package_json_path",
    "packaged_dist_path",
    "packaged_public_key_path",
    "manifest_path",
    "signature_path",
  ];
  const digestNames = [
    "builder_config_sha256",
    "effective_builder_config_sha256",
    "executable_sha256",
    "stage_receipt_sha256",
    "packaged_package_json_sha256",
    "packaged_dist_inventory_sha256",
    "packaged_public_key_sha256",
    "manifest_sha256",
    "signature_sha256",
  ];
  const applicationResourcesRoot =
    host.platform === "darwin"
      ? join(
          value.application_root,
          "Contents",
          "Resources",
        )
      : join(value.application_root, "resources");
  const expectedResourceRoot = join(
    applicationResourcesRoot,
    "kestrel",
  );
  const expectedExecutablePath =
    host.platform === "darwin"
      ? join(
          value.application_root,
          "Contents",
          "MacOS",
          "Kestrel Developer",
        )
      : host.platform === "win32"
        ? join(
            value.application_root,
            "Kestrel Developer.exe",
          )
        : join(value.application_root, "kestrel-desktop");
  const packagedApplicationRoot = join(
    applicationResourcesRoot,
    "app",
  );
  if (
    !exactKeys(value, BUILD_RECEIPT_KEYS) ||
    value.schema !== BUILD_RECEIPT_SCHEMA ||
    value.build_mode !== "developer" ||
    value.key_id !== "developer" ||
    value.signed !== false ||
    value.publishable !== false ||
    value.directory_only !== true ||
    value.platform !== host.platform ||
    value.architecture !== host.architecture ||
    value.app_name !== "Kestrel Developer" ||
    typeof value.app_version !== "string" ||
    !APP_VERSION_PATTERN.test(value.app_version) ||
    typeof value.electron_version !== "string" ||
    typeof value.electron_builder_version !== "string" ||
    !SOURCE_COMMIT_PATTERN.test(value.source_commit) ||
    !validCount(value.production_dependency_count) ||
    !validCount(value.executable_size, 1) ||
    !validCount(value.packaged_dist_file_count, 1) ||
    !validCount(value.packaged_dist_total_bytes, 1) ||
    pathNames.some(
      (name) => !isCanonicalAbsolutePath(value[name]),
    ) ||
    digestNames.some((name) => !validDigest(value[name])) ||
    value.resource_root !== expectedResourceRoot ||
    value.executable_path !== expectedExecutablePath ||
    value.packaged_package_json_path !==
      join(packagedApplicationRoot, "package.json") ||
    value.packaged_dist_path !==
      join(packagedApplicationRoot, "dist") ||
    value.packaged_public_key_path !==
      join(
        packagedApplicationRoot,
        "config",
        "desktop-developer-public-key.pem",
      ) ||
    value.manifest_path !==
      join(expectedResourceRoot, RESOURCE_MANIFEST_NAME) ||
    value.signature_path !==
      join(expectedResourceRoot, RESOURCE_SIGNATURE_NAME) ||
    !isContained(
      value.application_root,
      value.resource_root,
    ) ||
    !isContained(
      value.application_root,
      value.executable_path,
    ) ||
    !isContained(
      value.application_root,
      value.packaged_package_json_path,
    ) ||
    !isContained(
      value.application_root,
      value.packaged_dist_path,
    ) ||
    !isContained(
      value.application_root,
      value.packaged_public_key_path,
    ) ||
    !isContained(value.resource_root, value.manifest_path) ||
    !isContained(value.resource_root, value.signature_path)
  ) {
    throw new Error("directory_smoke_build_receipt_invalid");
  }
  return value;
}

export function safeSmokeEnvironment(source = process.env) {
  const environment = {};
  for (const name of SAFE_ENVIRONMENT_NAMES) {
    const value = source[name];
    if (
      typeof value === "string" &&
      value.length > 0 &&
      value.length <= 4096 &&
      !/[\u0000-\u001f\u007f-\u009f]/.test(value)
    ) {
      environment[name] = value;
    }
  }
  return environment;
}

export function validateSmokePlatform(platform) {
  if (platform !== "darwin") {
    throw new Error("directory_smoke_platform_unsupported");
  }
  return platform;
}

export function validateDirectorySmokeReceipt(bytes) {
  const value = parseCanonicalJson(
    bytes,
    MAX_CONTROL_BYTES,
    "directory_smoke_receipt",
  );
  const trueClaims = [
    "qualified",
    "authenticated_readiness",
    "authenticated_recovery",
    "authenticated_shutdown",
    "mission_command_loaded",
    "memory_identity_reused",
    "processes_exited",
    "listeners_closed",
    "owner_data_removed",
  ];
  const digestNames = [
    "build_receipt_sha256",
    "executable_sha256",
    "manifest_sha256",
    "memory_identity_sha256",
    "packaged_dist_inventory_sha256",
  ];
  if (
    !exactKeys(value, SMOKE_RECEIPT_KEYS) ||
    value.schema !== SMOKE_RECEIPT_SCHEMA ||
    value.build_mode !== "developer" ||
    !SOURCE_COMMIT_PATTERN.test(value.source_commit) ||
    value.platform !== process.platform ||
    value.architecture !== process.arch ||
    value.cycle_count !== 2 ||
    value.memory_layer_count !== 6 ||
    !validCount(value.captured_process_count, 2) ||
    trueClaims.some((name) => value[name] !== true) ||
    value.signed !== false ||
    value.publishable !== false ||
    value.native_keyring !== false ||
    digestNames.some((name) => !validDigest(value[name]))
  ) {
    throw new Error("directory_smoke_receipt_invalid");
  }
  return value;
}

function sha256Bytes(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}

function sameIdentity(...entries) {
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
  maximumBytes,
  collect = false,
  minimumBytes = 1,
) {
  const before = await lstat(pathValue, { bigint: true });
  if (
    before.isSymbolicLink() ||
    !before.isFile() ||
    before.nlink !== 1n ||
    before.size < BigInt(minimumBytes) ||
    before.size > BigInt(maximumBytes) ||
    before.size > BigInt(Number.MAX_SAFE_INTEGER)
  ) {
    throw new Error("directory_smoke_file_untrusted");
  }
  const handle = await open(  // codeql[js/file-system-race] — O_NOFOLLOW + fstat identity check
    pathValue,
    fsConstants.O_RDONLY |
      (fsConstants.O_NOFOLLOW ?? 0) |
      (fsConstants.O_CLOEXEC ?? 0),
  );
  try {
    const opened = await handle.stat({ bigint: true });
    if (
      !opened.isFile() ||
      opened.nlink !== 1n ||
      !sameIdentity(before, opened)
    ) {
      throw new Error("directory_smoke_file_changed");
    }
    const size = Number(opened.size);
    const bytes = collect
      ? Buffer.allocUnsafe(size)
      : null;
    const scratch = collect
      ? null
      : Buffer.allocUnsafe(
          Math.min(Math.max(size, 1), READ_CHUNK_BYTES),
        );
    let position = 0;
    const digest = createHash("sha256");
    while (position < size) {
      const target = collect ? bytes : scratch;
      const targetOffset = collect ? position : 0;
      const length = collect
        ? size - position
        : Math.min(scratch.byteLength, size - position);
      const { bytesRead } = await handle.read(
        target,
        targetOffset,
        length,
        position,
      );
      if (bytesRead <= 0) {
        throw new Error("directory_smoke_file_changed");
      }
      digest.update(
        target.subarray(
          targetOffset,
          targetOffset + bytesRead,
        ),
      );
      position += bytesRead;
    }
    const openedAfter = await handle.stat({ bigint: true });
    const after = await lstat(pathValue, { bigint: true });
    if (
      after.isSymbolicLink() ||
      !after.isFile() ||
      after.nlink !== 1n ||
      !sameIdentity(before, opened, openedAfter, after)
    ) {
      throw new Error("directory_smoke_file_changed");
    }
    return {
      bytes,
      identity: {
        dev: Number(opened.dev),
        ino: Number(opened.ino),
      },
      sha256: digest.digest("hex"),
      size,
    };
  } finally {
    await handle.close();
  }
}

async function qualifyDirectory(
  pathValue,
  ownerOnly = false,
) {
  const requested = resolve(pathValue);
  const metadata = await lstat(requested);
  const owner = process.getuid?.();
  if (
    metadata.isSymbolicLink() ||
    !metadata.isDirectory() ||
    (ownerOnly &&
      (owner === undefined ||
        metadata.uid !== owner ||
        (metadata.mode & 0o777) !== 0o700))
  ) {
    throw new Error("directory_smoke_directory_untrusted");
  }
  const canonical = await realpath(requested);
  if (canonical !== requested) {
    throw new Error("directory_smoke_directory_untrusted");
  }
  return {
    identity: {
      dev: metadata.dev,
      ino: metadata.ino,
    },
    path: canonical,
  };
}

async function readCanonicalFile(
  pathValue,
  maximumBytes,
  ownerOnly = false,
) {
  const inspected = await inspectRegularFile(
    pathValue,
    maximumBytes,
    true,
  );
  if (ownerOnly && process.platform !== "win32") {
    const metadata = await lstat(pathValue);
    if (
      metadata.uid !== process.getuid?.() ||
      (metadata.mode & 0o777) !== 0o600
    ) {
      throw new Error("directory_smoke_file_untrusted");
    }
  }
  return {
    ...inspected,
    value: parseCanonicalJson(
      inspected.bytes,
      maximumBytes,
      "directory_smoke_control",
    ),
  };
}

export async function inspectCanonicalOwnerControl(
  pathValue,
  schema,
  keys,
  maximumBytes = MAX_CONTROL_BYTES,
) {
  const record = await readCanonicalFile(
    pathValue,
    maximumBytes,
    true,
  );
  if (
    !exactKeys(record.value, keys) ||
    record.value.schema !== schema
  ) {
    throw new Error("directory_smoke_control_invalid");
  }
  return record;
}

function normalizeResourceRelativePath(value) {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value.includes("\\") ||
    value.includes("\0") ||
    /[\u0000-\u001f\u007f]/.test(value) ||
    value.startsWith("/") ||
    value.endsWith("/")
  ) {
    throw new Error("directory_smoke_resource_untrusted");
  }
  const segments = value.split("/");
  if (
    segments.some(
      (segment) =>
        segment.length === 0 ||
        segment === "." ||
        segment === "..",
    )
  ) {
    throw new Error("directory_smoke_resource_untrusted");
  }
  return value;
}

function registerPortableResourcePath(paths, relativePath) {
  const segments = relativePath.split("/");
  for (
    let length = 1;
    length <= segments.length;
    length += 1
  ) {
    const prefix = segments.slice(0, length).join("/");
    const folded = prefix
      .normalize("NFKC")
      .toUpperCase()
      .toLowerCase();
    const previous = paths.get(folded);
    if (previous !== undefined && previous !== prefix) {
      throw new Error("directory_smoke_resource_untrusted");
    }
    paths.set(folded, prefix);
  }
}

async function inventoryManifestResources(root) {
  const canonicalRoot = (await qualifyDirectory(root)).path;
  const files = new Map();
  const portablePaths = new Map();
  let totalBytes = 0;

  async function walk(directory, segments) {
    const entries = await readdir(directory, {
      withFileTypes: true,
    });
    entries.sort((left, right) =>
      compareStrings(left.name, right.name),
    );
    for (const entry of entries) {
      const relativePath = normalizeResourceRelativePath(
        [...segments, entry.name].join("/"),
      );
      registerPortableResourcePath(
        portablePaths,
        relativePath,
      );
      const pathValue = join(directory, entry.name);
      const metadata = await lstat(pathValue);
      if (
        entry.isSymbolicLink() ||
        metadata.isSymbolicLink() ||
        (!entry.isDirectory() && !entry.isFile())
      ) {
        throw new Error("directory_smoke_resource_untrusted");
      }
      const canonical = await realpath(pathValue);
      if (!isContained(canonicalRoot, canonical)) {
        throw new Error("directory_smoke_resource_untrusted");
      }
      if (entry.isDirectory()) {
        await walk(pathValue, [...segments, entry.name]);
        continue;
      }
      if (metadata.nlink !== 1) {
        throw new Error("directory_smoke_resource_untrusted");
      }
      if (
        segments.length === 0 &&
        (relativePath === RESOURCE_MANIFEST_NAME ||
          relativePath === RESOURCE_SIGNATURE_NAME)
      ) {
        continue;
      }
      if (files.size >= MAX_INVENTORY_FILES) {
        throw new Error("directory_smoke_resource_untrusted");
      }
      const availableBytes =
        MAX_INVENTORY_BYTES - totalBytes;
      if (availableBytes < 0) {
        throw new Error("directory_smoke_resource_untrusted");
      }
      let inspected;
      try {
        inspected = await inspectRegularFile(
          pathValue,
          availableBytes,
          false,
          0,
        );
      } catch {
        throw new Error("directory_smoke_resource_untrusted");
      }
      totalBytes += inspected.size;
      if (totalBytes > MAX_INVENTORY_BYTES) {
        throw new Error("directory_smoke_resource_untrusted");
      }
      files.set(relativePath, {
        sha256: inspected.sha256,
        size: inspected.size,
      });
    }
  }

  await walk(canonicalRoot, []);
  return files;
}

function validateDeclaredResourceFiles(manifest) {
  if (
    manifest === null ||
    Array.isArray(manifest) ||
    typeof manifest !== "object" ||
    manifest.files === null ||
    Array.isArray(manifest.files) ||
    typeof manifest.files !== "object"
  ) {
    throw new Error("directory_smoke_manifest_invalid");
  }
  const names = Object.keys(manifest.files).sort(compareStrings);
  if (
    names.length === 0 ||
    names.length > MAX_INVENTORY_FILES
  ) {
    throw new Error("directory_smoke_manifest_invalid");
  }
  const portablePaths = new Map();
  for (const name of names) {
    normalizeResourceRelativePath(name);
    registerPortableResourcePath(portablePaths, name);
    const entry = manifest.files[name];
    if (
      !exactKeys(entry, new Set(["sha256", "size"])) ||
      !validDigest(entry.sha256) ||
      !validCount(entry.size)
    ) {
      throw new Error("directory_smoke_manifest_invalid");
    }
  }
  const sbom = manifest.files[RESOURCE_SBOM_NAME];
  if (
    sbom === undefined ||
    !validDigest(manifest.sbom_sha256) ||
    sbom.sha256 !== manifest.sbom_sha256
  ) {
    throw new Error("directory_smoke_manifest_invalid");
  }
  return names;
}

function assertResourceInventoryMatches(
  declared,
  declaredNames,
  actual,
) {
  const actualNames = [...actual.keys()].sort(compareStrings);
  if (
    actualNames.length !== declaredNames.length ||
    actualNames.some(
      (name, index) => name !== declaredNames[index],
    )
  ) {
    throw new Error(
      "directory_smoke_resource_coverage_mismatch",
    );
  }
  for (const name of declaredNames) {
    const expected = declared[name];
    const observed = actual.get(name);
    if (
      observed.size !== expected.size ||
      observed.sha256 !== expected.sha256
    ) {
      throw new Error(
        "directory_smoke_resource_digest_mismatch",
      );
    }
  }
}

export async function verifyManifestInventory(root, manifest) {
  const declaredNames = validateDeclaredResourceFiles(manifest);
  const first = await inventoryManifestResources(root);
  assertResourceInventoryMatches(
    manifest.files,
    declaredNames,
    first,
  );
  const final = await inventoryManifestResources(root);
  assertResourceInventoryMatches(
    manifest.files,
    declaredNames,
    final,
  );
  return manifest.files;
}

export function assertExecutableEvidence(
  receipt,
  executable,
  metadata,
) {
  if (
    executable?.sha256 !== receipt?.executable_sha256 ||
    executable?.size !== receipt?.executable_size ||
    metadata?.isSymbolicLink?.() !== false ||
    metadata?.isFile?.() !== true ||
    metadata?.nlink !== 1 ||
    (process.platform !== "win32" &&
      (metadata.mode & 0o111) === 0)
  ) {
    throw new Error("directory_smoke_executable_invalid");
  }
  return true;
}

function sameObjectIdentity(left, right) {
  return (
    left !== null &&
    left !== undefined &&
    right !== null &&
    right !== undefined &&
    left.dev === right.dev &&
    left.ino === right.ino
  );
}

export async function verifyExecutableForLaunch(
  receipt,
  expected = {},
) {
  try {
    const application = await qualifyDirectory(
      receipt.application_root,
    );
    if (
      expected.applicationIdentity !== undefined &&
      !sameObjectIdentity(
        application.identity,
        expected.applicationIdentity,
      )
    ) {
      throw new Error("application identity changed");
    }
    const executableParent = await qualifyDirectory(
      dirname(receipt.executable_path),
    );
    if (
      !isContained(application.path, executableParent.path) ||
      (await realpath(receipt.executable_path)) !==
        receipt.executable_path
    ) {
      throw new Error("executable path escaped");
    }
    const executable = await inspectRegularFile(
      receipt.executable_path,
      1024 * 1024 * 1024,
    );
    const metadata = await lstat(receipt.executable_path);
    assertExecutableEvidence(
      receipt,
      executable,
      metadata,
    );
    if (
      expected.executableIdentity !== undefined &&
      !sameObjectIdentity(
        executable.identity,
        expected.executableIdentity,
      )
    ) {
      throw new Error("executable identity changed");
    }
    const finalApplication = await qualifyDirectory(
      receipt.application_root,
    );
    const finalParent = await qualifyDirectory(
      dirname(receipt.executable_path),
    );
    if (
      !sameObjectIdentity(
        application.identity,
        finalApplication.identity,
      ) ||
      !sameObjectIdentity(
        executableParent.identity,
        finalParent.identity,
      ) ||
      !isContained(finalApplication.path, finalParent.path) ||
      (await realpath(receipt.executable_path)) !==
        receipt.executable_path
    ) {
      throw new Error("executable path changed");
    }
    return {
      applicationIdentity: application.identity,
      executableIdentity: executable.identity,
      path: receipt.executable_path,
      sha256: executable.sha256,
      size: executable.size,
    };
  } catch (error) {
    if (
      error instanceof Error &&
      error.message === "directory_smoke_executable_invalid"
    ) {
      throw error;
    }
    throw new Error("directory_smoke_executable_invalid");
  }
}

async function inventoryPackagedApplicationDist(root) {
  const canonicalRoot = (await qualifyDirectory(root)).path;
  const files = {};
  const portablePaths = new Map();
  let fileCount = 0;
  let totalBytes = 0;

  async function walk(directory, segments) {
    const entries = await readdir(directory, {
      withFileTypes: true,
    });
    entries.sort((left, right) =>
      compareStrings(left.name, right.name),
    );
    for (const entry of entries) {
      const relativePath = normalizeResourceRelativePath(
        [...segments, entry.name].join("/"),
      );
      registerPortableResourcePath(
        portablePaths,
        relativePath,
      );
      const pathValue = join(directory, entry.name);
      const metadata = await lstat(pathValue);
      if (
        entry.isSymbolicLink() ||
        metadata.isSymbolicLink() ||
        (!entry.isDirectory() && !entry.isFile())
      ) {
        throw new Error("packaged dist path is untrusted");
      }
      const canonical = await realpath(pathValue);
      if (!isContained(canonicalRoot, canonical)) {
        throw new Error("packaged dist path escaped");
      }
      if (entry.isDirectory()) {
        await walk(pathValue, [...segments, entry.name]);
        continue;
      }
      if (
        metadata.nlink !== 1 ||
        fileCount >= MAX_INVENTORY_FILES
      ) {
        throw new Error("packaged dist file is untrusted");
      }
      const availableBytes =
        MAX_INVENTORY_BYTES - totalBytes;
      if (availableBytes < 0) {
        throw new Error("packaged dist exceeds its bound");
      }
      const inspected = await inspectRegularFile(
        pathValue,
        availableBytes,
        false,
        0,
      );
      fileCount += 1;
      totalBytes += inspected.size;
      if (
        !Number.isSafeInteger(totalBytes) ||
        totalBytes > MAX_INVENTORY_BYTES
      ) {
        throw new Error("packaged dist exceeds its bound");
      }
      files[relativePath] = {
        sha256: inspected.sha256,
        size: inspected.size,
      };
    }
  }

  await walk(canonicalRoot, []);
  if (
    fileCount === 0 ||
    files["main.js"] === undefined
  ) {
    throw new Error("packaged dist is incomplete");
  }
  const inventoryBytes = canonicalSmokeJson({
    schema: DIRECTORY_INVENTORY_SCHEMA,
    files,
  });
  return {
    fileCount,
    inventorySha256: sha256Bytes(inventoryBytes),
    root: canonicalRoot,
    totalBytes,
  };
}

function assertPackagedDistEvidence(receipt, evidence) {
  if (
    evidence.root !== receipt.packaged_dist_path ||
    evidence.fileCount !==
      receipt.packaged_dist_file_count ||
    evidence.totalBytes !==
      receipt.packaged_dist_total_bytes ||
    evidence.inventorySha256 !==
      receipt.packaged_dist_inventory_sha256
  ) {
    throw new Error(
      "directory_smoke_packaged_dist_invalid",
    );
  }
}

export async function verifyPackagedApplicationDist(
  receipt,
  expectedApplicationIdentity = undefined,
) {
  try {
    const application = await qualifyDirectory(
      receipt.application_root,
    );
    if (
      expectedApplicationIdentity !== undefined &&
      !sameObjectIdentity(
        application.identity,
        expectedApplicationIdentity,
      )
    ) {
      throw new Error("application identity changed");
    }
    const first = await inventoryPackagedApplicationDist(
      receipt.packaged_dist_path,
    );
    assertPackagedDistEvidence(receipt, first);
    const finalApplication = await qualifyDirectory(
      receipt.application_root,
    );
    const final = await inventoryPackagedApplicationDist(
      receipt.packaged_dist_path,
    );
    if (
      !sameObjectIdentity(
        application.identity,
        finalApplication.identity,
      ) ||
      first.inventorySha256 !== final.inventorySha256 ||
      first.fileCount !== final.fileCount ||
      first.totalBytes !== final.totalBytes
    ) {
      throw new Error("packaged dist changed");
    }
    assertPackagedDistEvidence(receipt, final);
    return {
      applicationIdentity: application.identity,
      fileCount: final.fileCount,
      inventorySha256: final.inventorySha256,
      totalBytes: final.totalBytes,
    };
  } catch (error) {
    if (
      error instanceof Error &&
      error.message ===
        "directory_smoke_packaged_dist_invalid"
    ) {
      throw error;
    }
    throw new Error(
      "directory_smoke_packaged_dist_invalid",
    );
  }
}

async function verifyPackagedEvidence(
  buildReceiptPath,
  repositoryRoot,
) {
  const buildRecord = await inspectRegularFile(
    buildReceiptPath,
    MAX_BUILD_RECEIPT_BYTES,
    true,
  );
  const receipt = parseDirectoryBuildReceipt(buildRecord.bytes);
  const application = await qualifyDirectory(
    receipt.application_root,
  );
  const resources = await qualifyDirectory(receipt.resource_root);
  if (
    !isContained(application.path, resources.path) ||
    resources.path !== receipt.resource_root
  ) {
    throw new Error("directory_smoke_resource_root_invalid");
  }

  const executable = await verifyExecutableForLaunch(
    receipt,
    {
      applicationIdentity: application.identity,
    },
  );
  const packagedDist =
    await verifyPackagedApplicationDist(
      receipt,
      application.identity,
    );

  const packageRecord = await inspectRegularFile(
    receipt.packaged_package_json_path,
    MAX_PACKAGE_BYTES,
    true,
  );
  if (
    packageRecord.sha256 !==
    receipt.packaged_package_json_sha256
  ) {
    throw new Error("directory_smoke_package_invalid");
  }
  let packageValue;
  try {
    packageValue = JSON.parse(packageRecord.bytes.toString("utf8"));
  } catch {
    throw new Error("directory_smoke_package_invalid");
  }
  const build = packageValue?.kestrelDesktopBuild;
  if (
    packageValue?.version !== receipt.app_version ||
    build?.schema !== PACKAGED_BUILD_SCHEMA ||
    build?.build_mode !== "developer" ||
    build?.key_id !== "developer" ||
    build?.source_commit !== receipt.source_commit ||
    build?.app_version !== receipt.app_version ||
    build?.platform !== receipt.platform ||
    build?.architecture !== receipt.architecture ||
    build?.resource_root_relative !== "kestrel" ||
    build?.smoke_authority !==
      "developer_directory_smoke_v1"
  ) {
    throw new Error("directory_smoke_package_invalid");
  }

  const manifest = await inspectRegularFile(
    receipt.manifest_path,
    MAX_MANIFEST_BYTES,
    true,
  );
  const signature = await inspectRegularFile(
    receipt.signature_path,
    MAX_SIGNATURE_BYTES,
    true,
  );
  const publicKey = await inspectRegularFile(
    receipt.packaged_public_key_path,
    MAX_PUBLIC_KEY_BYTES,
    true,
  );
  if (
    manifest.sha256 !== receipt.manifest_sha256 ||
    signature.sha256 !== receipt.signature_sha256 ||
    publicKey.sha256 !== receipt.packaged_public_key_sha256 ||
    signature.size !== 64
  ) {
    throw new Error("directory_smoke_manifest_invalid");
  }
  let signatureValid = false;
  try {
    signatureValid = verifySignature(
      null,
      manifest.bytes,
      createPublicKey(publicKey.bytes),
      signature.bytes,
    );
  } catch {
    signatureValid = false;
  }
  if (!signatureValid) {
    throw new Error("directory_smoke_manifest_invalid");
  }
  const manifestValue = parseCanonicalJson(
    manifest.bytes,
    MAX_MANIFEST_BYTES,
    "directory_smoke_manifest",
  );
  if (
    !exactKeys(manifestValue, RESOURCE_MANIFEST_KEYS) ||
    manifestValue?.schema !== RESOURCE_SCHEMA ||
    manifestValue?.build_mode !== "developer" ||
    manifestValue?.key_id !== "developer" ||
    manifestValue?.source_commit !== receipt.source_commit ||
    manifestValue?.app_version !== receipt.app_version ||
    manifestValue?.platform !== receipt.platform ||
    manifestValue?.architecture !== receipt.architecture ||
    !validDigest(manifestValue?.python_lock_sha256) ||
    !validDigest(manifestValue?.desktop_npm_lock_sha256) ||
    !validDigest(manifestValue?.web_npm_lock_sha256) ||
    !validDigest(manifestValue?.sbom_sha256)
  ) {
    throw new Error("directory_smoke_manifest_invalid");
  }
  await verifyManifestInventory(
    resources.path,
    manifestValue,
  );
  const sidecarRelativePath =
    receipt.platform === "win32"
      ? "sidecar/kestrel-desktop-sidecar.exe"
      : "sidecar/kestrel-desktop-sidecar";
  const sidecar = manifestValue.files?.[sidecarRelativePath];
  if (
    sidecar === null ||
    typeof sidecar !== "object" ||
    !validCount(sidecar.size, 1) ||
    !validDigest(sidecar.sha256)
  ) {
    throw new Error("directory_smoke_manifest_invalid");
  }

  const git = spawnSync(
    "/usr/bin/git",
    ["rev-parse", "HEAD"],
    {
      cwd: repositoryRoot,
      encoding: "utf8",
      maxBuffer: MAX_CONTROL_BYTES,
      env: {
        LANG: "C",
        LC_ALL: "C",
        PATH: "/usr/bin:/bin",
      },
    },
  );
  if (
    git.error ||
    git.status !== 0 ||
    git.stdout.trim() !== receipt.source_commit
  ) {
    throw new Error("directory_smoke_source_mismatch");
  }
  return {
    applicationIdentity: application.identity,
    buildReceiptSha256: buildRecord.sha256,
    executableIdentity: executable.executableIdentity,
    manifest: manifestValue,
    packagedDistInventorySha256:
      packagedDist.inventorySha256,
    receipt,
    sidecar,
  };
}

async function privateEmptyControlPublicationPending(pathValue) {
  try {
    const metadata = await lstat(pathValue);
    if (
      metadata.isSymbolicLink() ||
      !metadata.isFile() ||
      metadata.nlink !== 1 ||
      metadata.size !== 0 ||
      (process.platform !== "win32" &&
        (metadata.uid !== process.getuid?.() ||
          (metadata.mode & 0o777) !== 0o600))
    ) {
      return false;
    }
    // A symlinked tmp root makes realpath() differ from resolve() even for a
    // legitimate file the test just wrote. The zero-byte publication check is
    // about detecting "file exists but not yet populated", not canonical
    // identity — the O_NOFOLLOW open + fstat in inspectRegularFile is the
    // actual race guard. Compare canonical-to-canonical instead.
    return (await realpath(pathValue)) === (await realpath(resolve(pathValue)));
  } catch {
    return false;
  }
}

export async function waitForCanonicalControl(
  pathValue,
  expectedKeys,
  schema,
  deadline,
) {
  while (Date.now() < deadline) {
    try {
      const record = await readCanonicalFile(
        pathValue,
        MAX_CONTROL_BYTES,
        true,
      );
      if (
        !exactKeys(record.value, expectedKeys) ||
        record.value.schema !== schema
      ) {
        throw new Error("directory_smoke_control_invalid");
      }
      return record;
    } catch (error) {
      if (
        error?.code !== "ENOENT" &&
        !(
          error instanceof Error &&
          error.message === "directory_smoke_file_untrusted" &&
          (await privateEmptyControlPublicationPending(pathValue))
        )
      ) {
        throw error;
      }
    }
    await new Promise((resolvePromise) =>
      setTimeout(resolvePromise, POLL_INTERVAL_MS),
    );
  }
  throw new Error("directory_smoke_control_timeout");
}

async function writeContinue(pathValue, controlRoot) {
  const root = await qualifyDirectory(controlRoot, true);
  if (!isContained(root.path, pathValue)) {
    throw new Error("directory_smoke_control_invalid");
  }
  const bytes = canonicalSmokeJson({
    schema: CONTINUE_SCHEMA,
    continue: true,
  });
  const handle = await open(
    pathValue,
    fsConstants.O_WRONLY |
      fsConstants.O_CREAT |
      fsConstants.O_EXCL |
      (fsConstants.O_NOFOLLOW ?? 0),
    0o600,
  );
  try {
    await handle.writeFile(bytes);
    await handle.sync();
    if (process.platform !== "win32") {
      await handle.chmod(0o600);
    }
    const opened = await handle.stat();
    const named = await lstat(pathValue);
    if (
      opened.dev !== named.dev ||
      opened.ino !== named.ino ||
      named.isSymbolicLink() ||
      !named.isFile() ||
      named.nlink !== 1 ||
      named.size !== bytes.byteLength
    ) {
      throw new Error("directory_smoke_control_changed");
    }
    return {
      dev: opened.dev,
      ino: opened.ino,
    };
  } finally {
    await handle.close();
  }
}

async function readSidecarReadiness(pathValue) {
  const record = await readCanonicalFile(
    pathValue,
    MAX_SIDECAR_READINESS_BYTES,
    true,
  );
  const value = record.value;
  if (
    !exactKeys(value, SIDECAR_READINESS_KEYS) ||
    value.schema !== SIDECAR_READINESS_SCHEMA ||
    !validCount(value.pid, 1) ||
    !validCount(value.port, 1) ||
    value.port > 65535 ||
    value.profile_id !== "default" ||
    typeof value.process_birth_marker !== "string" ||
    value.process_birth_marker.length === 0 ||
    value.process_birth_marker.length > 256 ||
    typeof value.sidecar_version !== "string" ||
    !validDigest(value.executable_digest) ||
    !validDigest(value.launch_nonce_digest) ||
    !PREFIXED_SHA256_PATTERN.test(
      value.resource_manifest_digest,
    )
  ) {
    throw new Error("directory_smoke_readiness_invalid");
  }
  return record;
}

function processTable() {
  const completed = spawnSync(
    "/bin/ps",
    ["-axo", "pid=,ppid=,lstart=,comm="],
    {
      encoding: "utf8",
      maxBuffer: PROCESS_OUTPUT_BYTES,
      env: {
        LANG: "C",
        LC_ALL: "C",
        PATH: "/usr/bin:/bin",
      },
    },
  );
  if (
    completed.error ||
    completed.status !== 0 ||
    completed.signal !== null
  ) {
    throw new Error("directory_smoke_process_identity_ambiguous");
  }
  const rows = new Map();
  for (const line of completed.stdout.split("\n")) {
    const match = line.match(
      /^\s*(\d+)\s+(\d+)\s+(\S+\s+\S+\s+\d+\s+\d+:\d+:\d+\s+\d+)\s+(.+?)\s*$/,
    );
    if (match === null) {
      if (line.trim().length !== 0) {
        throw new Error(
          "directory_smoke_process_identity_ambiguous",
        );
      }
      continue;
    }
    const pid = Number(match[1]);
    const ppid = Number(match[2]);
    if (
      !Number.isSafeInteger(pid) ||
      !Number.isSafeInteger(ppid) ||
      rows.has(pid)
    ) {
      throw new Error(
        "directory_smoke_process_identity_ambiguous",
      );
    }
    rows.set(pid, {
      pid,
      ppid,
      birth: match[3],
      command: match[4],
    });
  }
  return rows;
}

export function assertReadinessProcessBinding(
  readiness,
  identities,
) {
  if (
    readiness === null ||
    typeof readiness !== "object" ||
    !validCount(readiness.pid, 1) ||
    typeof readiness.process_birth_marker !== "string" ||
    !Array.isArray(identities)
  ) {
    throw new Error(
      "directory_smoke_readiness_identity_mismatch",
    );
  }
  const matches = identities.filter(
    (identity) => identity?.pid === readiness.pid,
  );
  if (matches.length !== 1) {
    throw new Error(
      "directory_smoke_readiness_identity_mismatch",
    );
  }
  const sidecar = matches[0];
  const birthMilliseconds = Date.parse(sidecar.birth);
  if (
    !Number.isSafeInteger(birthMilliseconds) ||
    birthMilliseconds <= 0 ||
    readiness.process_birth_marker !==
      `developer-ps-lstart-ms:${birthMilliseconds}`
  ) {
    throw new Error(
      "directory_smoke_readiness_identity_mismatch",
    );
  }
  return sidecar;
}

function captureProcessTree(rootPid, requiredPid) {
  const rows = processTable();
  const root = rows.get(rootPid);
  if (root === undefined) {
    throw new Error("directory_smoke_process_identity_ambiguous");
  }
  const captured = new Map([[rootPid, root]]);
  let changed = true;
  while (changed) {
    changed = false;
    for (const row of rows.values()) {
      if (!captured.has(row.pid) && captured.has(row.ppid)) {
        captured.set(row.pid, row);
        changed = true;
      }
    }
  }
  if (!captured.has(requiredPid)) {
    throw new Error("directory_smoke_process_identity_ambiguous");
  }
  return [...captured.values()];
}

function sameProcess(left, right) {
  return (
    left.pid === right.pid &&
    left.ppid === right.ppid &&
    left.birth === right.birth &&
    left.command === right.command
  );
}

export function parseListenerOwnerPids(output) {
  if (
    typeof output !== "string" ||
    Buffer.byteLength(output, "utf8") >
      MAX_LISTENER_OUTPUT_BYTES
  ) {
    throw new Error(
      "directory_smoke_listener_identity_ambiguous",
    );
  }
  const pids = [];
  const observed = new Set();
  for (const line of output.split("\n")) {
    if (line.length === 0) {
      continue;
    }
    const match = line.match(/^([1-9][0-9]*)$/);
    const pid = match === null ? NaN : Number(match[1]);
    if (
      !Number.isSafeInteger(pid) ||
      pid <= 0 ||
      observed.has(pid)
    ) {
      throw new Error(
        "directory_smoke_listener_identity_ambiguous",
      );
    }
    observed.add(pid);
    pids.push(pid);
  }
  return pids.sort((left, right) => left - right);
}

function inspectListenerOwnerPids(port) {
  if (!validCount(port, 1) || port > 65535) {
    throw new Error(
      "directory_smoke_listener_identity_ambiguous",
    );
  }
  const completed = spawnSync(
    "/usr/sbin/lsof",
    [
      "-nP",
      "-a",
      `-iTCP:${port}`,
      "-sTCP:LISTEN",
      "-t",
    ],
    {
      encoding: "utf8",
      maxBuffer: MAX_LISTENER_OUTPUT_BYTES,
      env: {
        LANG: "C",
        LC_ALL: "C",
        PATH: "/usr/bin:/bin:/usr/sbin",
      },
    },
  );
  if (
    completed.error ||
    completed.signal !== null ||
    ![0, 1].includes(completed.status) ||
    (completed.status === 1 &&
      completed.stdout.trim().length !== 0)
  ) {
    throw new Error(
      "directory_smoke_listener_identity_ambiguous",
    );
  }
  return parseListenerOwnerPids(completed.stdout);
}

export function assertListenerOwnerBinding(
  listenerOwnerPids,
  sidecarIdentity,
  currentIdentity,
) {
  if (
    !Array.isArray(listenerOwnerPids) ||
    listenerOwnerPids.length !== 1 ||
    listenerOwnerPids[0] !== sidecarIdentity?.pid ||
    currentIdentity === undefined ||
    !sameProcess(sidecarIdentity, currentIdentity)
  ) {
    throw new Error(
      "directory_smoke_listener_identity_mismatch",
    );
  }
  return true;
}

async function waitForProcessesGone(
  identities,
  deadline,
) {
  while (Date.now() < deadline) {
    const rows = processTable();
    if (
      identities.every((identity) => {
        const current = rows.get(identity.pid);
        return (
          current === undefined ||
          !sameProcess(identity, current)
        );
      })
    ) {
      return;
    }
    await new Promise((resolvePromise) =>
      setTimeout(resolvePromise, POLL_INTERVAL_MS),
    );
  }
  throw new Error("directory_smoke_orphan_process");
}

async function connectToListener(port, expectLive) {
  return new Promise((resolvePromise, rejectPromise) => {
    let settled = false;
    const socket = createConnection({
      host: "127.0.0.1",
      port,
    });
    const finish = (live) => {
      if (settled) {
        return;
      }
      settled = true;
      clearTimeout(timer);
      socket.destroy();
      if (live === expectLive) {
        resolvePromise();
      } else {
        rejectPromise(
          new Error(
            expectLive
              ? "directory_smoke_listener_unavailable"
              : "directory_smoke_orphan_listener",
          ),
        );
      }
    };
    socket.once("connect", () => finish(true));
    socket.once("error", () => finish(false));
    const timer = setTimeout(
      () => finish(false),
      1000,
    );
  });
}

async function verifyLiveListenerOwnership(
  port,
  sidecarIdentity,
) {
  let rows = processTable();
  assertListenerOwnerBinding(
    inspectListenerOwnerPids(port),
    sidecarIdentity,
    rows.get(sidecarIdentity.pid),
  );
  await connectToListener(port, true);
  rows = processTable();
  assertListenerOwnerBinding(
    inspectListenerOwnerPids(port),
    sidecarIdentity,
    rows.get(sidecarIdentity.pid),
  );
}

async function waitForListenerClosed(port, deadline) {
  while (Date.now() < deadline) {
    const owners = inspectListenerOwnerPids(port);
    if (owners.length === 0) {
      try {
        await connectToListener(port, false);
        if (inspectListenerOwnerPids(port).length === 0) {
          return;
        }
      } catch {
        // The endpoint was still live during this bounded race.
      }
    }
    await new Promise((resolvePromise) =>
      setTimeout(resolvePromise, POLL_INTERVAL_MS),
    );
  }
  throw new Error("directory_smoke_orphan_listener");
}

export function captureBoundedDiagnosticStream(stream) {
  let byteLength = 0;
  let observed = 0;
  let overflow = false;
  stream?.on("data", (chunk) => {
    const value = Buffer.isBuffer(chunk)
      ? chunk
      : Buffer.from(chunk);
    observed += value.byteLength;
    byteLength = Math.min(observed, MAX_STDERR_BYTES);
    overflow = observed > MAX_STDERR_BYTES;
  });
  return () => ({
    byteLength,
    overflow,
  });
}

async function waitForChildExit(child, deadline) {
  if (
    child.exitCode !== null ||
    child.signalCode !== null
  ) {
    return {
      code: child.exitCode,
      signal: child.signalCode,
    };
  }
  return new Promise((resolvePromise, rejectPromise) => {
    const remaining = Math.max(1, deadline - Date.now());
    const timer = setTimeout(() => {
      rejectPromise(new Error("directory_smoke_app_exit_timeout"));
    }, remaining);
    child.once("exit", (code, signal) => {
      clearTimeout(timer);
      resolvePromise({ code, signal });
    });
    child.once("error", () => {
      clearTimeout(timer);
      rejectPromise(new Error("directory_smoke_app_spawn_failed"));
    });
  });
}

async function requireAbsent(pathValue, errorCode) {
  try {
    await lstat(pathValue);
  } catch (error) {
    if (error?.code === "ENOENT") {
      return;
    }
    throw error;
  }
  throw new Error(errorCode);
}

export async function removeCapturedControl(
  pathValue,
  controlRoot,
  expectedIdentity,
) {
  if (!isContained(controlRoot, pathValue)) {
    throw new Error("directory_smoke_control_invalid");
  }
  const before = await lstat(pathValue);
  if (
    before.isSymbolicLink() ||
    !before.isFile() ||
    before.nlink !== 1 ||
    before.dev !== expectedIdentity.dev ||
    before.ino !== expectedIdentity.ino
  ) {
    throw new Error("directory_smoke_control_changed");
  }
  const handle = await open(  // codeql[js/file-system-race] — O_NOFOLLOW + fstat identity check
    pathValue,
    fsConstants.O_RDONLY |
      (fsConstants.O_NOFOLLOW ?? 0),
  );
  try {
    const opened = await handle.stat();
    const after = await lstat(pathValue);
    if (
      opened.dev !== before.dev ||
      opened.ino !== before.ino ||
      after.dev !== before.dev ||
      after.ino !== before.ino
    ) {
      throw new Error("directory_smoke_control_changed");
    }
  } finally {
    await handle.close();
  }
  const final = await lstat(pathValue);
  if (
    final.dev !== before.dev ||
    final.ino !== before.ino
  ) {
    throw new Error("directory_smoke_control_changed");
  }
  await unlink(pathValue);
}

export function assertSafeCycleCompletion(state) {
  if (state.cleanupStarted && !state.appExited) {
    throw new Error("directory_smoke_pre_exit_cleanup");
  }
  if (!state.appExited) {
    throw new Error("directory_smoke_app_exit_failed");
  }
  if (!state.readinessRemoved) {
    throw new Error("directory_smoke_readiness_not_removed");
  }
  if (!state.identitiesGone) {
    throw new Error("directory_smoke_orphan_process");
  }
  if (!state.listenerClosed) {
    throw new Error("directory_smoke_orphan_listener");
  }
  return true;
}

export function validateMemorySnapshotEntries(snapshot) {
  const expected = MEMORY_FILES.map(
    (name) => `memory/${name}`,
  ).sort(compareStrings);
  if (
    !Array.isArray(snapshot) ||
    snapshot.length !== expected.length ||
    snapshot.some(
      (entry, index) =>
        entry === null ||
        typeof entry !== "object" ||
        entry.relative !== expected[index] ||
        !Number.isInteger(entry.dev) ||
        entry.dev < 0 ||
        !Number.isInteger(entry.ino) ||
        entry.ino <= 0,
    )
  ) {
    throw new Error("directory_smoke_memory_set_invalid");
  }
  return snapshot;
}

async function memorySnapshot(profileRoot) {
  const canonicalProfile = (
    await qualifyDirectory(profileRoot, true)
  ).path;
  const memoryRoot = join(canonicalProfile, "memory");
  await qualifyDirectory(memoryRoot, true);
  const found = [];
  async function walk(directory) {
    const entries = await readdir(directory, {
      withFileTypes: true,
    });
    for (const entry of entries) {
      const pathValue = join(directory, entry.name);
      const metadata = await lstat(pathValue);
      if (
        entry.isSymbolicLink() ||
        metadata.isSymbolicLink() ||
        (!entry.isDirectory() && !entry.isFile())
      ) {
        throw new Error("directory_smoke_memory_set_invalid");
      }
      if (entry.isDirectory()) {
        await walk(pathValue);
      } else if (entry.name.endsWith(".mv2")) {
        if (
          metadata.nlink !== 1 ||
          (await realpath(pathValue)) !== pathValue ||
          !isContained(canonicalProfile, pathValue)
        ) {
          throw new Error("directory_smoke_memory_set_invalid");
        }
        found.push({
          relative: relative(canonicalProfile, pathValue)
            .split(sep)
            .join("/"),
          dev: metadata.dev,
          ino: metadata.ino,
        });
      }
    }
  }
  await walk(canonicalProfile);
  found.sort((left, right) =>
    compareStrings(left.relative, right.relative),
  );
  return validateMemorySnapshotEntries(found);
}

function memorySnapshotsEqual(left, right) {
  return (
    left.length === right.length &&
    left.every(
      (entry, index) =>
        entry.relative === right[index]?.relative &&
        entry.dev === right[index]?.dev &&
        entry.ino === right[index]?.ino,
    )
  );
}

function memorySnapshotDigest(snapshot) {
  return sha256Bytes(
    canonicalSmokeJson(
      snapshot.map((entry) => ({
        dev: entry.dev,
        ino: entry.ino,
        name: entry.relative,
      })),
    ),
  );
}

async function runCycle(
  evidence,
  userDataPath,
  cycle,
) {
  const deadline = Date.now() + CYCLE_TIMEOUT_MS;
  const profileRoot = join(
    userDataPath,
    "profiles",
    "default",
  );
  const controlRoot = join(
    userDataPath,
    CONTROL_DIRECTORY,
  );
  const readyPath = join(controlRoot, "ready.json");
  const continuePath = join(controlRoot, "continue.json");
  const completedPath = join(controlRoot, "completed.json");
  const readinessPath = join(
    profileRoot,
    "runtime",
    "desktop-readiness.json",
  );
  const packagedDist =
    await verifyPackagedApplicationDist(
      evidence.receipt,
      evidence.applicationIdentity,
    );
  if (
    packagedDist.inventorySha256 !==
      evidence.packagedDistInventorySha256
  ) {
    throw new Error(
      "directory_smoke_packaged_dist_invalid",
    );
  }
  const executable = await verifyExecutableForLaunch(
    evidence.receipt,
    {
      applicationIdentity: evidence.applicationIdentity,
      executableIdentity: evidence.executableIdentity,
    },
  );
  const child = spawn(
    executable.path,
    [
      DIRECTORY_SMOKE_ARGUMENT,
      `--user-data-dir=${userDataPath}`,
    ],
    {
      cwd: evidence.receipt.application_root,
      detached: false,
      env: safeSmokeEnvironment(),
      shell: false,
      stdio: ["ignore", "pipe", "pipe"],
      windowsHide: true,
    },
  );
  if (!validCount(child.pid, 1)) {
    throw new Error("directory_smoke_app_spawn_failed");
  }
  const stdout = captureBoundedDiagnosticStream(child.stdout);
  const stderr = captureBoundedDiagnosticStream(child.stderr);
  let identities = [];
  let port = null;
  try {
    const readyRecord = await waitForCanonicalControl(
      readyPath,
      READY_KEYS,
      READY_SCHEMA,
      deadline,
    );
    const ready = readyRecord.value;
    if (
      ready.authenticated_readiness !== true ||
      ready.authenticated_recovery !== true ||
      ready.build_mode !== "developer" ||
      ready.hidden !== true ||
      ready.mission_command_url !== "kestrel://app/index.html" ||
      ready.source_commit !== evidence.receipt.source_commit ||
      !Array.isArray(ready.memory_files) ||
      ready.memory_files.length !== MEMORY_FILES.length ||
      ready.memory_files.some(
        (name, index) => name !== MEMORY_FILES[index],
      )
    ) {
      throw new Error("directory_smoke_ready_invalid");
    }
    const readinessRecord =
      await readSidecarReadiness(readinessPath);
    const readiness = readinessRecord.value;
    if (
      readiness.sidecar_version !==
        evidence.receipt.app_version ||
      readiness.executable_digest !==
        evidence.sidecar.sha256 ||
      readiness.resource_manifest_digest !==
        `sha256:${evidence.receipt.manifest_sha256}`
    ) {
      throw new Error("directory_smoke_readiness_invalid");
    }
    port = readiness.port;
    identities = captureProcessTree(
      child.pid,
      readiness.pid,
    );
    const sidecarIdentity =
      assertReadinessProcessBinding(
        readiness,
        identities,
      );
    await verifyLiveListenerOwnership(
      port,
      sidecarIdentity,
    );
    const continueIdentity = await writeContinue(
      continuePath,
      controlRoot,
    );
    const completedRecord = await waitForCanonicalControl(
      completedPath,
      COMPLETED_KEYS,
      COMPLETED_SCHEMA,
      deadline,
    );
    if (
      completedRecord.value.authenticated_shutdown !== true ||
      completedRecord.value.child_exited !== true
    ) {
      throw new Error("directory_smoke_completed_invalid");
    }
    const exit = await waitForChildExit(child, deadline);
    if (exit.code !== 0 || exit.signal !== null) {
      throw new Error("directory_smoke_app_exit_failed");
    }
    await requireAbsent(
      readinessPath,
      "directory_smoke_readiness_not_removed",
    );
    await waitForProcessesGone(identities, deadline);
    await waitForListenerClosed(port, deadline);
    assertSafeCycleCompletion({
      appExited: true,
      cleanupStarted: false,
      identitiesGone: true,
      listenerClosed: true,
      readinessRemoved: true,
    });
    const snapshot = await memorySnapshot(profileRoot);
    for (const [pathValue, identity] of [
      [readyPath, readyRecord.identity],
      [continuePath, continueIdentity],
      [completedPath, completedRecord.identity],
    ]) {
      await removeCapturedControl(
        pathValue,
        controlRoot,
        identity,
      );
    }
    await requireAbsent(
      readyPath,
      "directory_smoke_control_not_removed",
    );
    await requireAbsent(
      continuePath,
      "directory_smoke_control_not_removed",
    );
    await requireAbsent(
      completedPath,
      "directory_smoke_control_not_removed",
    );
    return {
      cycle,
      identityCount: identities.length,
      snapshot,
    };
  } catch (error) {
    if (
      child.exitCode === null &&
      child.signalCode === null
    ) {
      child.kill("SIGTERM");
    }
    await Promise.race([
      waitForChildExit(
        child,
        Date.now() + 2_000,
      ).catch(() => undefined),
      new Promise((resolvePromise) =>
        setTimeout(resolvePromise, 2_000),
      ),
    ]);
    throw error;
  } finally {
    stdout();
    stderr();
  }
}

async function createPrivateUserData() {
  const parent = await realpath(tmpdir());
  const created = await mkdtemp(
    join(parent, "kestrel-directory-smoke-"),
  );
  await chmod(created, 0o700);
  const qualified = await qualifyDirectory(created, true);
  if (!isContained(parent, qualified.path)) {
    throw new Error("directory_smoke_user_data_invalid");
  }
  return qualified;
}

async function removeQualifiedUserData(userData) {
  const current = await qualifyDirectory(
    userData.path,
    true,
  );
  if (
    current.identity.dev !== userData.identity.dev ||
    current.identity.ino !== userData.identity.ino
  ) {
    throw new Error("directory_smoke_user_data_changed");
  }
  await rm(userData.path, {
    recursive: true,
    force: false,
  });
  await requireAbsent(
    userData.path,
    "directory_smoke_user_data_not_removed",
  );
}

async function writeExclusiveReceipt(pathValue, receipt) {
  const parent = await qualifyDirectory(dirname(pathValue));
  if (
    !isCanonicalAbsolutePath(pathValue) ||
    !isContained(parent.path, pathValue)
  ) {
    throw new Error("directory_smoke_receipt_path_invalid");
  }
  await requireAbsent(
    pathValue,
    "directory_smoke_receipt_exists",
  );
  const bytes = canonicalSmokeJson(receipt);
  validateDirectorySmokeReceipt(bytes);
  const handle = await open(
    pathValue,
    fsConstants.O_WRONLY |
      fsConstants.O_CREAT |
      fsConstants.O_EXCL |
      (fsConstants.O_NOFOLLOW ?? 0),
    0o600,
  );
  try {
    await handle.writeFile(bytes);
    await handle.sync();
    if (process.platform !== "win32") {
      await handle.chmod(0o600);
    }
  } finally {
    await handle.close();
  }
  const written = await readCanonicalFile(
    pathValue,
    MAX_CONTROL_BYTES,
    true,
  );
  if (!written.bytes.equals(bytes)) {
    throw new Error("directory_smoke_receipt_changed");
  }
}

export async function runDeveloperDirectorySmoke(input) {
  validateSmokePlatform(process.platform);
  const repositoryRoot = (
    await qualifyDirectory(
      input.repositoryRoot ?? DEFAULT_REPOSITORY_ROOT,
    )
  ).path;
  const buildReceiptPath = resolve(input.buildReceiptPath);
  const receiptPath = resolve(input.receiptPath);
  if (
    !isCanonicalAbsolutePath(buildReceiptPath) ||
    !isCanonicalAbsolutePath(receiptPath) ||
    buildReceiptPath === receiptPath
  ) {
    throw new Error("directory_smoke_input_invalid");
  }
  const evidence = await verifyPackagedEvidence(
    buildReceiptPath,
    repositoryRoot,
  );
  const userData = await createPrivateUserData();
  let ownerDataRemoved = false;
  try {
    const first = await runCycle(
      evidence,
      userData.path,
      1,
    );
    const second = await runCycle(
      evidence,
      userData.path,
      2,
    );
    if (
      !memorySnapshotsEqual(first.snapshot, second.snapshot)
    ) {
      throw new Error("directory_smoke_memory_identity_changed");
    }
    await removeQualifiedUserData(userData);
    ownerDataRemoved = true;
    const receipt = {
      schema: SMOKE_RECEIPT_SCHEMA,
      build_mode: "developer",
      source_commit: evidence.receipt.source_commit,
      platform: evidence.receipt.platform,
      architecture: evidence.receipt.architecture,
      qualified: true,
      cycle_count: 2,
      memory_layer_count: MEMORY_FILES.length,
      captured_process_count:
        first.identityCount + second.identityCount,
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
      build_receipt_sha256:
        evidence.buildReceiptSha256,
      executable_sha256:
        evidence.receipt.executable_sha256,
      manifest_sha256:
        evidence.receipt.manifest_sha256,
      packaged_dist_inventory_sha256:
        evidence.packagedDistInventorySha256,
      memory_identity_sha256:
        memorySnapshotDigest(second.snapshot),
    };
    await writeExclusiveReceipt(receiptPath, receipt);
    return receipt;
  } catch (error) {
    if (ownerDataRemoved) {
      error?.addNote?.(
        "directory smoke failed after owner data removal",
      );
    }
    throw error;
  }
}

function parseArguments(argv) {
  if (argv.length !== 4) {
    throw new Error(
      "usage: smoke-dir --build-receipt PATH --receipt PATH",
    );
  }
  const values = {};
  for (let index = 0; index < argv.length; index += 2) {
    const name = argv[index];
    const value = argv[index + 1];
    if (
      !["--build-receipt", "--receipt"].includes(name) ||
      Object.hasOwn(values, name) ||
      typeof value !== "string" ||
      value.length === 0
    ) {
      throw new Error("directory_smoke_arguments_invalid");
    }
    values[name] = value;
  }
  if (
    values["--build-receipt"] === undefined ||
    values["--receipt"] === undefined
  ) {
    throw new Error("directory_smoke_arguments_invalid");
  }
  return {
    buildReceiptPath: values["--build-receipt"],
    receiptPath: values["--receipt"],
  };
}

const invokedPath =
  process.argv[1] === undefined ? null : resolve(process.argv[1]);
if (invokedPath === fileURLToPath(import.meta.url)) {
  try {
    const receipt = await runDeveloperDirectorySmoke(
      parseArguments(process.argv.slice(2)),
    );
    process.stdout.write(
      canonicalSmokeJson(receipt),
    );
  } catch (error) {
    const code =
      error instanceof Error &&
      /^[a-z0-9_]+$/.test(error.message)
        ? error.message
        : "directory_smoke_failed";
    process.stderr.write(
      `smoke-dir: ${code}; isolated owner data preserved\n`,
    );
    process.exitCode = 1;
  }
}
