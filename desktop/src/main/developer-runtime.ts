import { execFile, spawn } from "node:child_process";
import { createHash } from "node:crypto";
import {
  constants,
  fstatSync,
  lstatSync,
  readSync,
  realpathSync,
  type Stats
} from "node:fs";
import {
  lstat,
  mkdir,
  open,
  realpath,
  unlink,
  type FileHandle
} from "node:fs/promises";
import {
  isAbsolute,
  join,
  relative,
  resolve,
  sep
} from "node:path";
import { promisify } from "node:util";
import type {
  NodeSupervisorDependencyInput,
  RetainedSidecarChild,
  SidecarProcessIdentity,
  SidecarSupervisorDependencies,
  VerifiedExecutableLaunchCapability,
  VerifiedExecutableSpawnRequest
} from "./sidecar-supervisor.js";
import { createNodeSupervisorDependencies } from "./sidecar-supervisor.js";
import type {
  CapturedPrivateFileIdentity,
  PrivateFilePlatformAdapter
} from "./private-files.js";
import {
  verifyDeveloperResourceManifest,
  type VerifiedResourceFile
} from "./resource-manifest.js";

const execFileAsync = promisify(execFile);
const SHA256_PATTERN = /^[0-9a-f]{64}$/;

type SpawnChild = (
  executable: string,
  args: [string],
  options: VerifiedExecutableSpawnRequest["options"]
) => RetainedSidecarChild;

export interface DeveloperRuntimeExecutableCapability
  extends VerifiedExecutableLaunchCapability {
  readonly mechanism: "developer_reverified_path";
  readonly processModel: "pyinstaller_onefile";
  readonly residualRisk: "path_to_exec_race_not_native_sealed";
}

export interface AcquireDeveloperRuntimeExecutableOptions {
  spawnChild?: SpawnChild;
}

interface OpenedExecutableIdentity {
  dev: number;
  ino: number;
  size: number;
  mode: number;
  nlink: number;
  mtimeMs: number;
  ctimeMs: number;
}

function executablePathError(): Error {
  return new Error("developer_executable_path_untrusted");
}

function openedIdentity(
  metadata: Awaited<ReturnType<FileHandle["stat"]>>
): OpenedExecutableIdentity {
  return {
    dev: Number(metadata.dev),
    ino: Number(metadata.ino),
    size: Number(metadata.size),
    mode: Number(metadata.mode),
    nlink: Number(metadata.nlink),
    mtimeMs: Number(metadata.mtimeMs),
    ctimeMs: Number(metadata.ctimeMs)
  };
}

function sameExecutableIdentity(
  metadata: {
    dev: number;
    ino: number;
    size: number;
    mode: number;
    nlink: number;
    mtimeMs: number;
    ctimeMs: number;
  },
  expected: OpenedExecutableIdentity
): boolean {
  return (
    metadata.dev === expected.dev &&
    metadata.ino === expected.ino &&
    metadata.size === expected.size &&
    metadata.mode === expected.mode &&
    metadata.nlink === expected.nlink &&
    metadata.mtimeMs === expected.mtimeMs &&
    metadata.ctimeMs === expected.ctimeMs
  );
}

function digestOpenedFileSync(descriptor: number, size: number): string {
  const digest = createHash("sha256");
  const buffer = Buffer.allocUnsafe(1024 * 1024);
  let position = 0;
  while (position < size) {
    const length = Math.min(buffer.byteLength, size - position);
    const bytesRead = readSync(
      descriptor,
      buffer,
      0,
      length,
      position
    );
    if (bytesRead !== length) {
      throw new Error("developer_executable_changed_before_spawn");
    }
    digest.update(buffer.subarray(0, bytesRead));
    position += bytesRead;
  }
  return digest.digest("hex");
}

async function digestOpenedFile(
  handle: FileHandle,
  size: number
): Promise<string> {
  const digest = createHash("sha256");
  const buffer = Buffer.allocUnsafe(1024 * 1024);
  let position = 0;
  while (position < size) {
    const length = Math.min(buffer.byteLength, size - position);
    const { bytesRead } = await handle.read(
      buffer,
      0,
      length,
      position
    );
    if (bytesRead !== length) {
      throw new Error("developer_executable_changed_during_read");
    }
    digest.update(buffer.subarray(0, bytesRead));
    position += bytesRead;
  }
  return digest.digest("hex");
}

export async function acquireDeveloperRuntimeExecutable(
  resource: VerifiedResourceFile,
  options: AcquireDeveloperRuntimeExecutableOptions = {}
): Promise<DeveloperRuntimeExecutableCapability> {
  if (
    !isAbsolute(resource.path) ||
    !Number.isSafeInteger(resource.size) ||
    resource.size <= 0 ||
    !SHA256_PATTERN.test(resource.sha256)
  ) {
    throw executablePathError();
  }
  const requested = resolve(resource.path);
  const before = await lstat(requested);
  if (
    before.isSymbolicLink() ||
    !before.isFile() ||
    before.nlink !== 1 ||
    before.size !== resource.size
  ) {
    throw executablePathError();
  }
  const canonical = await realpath(requested);
  if (canonical !== requested) {
    throw executablePathError();
  }
  const flags =
    constants.O_RDONLY |
    (constants.O_NOFOLLOW ?? 0);
  const handle = await open(canonical, flags);
  let closed = false;
  try {
    const opened = await handle.stat();
    const identity = openedIdentity(opened);
    const afterOpen = await lstat(canonical);
    if (
      !opened.isFile() ||
      opened.nlink !== 1 ||
      !sameExecutableIdentity(before, identity) ||
      !sameExecutableIdentity(afterOpen, identity) ||
      (opened.mode & 0o111) === 0
    ) {
      throw executablePathError();
    }
    const digest = await digestOpenedFile(handle, opened.size);
    const afterDigest = await handle.stat();
    const pathAfterDigest = await lstat(canonical);
    if (
      !sameExecutableIdentity(afterDigest, identity) ||
      !sameExecutableIdentity(pathAfterDigest, identity)
    ) {
      throw new Error("developer_executable_changed_during_read");
    }
    if (digest !== resource.sha256) {
      throw new Error("developer_executable_digest_mismatch");
    }

    let retainedChild: RetainedSidecarChild | null = null;
    const spawnChild: SpawnChild =
      options.spawnChild ??
      ((executable, args, spawnOptions) =>
        spawn(executable, args, spawnOptions) as RetainedSidecarChild);
    const capability: DeveloperRuntimeExecutableCapability = {
      resource,
      mechanism: "developer_reverified_path",
      processModel: "pyinstaller_onefile",
      residualRisk: "path_to_exec_race_not_native_sealed",
      spawn(request): RetainedSidecarChild {
        if (closed) {
          throw new Error("developer_executable_handle_closed");
        }
        if (retainedChild !== null) {
          throw new Error("developer_executable_already_spawned");
        }
        const openedBeforeSpawn = fstatSync(handle.fd);
        if (
          !sameExecutableIdentity(openedBeforeSpawn, identity) ||
          digestOpenedFileSync(handle.fd, identity.size) !==
            resource.sha256 ||
          !sameExecutableIdentity(fstatSync(handle.fd), identity)
        ) {
          throw new Error(
            "developer_executable_changed_before_spawn"
          );
        }
        let pathMetadata;
        let reverifiedCanonical;
        try {
          pathMetadata = lstatSync(canonical);
          reverifiedCanonical = realpathSync(canonical);
        } catch {
          throw new Error("developer_executable_path_identity_changed");
        }
        if (
          pathMetadata.isSymbolicLink() ||
          !pathMetadata.isFile() ||
          pathMetadata.nlink !== 1 ||
          !sameExecutableIdentity(pathMetadata, identity) ||
          reverifiedCanonical !== canonical
        ) {
          throw new Error("developer_executable_path_identity_changed");
        }
        const child = spawnChild(
          canonical,
          request.args,
          request.options
        );
        retainedChild = child;
        return child;
      },
      async close(): Promise<void> {
        if (closed) {
          return;
        }
        if (
          retainedChild !== null &&
          retainedChild.exitCode === null &&
          retainedChild.signalCode === null
        ) {
          throw new Error(
            "developer_executable_child_exit_unconfirmed"
          );
        }
        await handle.close();
        closed = true;
      }
    };
    return capability;
  } catch (error) {
    if (!closed) {
      await handle.close();
      closed = true;
    }
    throw error;
  }
}

export interface DeveloperRuntimePrivateFileAdapter
  extends PrivateFilePlatformAdapter {
  readonly mutationMechanism: "developer_identity_revalidation";
  readonly deleteMechanism: "developer_identity_revalidation";
  readonly residualRisk: "path_revalidation_not_native_handle_sealed";
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

function ownerOnlyMode(
  metadata: Stats,
  kind: "directory" | "file",
  owner: number
): void {
  if (
    metadata.isSymbolicLink() ||
    (kind === "directory"
      ? !metadata.isDirectory()
      : !metadata.isFile() || metadata.nlink !== 1)
  ) {
    throw new Error("private_artifact_type_untrusted");
  }
  if (metadata.uid !== owner) {
    throw new Error("private_artifact_owner_untrusted");
  }
  const expectedMode = kind === "directory" ? 0o700 : 0o600;
  if ((metadata.mode & 0o777) !== expectedMode) {
    throw new Error("private_artifact_permissions_untrusted");
  }
}

export async function createDeveloperRuntimePrivateFileAdapter(
  userData: string,
  platform: NodeJS.Platform = process.platform
): Promise<DeveloperRuntimePrivateFileAdapter> {
  if (platform === "win32" || process.getuid === undefined) {
    throw new Error("developer_private_runtime_platform_unqualified");
  }
  const owner = process.getuid();
  const requestedAnchor = resolve(userData);
  const anchorMetadata = await lstat(requestedAnchor);
  if (
    anchorMetadata.isSymbolicLink() ||
    !anchorMetadata.isDirectory()
  ) {
    throw new Error("developer_user_data_untrusted");
  }
  const canonicalAnchor = await realpath(requestedAnchor);
  ownerOnlyMode(anchorMetadata, "directory", owner);
  const anchorIdentity = {
    dev: anchorMetadata.dev,
    ino: anchorMetadata.ino
  };

  function anchoredCandidate(path: string): string {
    const candidate = resolve(path);
    if (isContained(canonicalAnchor, candidate)) {
      return candidate;
    }
    if (isContained(requestedAnchor, candidate)) {
      return resolve(
        canonicalAnchor,
        relative(requestedAnchor, candidate)
      );
    }
    return candidate;
  }

  async function validateAnchor(): Promise<void> {
    const current = await lstat(canonicalAnchor);
    if (
      current.dev !== anchorIdentity.dev ||
      current.ino !== anchorIdentity.ino
    ) {
      throw new Error("developer_user_data_identity_changed");
    }
    ownerOnlyMode(current, "directory", owner);
    if ((await realpath(canonicalAnchor)) !== canonicalAnchor) {
      throw new Error("developer_user_data_untrusted");
    }
  }

  async function validateExistingAncestors(candidate: string): Promise<void> {
    const parent = resolve(candidate);
    if (
      !isContained(canonicalAnchor, parent) ||
      parent === canonicalAnchor
    ) {
      throw new Error("private_directory_outside_trusted_anchor");
    }
    const fromAnchor = relative(canonicalAnchor, parent);
    let current = canonicalAnchor;
    await validateAnchor();
    for (const segment of fromAnchor.split(sep)) {
      current = join(current, segment);
      const metadata = await lstat(current);
      ownerOnlyMode(metadata, "directory", owner);
      if ((await realpath(current)) !== current) {
        throw new Error("private_profile_symlink_untrusted");
      }
    }
  }

  return {
    platform,
    mutationMechanism: "developer_identity_revalidation",
    deleteMechanism: "developer_identity_revalidation",
    residualRisk: "path_revalidation_not_native_handle_sealed",
    currentOwnerId: () => owner,
    async qualifyOwnerOnly(
      path: string,
      kind: "directory" | "file"
    ): Promise<void> {
      const candidate = anchoredCandidate(path);
      if (!isContained(canonicalAnchor, candidate)) {
        throw new Error("private_artifact_outside_trusted_anchor");
      }
      const metadata = await lstat(candidate);
      ownerOnlyMode(metadata, kind, owner);
      if ((await realpath(candidate)) !== candidate) {
        throw new Error("private_profile_symlink_untrusted");
      }
    },
    async preparePrivateDirectory(
      trustedAnchor: string,
      path: string
    ): Promise<string> {
      await validateAnchor();
      if (
        (await realpath(resolve(trustedAnchor))) !== canonicalAnchor ||
        ![requestedAnchor, canonicalAnchor].includes(
          resolve(trustedAnchor)
        )
      ) {
        throw new Error("private_directory_anchor_mismatch");
      }
      const target = anchoredCandidate(path);
      if (
        !isContained(canonicalAnchor, target) ||
        target === canonicalAnchor
      ) {
        throw new Error("private_directory_outside_trusted_anchor");
      }
      const fromAnchor = relative(canonicalAnchor, target);
      let current = canonicalAnchor;
      for (const segment of fromAnchor.split(sep)) {
        current = join(current, segment);
        let metadata;
        try {
          metadata = await lstat(current);
        } catch (error) {
          if ((error as NodeJS.ErrnoException).code !== "ENOENT") {
            throw error;
          }
          try {
            await mkdir(current, { mode: 0o700 });
          } catch (mkdirError) {
            if ((mkdirError as NodeJS.ErrnoException).code !== "EEXIST") {
              throw mkdirError;
            }
          }
          metadata = await lstat(current);
        }
        if (metadata.isSymbolicLink()) {
          throw new Error("private_profile_symlink_untrusted");
        }
        ownerOnlyMode(metadata, "directory", owner);
        const canonical = await realpath(current);
        if (
          canonical !== current ||
          !isContained(canonicalAnchor, canonical)
        ) {
          throw new Error("private_profile_symlink_untrusted");
        }
      }
      return target;
    },
    async deleteCapturedFile(
      path: string,
      identity: CapturedPrivateFileIdentity
    ): Promise<void> {
      const candidate = anchoredCandidate(path);
      const parent = resolve(candidate, "..");
      await validateExistingAncestors(parent);
      let metadata;
      try {
        metadata = await lstat(candidate);
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code === "ENOENT") {
          return;
        }
        throw error;
      }
      ownerOnlyMode(metadata, "file", owner);
      if (
        metadata.dev !== identity.dev ||
        metadata.ino !== identity.ino
      ) {
        return;
      }
      const immediate = await lstat(candidate);
      ownerOnlyMode(immediate, "file", owner);
      if (
        immediate.dev !== identity.dev ||
        immediate.ino !== identity.ino
      ) {
        return;
      }
      await unlink(candidate);
    }
  };
}

export interface DeveloperMacProcessEvidence {
  pid: number;
  parentPid?: number;
  uid: number;
  birthMilliseconds: number;
  executablePath: string;
  executableDigest: string;
}

export type DeveloperMacProcessReader = (
  pid: number
) => Promise<DeveloperMacProcessEvidence | null>;

interface DeveloperMacProcessCommandOptions {
  encoding: "utf8";
  env: {
    LANG: string;
    LC_ALL: string;
    PATH: string;
  };
  maxBuffer: number;
  timeout: number;
}

type DeveloperMacProcessCommandRunner = (
  executable: string,
  argumentsValue: string[],
  options: DeveloperMacProcessCommandOptions
) => Promise<{ stdout: string }>;

export interface ReadMacOSDeveloperProcessOptions {
  runCommand?: DeveloperMacProcessCommandRunner;
}

export interface MacOSDeveloperRetainedChildQualifier {
  readonly mechanism: "developer_retained_child_ps_milliseconds";
  readonly residualRisk: "ps_birth_time_second_resolution";
  inspectRetainedChild(
    child: RetainedSidecarChild,
    executableDigest: string
  ): Promise<SidecarProcessIdentity>;
  qualifyDeveloperOneFilePayload(
    child: RetainedSidecarChild,
    payloadPid: number,
    executableDigest: string
  ): Promise<SidecarProcessIdentity>;
  inspectProcess(pid: number): Promise<SidecarProcessIdentity | null>;
}

export interface MacOSDeveloperRetainedChildOptions {
  platform?: NodeJS.Platform;
  readProcess?: DeveloperMacProcessReader;
}

interface MacOSExecutableMapping {
  path: string;
  device: bigint;
  inode: bigint;
  size: bigint;
}

const MAX_PROCESS_EVIDENCE_BYTES = 64 * 1024;
const MAX_PROCESS_PATH_BYTES = 4 * 1024;

function processPathTrusted(path: string): boolean {
  return (
    isAbsolute(path) &&
    Buffer.byteLength(path, "utf8") > 0 &&
    Buffer.byteLength(path, "utf8") <= MAX_PROCESS_PATH_BYTES &&
    !/[\u0000-\u001f\u007f]/u.test(path)
  );
}

function parseMacOSExecutableMappings(
  stdout: string,
  pid: number
): MacOSExecutableMapping[] | null {
  if (
    Buffer.byteLength(stdout, "utf8") > MAX_PROCESS_EVIDENCE_BYTES
  ) {
    return null;
  }
  const lines = stdout.split(/\r?\n/).filter((line) => line !== "");
  if (lines.shift() !== `p${pid}` || lines.length > 128) {
    return null;
  }
  const mappings: MacOSExecutableMapping[] = [];
  let descriptor = "";
  let type = "";
  let path = "";
  let device = "";
  let inode = "";
  let size = "";
  const flush = (): boolean => {
    if (descriptor === "") {
      return true;
    }
    if (
      descriptor === "txt" &&
      type === "REG" &&
      processPathTrusted(path) &&
      /^0x[0-9a-f]+$/i.test(device) &&
      /^\d+$/.test(inode) &&
      /^\d+$/.test(size)
    ) {
      mappings.push({
        path,
        device: BigInt(device),
        inode: BigInt(inode),
        size: BigInt(size)
      });
    }
    descriptor = "";
    type = "";
    path = "";
    device = "";
    inode = "";
    size = "";
    return mappings.length <= 32;
  };
  for (const line of lines) {
    const field = line.slice(0, 1);
    const value = line.slice(1);
    if (field === "f") {
      if (!flush()) {
        return null;
      }
      descriptor = value;
    } else if (field === "t") {
      type = value;
    } else if (field === "n") {
      path = value;
    } else if (field === "D") {
      device = value;
    } else if (field === "i") {
      inode = value;
    } else if (field === "s") {
      size = value;
    }
  }
  return flush() && mappings.length > 0 ? mappings : null;
}

function sameMappedFileIdentity(
  metadata: {
    dev: bigint;
    ino: bigint;
    size: bigint;
    mode: bigint;
    nlink: bigint;
    mtimeNs: bigint;
    ctimeNs: bigint;
  },
  expected: {
    dev: bigint;
    ino: bigint;
    size: bigint;
    mode: bigint;
    nlink: bigint;
    mtimeNs: bigint;
    ctimeNs: bigint;
  }
): boolean {
  return (
    metadata.dev === expected.dev &&
    metadata.ino === expected.ino &&
    metadata.size === expected.size &&
    metadata.mode === expected.mode &&
    metadata.nlink === expected.nlink &&
    metadata.mtimeNs === expected.mtimeNs &&
    metadata.ctimeNs === expected.ctimeNs
  );
}

async function inspectMappedMacOSExecutable(
  commandPath: string,
  mappings: readonly MacOSExecutableMapping[]
): Promise<{ path: string; digest: string } | null> {
  if (!processPathTrusted(commandPath)) {
    return null;
  }
  const canonicalCommand = await realpath(resolve(commandPath));
  const matching: MacOSExecutableMapping[] = [];
  for (const mapping of mappings) {
    try {
      if (
        (await realpath(resolve(mapping.path))) === canonicalCommand
      ) {
        matching.push(mapping);
      }
    } catch {
      // A deleted or replaced mapped pathname cannot qualify.
    }
  }
  if (matching.length !== 1) {
    return null;
  }
  const mapping = matching[0]!;
  const before = await lstat(canonicalCommand, { bigint: true });
  if (
    before.isSymbolicLink() ||
    !before.isFile() ||
    before.nlink !== 1n ||
    (before.mode & 0o111n) === 0n ||
    before.dev !== mapping.device ||
    before.ino !== mapping.inode ||
    before.size !== mapping.size ||
    before.size <= 0n ||
    before.size > BigInt(Number.MAX_SAFE_INTEGER)
  ) {
    return null;
  }
  const handle = await open(
    canonicalCommand,
    constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0)
  );
  try {
    const opened = await handle.stat({ bigint: true });
    const pathAfterOpen = await lstat(canonicalCommand, {
      bigint: true
    });
    if (
      !opened.isFile() ||
      opened.nlink !== 1n ||
      !sameMappedFileIdentity(opened, before) ||
      !sameMappedFileIdentity(pathAfterOpen, before)
    ) {
      return null;
    }
    const digest = await digestOpenedFile(
      handle,
      Number(opened.size)
    );
    const afterDigest = await handle.stat({ bigint: true });
    const pathAfterDigest = await lstat(canonicalCommand, {
      bigint: true
    });
    if (
      !sameMappedFileIdentity(afterDigest, before) ||
      !sameMappedFileIdentity(pathAfterDigest, before)
    ) {
      return null;
    }
    return { path: canonicalCommand, digest };
  } finally {
    await handle.close();
  }
}

export async function readMacOSDeveloperProcess(
  pid: number,
  options: ReadMacOSDeveloperProcessOptions = {}
): Promise<DeveloperMacProcessEvidence | null> {
  if (!Number.isSafeInteger(pid) || pid <= 0) {
    return null;
  }
  try {
    const commandOptions: DeveloperMacProcessCommandOptions = {
      encoding: "utf8" as const,
      env: {
        LANG: "C",
        LC_ALL: "C",
        PATH: "/usr/bin:/bin"
      },
      maxBuffer: MAX_PROCESS_EVIDENCE_BYTES,
      timeout: 2_000
    };
    const runCommand: DeveloperMacProcessCommandRunner =
      options.runCommand ??
      (async (executable, argumentsValue, commandInput) => {
        const result = await execFileAsync(
          executable,
          argumentsValue,
          commandInput
        );
        return { stdout: result.stdout };
      });
    const processResult = await runCommand(
      "/bin/ps",
      [
        "-ww",
        "-p",
        String(pid),
        "-o",
        "uid=",
        "-o",
        "ppid=",
        "-o",
        "lstart=",
        "-o",
        "comm="
      ],
      commandOptions
    );
    const match =
      /^\s*(\d+)\s+(\d+)\s+(\S+)\s+(\S+)\s+(\d{1,2})\s+(\d{2}:\d{2}:\d{2})\s+(\d{4})\s+(.+?)\s*$/u.exec(
        processResult.stdout
      );
    if (match === null || !processPathTrusted(match[8]!)) {
      return null;
    }
    const birthMilliseconds = Date.parse(
      `${match[3]} ${match[4]} ${match[5]} ${match[6]} ${match[7]}`
    );
    const parentPid = Number(match[2]);
    const uid = Number(match[1]);
    if (
      !Number.isSafeInteger(parentPid) ||
      parentPid < 0 ||
      !Number.isSafeInteger(uid) ||
      uid < 0 ||
      !Number.isSafeInteger(birthMilliseconds) ||
      birthMilliseconds <= 0
    ) {
      return null;
    }
    const commandPath = match[8]!;
    const mappingResult = await runCommand(
      "/usr/sbin/lsof",
      [
        "-a",
        "-p",
        String(pid),
        "-d",
        "txt",
        "-F",
        "pftnDsi",
        "--",
        commandPath
      ],
      commandOptions
    );
    const mappings = parseMacOSExecutableMappings(
      mappingResult.stdout,
      pid
    );
    if (mappings === null) {
      return null;
    }
    const executable = await inspectMappedMacOSExecutable(
      commandPath,
      mappings
    );
    if (executable === null) {
      return null;
    }
    return {
      pid,
      parentPid,
      uid,
      birthMilliseconds,
      executablePath: executable.path,
      executableDigest: executable.digest
    };
  } catch {
    return null;
  }
}

function developerMacProcessIdentity(
  evidence: DeveloperMacProcessEvidence
): SidecarProcessIdentity | null {
  if (
    evidence.pid <= 0 ||
    !Number.isSafeInteger(evidence.pid) ||
    !Number.isSafeInteger(evidence.uid) ||
    evidence.uid < 0 ||
    (evidence.parentPid !== undefined &&
      (!Number.isSafeInteger(evidence.parentPid) ||
        evidence.parentPid < 0)) ||
    !Number.isSafeInteger(evidence.birthMilliseconds) ||
    evidence.birthMilliseconds <= 0 ||
    !processPathTrusted(evidence.executablePath) ||
    !SHA256_PATTERN.test(evidence.executableDigest)
  ) {
    return null;
  }
  return {
    pid: evidence.pid,
    ...(evidence.parentPid === undefined
      ? {}
      : { parentPid: evidence.parentPid }),
    ownerDigest: createHash("sha256")
      .update(`uid:${evidence.uid}`)
      .digest("hex"),
    processBirthMarker:
      `developer-ps-lstart-ms:${evidence.birthMilliseconds}`,
    executablePath: evidence.executablePath,
    executableDigest: evidence.executableDigest
  };
}

export function createMacOSDeveloperRetainedChildQualifier(
  options: MacOSDeveloperRetainedChildOptions = {}
): MacOSDeveloperRetainedChildQualifier {
  if ((options.platform ?? process.platform) !== "darwin") {
    throw new Error("developer_retained_child_platform_unqualified");
  }
  const readProcess = options.readProcess ?? readMacOSDeveloperProcess;
  let retained:
    | {
        child: RetainedSidecarChild;
        identity: SidecarProcessIdentity;
        payload: SidecarProcessIdentity | null;
      }
    | null = null;

  function sameStableIdentity(
    left: SidecarProcessIdentity,
    right: SidecarProcessIdentity
  ): boolean {
    return (
      left.pid === right.pid &&
      left.ownerDigest === right.ownerDigest &&
      left.processBirthMarker === right.processBirthMarker &&
      left.executablePath === right.executablePath &&
      left.executableDigest === right.executableDigest
    );
  }

  function sameQualifiedIdentity(
    left: SidecarProcessIdentity,
    right: SidecarProcessIdentity
  ): boolean {
    return (
      sameStableIdentity(left, right) &&
      left.parentPid === right.parentPid
    );
  }

  async function observedIdentity(
    pid: number
  ): Promise<SidecarProcessIdentity | null> {
    const evidence = await readProcess(pid);
    if (evidence === null || evidence.pid !== pid) {
      return null;
    }
    return developerMacProcessIdentity(evidence);
  }

  return {
    mechanism: "developer_retained_child_ps_milliseconds",
    residualRisk: "ps_birth_time_second_resolution",
    async inspectRetainedChild(
      child,
      executableDigest
    ): Promise<SidecarProcessIdentity> {
      if (
        child.pid === undefined ||
        child.pid <= 0 ||
        child.exitCode !== null ||
        child.signalCode !== null ||
        !SHA256_PATTERN.test(executableDigest)
      ) {
        throw new Error("developer_retained_child_identity_unavailable");
      }
      if (retained !== null && retained.child !== child) {
        throw new Error("developer_existing_process_attach_forbidden");
      }
      const observed = await observedIdentity(child.pid);
      if (
        observed === null ||
        observed.executableDigest !== executableDigest
      ) {
        throw new Error("developer_retained_child_identity_unavailable");
      }
      if (
        retained !== null &&
        (!sameQualifiedIdentity(retained.identity, observed) ||
          retained.child !== child)
      ) {
        throw new Error("developer_retained_child_identity_changed");
      }
      retained = {
        child,
        identity: observed,
        payload: retained?.payload ?? null
      };
      return observed;
    },
    async qualifyDeveloperOneFilePayload(
      child,
      payloadPid,
      executableDigest
    ): Promise<SidecarProcessIdentity> {
      if (
        retained === null ||
        retained.child !== child ||
        child.pid === undefined ||
        child.pid !== retained.identity.pid ||
        child.exitCode !== null ||
        child.signalCode !== null ||
        !Number.isSafeInteger(payloadPid) ||
        payloadPid <= 0 ||
        payloadPid === child.pid ||
        executableDigest !== retained.identity.executableDigest ||
        !SHA256_PATTERN.test(executableDigest)
      ) {
        throw new Error(
          "developer_onefile_payload_identity_unavailable"
        );
      }
      const [launcherBefore, payloadBefore] = await Promise.all([
        observedIdentity(child.pid),
        observedIdentity(payloadPid)
      ]);
      if (
        launcherBefore === null ||
        payloadBefore === null ||
        !sameQualifiedIdentity(
          launcherBefore,
          retained.identity
        ) ||
        payloadBefore.parentPid !== child.pid ||
        launcherBefore.ownerDigest === undefined ||
        payloadBefore.ownerDigest === undefined ||
        launcherBefore.ownerDigest !== payloadBefore.ownerDigest ||
        launcherBefore.executableDigest !== executableDigest ||
        payloadBefore.executableDigest !== executableDigest ||
        launcherBefore.executablePath === undefined ||
        payloadBefore.executablePath === undefined ||
        launcherBefore.executablePath !== payloadBefore.executablePath ||
        (retained.payload !== null &&
          !sameQualifiedIdentity(retained.payload, payloadBefore))
      ) {
        throw new Error(
          "developer_onefile_payload_identity_unavailable"
        );
      }
      const [launcherAfter, payloadAfter] = await Promise.all([
        observedIdentity(child.pid),
        observedIdentity(payloadPid)
      ]);
      if (
        launcherAfter === null ||
        payloadAfter === null ||
        !sameQualifiedIdentity(launcherBefore, launcherAfter) ||
        !sameQualifiedIdentity(payloadBefore, payloadAfter)
      ) {
        throw new Error(
          "developer_onefile_payload_identity_changed"
        );
      }
      retained.payload = payloadAfter;
      return payloadAfter;
    },
    async inspectProcess(
      pid
    ): Promise<SidecarProcessIdentity | null> {
      if (retained === null) {
        return null;
      }
      if (pid === retained.identity.pid) {
        if (
          retained.child.pid !== pid ||
          retained.child.exitCode !== null ||
          retained.child.signalCode !== null
        ) {
          return null;
        }
        const observed = await observedIdentity(pid);
        return observed !== null &&
          sameQualifiedIdentity(observed, retained.identity)
          ? observed
          : null;
      }
      const payload = retained.payload;
      if (payload === null || pid !== payload.pid) {
        return null;
      }
      const observed = await observedIdentity(pid);
      return observed !== null &&
        sameStableIdentity(observed, payload)
        ? observed
        : null;
    }
  };
}

export type PackagedSupervisorRuntime =
  | "developer-runtime"
  | "production-runtime";

export function selectPackagedSupervisorRuntime(
  buildTrust: Readonly<{
    buildMode: "developer" | "release";
    keyId: "developer" | "release";
  }>
): PackagedSupervisorRuntime {
  if (buildTrust.buildMode !== buildTrust.keyId) {
    throw new Error("desktop_build_mode_key_mismatch");
  }
  return buildTrust.buildMode === "developer"
    ? "developer-runtime"
    : "production-runtime";
}

export interface DeveloperRuntimeSupervisorDependencyInput
  extends Omit<
    NodeSupervisorDependencyInput,
    "privateFileAdapter" | "processInspector" | "resourceVerifier"
  > {
  userDataPath: string;
  platform?: NodeJS.Platform;
  readProcess?: DeveloperMacProcessReader;
  resourceVerifier?: NodeSupervisorDependencyInput["resourceVerifier"];
}

export async function createDeveloperRuntimeSupervisorDependencies(
  input: DeveloperRuntimeSupervisorDependencyInput
): Promise<SidecarSupervisorDependencies> {
  const identity = input.resourceVerification.expectedIdentity;
  if (
    selectPackagedSupervisorRuntime(identity) !== "developer-runtime"
  ) {
    throw new Error("developer_runtime_build_identity_required");
  }
  const platform = input.platform ?? process.platform;
  if (platform !== "darwin" || identity.platform !== "darwin") {
    throw new Error("developer_runtime_platform_unqualified");
  }
  const canonicalUserData = await realpath(resolve(input.userDataPath));
  const canonicalTrustedAnchor = await realpath(
    resolve(input.profile.trustedAnchor)
  );
  if (canonicalUserData !== canonicalTrustedAnchor) {
    throw new Error("developer_runtime_user_data_identity_mismatch");
  }

  const adapter =
    await createDeveloperRuntimePrivateFileAdapter(canonicalUserData);
  const readProcess = input.readProcess ?? readMacOSDeveloperProcess;
  const retainedChild =
    createMacOSDeveloperRetainedChildQualifier({
      platform,
      readProcess
    });
  const processInspector = async (
    pid: number
  ): Promise<SidecarProcessIdentity | null> => {
    const retained = await retainedChild.inspectProcess(pid);
    if (retained !== null) {
      return retained;
    }
    if (pid !== process.pid) {
      return null;
    }
    const evidence = await readProcess(pid);
    if (evidence === null || evidence.pid !== pid) {
      return null;
    }
    return developerMacProcessIdentity(evidence);
  };
  const base = createNodeSupervisorDependencies({
    ...input,
    resourceVerifier:
      input.resourceVerifier ?? verifyDeveloperResourceManifest,
    privateFileAdapter: adapter,
    processInspector
  });
  return {
    ...base,
    async acquireVerifiedExecutable(resources, relativePath) {
      const resource = resources.files.get(relativePath);
      if (resource === undefined) {
        throw new Error("verified_sidecar_missing");
      }
      return acquireDeveloperRuntimeExecutable(resource);
    },
    inspectRetainedChild: (child, expectedExecutableDigest) =>
      retainedChild.inspectRetainedChild(
        child,
        expectedExecutableDigest
      ),
    qualifyDeveloperOneFilePayload: (
      child,
      payloadPid,
      expectedExecutableDigest
    ) =>
      retainedChild.qualifyDeveloperOneFilePayload(
        child,
        payloadPid,
        expectedExecutableDigest
      )
  };
}
