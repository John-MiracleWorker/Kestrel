import { constants as fsConstants } from "node:fs";
import {
  lstat,
  open,
  readdir,
  realpath
} from "node:fs/promises";
import { isAbsolute, join, relative, resolve, sep } from "node:path";
import { DESKTOP_APP_ENTRY_URL } from "../contracts.js";
import type { PackagedBuildTrust } from "./build-trust.js";
import {
  createDeveloperRuntimePrivateFileAdapter,
  type DeveloperRuntimePrivateFileAdapter
} from "./developer-runtime.js";
import type { SidecarSupervisorState } from "./sidecar-supervisor.js";

export const DIRECTORY_SMOKE_MEMORY_FILES = Object.freeze([
  "working.mv2",
  "episodic.mv2",
  "semantic.mv2",
  "procedural.mv2",
  "self.mv2",
  "policy.mv2"
] as const);

const REQUEST_ARGUMENT = "--kestrel-directory-smoke";
const CONTROL_DIRECTORY = "directory-smoke-v1";
const MAX_CONTROL_BYTES = 4 * 1024;
const CONTINUE_TIMEOUT_MS = 30_000;
const MISSION_TIMEOUT_MS = 15_000;
const POLL_INTERVAL_MS = 25;
const SOURCE_COMMIT_PATTERN = /^[0-9a-f]{40}$/;
const MISSION_SELECTOR =
  '.mission-shell[data-active-section="mission"]';
const MISSION_PROBE =
  `Boolean(document.querySelector('${MISSION_SELECTOR}'))`;

interface DirectorySmokeReady {
  readonly schema: "kestrel.desktop.directory-smoke-ready.v1";
  readonly authenticated_readiness: true;
  readonly authenticated_recovery: true;
  readonly build_mode: "developer";
  readonly hidden: true;
  readonly memory_files: readonly string[];
  readonly mission_command_url: typeof DESKTOP_APP_ENTRY_URL;
  readonly source_commit: string;
}

interface DirectorySmokeCompleted {
  readonly schema: "kestrel.desktop.directory-smoke-completed.v1";
  readonly authenticated_shutdown: true;
  readonly child_exited: true;
}

export interface DirectorySmokePaths {
  readonly controlRoot: string;
  readonly readyPath: string;
  readonly continuePath: string;
  readonly completedPath: string;
}

export interface DirectorySmokeCycle {
  readonly paths: DirectorySmokePaths;
  runAfterMissionCommandLoaded(): Promise<void>;
  waitForReadyForTest(): Promise<DirectorySmokeReady>;
  continueForTest(): Promise<void>;
}

export interface DirectorySmokeWebContents {
  isDestroyed(): boolean;
  getURL(): string;
  executeJavaScript(source: string): Promise<unknown>;
}

function compareStrings(left: string, right: string): number {
  const leftPoints = Array.from(left, (value) => value.codePointAt(0) ?? 0);
  const rightPoints = Array.from(right, (value) => value.codePointAt(0) ?? 0);
  const length = Math.min(leftPoints.length, rightPoints.length);
  for (let index = 0; index < length; index += 1) {
    const difference = leftPoints[index]! - rightPoints[index]!;
    if (difference !== 0) {
      return difference;
    }
  }
  return leftPoints.length - rightPoints.length;
}

function canonicalJson(value: unknown): Buffer {
  function serialize(current: unknown): string {
    if (current === null || typeof current !== "object") {
      const scalar = JSON.stringify(current);
      if (scalar === undefined) {
        throw new Error("desktop_directory_smoke_control_invalid");
      }
      return scalar;
    }
    if (Array.isArray(current)) {
      return `[${current.map((item) => serialize(item)).join(",")}]`;
    }
    const record = current as Record<string, unknown>;
    return `{${Object.keys(record)
      .sort(compareStrings)
      .map((key) => `${JSON.stringify(key)}:${serialize(record[key])}`)
      .join(",")}}`;
  }
  return Buffer.from(`${serialize(value)}\n`, "utf8");
}

function isContained(root: string, candidate: string): boolean {
  const fromRoot = relative(root, candidate);
  return (
    fromRoot !== "" &&
    fromRoot !== ".." &&
    !fromRoot.startsWith(`..${sep}`) &&
    !isAbsolute(fromRoot)
  );
}

function pathsFor(userDataPath: string): DirectorySmokePaths {
  const controlRoot = join(userDataPath, CONTROL_DIRECTORY);
  return Object.freeze({
    controlRoot,
    readyPath: join(controlRoot, "ready.json"),
    continuePath: join(controlRoot, "continue.json"),
    completedPath: join(controlRoot, "completed.json")
  });
}

function sameIdentity(
  left: {
    dev: number;
    ino: number;
    size: number;
    mode: number;
    nlink: number;
  },
  right: {
    dev: number;
    ino: number;
    size: number;
    mode: number;
    nlink: number;
  }
): boolean {
  return (
    left.dev === right.dev &&
    left.ino === right.ino &&
    left.size === right.size &&
    left.mode === right.mode &&
    left.nlink === right.nlink
  );
}

function requireOwnerOnlyFile(
  metadata: {
    isFile(): boolean;
    isSymbolicLink(): boolean;
    uid: number;
    mode: number;
    nlink: number;
    size: number;
  },
  owner: number,
  allowEmpty = false
): void {
  if (
    metadata.isSymbolicLink() ||
    !metadata.isFile() ||
    metadata.nlink !== 1 ||
    metadata.uid !== owner ||
    (metadata.mode & 0o777) !== 0o600 ||
    (!allowEmpty && metadata.size <= 0) ||
    metadata.size > MAX_CONTROL_BYTES
  ) {
    throw new Error("desktop_directory_smoke_control_untrusted");
  }
}

function currentDeveloperOwner(
  adapter: DeveloperRuntimePrivateFileAdapter
): number {
  const owner = adapter.currentOwnerId();
  if (
    typeof owner !== "number" ||
    !Number.isSafeInteger(owner) ||
    owner < 0
  ) {
    throw new Error("desktop_directory_smoke_control_untrusted");
  }
  return owner;
}

async function prepareControlRoot(
  adapter: DeveloperRuntimePrivateFileAdapter,
  userDataPath: string,
  controlRoot: string
): Promise<void> {
  const prepared = await adapter.preparePrivateDirectory(
    userDataPath,
    controlRoot
  );
  if (
    (await realpath(prepared)) !== prepared ||
    (await realpath(controlRoot)) !== prepared
  ) {
    throw new Error("desktop_directory_smoke_control_untrusted");
  }
  await adapter.qualifyOwnerOnly(controlRoot, "directory");
}

async function writeExclusiveJson(
  pathValue: string,
  value: unknown,
  adapter: DeveloperRuntimePrivateFileAdapter,
  controlRoot: string
): Promise<void> {
  const bytes = canonicalJson(value);
  if (bytes.byteLength === 0 || bytes.byteLength > MAX_CONTROL_BYTES) {
    throw new Error("desktop_directory_smoke_control_too_large");
  }
  if (!isContained(controlRoot, pathValue)) {
    throw new Error("desktop_directory_smoke_control_untrusted");
  }
  await adapter.qualifyOwnerOnly(controlRoot, "directory");
  const handle = await open(
    pathValue,
    fsConstants.O_WRONLY |
      fsConstants.O_CREAT |
      fsConstants.O_EXCL |
      (fsConstants.O_NOFOLLOW ?? 0),
    0o600
  );
  let identity: { dev: number; ino: number } | null = null;
  try {
    const opened = await handle.stat();
    requireOwnerOnlyFile(opened, currentDeveloperOwner(adapter), true);
    identity = { dev: opened.dev, ino: opened.ino };
    await handle.writeFile(bytes);
    await handle.sync();
    const written = await handle.stat();
    requireOwnerOnlyFile(written, currentDeveloperOwner(adapter));
    if (written.size !== bytes.byteLength) {
      throw new Error("desktop_directory_smoke_control_invalid");
    }
    const named = await lstat(pathValue);
    requireOwnerOnlyFile(named, currentDeveloperOwner(adapter));
    if (!sameIdentity(written, named)) {
      throw new Error("desktop_directory_smoke_control_changed");
    }
    await adapter.qualifyOwnerOnly(controlRoot, "directory");
    await adapter.qualifyOwnerOnly(pathValue, "file");
  } catch (error) {
    if (identity !== null) {
      await adapter
        .deleteCapturedFile(pathValue, identity)
        .catch(() => undefined);
    }
    throw error;
  } finally {
    await handle.close();
  }
}

async function readCanonicalJson(
  pathValue: string,
  adapter: DeveloperRuntimePrivateFileAdapter,
  controlRoot: string
): Promise<unknown> {
  if (!isContained(controlRoot, pathValue)) {
    throw new Error("desktop_directory_smoke_control_untrusted");
  }
  await adapter.qualifyOwnerOnly(controlRoot, "directory");
  const before = await lstat(pathValue);
  requireOwnerOnlyFile(before, currentDeveloperOwner(adapter));
  const handle = await open(
    pathValue,
    fsConstants.O_RDONLY |
      (fsConstants.O_NOFOLLOW ?? 0)
  );
  try {
    const opened = await handle.stat();
    requireOwnerOnlyFile(opened, currentDeveloperOwner(adapter));
    if (!sameIdentity(before, opened)) {
      throw new Error("desktop_directory_smoke_control_changed");
    }
    const bounded = Buffer.alloc(MAX_CONTROL_BYTES + 1);
    const { bytesRead } = await handle.read(
      bounded,
      0,
      bounded.byteLength,
      0
    );
    if (bytesRead <= 0 || bytesRead > MAX_CONTROL_BYTES) {
      throw new Error("desktop_directory_smoke_control_invalid");
    }
    const bytes = bounded.subarray(0, bytesRead);
    const after = await lstat(pathValue);
    requireOwnerOnlyFile(after, currentDeveloperOwner(adapter));
    if (!sameIdentity(opened, after)) {
      throw new Error("desktop_directory_smoke_control_changed");
    }
    await adapter.qualifyOwnerOnly(controlRoot, "directory");
    await adapter.qualifyOwnerOnly(pathValue, "file");
    let value: unknown;
    try {
      value = JSON.parse(bytes.toString("utf8"));
    } catch {
      throw new Error("desktop_directory_smoke_control_invalid");
    }
    if (!canonicalJson(value).equals(bytes)) {
      throw new Error("desktop_directory_smoke_control_invalid");
    }
    return value;
  } finally {
    await handle.close();
  }
}

async function waitForFile(
  pathValue: string,
  timeoutMs: number,
  adapter: DeveloperRuntimePrivateFileAdapter,
  controlRoot: string
): Promise<unknown> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      return await readCanonicalJson(pathValue, adapter, controlRoot);
    } catch (error) {
      if (
        error instanceof Error &&
        "code" in error &&
        error.code === "ENOENT"
      ) {
        await new Promise((resolvePromise) =>
          setTimeout(resolvePromise, POLL_INTERVAL_MS)
        );
        continue;
      }
      throw error;
    }
  }
  throw new Error("desktop_directory_smoke_control_timeout");
}

async function verifyCanonicalMemorySet(
  userDataPath: string,
  profileRoot: string,
  adapter: DeveloperRuntimePrivateFileAdapter
): Promise<void> {
  const canonicalOwner = await realpath(userDataPath);
  const canonicalProfile = await realpath(profileRoot);
  if (
    !isContained(canonicalOwner, canonicalProfile)
  ) {
    throw new Error("desktop_directory_smoke_profile_invalid");
  }
  await adapter.qualifyOwnerOnly(canonicalProfile, "directory");
  const expectedRoot = await realpath(join(canonicalProfile, "memory"));
  await adapter.qualifyOwnerOnly(expectedRoot, "directory");
  const expectedPaths = new Set(
    DIRECTORY_SMOKE_MEMORY_FILES.map((name) => join(expectedRoot, name))
  );
  const found: string[] = [];
  const walk = async (directory: string): Promise<void> => {
    await adapter.qualifyOwnerOnly(directory, "directory");
    const entries = await readdir(directory, { withFileTypes: true });
    for (const entry of entries) {
      const pathValue = join(directory, entry.name);
      const metadata = await lstat(pathValue);
      if (entry.isSymbolicLink() || metadata.isSymbolicLink()) {
        throw new Error("desktop_directory_smoke_memory_set_invalid");
      }
      if (entry.isDirectory()) {
        await walk(pathValue);
      } else if (entry.name.endsWith(".mv2")) {
        await adapter.qualifyOwnerOnly(pathValue, "file");
        if (!metadata.isFile() || metadata.nlink !== 1) {
          throw new Error("desktop_directory_smoke_memory_set_invalid");
        }
        const canonical = await realpath(pathValue);
        if (!isContained(canonicalProfile, canonical)) {
          throw new Error("desktop_directory_smoke_memory_set_invalid");
        }
        found.push(canonical);
      }
    }
    await adapter.qualifyOwnerOnly(directory, "directory");
  };
  await walk(canonicalProfile);
  if (
    found.length !== expectedPaths.size ||
    found.some((pathValue) => !expectedPaths.has(pathValue)) ||
    [...expectedPaths].some((pathValue) => !found.includes(pathValue))
  ) {
    throw new Error("desktop_directory_smoke_memory_set_invalid");
  }
}

export function selectDirectorySmokeRequest(
  trust: PackagedBuildTrust,
  argv: readonly string[]
): boolean {
  const requests = argv.filter((value) => value === REQUEST_ARGUMENT);
  const malformed = argv.some((value) =>
    value.startsWith(`${REQUEST_ARGUMENT}=`)
  );
  if (requests.length > 1 || malformed) {
    throw new Error("desktop_directory_smoke_request_invalid");
  }
  if (requests.length === 0) {
    return false;
  }
  if (
    trust.buildMode !== "developer" ||
    trust.keyId !== "developer" ||
    trust.smokeAuthority !== "developer_directory_smoke_v1"
  ) {
    throw new Error("desktop_directory_smoke_forbidden");
  }
  return true;
}

export async function waitForDirectorySmokeMission(
  webContents: DirectorySmokeWebContents,
  options: {
    timeoutMs?: number;
    pollIntervalMs?: number;
  } = {}
): Promise<void> {
  const deadline = Date.now() + (options.timeoutMs ?? MISSION_TIMEOUT_MS);
  while (Date.now() < deadline) {
    if (
      webContents.isDestroyed() ||
      webContents.getURL() !== DESKTOP_APP_ENTRY_URL
    ) {
      throw new Error("desktop_directory_smoke_mission_untrusted");
    }
    const remaining = Math.max(1, deadline - Date.now());
    let timer: NodeJS.Timeout | undefined;
    const matched = await Promise.race([
      webContents.executeJavaScript(MISSION_PROBE),
      new Promise<never>((_resolve, reject) => {
        timer = setTimeout(
          () =>
            reject(
              new Error("desktop_directory_smoke_mission_timeout")
            ),
          remaining
        );
      })
    ]).finally(() => {
      if (timer !== undefined) {
        clearTimeout(timer);
      }
    });
    if (matched === true) {
      return;
    }
    await new Promise((resolvePromise) =>
      setTimeout(
        resolvePromise,
        options.pollIntervalMs ?? POLL_INTERVAL_MS
      )
    );
  }
  throw new Error("desktop_directory_smoke_mission_timeout");
}

export function createDirectorySmokeCycle(input: {
  userDataPath: string;
  profileRoot: string;
  sourceCommit: string;
  supervisorState(): SidecarSupervisorState;
  stopSupervisor(): Promise<void>;
  quit(): void;
  continueTimeoutMs?: number;
}): DirectorySmokeCycle {
  const userDataPath = resolve(input.userDataPath);
  const profileRoot = resolve(input.profileRoot);
  if (
    profileRoot !== join(userDataPath, "profiles", "default") ||
    !SOURCE_COMMIT_PATTERN.test(input.sourceCommit)
  ) {
    throw new Error("desktop_directory_smoke_profile_invalid");
  }
  const paths = pathsFor(userDataPath);
  const adapterPromise =
    createDeveloperRuntimePrivateFileAdapter(userDataPath);
  const ready: DirectorySmokeReady = Object.freeze({
    schema: "kestrel.desktop.directory-smoke-ready.v1",
    authenticated_readiness: true,
    authenticated_recovery: true,
    build_mode: "developer",
    hidden: true,
    memory_files: [...DIRECTORY_SMOKE_MEMORY_FILES],
    mission_command_url: DESKTOP_APP_ENTRY_URL,
    source_commit: input.sourceCommit
  });
  const completed: DirectorySmokeCompleted = Object.freeze({
    schema: "kestrel.desktop.directory-smoke-completed.v1",
    authenticated_shutdown: true,
    child_exited: true
  });

  return Object.freeze({
    paths,
    async runAfterMissionCommandLoaded(): Promise<void> {
      let stopAttempted = false;
      let stopped = false;
      try {
        const adapter = await adapterPromise;
        await prepareControlRoot(adapter, userDataPath, paths.controlRoot);
        const state = input.supervisorState();
        if (state.kind !== "ready" || state.profileId !== "default") {
          throw new Error("desktop_directory_smoke_readiness_unverified");
        }
        await verifyCanonicalMemorySet(
          userDataPath,
          profileRoot,
          adapter
        );
        await writeExclusiveJson(
          paths.readyPath,
          ready,
          adapter,
          paths.controlRoot
        );
        const continuation = await waitForFile(
          paths.continuePath,
          input.continueTimeoutMs ?? CONTINUE_TIMEOUT_MS,
          adapter,
          paths.controlRoot
        );
        if (
          continuation === null ||
          Array.isArray(continuation) ||
          typeof continuation !== "object" ||
          Object.keys(continuation).length !== 2 ||
          (continuation as Record<string, unknown>).schema !==
            "kestrel.desktop.directory-smoke-continue.v1" ||
          (continuation as Record<string, unknown>).continue !== true
        ) {
          throw new Error("desktop_directory_smoke_control_invalid");
        }
        stopAttempted = true;
        await input.stopSupervisor();
        stopped = true;
        await writeExclusiveJson(
          paths.completedPath,
          completed,
          adapter,
          paths.controlRoot
        );
        input.quit();
      } catch (error) {
        if (!stopped && !stopAttempted) {
          stopAttempted = true;
          try {
            await input.stopSupervisor();
            stopped = true;
          } catch (shutdownError) {
            if (
              shutdownError instanceof Error &&
              shutdownError.message === "authenticated_shutdown_failed"
            ) {
              stopped = true;
            } else {
              throw shutdownError;
            }
          }
        } else if (
          !stopped &&
          error instanceof Error &&
          error.message === "authenticated_shutdown_failed"
        ) {
          stopped = true;
        }
        if (stopped) {
          input.quit();
        }
        throw error;
      }
    },
    async waitForReadyForTest(): Promise<DirectorySmokeReady> {
      const adapter = await adapterPromise;
      await prepareControlRoot(adapter, userDataPath, paths.controlRoot);
      return waitForFile(
        paths.readyPath,
        CONTINUE_TIMEOUT_MS,
        adapter,
        paths.controlRoot
      ) as Promise<DirectorySmokeReady>;
    },
    async continueForTest(): Promise<void> {
      const adapter = await adapterPromise;
      await prepareControlRoot(adapter, userDataPath, paths.controlRoot);
      await writeExclusiveJson(
        paths.continuePath,
        {
          schema: "kestrel.desktop.directory-smoke-continue.v1",
          continue: true
        },
        adapter,
        paths.controlRoot
      );
    }
  });
}
