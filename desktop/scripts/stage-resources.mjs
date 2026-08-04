#!/usr/bin/env node

import { spawnSync } from "node:child_process";
import {
  constants as fsConstants,
  copyFile,
  lstat,
  mkdir,
  open,
  readdir,
  realpath,
  rm,
  writeFile,
} from "node:fs/promises";
import {
  generateKeyPairSync,
  createHash,
  sign,
} from "node:crypto";
import { dirname, isAbsolute, join, parse, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const SCRIPT_DIRECTORY = dirname(fileURLToPath(import.meta.url));
const REPOSITORY_ROOT = resolve(SCRIPT_DIRECTORY, "../..");
const MANIFEST_NAME = "kestrel-resource-manifest.json";
const SIGNATURE_NAME = "kestrel-resource-manifest.sig";
const PUBLIC_KEY_NAME = "desktop-developer-public-key.pem";
const STAGE_RECEIPT_SCHEMA = "kestrel.desktop.stage.v1";
const ASSET_RECEIPT_SCHEMA = "kestrel.desktop.asset-build.v1";
const SIDECAR_RECEIPT_SCHEMA = "kestrel.desktop.sidecar-build.v1";
const SBOM_RECEIPT_SCHEMA = "kestrel.desktop.sbom.v1";
const MAX_CONTROL_BYTES = 64 * 1024;
const MAX_ASSET_RECEIPT_BYTES = 1024 * 1024;
const MAX_SBOM_BYTES = 16 * 1024 * 1024;
const SHA256_PATTERN = /^[0-9a-f]{64}$/;
const COMMIT_PATTERN = /^[0-9a-f]{40}$/;
const VERSION_PATTERN = /^[0-9A-Za-z][0-9A-Za-z.+-]{0,63}$/;
const FORBIDDEN_NAMES = new Set([
  ".cache",
  ".nest",
  "__pycache__",
  "benchmark",
  "benchmarks",
  "credentials.json",
  "desktop-developer-public-key.pem",
  "kestrel-resource-manifest.json",
  "kestrel-resource-manifest.sig",
  "pytest",
  "qrcode",
  "sbom.cdx.json",
  "tests",
  "video_frames",
  "vite.config.js",
  "vite.config.ts",
]);
const ARGUMENT_NAMES = new Set([
  "--desktop-receipt",
  "--identity",
  "--notices-receipt",
  "--output",
  "--python",
  "--receipt",
  "--sbom-receipt",
  "--sidecar-receipt",
  "--web-receipt",
]);
const ASSET_RECEIPT_KEYS = new Set([
  "files",
  "kind",
  "lock_sha256",
  "root",
  "schema",
  "source_commit",
]);
const SIDECAR_RECEIPT_KEYS = new Set([
  "app_version",
  "architecture",
  "binary_path",
  "binary_sha256",
  "binary_size",
  "entrypoint_sha256",
  "platform",
  "pyinstaller_version",
  "python_executable",
  "python_executable_sha256",
  "python_lock_sha256",
  "python_version",
  "schema",
  "source_commit",
  "spec_sha256",
  "upx_enabled",
  "web_asset_receipt_sha256",
]);
const SBOM_RECEIPT_KEYS = new Set([
  "app_version",
  "desktop_npm_lock_sha256",
  "python_lock_sha256",
  "sbom_path",
  "sbom_sha256",
  "sbom_size",
  "schema",
  "sidecar_binary_sha256",
  "source_commit",
  "web_asset_receipt_sha256",
  "web_npm_lock_sha256",
]);
const IDENTITY_KEYS = new Set([
  "app_version",
  "architecture",
  "build_mode",
  "desktop_npm_lock_sha256",
  "key_id",
  "platform",
  "python_lock_sha256",
  "sbom_sha256",
  "source_commit",
  "web_npm_lock_sha256",
]);

function compareStrings(left, right) {
  const leftPoints = Array.from(left, (value) => value.codePointAt(0) ?? 0);
  const rightPoints = Array.from(right, (value) => value.codePointAt(0) ?? 0);
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

function requireString(value, label) {
  if (typeof value !== "string" || value.length === 0) {
    throw new Error(`${label} must be a non-empty string`);
  }
  return value;
}

function requireSha256(value, label) {
  if (typeof value !== "string" || !SHA256_PATTERN.test(value)) {
    throw new Error(`${label} must be a lowercase SHA-256 digest`);
  }
  return value;
}

function requireCommit(value, label) {
  if (typeof value !== "string" || !COMMIT_PATTERN.test(value)) {
    throw new Error(`${label} must be an exact lowercase commit`);
  }
  return value;
}

function sameOpenedFile(before, opened, after) {
  return (
    before.dev === opened.dev &&
    before.ino === opened.ino &&
    before.size === opened.size &&
    before.mtimeNs === opened.mtimeNs &&
    opened.dev === after.dev &&
    opened.ino === after.ino &&
    opened.size === after.size &&
    opened.mtimeNs === after.mtimeNs
  );
}

async function inspectRegularFile(pathValue, label, maximumBytes = null, collect = false) {
  const before = await lstat(pathValue, { bigint: true });
  if (!before.isFile() || before.isSymbolicLink() || before.nlink !== 1n) {
    throw new Error(`${label} must be a unique regular file`);
  }
  if (before.size > BigInt(Number.MAX_SAFE_INTEGER)) {
    throw new Error(`${label} is too large`);
  }
  if (maximumBytes !== null && before.size > BigInt(maximumBytes)) {
    throw new Error(`${label} exceeds its size limit`);
  }
  const handle = await open(  // codeql[js/file-system-race] — O_NOFOLLOW + fstat identity check
    pathValue,
    fsConstants.O_RDONLY |
      (fsConstants.O_NOFOLLOW ?? 0) |
      (fsConstants.O_CLOEXEC ?? 0),
  );
  try {
    const opened = await handle.stat({ bigint: true });
    if (!opened.isFile() || opened.nlink !== 1n || opened.size !== before.size) {
      throw new Error(`${label} changed during open`);
    }
    const digest = createHash("sha256");
    const chunks = [];
    const buffer = Buffer.allocUnsafe(1024 * 1024);
    let position = 0;
    const size = Number(opened.size);
    while (position < size) {
      const length = Math.min(buffer.length, size - position);
      const { bytesRead } = await handle.read(buffer, 0, length, position);
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
    const after = await lstat(pathValue, { bigint: true });
    if (!after.isFile() || after.isSymbolicLink() || after.nlink !== 1n) {
      throw new Error(`${label} changed during read`);
    }
    if (!sameOpenedFile(before, opened, after)) {
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

async function readCanonicalJson(pathValue, label, maximumBytes, expectedKeys) {
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
    throw new Error(`${label} is invalid UTF-8 JSON`);
  }
  requireExactKeys(value, expectedKeys, label);
  if (!canonicalJsonBytes(value).equals(inspected.bytes)) {
    throw new Error(`${label} is not canonical`);
  }
  return { digest: inspected.sha256, value };
}

function validateRelativePath(relativePath) {
  if (
    typeof relativePath !== "string" ||
    relativePath.length === 0 ||
    relativePath.startsWith("/") ||
    relativePath.endsWith("/") ||
    relativePath.includes("\\") ||
    relativePath.includes("\0")
  ) {
    throw new Error(`asset receipt contains an unsafe path: ${relativePath}`);
  }
  const parts = relativePath.split("/");
  if (parts.some((part) => part === "" || part === "." || part === "..")) {
    throw new Error(`asset receipt contains an unsafe path: ${relativePath}`);
  }
  return parts;
}

function rejectForbiddenPayload(relativePath) {
  const parts = validateRelativePath(relativePath);
  const folded = parts.map((part) => part.normalize("NFKC").toLowerCase());
  if (
    folded.some((part) => FORBIDDEN_NAMES.has(part) || part.startsWith(".env")) ||
    folded.at(-1).endsWith(".map")
  ) {
    throw new Error(`forbidden staged payload: ${relativePath}`);
  }
}

async function inventoryRoot(root) {
  const rootMetadata = await lstat(root, { bigint: true });
  if (!rootMetadata.isDirectory() || rootMetadata.isSymbolicLink()) {
    throw new Error("asset root must be a real directory");
  }
  const inventory = {};
  const portablePaths = new Map();

  async function visit(directory, parts) {
    const entries = await readdir(directory, { withFileTypes: true });
    entries.sort((left, right) => compareStrings(left.name, right.name));
    for (const entry of entries) {
      const childParts = [...parts, entry.name];
      const relativePath = childParts.join("/");
      validateRelativePath(relativePath);
      for (let length = 1; length <= childParts.length; length += 1) {
        const prefix = childParts.slice(0, length).join("/");
        const folded = prefix.normalize("NFKC").toLowerCase();
        const previous = portablePaths.get(folded);
        if (previous !== undefined && previous !== prefix) {
          throw new Error(
            `asset inventory contains case-colliding paths: ${previous}, ${prefix}`,
          );
        }
        portablePaths.set(folded, prefix);
      }
      const child = join(directory, entry.name);
      const metadata = await lstat(child, { bigint: true });
      if (metadata.isSymbolicLink()) {
        throw new Error(`forbidden staged payload: ${relativePath}`);
      }
      if (metadata.isDirectory()) {
        await visit(child, childParts);
        continue;
      }
      if (!metadata.isFile()) {
        throw new Error(`forbidden staged payload: ${relativePath}`);
      }
      rejectForbiddenPayload(relativePath);
      const inspected = await inspectRegularFile(
        child,
        `asset ${relativePath}`,
      );
      inventory[relativePath] = {
        size: inspected.size,
        sha256: inspected.sha256,
      };
    }
  }

  await visit(root, []);
  return inventory;
}

function validateDeclaredInventory(files, label) {
  if (files === null || Array.isArray(files) || typeof files !== "object") {
    throw new Error(`${label} files must be an object`);
  }
  const portablePaths = new Map();
  for (const relativePath of Object.keys(files).sort(compareStrings)) {
    rejectForbiddenPayload(relativePath);
    const parts = validateRelativePath(relativePath);
    for (let length = 1; length <= parts.length; length += 1) {
      const prefix = parts.slice(0, length).join("/");
      const folded = prefix.normalize("NFKC").toLowerCase();
      const previous = portablePaths.get(folded);
      if (previous !== undefined && previous !== prefix) {
        throw new Error(
          `${label} contains case-colliding paths: ${previous}, ${prefix}`,
        );
      }
      portablePaths.set(folded, prefix);
    }
    const entry = files[relativePath];
    requireExactKeys(entry, new Set(["sha256", "size"]), `${label} file entry`);
    if (
      !Number.isSafeInteger(entry.size) ||
      entry.size < 0 ||
      typeof entry.sha256 !== "string" ||
      !SHA256_PATTERN.test(entry.sha256)
    ) {
      throw new Error(`${label} file entry is invalid: ${relativePath}`);
    }
  }
}

function inventoriesEqual(declared, actual) {
  const declaredBytes = canonicalJsonBytes(declared);
  const actualBytes = canonicalJsonBytes(actual);
  return declaredBytes.equals(actualBytes);
}

async function validateAssetReceipt(receiptRecord, expectedKind) {
  const receipt = receiptRecord.value;
  if (receipt.schema !== ASSET_RECEIPT_SCHEMA || receipt.kind !== expectedKind) {
    throw new Error(`${expectedKind} asset receipt schema or kind mismatch`);
  }
  requireCommit(receipt.source_commit, `${expectedKind} source commit`);
  requireSha256(receipt.lock_sha256, `${expectedKind} lock digest`);
  requireString(receipt.root, `${expectedKind} root`);
  if (!isAbsolute(receipt.root)) {
    throw new Error(`${expectedKind} asset root must be absolute`);
  }
  const root = await realpath(receipt.root);
  if (root !== resolve(receipt.root)) {
    throw new Error(`${expectedKind} asset root must not traverse symlinks`);
  }
  validateDeclaredInventory(receipt.files, `${expectedKind} receipt`);
  if (expectedKind === "notices") {
    const noticeFiles = Object.keys(receipt.files).sort(compareStrings);
    if (
      noticeFiles.length !== 2 ||
      noticeFiles[0] !== "LICENSE" ||
      noticeFiles[1] !== "THIRD_PARTY_NOTICES.txt"
    ) {
      throw new Error("notices receipt must contain the complete exact notice set");
    }
  }
  if (expectedKind === "desktop-credential") {
    const expected = [
      "form.js",
      "index.html",
      "preload.js",
      "styles.css",
    ];
    if (
      Object.keys(receipt.files).sort(compareStrings).join("\n") !==
      expected.join("\n")
    ) {
      throw new Error("desktop credential receipt inventory mismatch");
    }
  }
  if (expectedKind === "web" && !Object.hasOwn(receipt.files, "index.html")) {
    throw new Error("web receipt inventory is missing index.html");
  }
  const actual = await inventoryRoot(root);
  if (!inventoriesEqual(receipt.files, actual)) {
    throw new Error(`${expectedKind} asset inventory mismatch`);
  }
  return { ...receiptRecord, root };
}

function validateIdentity(identity) {
  if (
    identity.build_mode !== "developer" ||
    identity.key_id !== "developer"
  ) {
    throw new Error("stage-resources only creates developer-signed resources");
  }
  requireCommit(identity.source_commit, "identity source commit");
  if (
    typeof identity.app_version !== "string" ||
    !VERSION_PATTERN.test(identity.app_version)
  ) {
    throw new Error("identity app version is invalid");
  }
  if (!["darwin", "linux", "win32"].includes(identity.platform)) {
    throw new Error("identity platform is invalid");
  }
  if (!["arm64", "x64"].includes(identity.architecture)) {
    throw new Error("identity architecture is invalid");
  }
  if (
    identity.platform !== process.platform ||
    identity.architecture !== process.arch
  ) {
    throw new Error("identity platform or architecture does not match the staging host");
  }
  for (const name of [
    "python_lock_sha256",
    "desktop_npm_lock_sha256",
    "web_npm_lock_sha256",
    "sbom_sha256",
  ]) {
    requireSha256(identity[name], `identity ${name}`);
  }
}

function validateSidecarReceipt(receipt, identity) {
  if (receipt.schema !== SIDECAR_RECEIPT_SCHEMA) {
    throw new Error("sidecar receipt schema mismatch");
  }
  if (receipt.upx_enabled !== false || receipt.pyinstaller_version !== "6.21.0") {
    throw new Error("sidecar receipt must prove PyInstaller 6.21.0 with UPX disabled");
  }
  for (const name of [
    "binary_sha256",
    "entrypoint_sha256",
    "python_executable_sha256",
    "python_lock_sha256",
    "spec_sha256",
    "web_asset_receipt_sha256",
  ]) {
    requireSha256(receipt[name], `sidecar ${name}`);
  }
  if (
    !Number.isSafeInteger(receipt.binary_size) ||
    receipt.binary_size <= 0 ||
    !isAbsolute(receipt.binary_path)
  ) {
    throw new Error("sidecar binary metadata is invalid");
  }
  for (const name of [
    "source_commit",
    "app_version",
    "platform",
    "architecture",
    "python_lock_sha256",
  ]) {
    if (receipt[name] !== identity[name]) {
      throw new Error(`sidecar receipt ${name} mismatch`);
    }
  }
}

function validateSbomReceipt(receipt, identity) {
  if (receipt.schema !== SBOM_RECEIPT_SCHEMA) {
    throw new Error("SBOM receipt schema mismatch");
  }
  for (const name of [
    "source_commit",
    "app_version",
    "python_lock_sha256",
    "desktop_npm_lock_sha256",
    "web_npm_lock_sha256",
    "sbom_sha256",
  ]) {
    if (receipt[name] !== identity[name]) {
      throw new Error(`SBOM receipt ${name} mismatch`);
    }
  }
  for (const name of [
    "desktop_npm_lock_sha256",
    "python_lock_sha256",
    "sbom_sha256",
    "sidecar_binary_sha256",
    "web_asset_receipt_sha256",
    "web_npm_lock_sha256",
  ]) {
    requireSha256(receipt[name], `SBOM receipt ${name}`);
  }
  if (
    !Number.isSafeInteger(receipt.sbom_size) ||
    receipt.sbom_size <= 0 ||
    !isAbsolute(receipt.sbom_path)
  ) {
    throw new Error("SBOM receipt file metadata is invalid");
  }
}

function parseArguments(argumentsValue) {
  if (argumentsValue.length % 2 !== 0) {
    throw new Error("every stage-resources option requires one value");
  }
  const parsed = {};
  for (let index = 0; index < argumentsValue.length; index += 2) {
    const name = argumentsValue[index];
    const value = argumentsValue[index + 1];
    if (!ARGUMENT_NAMES.has(name)) {
      throw new Error(`unknown stage-resources option: ${name}`);
    }
    if (Object.hasOwn(parsed, name)) {
      throw new Error(`duplicate stage-resources option: ${name}`);
    }
    if (typeof value !== "string" || value.length === 0) {
      throw new Error(`missing stage-resources value for ${name}`);
    }
    parsed[name] = value;
  }
  for (const name of ARGUMENT_NAMES) {
    if (!Object.hasOwn(parsed, name)) {
      throw new Error(`missing required stage-resources option: ${name}`);
    }
  }
  return parsed;
}

function assertOutputBoundary(output, receiptPath) {
  const root = parse(output).root;
  if (output === root || output === REPOSITORY_ROOT) {
    throw new Error("unsafe stage output path");
  }
  const receiptRelative = relative(output, receiptPath);
  if (
    receiptRelative === "" ||
    (!receiptRelative.startsWith(`..${sep}`) && receiptRelative !== "..")
  ) {
    throw new Error("stage receipt must be outside the staged resource root");
  }
}

async function copyVerified(source, destination, expected, executable = false) {
  await mkdir(dirname(destination), { recursive: true });
  await copyFile(source, destination, fsConstants.COPYFILE_EXCL);
  if (executable && process.platform !== "win32") {
    const handle = await open(destination, fsConstants.O_RDONLY);
    try {
      await handle.chmod(0o755);
    } finally {
      await handle.close();
    }
  }
  const copied = await inspectRegularFile(destination, `copied ${destination}`);
  if (copied.size !== expected.size || copied.sha256 !== expected.sha256) {
    throw new Error(`copied payload digest mismatch: ${destination}`);
  }
}

function runPython(pythonExecutable, argumentsValue, label) {
  const completed = spawnSync(pythonExecutable, argumentsValue, {
    cwd: REPOSITORY_ROOT,
    encoding: "utf8",
    maxBuffer: 1024 * 1024,
    windowsHide: true,
  });
  if (completed.error) {
    throw new Error(`${label} failed to start: ${completed.error.message}`);
  }
  if (completed.status !== 0) {
    const detail = (completed.stderr || completed.stdout || "").trim();
    throw new Error(`${label} failed: ${detail}`);
  }
}

async function writeExclusive(pathValue, payload, mode = 0o644) {
  await mkdir(dirname(pathValue), { recursive: true });
  const handle = await open(
    pathValue,
    fsConstants.O_WRONLY | fsConstants.O_CREAT | fsConstants.O_EXCL,
    mode,
  );
  try {
    await handle.writeFile(payload);
    await handle.sync();
  } finally {
    await handle.close();
  }
}

async function stageResources(argumentsValue) {
  const argumentsMap = parseArguments(argumentsValue);
  const output = resolve(argumentsMap["--output"]);
  const stageReceiptPath = resolve(argumentsMap["--receipt"]);
  assertOutputBoundary(output, stageReceiptPath);
  const outputParent = dirname(output);
  await mkdir(outputParent, { recursive: true });
  try {
    await lstat(output);
    throw new Error("staged resource output must not already exist");
  } catch (error) {
    if (error.code !== "ENOENT") {
      throw error;
    }
  }
  try {
    await lstat(stageReceiptPath);
    throw new Error("stage receipt must not already exist");
  } catch (error) {
    if (error.code !== "ENOENT") {
      throw error;
    }
  }
  const pythonExecutable = resolve(argumentsMap["--python"]);
  const pythonMetadata = await lstat(pythonExecutable);
  if (!pythonMetadata.isFile() && !pythonMetadata.isSymbolicLink()) {
    throw new Error("Python executable is invalid");
  }

  const [sidecarRecord, webRecord, desktopRecord, noticesRecord, sbomRecord, identityRecord] =
    await Promise.all([
      readCanonicalJson(
        argumentsMap["--sidecar-receipt"],
        "sidecar receipt",
        MAX_CONTROL_BYTES,
        SIDECAR_RECEIPT_KEYS,
      ),
      readCanonicalJson(
        argumentsMap["--web-receipt"],
        "web receipt",
        MAX_ASSET_RECEIPT_BYTES,
        ASSET_RECEIPT_KEYS,
      ),
      readCanonicalJson(
        argumentsMap["--desktop-receipt"],
        "desktop receipt",
        MAX_ASSET_RECEIPT_BYTES,
        ASSET_RECEIPT_KEYS,
      ),
      readCanonicalJson(
        argumentsMap["--notices-receipt"],
        "notices receipt",
        MAX_ASSET_RECEIPT_BYTES,
        ASSET_RECEIPT_KEYS,
      ),
      readCanonicalJson(
        argumentsMap["--sbom-receipt"],
        "SBOM receipt",
        MAX_CONTROL_BYTES,
        SBOM_RECEIPT_KEYS,
      ),
      readCanonicalJson(
        argumentsMap["--identity"],
        "desktop identity",
        MAX_CONTROL_BYTES,
        IDENTITY_KEYS,
      ),
    ]);

  validateIdentity(identityRecord.value);
  validateSidecarReceipt(sidecarRecord.value, identityRecord.value);
  validateSbomReceipt(sbomRecord.value, identityRecord.value);
  const [web, desktop, notices] = await Promise.all([
    validateAssetReceipt(webRecord, "web"),
    validateAssetReceipt(desktopRecord, "desktop-credential"),
    validateAssetReceipt(noticesRecord, "notices"),
  ]);
  const identity = identityRecord.value;
  for (const receipt of [web.value, desktop.value, notices.value, sbomRecord.value]) {
    if (receipt.source_commit !== identity.source_commit) {
      throw new Error("input receipt source commit mismatch");
    }
  }
  if (
    web.value.lock_sha256 !== identity.web_npm_lock_sha256 ||
    desktop.value.lock_sha256 !== identity.desktop_npm_lock_sha256
  ) {
    throw new Error("asset receipt lock digest mismatch");
  }
  if (sidecarRecord.value.web_asset_receipt_sha256 !== webRecord.digest) {
    throw new Error("sidecar web receipt digest mismatch");
  }
  if (
    sbomRecord.value.sidecar_binary_sha256 !==
      sidecarRecord.value.binary_sha256 ||
    sbomRecord.value.web_asset_receipt_sha256 !== webRecord.digest
  ) {
    throw new Error("SBOM receipt sidecar or web receipt digest mismatch");
  }

  const sidecar = await inspectRegularFile(
    sidecarRecord.value.binary_path,
    "sidecar binary",
  );
  if (
    sidecar.size !== sidecarRecord.value.binary_size ||
    sidecar.sha256 !== sidecarRecord.value.binary_sha256
  ) {
    throw new Error("sidecar binary receipt mismatch");
  }
  const sbom = await inspectRegularFile(
    sbomRecord.value.sbom_path,
    "desktop SBOM",
    MAX_SBOM_BYTES,
  );
  if (
    sbom.size !== sbomRecord.value.sbom_size ||
    sbom.sha256 !== sbomRecord.value.sbom_sha256
  ) {
    throw new Error("SBOM file receipt mismatch");
  }

  let createdOutput = false;
  try {
    await mkdir(output);
    createdOutput = true;
    const sidecarName = process.platform === "win32"
      ? "kestrel-desktop-sidecar.exe"
      : "kestrel-desktop-sidecar";
    if (
      sidecarRecord.value.binary_path.split(/[\\/]/).at(-1) !== sidecarName
    ) {
      throw new Error("sidecar binary name mismatch");
    }
    await copyVerified(
      sidecarRecord.value.binary_path,
      join(output, "sidecar", sidecarName),
      sidecar,
      true,
    );
    for (const relativePath of Object.keys(web.value.files).sort(compareStrings)) {
      await copyVerified(
        join(web.root, ...relativePath.split("/")),
        join(output, "web", "dist", ...relativePath.split("/")),
        web.value.files[relativePath],
      );
    }
    for (const relativePath of Object.keys(desktop.value.files).sort(compareStrings)) {
      await copyVerified(
        join(desktop.root, ...relativePath.split("/")),
        join(output, "desktop", "dist", "credential", ...relativePath.split("/")),
        desktop.value.files[relativePath],
      );
    }
    for (const relativePath of Object.keys(notices.value.files).sort(compareStrings)) {
      await copyVerified(
        join(notices.root, ...relativePath.split("/")),
        join(output, "licenses", ...relativePath.split("/")),
        notices.value.files[relativePath],
      );
    }
    await copyVerified(
      sbomRecord.value.sbom_path,
      join(output, "sbom.cdx.json"),
      sbom,
    );

    const { privateKey, publicKey } = generateKeyPairSync("ed25519");
    const publicKeyBytes = Buffer.from(
      publicKey.export({ format: "pem", type: "spki" }),
      "utf8",
    );
    const publicKeyPath = join(output, PUBLIC_KEY_NAME);
    await writeExclusive(publicKeyPath, publicKeyBytes);
    const manifestPath = join(output, MANIFEST_NAME);
    runPython(
      pythonExecutable,
      [
        join(REPOSITORY_ROOT, "scripts", "generate_desktop_resource_manifest.py"),
        output,
        "--identity",
        resolve(argumentsMap["--identity"]),
        "--output",
        manifestPath,
      ],
      "desktop manifest generation",
    );
    const manifest = await inspectRegularFile(
      manifestPath,
      "desktop resource manifest",
      1024 * 1024,
      true,
    );
    const signatureBytes = sign(null, manifest.bytes, privateKey);
    if (signatureBytes.length !== 64) {
      throw new Error("developer manifest signature has an invalid size");
    }
    const signaturePath = join(output, SIGNATURE_NAME);
    await writeExclusive(signaturePath, signatureBytes);
    runPython(
      pythonExecutable,
      [
        join(REPOSITORY_ROOT, "scripts", "verify_desktop_resource_manifest.py"),
        "developer",
        output,
        "--identity",
        resolve(argumentsMap["--identity"]),
        "--public-key",
        publicKeyPath,
      ],
      "desktop manifest verification",
    );

    const signature = await inspectRegularFile(
      signaturePath,
      "desktop resource signature",
      4096,
    );
    const publicKeyReceipt = await inspectRegularFile(
      publicKeyPath,
      "desktop developer public key",
      16 * 1024,
    );
    const stageReceipt = {
      schema: STAGE_RECEIPT_SCHEMA,
      build_mode: identity.build_mode,
      key_id: identity.key_id,
      source_commit: identity.source_commit,
      app_version: identity.app_version,
      platform: identity.platform,
      architecture: identity.architecture,
      resource_root: output,
      sidecar_relative_path: `sidecar/${sidecarName}`,
      manifest_path: manifestPath,
      manifest_sha256: manifest.sha256,
      signature_path: signaturePath,
      signature_sha256: signature.sha256,
      public_key_path: publicKeyPath,
      public_key_sha256: publicKeyReceipt.sha256,
      sbom_sha256: identity.sbom_sha256,
      input_receipt_sha256: {
        desktop: desktopRecord.digest,
        notices: noticesRecord.digest,
        sbom: sbomRecord.digest,
        sidecar: sidecarRecord.digest,
        web: webRecord.digest,
      },
    };
    const stageReceiptBytes = canonicalJsonBytes(stageReceipt);
    if (stageReceiptBytes.length >= MAX_CONTROL_BYTES) {
      throw new Error("stage receipt exceeds 64 KiB");
    }
    await writeExclusive(stageReceiptPath, stageReceiptBytes);
    return stageReceipt;
  } catch (error) {
    if (createdOutput) {
      await rm(output, { force: true, recursive: true });
    }
    throw error;
  }
}

try {
  const receipt = await stageResources(process.argv.slice(2));
  process.stdout.write(`${JSON.stringify(receipt)}\n`);
} catch (error) {
  const message = error instanceof Error ? error.message : String(error);
  process.stderr.write(`stage-resources: ${message}\n`);
  process.exitCode = 1;
}
