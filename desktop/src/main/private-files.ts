import { createHash, randomBytes as secureRandomBytes } from "node:crypto";
import { constants } from "node:fs";
import {
  chmod,
  lstat,
  mkdir,
  open,
  realpath,
  unlink
} from "node:fs/promises";
import {
  dirname,
  isAbsolute,
  join,
  relative,
  resolve,
  sep
} from "node:path";
import { z } from "zod";

const MAX_PRIVATE_JSON_BYTES = 16 * 1024;
const MEMORY_LAYERS = [
  "working",
  "episodic",
  "semantic",
  "procedural",
  "self",
  "policy"
] as const;
const sha256Schema = z.string().regex(/^[0-9a-f]{64}$/);
const prefixedSha256Schema = z.string().regex(/^sha256:[0-9a-f]{64}$/);
const sidecarReadinessSchema = z
  .object({
    schema: z.literal("kestrel.desktop.sidecar_readiness.v1"),
    pid: z.number().int().positive(),
    process_birth_marker: z.string().trim().min(1).max(256),
    port: z.number().int().min(1).max(65_535),
    profile_id: z.string().trim().min(1).max(120),
    sidecar_version: z.string().trim().min(1).max(64),
    executable_digest: sha256Schema,
    resource_manifest_digest: prefixedSha256Schema,
    launch_nonce_digest: sha256Schema
  })
  .strict();

export type SidecarReadiness = z.infer<typeof sidecarReadinessSchema>;

export interface PrivateProfileInput {
  profileId: string;
  profileRoot: string;
  statePath: string;
  memoryDir: string;
  runtimeSettingsPath: string;
}

export interface ResolvedPrivateProfile {
  profileId: string;
  profileRoot: string;
  statePath: string;
  memoryDir: string;
  runtimeSettingsPath: string;
  runtimeDirectory: string;
  readinessPath: string;
  leaseControlRoot: string;
}

export interface PrivateFilePlatformAdapter {
  platform: NodeJS.Platform;
  currentOwnerId(): number | string | null;
  qualifyOwnerOnly(path: string, kind: "directory" | "file"): Promise<void>;
}

export interface PrivateLaunchFiles {
  bootstrapPath: string;
  readinessPath: string;
  launchNonce: string;
  launchNonceDigest: string;
  apiToken: string;
  profile: ResolvedPrivateProfile;
  cleanup(): Promise<void>;
}

export interface CreatePrivateLaunchFilesInput {
  profile: ResolvedPrivateProfile;
  parentPid: number;
  parentBirthMarker: string;
  resourceManifestDigest: string;
  randomBytes?: (size: number) => Uint8Array;
  platformAdapter?: PrivateFilePlatformAdapter;
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

function requiredText(value: string, field: string, maxLength: number): string {
  const normalized = value.trim();
  if (
    normalized.length === 0 ||
    normalized.length > maxLength ||
    normalized.includes("\0")
  ) {
    throw new Error(`${field}_invalid`);
  }
  return normalized;
}

export function createNodePrivateFileAdapter(
  platform: NodeJS.Platform = process.platform
): PrivateFilePlatformAdapter {
  return {
    platform,
    currentOwnerId: () =>
      platform === "win32" || process.getuid === undefined
        ? null
        : process.getuid(),
    async qualifyOwnerOnly(
      path: string,
      kind: "directory" | "file"
    ): Promise<void> {
      if (platform === "win32") {
        throw new Error("windows_owner_acl_unqualified");
      }
      const metadata = await lstat(path);
      if (
        metadata.isSymbolicLink() ||
        (kind === "directory"
          ? !metadata.isDirectory()
          : !metadata.isFile()) ||
        (kind === "file" && metadata.nlink !== 1)
      ) {
        throw new Error("private_artifact_type_untrusted");
      }
      const currentOwner = process.getuid?.();
      if (currentOwner === undefined || metadata.uid !== currentOwner) {
        throw new Error("private_artifact_owner_untrusted");
      }
      const expectedMode = kind === "directory" ? 0o700 : 0o600;
      if ((metadata.mode & 0o777) !== expectedMode) {
        throw new Error("private_artifact_permissions_untrusted");
      }
    }
  };
}

async function preparePrivateDirectory(
  path: string,
  adapter: PrivateFilePlatformAdapter
): Promise<string> {
  await mkdir(path, { recursive: true, mode: 0o700 });
  const leaf = await lstat(path);
  if (leaf.isSymbolicLink() || !leaf.isDirectory()) {
    throw new Error("private_profile_symlink_untrusted");
  }
  if (adapter.platform !== "win32") {
    await chmod(path, 0o700);
  }
  const canonical = await realpath(path);
  await adapter.qualifyOwnerOnly(canonical, "directory");
  return canonical;
}

function resolvedDescendant(
  value: string,
  requestedRoot: string,
  profileRoot: string,
  field: string
): string {
  const requestedCandidate = resolve(value);
  if (
    !isContained(requestedRoot, requestedCandidate) ||
    requestedCandidate === requestedRoot
  ) {
    throw new Error(`${field}_outside_profile`);
  }
  const fromRequestedRoot = relative(requestedRoot, requestedCandidate);
  const candidate = resolve(profileRoot, fromRequestedRoot);
  if (!isContained(profileRoot, candidate) || candidate === profileRoot) {
    throw new Error(`${field}_outside_profile`);
  }
  return candidate;
}

async function validateOptionalPrivateFile(
  path: string,
  profileRoot: string,
  field: string,
  adapter: PrivateFilePlatformAdapter
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
  if (metadata.isSymbolicLink() || !metadata.isFile() || metadata.nlink !== 1) {
    throw new Error(`${field}_symlink_untrusted`);
  }
  const canonical = await realpath(path);
  if (!isContained(profileRoot, canonical)) {
    throw new Error(`${field}_outside_profile`);
  }
  await adapter.qualifyOwnerOnly(canonical, "file");
}

function leaseControlRoot(
  statePath: string,
  memoryDir: string,
  profileId: string
): string {
  const identity = JSON.stringify({
    memory_dir: memoryDir,
    profile_id: profileId,
    schema: "kestrel.runtime_profile_control.v1",
    state_path: statePath
  });
  return join(
    dirname(statePath),
    ".kestrel-runtime-profiles",
    createHash("sha256").update(identity).digest("hex")
  );
}

export async function resolvePrivateProfile(
  input: PrivateProfileInput,
  adapter: PrivateFilePlatformAdapter = createNodePrivateFileAdapter()
): Promise<ResolvedPrivateProfile> {
  const profileId = requiredText(input.profileId, "profile_id", 120);
  const requestedRoot = resolve(input.profileRoot);
  const profileRoot = await preparePrivateDirectory(requestedRoot, adapter);
  const statePath = resolvedDescendant(
    input.statePath,
    requestedRoot,
    profileRoot,
    "state_path"
  );
  const memoryDir = resolvedDescendant(
    input.memoryDir,
    requestedRoot,
    profileRoot,
    "memory_dir"
  );
  const runtimeSettingsPath = resolvedDescendant(
    input.runtimeSettingsPath,
    requestedRoot,
    profileRoot,
    "runtime_settings_path"
  );
  const runtimeDirectory = join(profileRoot, "runtime");

  for (const directory of [
    dirname(statePath),
    memoryDir,
    dirname(runtimeSettingsPath),
    runtimeDirectory
  ]) {
    const canonical = await preparePrivateDirectory(directory, adapter);
    if (!isContained(profileRoot, canonical)) {
      throw new Error("private_profile_path_untrusted");
    }
  }
  await validateOptionalPrivateFile(
    statePath,
    profileRoot,
    "state_path",
    adapter
  );
  await validateOptionalPrivateFile(
    runtimeSettingsPath,
    profileRoot,
    "runtime_settings_path",
    adapter
  );

  return {
    profileId,
    profileRoot,
    statePath,
    memoryDir,
    runtimeSettingsPath,
    runtimeDirectory,
    readinessPath: join(runtimeDirectory, "desktop-readiness.json"),
    leaseControlRoot: leaseControlRoot(
      statePath,
      memoryDir,
      profileId
    )
  };
}

function randomSecret(
  randomBytes: (size: number) => Uint8Array,
  field: string
): string {
  const bytes = Buffer.from(randomBytes(32));
  if (bytes.byteLength !== 32) {
    throw new Error(`${field}_must_be_256-bit`);
  }
  return bytes.toString("hex");
}

async function openExclusivePrivateFile(path: string) {
  const flags =
    constants.O_WRONLY |
    constants.O_CREAT |
    constants.O_EXCL |
    (constants.O_NOFOLLOW ?? 0);
  return open(path, flags, 0o600);
}

async function sameFile(path: string, dev: number, ino: number): Promise<boolean> {
  try {
    const metadata = await lstat(path);
    return (
      !metadata.isSymbolicLink() &&
      metadata.isFile() &&
      metadata.dev === dev &&
      metadata.ino === ino
    );
  } catch {
    return false;
  }
}

async function removeCapturedFile(
  path: string,
  identity: { dev: number; ino: number } | null
): Promise<void> {
  if (
    identity !== null &&
    (await sameFile(path, identity.dev, identity.ino))
  ) {
    await unlink(path).catch((error: NodeJS.ErrnoException) => {
      if (error.code !== "ENOENT") {
        throw error;
      }
    });
  }
}

export async function readPrivateJsonArtifact(
  path: string,
  adapter: PrivateFilePlatformAdapter
): Promise<unknown> {
  let before;
  try {
    before = await lstat(path);
  } catch (error) {
    const candidate = error as NodeJS.ErrnoException;
    if (candidate.code === "ENOENT") {
      throw new Error("private_artifact_missing");
    }
    throw error;
  }
  if (before.isSymbolicLink()) {
    throw new Error("private_artifact_symlink_untrusted");
  }
  if (!before.isFile() || before.nlink !== 1) {
    throw new Error("private_artifact_type_untrusted");
  }
  if (before.size > MAX_PRIVATE_JSON_BYTES) {
    throw new Error("private_artifact_exceeds_16 KiB");
  }
  await adapter.qualifyOwnerOnly(path, "file");
  const flags =
    constants.O_RDONLY |
    (constants.O_NOFOLLOW ?? 0);
  const handle = await open(path, flags);
  try {
    const opened = await handle.stat();
    if (
      opened.dev !== before.dev ||
      opened.ino !== before.ino ||
      opened.size !== before.size ||
      opened.size > MAX_PRIVATE_JSON_BYTES
    ) {
      throw new Error("private_artifact_changed_during_read");
    }
    const bytes = Buffer.allocUnsafe(opened.size);
    let position = 0;
    while (position < opened.size) {
      const { bytesRead } = await handle.read(
        bytes,
        position,
        opened.size - position,
        position
      );
      if (bytesRead <= 0) {
        throw new Error("private_artifact_changed_during_read");
      }
      position += bytesRead;
    }
    const final = await handle.stat();
    if (
      final.dev !== opened.dev ||
      final.ino !== opened.ino ||
      bytes.byteLength !== opened.size ||
      final.size !== opened.size ||
      final.mtimeMs !== opened.mtimeMs
    ) {
      throw new Error("private_artifact_changed_during_read");
    }
    try {
      return JSON.parse(bytes.toString("utf8"));
    } catch {
      throw new Error("private_artifact_json_invalid");
    }
  } finally {
    await handle.close();
  }
}

export async function readSidecarReadiness(
  path: string,
  adapter: PrivateFilePlatformAdapter = createNodePrivateFileAdapter()
): Promise<SidecarReadiness> {
  const result = sidecarReadinessSchema.safeParse(
    await readPrivateJsonArtifact(path, adapter)
  );
  if (!result.success) {
    throw new Error("desktop_readiness_invalid");
  }
  return result.data;
}

export async function createPrivateLaunchFiles(
  input: CreatePrivateLaunchFilesInput
): Promise<PrivateLaunchFiles> {
  const adapter = input.platformAdapter ?? createNodePrivateFileAdapter();
  if (
    !Number.isSafeInteger(input.parentPid) ||
    input.parentPid <= 0
  ) {
    throw new Error("parent_pid_invalid");
  }
  const parentBirthMarker = requiredText(
    input.parentBirthMarker,
    "parent_birth_marker",
    256
  );
  if (!prefixedSha256Schema.safeParse(input.resourceManifestDigest).success) {
    throw new Error("resource_manifest_digest_invalid");
  }
  await adapter.qualifyOwnerOnly(input.profile.runtimeDirectory, "directory");
  const randomBytes = input.randomBytes ?? secureRandomBytes;
  const launchNonce = randomSecret(randomBytes, "launch_nonce");
  const apiToken = randomSecret(randomBytes, "api_token");
  const launchNonceDigest = createHash("sha256")
    .update(launchNonce)
    .digest("hex");
  const bootstrapPath = join(
    input.profile.runtimeDirectory,
    `desktop-bootstrap-${launchNonceDigest.slice(0, 24)}.json`
  );
  const payload = {
    schema: "kestrel.desktop.bootstrap.v1",
    profile_id: input.profile.profileId,
    profile_root: input.profile.profileRoot,
    state_path: input.profile.statePath,
    memory_dir: input.profile.memoryDir,
    runtime_settings_path: input.profile.runtimeSettingsPath,
    launch_nonce: launchNonce,
    api_token: apiToken,
    parent_pid: input.parentPid,
    parent_birth_marker: parentBirthMarker,
    resource_manifest_digest: input.resourceManifestDigest,
    memory_layers: [...MEMORY_LAYERS]
  };
  const bytes = Buffer.from(JSON.stringify(payload), "utf8");
  if (bytes.byteLength > MAX_PRIVATE_JSON_BYTES) {
    throw new Error("desktop_bootstrap_exceeds_16 KiB");
  }

  let bootstrapIdentity: { dev: number; ino: number } | null = null;
  let handle;
  try {
    handle = await openExclusivePrivateFile(bootstrapPath);
    const opened = await handle.stat();
    bootstrapIdentity = { dev: opened.dev, ino: opened.ino };
    await handle.writeFile(bytes);
    await handle.sync();
    if (adapter.platform !== "win32") {
      await handle.chmod(0o600);
    }
    await handle.close();
    handle = undefined;
    await adapter.qualifyOwnerOnly(bootstrapPath, "file");
  } catch (error) {
    await handle?.close().catch(() => undefined);
    await removeCapturedFile(bootstrapPath, bootstrapIdentity).catch(
      () => undefined
    );
    throw error;
  }

  return {
    bootstrapPath,
    readinessPath: input.profile.readinessPath,
    launchNonce,
    launchNonceDigest,
    apiToken,
    profile: input.profile,
    async cleanup(): Promise<void> {
      await removeCapturedFile(bootstrapPath, bootstrapIdentity);
      try {
        const readiness = await readSidecarReadiness(
          input.profile.readinessPath,
          adapter
        );
        if (
          readiness.profile_id === input.profile.profileId &&
          readiness.launch_nonce_digest === launchNonceDigest
        ) {
          const metadata = await lstat(input.profile.readinessPath);
          await removeCapturedFile(input.profile.readinessPath, {
            dev: metadata.dev,
            ino: metadata.ino
          });
        }
      } catch (error) {
        const message = error instanceof Error ? error.message : "";
        if (
          message !== "private_artifact_missing" &&
          message !== "desktop_readiness_invalid"
        ) {
          throw error;
        }
      }
    }
  };
}
