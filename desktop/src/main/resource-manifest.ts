import {
  createHash,
  timingSafeEqual,
  verify as verifySignature,
  type KeyObject
} from "node:crypto";
import { constants } from "node:fs";
import {
  lstat,
  open,
  readdir,
  realpath
} from "node:fs/promises";
import {
  isAbsolute,
  join,
  posix,
  relative,
  resolve,
  sep
} from "node:path";
import { z } from "zod";

const MANIFEST_SCHEMA = "kestrel.desktop.resources.v1";
const MANIFEST_NAME = "kestrel-resource-manifest.json";
const SIGNATURE_NAME = "kestrel-resource-manifest.sig";
const SBOM_NAME = "sbom.cdx.json";
const MAX_MANIFEST_BYTES = 1024 * 1024;
const MAX_SIGNATURE_BYTES = 4 * 1024;
const READ_CHUNK_BYTES = 1024 * 1024;
const MAX_RENDERER_ASSET_BYTES = 16 * 1024 * 1024;
const MAX_RENDERER_SNAPSHOT_BYTES = 64 * 1024 * 1024;
const RENDERER_PREFIX = "web/dist/";
const MAX_CREDENTIAL_ASSET_BYTES = 4 * 1024 * 1024;
const MAX_CREDENTIAL_SNAPSHOT_BYTES = 8 * 1024 * 1024;
const CREDENTIAL_PREFIX = "desktop/dist/credential/";
const CREDENTIAL_FILES = new Set([
  "index.html",
  "form.js",
  "styles.css",
  "preload.js"
]);

export function resolvePackagedResourceRoot(
  electronResourcesPath: string,
  resourceRootRelative: "kestrel"
): string {
  return join(electronResourcesPath, resourceRootRelative);
}
const sha256Schema = z.string().regex(/^[0-9a-f]{64}$/);
const sourceCommitSchema = z.string().regex(/^[0-9a-f]{40}$/);
const buildModeSchema = z.enum(["developer", "release"]);
const resourceFileSchema = z
  .object({
    size: z.number().int().nonnegative().safe(),
    sha256: sha256Schema
  })
  .strict();
const resourceManifestSchema = z
  .object({
    schema: z.literal(MANIFEST_SCHEMA),
    build_mode: buildModeSchema,
    key_id: buildModeSchema,
    source_commit: sourceCommitSchema,
    app_version: z
      .string()
      .regex(/^[0-9A-Za-z][0-9A-Za-z.+-]{0,63}$/),
    platform: z.enum(["darwin", "linux", "win32"]),
    architecture: z.enum(["arm64", "x64"]),
    python_lock_sha256: sha256Schema,
    desktop_npm_lock_sha256: sha256Schema,
    web_npm_lock_sha256: sha256Schema,
    sbom_sha256: sha256Schema,
    files: z.record(z.string(), resourceFileSchema)
  })
  .strict()
  .refine((value) => value.build_mode === value.key_id);

export interface ResourceManifestFile {
  size: number;
  sha256: string;
}

export interface ResourceManifest {
  schema: typeof MANIFEST_SCHEMA;
  build_mode: "developer" | "release";
  key_id: "developer" | "release";
  source_commit: string;
  app_version: string;
  platform: "darwin" | "linux" | "win32";
  architecture: "arm64" | "x64";
  python_lock_sha256: string;
  desktop_npm_lock_sha256: string;
  web_npm_lock_sha256: string;
  sbom_sha256: string;
  files: Record<string, ResourceManifestFile>;
}

export interface PackagedResourceIdentity {
  readonly buildMode: "developer" | "release";
  readonly keyId: "developer" | "release";
  readonly sourceCommit: string;
  readonly appVersion: string;
  readonly platform: "darwin" | "linux" | "win32";
  readonly architecture: "arm64" | "x64";
  readonly pythonLockSha256: string;
  readonly desktopNpmLockSha256: string;
  readonly webNpmLockSha256: string;
  readonly sbomSha256: string;
}

export interface VerifiedResourceFile extends ResourceManifestFile {
  path: string;
}

export interface VerifiedResourceSet {
  resourceRoot: string;
  manifestDigest: `sha256:${string}`;
  manifest: ResourceManifest;
  files: Map<string, VerifiedResourceFile>;
  rendererAssets: VerifiedRendererAssets;
  credentialAssets: VerifiedCredentialAssets;
}

export interface VerifiedRendererAssets {
  readonly totalBytes: number;
  read(relativePath: string): Uint8Array | undefined;
}

export interface VerifiedCredentialAssets {
  readonly totalBytes: number;
  read(relativePath: string): Uint8Array | undefined;
}

export interface VerifyResourceManifestInput {
  resourceRoot: string;
  manifestPath: string;
  signaturePath: string;
  trustedKeys: ReadonlyMap<string, KeyObject>;
  requiredFiles: readonly string[];
  expectedIdentity: PackagedResourceIdentity;
}

export class ResourceVerificationError extends Error {
  constructor(
    readonly code:
      | "resource_digest_mismatch"
      | "resource_manifest_invalid"
      | "resource_manifest_not_canonical"
      | "resource_manifest_too_large"
      | "resource_missing"
      | "resource_path_untrusted"
      | "resource_build_mode_untrusted"
      | "resource_identity_mismatch"
      | "resource_payload_coverage_mismatch"
      | "resource_sbom_mismatch"
      | "resource_signature_invalid"
      | "resource_signature_too_large"
      | "resource_signing_key_untrusted"
      | "credential_asset_too_large"
      | "credential_snapshot_too_large"
      | "renderer_asset_too_large"
      | "renderer_snapshot_too_large"
  ) {
    super(code);
    this.name = "ResourceVerificationError";
  }
}

function compareUnicodeCodePoints(left: string, right: string): number {
  const leftPoints = Array.from(left, (value) => value.codePointAt(0) ?? 0);
  const rightPoints = Array.from(right, (value) => value.codePointAt(0) ?? 0);
  const length = Math.min(leftPoints.length, rightPoints.length);
  for (let index = 0; index < length; index += 1) {
    const difference = (leftPoints[index] ?? 0) - (rightPoints[index] ?? 0);
    if (difference !== 0) {
      return difference;
    }
  }
  return leftPoints.length - rightPoints.length;
}

function canonicalJson(value: unknown): string {
  if (value === null) {
    return "null";
  }
  if (Array.isArray(value)) {
    return `[${value.map(canonicalJson).join(",")}]`;
  }
  if (typeof value === "object") {
    const entries = Object.entries(value).sort(([left], [right]) =>
      compareUnicodeCodePoints(left, right)
    );
    return `{${entries
      .map(
        ([key, child]) =>
          `${JSON.stringify(key)}:${canonicalJson(child)}`
      )
      .join(",")}}`;
  }
  if (
    typeof value === "string" ||
    typeof value === "boolean" ||
    (typeof value === "number" && Number.isFinite(value))
  ) {
    const encoded = JSON.stringify(value);
    if (encoded !== undefined) {
      return encoded;
    }
  }
  throw new ResourceVerificationError("resource_manifest_invalid");
}

export function canonicalResourceManifestBytes(
  manifest: ResourceManifest
): Buffer {
  return Buffer.from(
    `${canonicalJson(manifest)}\n`,
    "utf8"
  );
}

function isContained(root: string, candidate: string): boolean {
  const fromRoot = relative(root, candidate);
  return (
    fromRoot === "" ||
    (!isAbsolute(fromRoot) &&
      fromRoot !== ".." &&
      !fromRoot.startsWith(`..${sep}`))
  );
}

function normalizeManifestPath(value: string): string {
  if (
    value.length === 0 ||
    value.includes("\\") ||
    value.includes("\0") ||
    Array.from(value).some((character) => {
      const point = character.codePointAt(0) ?? 0;
      return point < 32 || point === 127;
    }) ||
    value.startsWith("/") ||
    value.endsWith("/")
  ) {
    throw new ResourceVerificationError("resource_path_untrusted");
  }
  const segments = value.split("/");
  if (
    segments.some(
      (segment) => segment.length === 0 || segment === "." || segment === ".."
    )
  ) {
    throw new ResourceVerificationError("resource_path_untrusted");
  }
  const normalized = posix.normalize(value);
  if (normalized !== value || posix.isAbsolute(normalized)) {
    throw new ResourceVerificationError("resource_path_untrusted");
  }
  return normalized;
}

function portableCasefold(value: string): string {
  return value.normalize("NFKC").toUpperCase().toLowerCase();
}

function registerPortableResourcePath(
  portablePaths: Map<string, string>,
  relativePath: string
): void {
  const segments = relativePath.split("/");
  for (let length = 1; length <= segments.length; length += 1) {
    const prefix = segments.slice(0, length).join("/");
    const folded = portableCasefold(prefix);
    const previous = portablePaths.get(folded);
    if (previous !== undefined && previous !== prefix) {
      throw new ResourceVerificationError("resource_path_untrusted");
    }
    portablePaths.set(folded, prefix);
  }
}

export function validatePortableResourcePaths(
  relativePaths: readonly string[]
): void {
  const portablePaths = new Map<string, string>();
  for (const relativePath of relativePaths) {
    const normalized = normalizeManifestPath(relativePath);
    registerPortableResourcePath(portablePaths, normalized);
  }
}

async function inventoryResourceFiles(
  canonicalRoot: string
): Promise<Set<string>> {
  const files = new Set<string>();
  const portablePaths = new Map<string, string>();
  const visit = async (
    directory: string,
    segments: readonly string[]
  ): Promise<void> => {
    let entries;
    try {
      entries = await readdir(directory, { withFileTypes: true });
    } catch {
      throw new ResourceVerificationError("resource_path_untrusted");
    }
    entries.sort((left, right) =>
      compareUnicodeCodePoints(left.name, right.name)
    );
    for (const entry of entries) {
      const relativePath = [...segments, entry.name].join("/");
      const normalized = normalizeManifestPath(relativePath);
      registerPortableResourcePath(portablePaths, normalized);
      const candidate = resolve(directory, entry.name);
      let metadata;
      try {
        metadata = await lstat(candidate);
      } catch {
        throw new ResourceVerificationError("resource_path_untrusted");
      }
      if (metadata.isSymbolicLink()) {
        throw new ResourceVerificationError("resource_path_untrusted");
      }
      if (metadata.isDirectory()) {
        await visit(candidate, [...segments, entry.name]);
        continue;
      }
      if (!metadata.isFile() || metadata.nlink !== 1) {
        throw new ResourceVerificationError("resource_path_untrusted");
      }
      if (
        segments.length === 0 &&
        (normalized === MANIFEST_NAME || normalized === SIGNATURE_NAME)
      ) {
        continue;
      }
      files.add(normalized);
    }
  };
  await visit(canonicalRoot, []);
  return files;
}

function manifestIdentity(
  manifest: ResourceManifest
): PackagedResourceIdentity {
  return {
    buildMode: manifest.build_mode,
    keyId: manifest.key_id,
    sourceCommit: manifest.source_commit,
    appVersion: manifest.app_version,
    platform: manifest.platform,
    architecture: manifest.architecture,
    pythonLockSha256: manifest.python_lock_sha256,
    desktopNpmLockSha256: manifest.desktop_npm_lock_sha256,
    webNpmLockSha256: manifest.web_npm_lock_sha256,
    sbomSha256: manifest.sbom_sha256
  };
}

function identityMatches(
  actual: PackagedResourceIdentity,
  expected: PackagedResourceIdentity
): boolean {
  return (
    actual.buildMode === expected.buildMode &&
    actual.keyId === expected.keyId &&
    actual.sourceCommit === expected.sourceCommit &&
    actual.appVersion === expected.appVersion &&
    actual.platform === expected.platform &&
    actual.architecture === expected.architecture &&
    actual.pythonLockSha256 === expected.pythonLockSha256 &&
    actual.desktopNpmLockSha256 === expected.desktopNpmLockSha256 &&
    actual.webNpmLockSha256 === expected.webNpmLockSha256 &&
    actual.sbomSha256 === expected.sbomSha256
  );
}

async function stableOpen(path: string, maxBytes: number) {
  let before;
  try {
    before = await lstat(path);
  } catch {
    throw new ResourceVerificationError("resource_missing");
  }
  if (!before.isFile() || before.isSymbolicLink() || before.nlink !== 1) {
    throw new ResourceVerificationError("resource_path_untrusted");
  }
  if (before.size > maxBytes) {
    throw new ResourceVerificationError(
      maxBytes === MAX_MANIFEST_BYTES
        ? "resource_manifest_too_large"
        : "resource_signature_too_large"
    );
  }
  const flags =
    constants.O_RDONLY |
    (constants.O_NOFOLLOW ?? 0);
  let handle;
  try {
    handle = await open(path, flags);
  } catch {
    throw new ResourceVerificationError("resource_path_untrusted");
  }
  const opened = await handle.stat();
  const after = await lstat(path);
  if (
    !opened.isFile() ||
    opened.nlink !== 1 ||
    !after.isFile() ||
    after.isSymbolicLink() ||
    after.nlink !== 1 ||
    opened.dev !== before.dev ||
    opened.ino !== before.ino ||
    after.dev !== opened.dev ||
    after.ino !== opened.ino ||
    opened.size !== before.size ||
    after.size !== opened.size
  ) {
    await handle.close();
    throw new ResourceVerificationError("resource_path_untrusted");
  }
  return { handle, metadata: opened };
}

async function readStableBounded(
  path: string,
  maxBytes: number
): Promise<Buffer> {
  const { handle, metadata } = await stableOpen(path, maxBytes);
  try {
    const bytes = Buffer.allocUnsafe(metadata.size);
    let position = 0;
    while (position < metadata.size) {
      const { bytesRead } = await handle.read(
        bytes,
        position,
        metadata.size - position,
        position
      );
      if (bytesRead <= 0) {
        throw new ResourceVerificationError("resource_path_untrusted");
      }
      position += bytesRead;
    }
    const final = await handle.stat();
    if (
      bytes.byteLength !== metadata.size ||
      final.size !== metadata.size ||
      final.mtimeMs !== metadata.mtimeMs
    ) {
      throw new ResourceVerificationError("resource_path_untrusted");
    }
    return bytes;
  } finally {
    await handle.close();
  }
}

async function reviewedResourcePath(
  canonicalRoot: string,
  relativePath: string
): Promise<string> {
  const normalized = normalizeManifestPath(relativePath);
  const candidate = resolve(canonicalRoot, ...normalized.split("/"));
  if (!isContained(canonicalRoot, candidate)) {
    throw new ResourceVerificationError("resource_path_untrusted");
  }
  let candidateMetadata;
  let canonicalCandidate;
  try {
    candidateMetadata = await lstat(candidate);
    canonicalCandidate = await realpath(candidate);
  } catch {
    throw new ResourceVerificationError("resource_missing");
  }
  if (
    candidateMetadata.isSymbolicLink() ||
    !candidateMetadata.isFile() ||
    !isContained(canonicalRoot, canonicalCandidate)
  ) {
    throw new ResourceVerificationError("resource_path_untrusted");
  }
  return canonicalCandidate;
}

async function reviewedControlPath(
  canonicalRoot: string,
  candidate: string
): Promise<string> {
  let metadata;
  let canonical;
  try {
    metadata = await lstat(candidate);
    canonical = await realpath(candidate);
  } catch {
    throw new ResourceVerificationError("resource_missing");
  }
  if (
    metadata.isSymbolicLink() ||
    !metadata.isFile() ||
    !isContained(canonicalRoot, canonical)
  ) {
    throw new ResourceVerificationError("resource_path_untrusted");
  }
  return canonical;
}

async function verifyResourceFile(
  canonicalRoot: string,
  relativePath: string,
  expected: ResourceManifestFile,
  captureBytes: boolean
): Promise<{
  file: VerifiedResourceFile;
  captured?: Buffer;
}> {
  const path = await reviewedResourcePath(canonicalRoot, relativePath);
  const { handle, metadata } = await stableOpen(path, Number.MAX_SAFE_INTEGER);
  try {
    if (metadata.size !== expected.size) {
      throw new ResourceVerificationError("resource_digest_mismatch");
    }
    const digest = createHash("sha256");
    const buffer = Buffer.allocUnsafe(READ_CHUNK_BYTES);
    const captured = captureBytes
      ? Buffer.allocUnsafe(metadata.size)
      : undefined;
    let position = 0;
    while (position < metadata.size) {
      const length = Math.min(buffer.byteLength, metadata.size - position);
      const { bytesRead } = await handle.read(buffer, 0, length, position);
      if (bytesRead <= 0) {
        throw new ResourceVerificationError("resource_digest_mismatch");
      }
      digest.update(buffer.subarray(0, bytesRead));
      captured?.set(buffer.subarray(0, bytesRead), position);
      position += bytesRead;
    }
    const final = await handle.stat();
    if (final.size !== metadata.size || final.mtimeMs !== metadata.mtimeMs) {
      throw new ResourceVerificationError("resource_path_untrusted");
    }
    const actual = digest.digest("hex");
    if (
      actual.length !== expected.sha256.length ||
      !timingSafeEqual(Buffer.from(actual), Buffer.from(expected.sha256))
    ) {
      throw new ResourceVerificationError("resource_digest_mismatch");
    }
    return {
      file: {
        path,
        size: metadata.size,
        sha256: actual
      },
      ...(captured === undefined ? {} : { captured })
    };
  } finally {
    await handle.close();
  }
}

class ImmutableRendererAssets implements VerifiedRendererAssets {
  readonly totalBytes: number;
  readonly #assets: Map<string, Buffer>;

  constructor(assets: ReadonlyMap<string, Uint8Array>) {
    this.#assets = new Map(
      [...assets].map(([path, bytes]) => [path, Buffer.from(bytes)])
    );
    this.totalBytes = [...this.#assets.values()].reduce(
      (total, bytes) => total + bytes.byteLength,
      0
    );
  }

  read(relativePath: string): Uint8Array | undefined {
    const bytes = this.#assets.get(relativePath);
    return bytes === undefined ? undefined : Uint8Array.from(bytes);
  }
}

function parseCanonicalManifest(bytes: Buffer): ResourceManifest {
  let decoded: unknown;
  try {
    decoded = JSON.parse(bytes.toString("utf8"));
  } catch {
    throw new ResourceVerificationError("resource_manifest_invalid");
  }
  const result = resourceManifestSchema.safeParse(decoded);
  if (!result.success || Object.keys(result.data.files).length === 0) {
    throw new ResourceVerificationError("resource_manifest_invalid");
  }
  const manifest = result.data as ResourceManifest;
  for (const relativePath of Object.keys(manifest.files)) {
    normalizeManifestPath(relativePath);
  }
  const canonical = canonicalResourceManifestBytes(manifest);
  if (
    canonical.byteLength !== bytes.byteLength ||
    !timingSafeEqual(canonical, bytes)
  ) {
    throw new ResourceVerificationError("resource_manifest_not_canonical");
  }
  return manifest;
}

async function verifyResourceManifestForMode(
  input: VerifyResourceManifestInput,
  expectedMode: "developer" | "release"
): Promise<VerifiedResourceSet> {
  let canonicalRoot: string;
  try {
    const rootMetadata = await lstat(input.resourceRoot);
    if (
      rootMetadata.isSymbolicLink() ||
      !rootMetadata.isDirectory()
    ) {
      throw new ResourceVerificationError(
        "resource_path_untrusted"
      );
    }
    canonicalRoot = await realpath(input.resourceRoot);
  } catch {
    throw new ResourceVerificationError("resource_path_untrusted");
  }
  if (
    resolve(input.manifestPath) !==
      resolve(input.resourceRoot, MANIFEST_NAME) ||
    resolve(input.signaturePath) !==
      resolve(input.resourceRoot, SIGNATURE_NAME)
  ) {
    throw new ResourceVerificationError("resource_path_untrusted");
  }
  const manifestPath = await reviewedControlPath(
    canonicalRoot,
    input.manifestPath
  );
  const signaturePath = await reviewedControlPath(
    canonicalRoot,
    input.signaturePath
  );
  const manifestBytes = await readStableBounded(
    manifestPath,
    MAX_MANIFEST_BYTES
  );
  const signatureBytes = await readStableBounded(
    signaturePath,
    MAX_SIGNATURE_BYTES
  );
  if (signatureBytes.byteLength !== 64) {
    throw new ResourceVerificationError("resource_signature_invalid");
  }
  const manifest = parseCanonicalManifest(manifestBytes);
  if (
    manifest.build_mode !== expectedMode ||
    manifest.key_id !== expectedMode ||
    input.expectedIdentity.buildMode !== expectedMode ||
    input.expectedIdentity.keyId !== expectedMode
  ) {
    throw new ResourceVerificationError(
      "resource_build_mode_untrusted"
    );
  }
  if (
    !identityMatches(
      manifestIdentity(manifest),
      input.expectedIdentity
    )
  ) {
    throw new ResourceVerificationError("resource_identity_mismatch");
  }
  const trustedKey = input.trustedKeys.get(manifest.key_id);
  if (
    trustedKey === undefined ||
    trustedKey.type !== "public" ||
    trustedKey.asymmetricKeyType !== "ed25519"
  ) {
    throw new ResourceVerificationError("resource_signing_key_untrusted");
  }
  let valid = false;
  try {
    valid = verifySignature(null, manifestBytes, trustedKey, signatureBytes);
  } catch {
    valid = false;
  }
  if (!valid) {
    throw new ResourceVerificationError("resource_signature_invalid");
  }

  const required = new Set(
    input.requiredFiles.map((path) => normalizeManifestPath(path))
  );
  for (const requiredPath of required) {
    if (!Object.hasOwn(manifest.files, requiredPath)) {
      throw new ResourceVerificationError("resource_missing");
    }
  }

  const sbom = manifest.files[SBOM_NAME];
  if (
    sbom === undefined ||
    sbom.sha256 !== manifest.sbom_sha256
  ) {
    throw new ResourceVerificationError("resource_sbom_mismatch");
  }

  let rendererSnapshotBytes = 0;
  let credentialSnapshotBytes = 0;
  for (const [relativePath, expected] of Object.entries(manifest.files)) {
    if (relativePath.startsWith(RENDERER_PREFIX)) {
      if (expected.size > MAX_RENDERER_ASSET_BYTES) {
        throw new ResourceVerificationError(
          "renderer_asset_too_large"
        );
      }
      rendererSnapshotBytes += expected.size;
      if (
        !Number.isSafeInteger(rendererSnapshotBytes) ||
        rendererSnapshotBytes > MAX_RENDERER_SNAPSHOT_BYTES
      ) {
        throw new ResourceVerificationError(
          "renderer_snapshot_too_large"
        );
      }
    }
    if (relativePath.startsWith(CREDENTIAL_PREFIX)) {
      const credentialRelativePath = relativePath.slice(
        CREDENTIAL_PREFIX.length
      );
      if (!CREDENTIAL_FILES.has(credentialRelativePath)) {
        throw new ResourceVerificationError(
          "resource_manifest_invalid"
        );
      }
      if (expected.size > MAX_CREDENTIAL_ASSET_BYTES) {
        throw new ResourceVerificationError(
          "credential_asset_too_large"
        );
      }
      credentialSnapshotBytes += expected.size;
      if (
        !Number.isSafeInteger(credentialSnapshotBytes) ||
        credentialSnapshotBytes >
          MAX_CREDENTIAL_SNAPSHOT_BYTES
      ) {
        throw new ResourceVerificationError(
          "credential_snapshot_too_large"
        );
      }
    }
  }

  const declaredFiles = new Set(Object.keys(manifest.files));
  const stagedFiles = await inventoryResourceFiles(canonicalRoot);
  if (
    declaredFiles.size !== stagedFiles.size ||
    [...declaredFiles].some((path) => !stagedFiles.has(path))
  ) {
    throw new ResourceVerificationError(
      "resource_payload_coverage_mismatch"
    );
  }

  const verifiedFiles = new Map<string, VerifiedResourceFile>();
  const rendererAssets = new Map<string, Buffer>();
  const credentialAssets = new Map<string, Buffer>();
  for (const [relativePath, expected] of Object.entries(manifest.files)) {
    const rendererRelativePath = relativePath.startsWith(RENDERER_PREFIX)
      ? relativePath.slice(RENDERER_PREFIX.length)
      : undefined;
    const credentialRelativePath = relativePath.startsWith(
      CREDENTIAL_PREFIX
    )
      ? relativePath.slice(CREDENTIAL_PREFIX.length)
      : undefined;
    const verified = await verifyResourceFile(
      canonicalRoot,
      relativePath,
      expected,
      rendererRelativePath !== undefined ||
        credentialRelativePath !== undefined
    );
    verifiedFiles.set(
      relativePath,
      verified.file
    );
    if (
      rendererRelativePath !== undefined &&
      rendererRelativePath.length > 0 &&
      verified.captured !== undefined
    ) {
      rendererAssets.set(rendererRelativePath, verified.captured);
    }
    if (
      credentialRelativePath !== undefined &&
      credentialRelativePath.length > 0 &&
      verified.captured !== undefined
    ) {
      credentialAssets.set(
        credentialRelativePath,
        verified.captured
      );
    }
  }
  const finalStagedFiles = await inventoryResourceFiles(canonicalRoot);
  if (
    finalStagedFiles.size !== declaredFiles.size ||
    [...declaredFiles].some((path) => !finalStagedFiles.has(path))
  ) {
    throw new ResourceVerificationError(
      "resource_payload_coverage_mismatch"
    );
  }
  return {
    resourceRoot: canonicalRoot,
    manifestDigest: `sha256:${createHash("sha256")
      .update(manifestBytes)
      .digest("hex")}`,
    manifest,
    files: verifiedFiles,
    rendererAssets: new ImmutableRendererAssets(rendererAssets),
    credentialAssets: new ImmutableRendererAssets(
      credentialAssets
    )
  };
}

export function verifyResourceManifest(
  input: VerifyResourceManifestInput
): Promise<VerifiedResourceSet> {
  return verifyResourceManifestForMode(input, "release");
}

export function verifyDeveloperResourceManifest(
  input: VerifyResourceManifestInput
): Promise<VerifiedResourceSet> {
  return verifyResourceManifestForMode(input, "developer");
}
