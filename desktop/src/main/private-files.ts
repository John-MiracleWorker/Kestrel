import {
  createHash,
  createHmac,
  randomBytes as secureRandomBytes,
  timingSafeEqual
} from "node:crypto";
import { constants } from "node:fs";
import {
  lstat,
  open,
  realpath
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
const sidecarFailureSchema = z
  .object({
    schema: z.literal("kestrel.desktop.sidecar-failure.v1"),
    launch_nonce_digest: sha256Schema,
    profile_id: z.string().trim().min(1).max(120),
    reason: z.enum([
      "profile_conflict",
      "state_incompatible",
      "state_corrupt",
      "memvid_reopen_failed"
    ]),
    resource_manifest_digest: prefixedSha256Schema,
    sidecar_version: z.string().trim().min(1).max(64),
    authentication_tag: sha256Schema
  })
  .strict();

export type SidecarReadiness = z.infer<typeof sidecarReadinessSchema>;
export type SidecarFailure = z.infer<typeof sidecarFailureSchema>;

export interface CapturedPrivateFileIdentity {
  dev: number;
  ino: number;
}

export interface CapturedSidecarReadiness {
  readiness: SidecarReadiness;
  identity: CapturedPrivateFileIdentity;
}

export interface CapturedSidecarFailure {
  failure: SidecarFailure;
  identity: CapturedPrivateFileIdentity;
}

export interface ExpectedSidecarFailure {
  apiToken: string;
  launchNonceDigest: string;
  profileId: string;
  resourceManifestDigest: string;
  sidecarVersion: string;
}

export interface PrivateProfileInput {
  profileId: string;
  trustedAnchor: string;
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
  preparePrivateDirectory(
    trustedAnchor: string,
    path: string
  ): Promise<string>;
  deleteCapturedFile(
    path: string,
    identity: CapturedPrivateFileIdentity
  ): Promise<void>;
}

export interface PrivateLaunchFiles {
  bootstrapPath: string;
  readinessPath: string;
  failurePath: string;
  launchNonce: string;
  launchNonceDigest: string;
  apiToken: string;
  profile: ResolvedPrivateProfile;
  cleanup(
    readinessIdentity?: CapturedPrivateFileIdentity,
    failureIdentity?: CapturedPrivateFileIdentity
  ): Promise<void>;
}

export interface CreatePrivateLaunchFilesInput {
  profile: ResolvedPrivateProfile;
  assuranceMode?: "developer" | "release";
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
    },
    async preparePrivateDirectory(): Promise<string> {
      // Node does not expose mkdirat/openat2-style directory-relative mutation.
      // A platform implementation must prove its anchor and no-symlink walk.
      throw new Error("private_directory_mutation_unqualified");
    },
    async deleteCapturedFile(): Promise<void> {
      // A pathname lstat followed by unlink has a substitution window. Native
      // adapters must delete the captured object with unlinkat/handle semantics.
      throw new Error("private_exact_delete_unqualified");
    }
  };
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

export function runtimeProfileControlIdentityBytes(
  statePath: string,
  memoryDir: string,
  profileId: string
): Buffer {
  return Buffer.from(
    JSON.stringify({
      memory_dir: memoryDir,
      profile_id: profileId,
      schema: "kestrel.runtime_profile_control.v1",
      state_path: statePath
    }),
    "utf8"
  );
}

function leaseControlRoot(
  statePath: string,
  memoryDir: string,
  profileId: string
): string {
  return join(
    dirname(statePath),
    ".kestrel-runtime-profiles",
    createHash("sha256")
      .update(
        runtimeProfileControlIdentityBytes(
          statePath,
          memoryDir,
          profileId
        )
      )
      .digest("hex")
  );
}

export async function resolvePrivateProfile(
  input: PrivateProfileInput,
  adapter: PrivateFilePlatformAdapter = createNodePrivateFileAdapter()
): Promise<ResolvedPrivateProfile> {
  const profileId = requiredText(input.profileId, "profile_id", 120);
  const requestedAnchor = resolve(input.trustedAnchor);
  const anchorMetadata = await lstat(requestedAnchor);
  if (anchorMetadata.isSymbolicLink() || !anchorMetadata.isDirectory()) {
    throw new Error("trusted_anchor_untrusted");
  }
  const trustedAnchor = await realpath(requestedAnchor);
  await adapter.qualifyOwnerOnly(trustedAnchor, "directory");
  const requestedRoot = resolve(input.profileRoot);
  if (
    !isContained(requestedAnchor, requestedRoot) ||
    requestedRoot === requestedAnchor
  ) {
    throw new Error("profile_root_outside_trusted_anchor");
  }
  const rootWithinAnchor = relative(requestedAnchor, requestedRoot);
  const anchoredRoot = resolve(trustedAnchor, rootWithinAnchor);
  if (
    !isContained(trustedAnchor, anchoredRoot) ||
    anchoredRoot === trustedAnchor
  ) {
    throw new Error("profile_root_outside_trusted_anchor");
  }
  const profileRoot = await adapter.preparePrivateDirectory(
    trustedAnchor,
    anchoredRoot
  );
  if (
    !isContained(trustedAnchor, profileRoot) ||
    profileRoot !== anchoredRoot
  ) {
    throw new Error("profile_root_outside_trusted_anchor");
  }
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
    const canonical = await adapter.preparePrivateDirectory(
      trustedAnchor,
      directory
    );
    if (
      !isContained(profileRoot, canonical) ||
      canonical !== directory
    ) {
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

async function readPrivateJsonArtifactWithIdentity(
  path: string,
  adapter: PrivateFilePlatformAdapter
): Promise<{
  value: unknown;
  identity: CapturedPrivateFileIdentity;
}> {
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
      return {
        value: JSON.parse(bytes.toString("utf8")),
        identity: { dev: opened.dev, ino: opened.ino }
      };
    } catch {
      throw new Error("private_artifact_json_invalid");
    }
  } finally {
    await handle.close();
  }
}

export async function readPrivateJsonArtifact(
  path: string,
  adapter: PrivateFilePlatformAdapter
): Promise<unknown> {
  return (await readPrivateJsonArtifactWithIdentity(path, adapter)).value;
}

export async function readSidecarReadiness(
  path: string,
  adapter: PrivateFilePlatformAdapter = createNodePrivateFileAdapter()
): Promise<CapturedSidecarReadiness> {
  const captured = await readPrivateJsonArtifactWithIdentity(path, adapter);
  const result = sidecarReadinessSchema.safeParse(
    captured.value
  );
  if (!result.success) {
    throw new Error("desktop_readiness_invalid");
  }
  return {
    readiness: result.data,
    identity: captured.identity
  };
}

function constantTimeTextEqual(left: string, right: string): boolean {
  const leftBytes = Buffer.from(left, "utf8");
  const rightBytes = Buffer.from(right, "utf8");
  return (
    leftBytes.byteLength === rightBytes.byteLength &&
    timingSafeEqual(leftBytes, rightBytes)
  );
}

function sidecarFailureAuthenticationTag(
  failure: Omit<SidecarFailure, "authentication_tag">,
  apiToken: string
): string {
  const canonical = JSON.stringify({
    launch_nonce_digest: failure.launch_nonce_digest,
    profile_id: failure.profile_id,
    reason: failure.reason,
    resource_manifest_digest: failure.resource_manifest_digest,
    schema: failure.schema,
    sidecar_version: failure.sidecar_version
  });
  return createHmac("sha256", apiToken)
    .update("kestrel.desktop.sidecar-failure.v1\0")
    .update(canonical)
    .digest("hex");
}

export async function readSidecarFailure(
  path: string,
  expected: ExpectedSidecarFailure,
  adapter: PrivateFilePlatformAdapter = createNodePrivateFileAdapter()
): Promise<CapturedSidecarFailure> {
  const captured = await readPrivateJsonArtifactWithIdentity(
    path,
    adapter
  );
  const parsed = sidecarFailureSchema.safeParse(captured.value);
  if (!parsed.success) {
    throw new Error("sidecar_failure_invalid");
  }
  const failure = parsed.data;
  const {
    authentication_tag: authenticationTag,
    ...unsigned
  } = failure;
  const expectedTag = sidecarFailureAuthenticationTag(
    unsigned,
    expected.apiToken
  );
  if (
    !constantTimeTextEqual(authenticationTag, expectedTag) ||
    !constantTimeTextEqual(
      failure.launch_nonce_digest,
      expected.launchNonceDigest
    ) ||
    failure.profile_id !== expected.profileId ||
    !constantTimeTextEqual(
      failure.resource_manifest_digest,
      expected.resourceManifestDigest
    ) ||
    failure.sidecar_version !== expected.sidecarVersion
  ) {
    throw new Error("sidecar_failure_invalid");
  }
  return {
    failure,
    identity: captured.identity
  };
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
  const failurePath = join(
    input.profile.runtimeDirectory,
    `desktop-failure-${launchNonceDigest.slice(0, 24)}.json`
  );
  const payload = {
    schema: "kestrel.desktop.bootstrap.v1",
    assurance_mode: input.assuranceMode ?? "release",
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
    await handle?.close();
    if (bootstrapIdentity !== null) {
      await adapter.deleteCapturedFile(
        bootstrapPath,
        bootstrapIdentity
      );
    }
    throw error;
  }

  return {
    bootstrapPath,
    readinessPath: input.profile.readinessPath,
    failurePath,
    launchNonce,
    launchNonceDigest,
    apiToken,
    profile: input.profile,
    async cleanup(
      readinessIdentity?: CapturedPrivateFileIdentity,
      failureIdentity?: CapturedPrivateFileIdentity
    ): Promise<void> {
      if (bootstrapIdentity !== null) {
        await adapter.deleteCapturedFile(
          bootstrapPath,
          bootstrapIdentity
        );
      }
      if (readinessIdentity !== undefined) {
        await adapter.deleteCapturedFile(
          input.profile.readinessPath,
          readinessIdentity
        );
      }
      if (failureIdentity !== undefined) {
        await adapter.deleteCapturedFile(
          failurePath,
          failureIdentity
        );
      }
    }
  };
}
