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
  realpath
} from "node:fs/promises";
import {
  isAbsolute,
  posix,
  relative,
  resolve,
  sep
} from "node:path";
import { z } from "zod";

const MANIFEST_SCHEMA = "kestrel.desktop.resources.v1";
const MAX_MANIFEST_BYTES = 1024 * 1024;
const MAX_SIGNATURE_BYTES = 4 * 1024;
const READ_CHUNK_BYTES = 1024 * 1024;
const MAX_RENDERER_ASSET_BYTES = 16 * 1024 * 1024;
const MAX_RENDERER_SNAPSHOT_BYTES = 64 * 1024 * 1024;
const RENDERER_PREFIX = "web/dist/";
const sha256Schema = z.string().regex(/^[0-9a-f]{64}$/);
const resourceFileSchema = z
  .object({
    size: z.number().int().nonnegative().safe(),
    sha256: sha256Schema
  })
  .strict();
const resourceManifestSchema = z
  .object({
    schema: z.literal(MANIFEST_SCHEMA),
    key_id: z.string().trim().min(1).max(128),
    files: z.record(z.string(), resourceFileSchema)
  })
  .strict();

export interface ResourceManifestFile {
  size: number;
  sha256: string;
}

export interface ResourceManifest {
  schema: typeof MANIFEST_SCHEMA;
  key_id: string;
  files: Record<string, ResourceManifestFile>;
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
}

export interface VerifiedRendererAssets {
  readonly totalBytes: number;
  read(relativePath: string): Uint8Array | undefined;
}

export interface VerifyResourceManifestInput {
  resourceRoot: string;
  manifestPath: string;
  signaturePath: string;
  trustedKeys: ReadonlyMap<string, KeyObject>;
  requiredFiles: readonly string[];
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
      | "resource_signature_invalid"
      | "resource_signature_too_large"
      | "resource_signing_key_untrusted"
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

function canonicalValue(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map(canonicalValue);
  }
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value)
        .sort(([left], [right]) => compareUnicodeCodePoints(left, right))
        .map(([key, child]) => [key, canonicalValue(child)])
    );
  }
  return value;
}

export function canonicalResourceManifestBytes(
  manifest: ResourceManifest
): Buffer {
  return Buffer.from(
    `${JSON.stringify(canonicalValue(manifest))}\n`,
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
    !after.isFile() ||
    after.isSymbolicLink() ||
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

export async function verifyResourceManifest(
  input: VerifyResourceManifestInput
): Promise<VerifiedResourceSet> {
  let canonicalRoot: string;
  try {
    canonicalRoot = await realpath(input.resourceRoot);
  } catch {
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

  let rendererSnapshotBytes = 0;
  for (const [relativePath, expected] of Object.entries(manifest.files)) {
    if (!relativePath.startsWith(RENDERER_PREFIX)) {
      continue;
    }
    if (expected.size > MAX_RENDERER_ASSET_BYTES) {
      throw new ResourceVerificationError("renderer_asset_too_large");
    }
    rendererSnapshotBytes += expected.size;
    if (
      !Number.isSafeInteger(rendererSnapshotBytes) ||
      rendererSnapshotBytes > MAX_RENDERER_SNAPSHOT_BYTES
    ) {
      throw new ResourceVerificationError("renderer_snapshot_too_large");
    }
  }

  const verifiedFiles = new Map<string, VerifiedResourceFile>();
  const rendererAssets = new Map<string, Buffer>();
  for (const [relativePath, expected] of Object.entries(manifest.files)) {
    const rendererRelativePath = relativePath.startsWith(RENDERER_PREFIX)
      ? relativePath.slice(RENDERER_PREFIX.length)
      : undefined;
    const verified = await verifyResourceFile(
      canonicalRoot,
      relativePath,
      expected,
      rendererRelativePath !== undefined
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
  }
  return {
    resourceRoot: canonicalRoot,
    manifestDigest: `sha256:${createHash("sha256")
      .update(manifestBytes)
      .digest("hex")}`,
    manifest,
    files: verifiedFiles,
    rendererAssets: new ImmutableRendererAssets(rendererAssets)
  };
}
