import { createHash, timingSafeEqual } from "node:crypto";
import { createReadStream } from "node:fs";
import {
  lstat,
  readFile,
  readlink,
  realpath
} from "node:fs/promises";
import { request as httpRequest } from "node:http";
import { StringDecoder } from "node:string_decoder";
import type { Readable } from "node:stream";
import { z } from "zod";
import {
  createNodePrivateFileAdapter,
  createPrivateLaunchFiles,
  readPrivateJsonArtifact,
  readSidecarReadiness,
  resolvePrivateProfile,
  type CapturedPrivateFileIdentity,
  type CapturedSidecarReadiness,
  type CreatePrivateLaunchFilesInput,
  type PrivateFilePlatformAdapter,
  type PrivateLaunchFiles,
  type PrivateProfileInput,
  type ResolvedPrivateProfile,
  type SidecarReadiness
} from "./private-files.js";
import {
  verifyResourceManifest,
  type VerifiedRendererAssets,
  type VerifiedResourceFile,
  type VerifiedResourceSet,
  type VerifyResourceManifestInput
} from "./resource-manifest.js";

const MEMORY_LAYERS = [
  "working",
  "episodic",
  "semantic",
  "procedural",
  "self",
  "policy"
] as const;
const LOG_LINE_BYTES = 1_024;
const HTTP_RESPONSE_BYTES = 16 * 1024;
const safeEnvironmentNames = [
  "LANG",
  "LC_ALL",
  "PATH",
  "SYSTEMROOT",
  "TEMP",
  "TMP",
  "TMPDIR",
  "TZ",
  "WINDIR"
] as const;
const sha256Schema = z.string().regex(/^[0-9a-f]{64}$/);
const prefixedSha256Schema = z.string().regex(/^sha256:[0-9a-f]{64}$/);
const authenticatedReadinessSchema = z
  .object({
    schema: z.literal("kestrel.desktop.readiness.v1"),
    ready: z.literal(true),
    profile_id: z.string().trim().min(1).max(120),
    launch_nonce_digest: sha256Schema,
    sidecar_version: z.string().trim().min(1).max(64),
    state_schema_version: z.number().int().nonnegative(),
    routing_schema_version: z.number().int().nonnegative(),
    memory_layers: z.tuple([
      z.literal("working"),
      z.literal("episodic"),
      z.literal("semantic"),
      z.literal("procedural"),
      z.literal("self"),
      z.literal("policy")
    ])
  })
  .strict();
const leaseMetadataSchema = z
  .object({
    schema: z.literal("kestrel.runtime_profile_lease.v1"),
    profile_id: z.string().trim().min(1).max(120),
    management: z.enum(["desktop", "cli"]),
    owner_digest: sha256Schema,
    pid: z.number().int().positive(),
    process_birth_marker: z.string().trim().min(1).max(256),
    executable_digest: sha256Schema,
    launch_nonce_digest: sha256Schema,
    base_url: z.string().url(),
    version: z.string().trim().min(1).max(64),
    created_at: z.string().trim().min(1).max(128)
  })
  .strict();

export interface AuthenticatedDesktopReadiness {
  schema: "kestrel.desktop.readiness.v1";
  ready: true;
  profile_id: string;
  launch_nonce_digest: string;
  sidecar_version: string;
  state_schema_version: number;
  routing_schema_version: number;
  memory_layers: [
    "working",
    "episodic",
    "semantic",
    "procedural",
    "self",
    "policy"
  ];
}

export type ProfileLeaseDisposition =
  | "available"
  | "attach_desktop"
  | "offer_desktop_takeover"
  | "version_conflict"
  | "stale_unverified"
  | "foreign_or_unrelated";

export interface ProfileLeaseCurrent {
  profileId: string;
  management: "desktop" | "cli";
  ownerDigest?: string;
  pid: number;
  processBirthMarker: string;
  executableDigest: string;
  launchNonceDigest: string;
  baseUrl: string;
  version: string;
}

export interface ProfileLeaseEvidence {
  status: ProfileLeaseDisposition;
  current?: ProfileLeaseCurrent;
  detail?: string;
}

export interface SidecarProcessIdentity {
  pid: number;
  ownerDigest?: string;
  processBirthMarker: string;
  executableDigest: string;
}

export interface DesktopParentIdentity {
  pid: number;
  processBirthMarker: string;
}

export interface RetainedSidecarChild {
  readonly pid?: number;
  readonly stdout: Readable | null;
  readonly stderr: Readable | null;
  exitCode: number | null;
  signalCode: NodeJS.Signals | null;
  on(event: string, listener: (...args: any[]) => void): this;
  once(event: string, listener: (...args: any[]) => void): this;
  removeListener?(event: string, listener: (...args: any[]) => void): this;
  kill(signal?: NodeJS.Signals | number): boolean;
}

export interface SidecarSpawnRequest {
  executable: string;
  args: [string];
  options: {
    shell: false;
    detached: false;
    stdio: ["ignore", "pipe", "pipe"];
    env: Record<string, string>;
  };
}

export interface VerifiedExecutableSpawnRequest {
  args: [string];
  options: SidecarSpawnRequest["options"];
}

export interface VerifiedExecutableLaunchCapability {
  readonly resource: VerifiedResourceFile;
  readonly mechanism:
    | "linux_openat2_fexecve"
    | "sealed_verified_native"
    | "test_verified_handle";
  spawn(request: VerifiedExecutableSpawnRequest): RetainedSidecarChild;
  close(): Promise<void>;
}

export interface SidecarSupervisorDependencies {
  verifyResources(): Promise<VerifiedResourceSet>;
  acquireVerifiedExecutable(
    resources: VerifiedResourceSet,
    relativePath: string
  ): Promise<VerifiedExecutableLaunchCapability>;
  resolveProfile(): Promise<ResolvedPrivateProfile>;
  inspectLease(
    profile: ResolvedPrivateProfile
  ): Promise<ProfileLeaseEvidence>;
  parentIdentity(): Promise<DesktopParentIdentity>;
  createLaunchFiles(
    input: CreatePrivateLaunchFilesInput
  ): Promise<PrivateLaunchFiles>;
  waitForReadiness(input: {
    path: string;
    child: RetainedSidecarChild;
    timeoutMs: number;
    signal: AbortSignal;
  }): Promise<CapturedSidecarReadiness>;
  inspectProcess(pid: number): Promise<SidecarProcessIdentity | null>;
  requestReadiness(input: {
    baseUrl: string;
    apiToken: string;
  }): Promise<AuthenticatedDesktopReadiness>;
  requestShutdown(input: {
    baseUrl: string;
    apiToken: string;
  }): Promise<void>;
  waitForExit(
    child: RetainedSidecarChild,
    timeoutMs: number
  ): Promise<boolean>;
  log(line: string): void;
}

export interface SidecarSupervisorConfig {
  sidecarRelativePath: string;
  sidecarVersion: string;
  readinessTimeoutMs: number;
  shutdownTimeoutMs: number;
  environment: Readonly<Record<string, string | undefined>>;
}

export type SidecarSupervisorState =
  | { kind: "verifying" }
  | { kind: "starting" }
  | {
      kind: "ready";
      profileId: string;
      baseUrl: string;
      sidecarVersion: string;
    }
  | { kind: "stopping" }
  | {
      kind: "recovery";
      reason:
        | "sidecar_unavailable"
        | "sidecar_unverified"
        | "profile_conflict"
        | "version_incompatible"
        | "reconciliation_required";
      detail: string;
    };

interface ActiveSidecar {
  child: RetainedSidecarChild;
  executable: VerifiedExecutableLaunchCapability;
  executableClosed: boolean;
  launch: PrivateLaunchFiles;
  profile: ResolvedPrivateProfile;
  resources: VerifiedResourceSet;
  baseUrl: string;
  processIdentity: SidecarProcessIdentity | null;
  readiness: SidecarReadiness | null;
  readinessIdentity: CapturedPrivateFileIdentity | null;
  verified: boolean;
}

export class SidecarSupervisorError extends Error {
  constructor(
    readonly code: string,
    readonly recoveryReason:
      | "sidecar_unavailable"
      | "sidecar_unverified"
      | "profile_conflict"
      | "version_incompatible"
      | "reconciliation_required"
  ) {
    super(code);
    this.name = "SidecarSupervisorError";
  }
}

function constantTimeEqual(left: string, right: string): boolean {
  const leftBytes = Buffer.from(left);
  const rightBytes = Buffer.from(right);
  return (
    leftBytes.byteLength === rightBytes.byteLength &&
    timingSafeEqual(leftBytes, rightBytes)
  );
}

function minimalEnvironment(
  source: Readonly<Record<string, string | undefined>>
): Record<string, string> {
  const environment: Record<string, string> = {};
  for (const name of safeEnvironmentNames) {
    const value = source[name];
    if (value !== undefined && value.length > 0 && !value.includes("\0")) {
      environment[name] = value;
    }
  }
  return environment;
}

function recoveryForLease(
  evidence: ProfileLeaseEvidence
): SidecarSupervisorError {
  if (evidence.status === "version_conflict") {
    return new SidecarSupervisorError(
      evidence.status,
      "version_incompatible"
    );
  }
  return new SidecarSupervisorError(evidence.status, "profile_conflict");
}

function fixedErrorCode(error: unknown): string {
  if (
    error !== null &&
    typeof error === "object" &&
    "code" in error &&
    typeof error.code === "string"
  ) {
    return error.code.slice(0, 160);
  }
  if (error instanceof SidecarSupervisorError) {
    return error.code;
  }
  return "sidecar_start_failed";
}

function installBoundedLogReader(
  stream: Readable | null,
  source: "stdout" | "stderr",
  secrets: readonly string[],
  log: (line: string) => void
): void {
  if (stream === null) {
    return;
  }
  const decoder = new StringDecoder("utf8");
  let pending = "";
  const emit = (rawLine: string): void => {
    let redacted = rawLine;
    for (const secret of secrets) {
      if (secret.length > 0) {
        redacted = redacted.split(secret).join("[redacted]");
      }
    }
    redacted = redacted
      .replace(/Bearer\s+\S+/gi, "Bearer [redacted]")
      .replace(
        /\b(api[_-]?token|launch[_-]?nonce)\s*=\s*\S+/gi,
        "$1=[redacted]"
      );
    const prefix = `[sidecar:${source}] `;
    const budget = LOG_LINE_BYTES - Buffer.byteLength(prefix);
    const bounded = Buffer.from(redacted, "utf8")
      .subarray(0, Math.max(0, budget))
      .toString("utf8");
    log(`${prefix}${bounded}`);
  };
  stream.on("data", (chunk: Buffer | string) => {
    pending += decoder.write(
      typeof chunk === "string" ? Buffer.from(chunk) : chunk
    );
    while (true) {
      const newline = pending.indexOf("\n");
      if (newline < 0) {
        if (Buffer.byteLength(pending) > 64 * 1024) {
          emit(pending);
          pending = "";
        }
        break;
      }
      emit(pending.slice(0, newline).replace(/\r$/, ""));
      pending = pending.slice(newline + 1);
    }
  });
  stream.on("end", () => {
    pending += decoder.end();
    if (pending.length > 0) {
      emit(pending);
      pending = "";
    }
  });
}

function validateLocalReadinessEvidence(
  active: ActiveSidecar,
  readiness: SidecarReadiness,
  processIdentity: SidecarProcessIdentity,
  lease: ProfileLeaseEvidence,
  expectedVersion: string,
  expectedExecutableDigest: string
): string {
  const childPid = active.child.pid;
  const expectedBaseUrl = `http://127.0.0.1:${readiness.port}/`;
  if (
    childPid === undefined ||
    active.child.exitCode !== null ||
    active.child.signalCode !== null ||
    readiness.pid !== childPid ||
    readiness.profile_id !== active.profile.profileId ||
    readiness.sidecar_version !== expectedVersion ||
    readiness.resource_manifest_digest !== active.resources.manifestDigest ||
    readiness.launch_nonce_digest !== active.launch.launchNonceDigest ||
    readiness.executable_digest !== expectedExecutableDigest ||
    processIdentity.pid !== childPid ||
    processIdentity.processBirthMarker !== readiness.process_birth_marker ||
    processIdentity.executableDigest !== readiness.executable_digest
  ) {
    throw new SidecarSupervisorError(
      "sidecar_readiness_identity_mismatch",
      "sidecar_unverified"
    );
  }
  const current = lease.current;
  if (
    lease.status !== "attach_desktop" ||
    current === undefined ||
    current.management !== "desktop" ||
    current.pid !== childPid ||
    current.profileId !== active.profile.profileId ||
    current.processBirthMarker !== readiness.process_birth_marker ||
    current.executableDigest !== readiness.executable_digest ||
    current.launchNonceDigest !== active.launch.launchNonceDigest ||
    current.baseUrl !== expectedBaseUrl ||
    current.version !== expectedVersion
  ) {
    throw new SidecarSupervisorError(
      "profile_lease_readiness_mismatch",
      "profile_conflict"
    );
  }
  return expectedBaseUrl;
}

function validateAuthenticatedReadiness(
  active: ActiveSidecar,
  apiReadiness: AuthenticatedDesktopReadiness,
  expectedVersion: string
): void {
  if (
    apiReadiness.schema !== "kestrel.desktop.readiness.v1" ||
    apiReadiness.ready !== true ||
    apiReadiness.profile_id !== active.profile.profileId ||
    apiReadiness.sidecar_version !== expectedVersion ||
    apiReadiness.launch_nonce_digest !== active.launch.launchNonceDigest ||
    apiReadiness.memory_layers.length !== MEMORY_LAYERS.length ||
    apiReadiness.memory_layers.some(
      (layer, index) => layer !== MEMORY_LAYERS[index]
    )
  ) {
    throw new SidecarSupervisorError(
      "authenticated_readiness_mismatch",
      "sidecar_unverified"
    );
  }
}

export class SidecarSupervisor {
  state: SidecarSupervisorState = {
    kind: "recovery",
    reason: "sidecar_unavailable",
    detail: "not_started"
  };
  private active: ActiveSidecar | null = null;
  private starting = false;
  private stopping = false;
  private generation = 0;
  private startPromise: Promise<VerifiedRendererAssets> | null = null;
  private stopPromise: Promise<void> | null = null;
  private launchAbort: AbortController | null = null;

  constructor(
    private readonly config: SidecarSupervisorConfig,
    private readonly dependencies: SidecarSupervisorDependencies
  ) {}

  async start(): Promise<VerifiedRendererAssets> {
    if (this.startPromise !== null || this.active !== null) {
      throw new SidecarSupervisorError(
        "sidecar_already_started",
        "sidecar_unavailable"
      );
    }
    this.stopping = false;
    this.generation += 1;
    const generation = this.generation;
    const abort = new AbortController();
    this.launchAbort = abort;
    const operation = this.launch(generation, abort.signal);
    this.startPromise = operation;
    void operation.then(
      () => {
        if (this.startPromise === operation) {
          this.startPromise = null;
        }
        if (this.launchAbort === abort) {
          this.launchAbort = null;
        }
      },
      () => {
        if (this.startPromise === operation) {
          this.startPromise = null;
        }
        if (this.launchAbort === abort) {
          this.launchAbort = null;
        }
      }
    );
    return operation;
  }

  private throwIfLaunchCancelled(
    generation: number,
    signal: AbortSignal
  ): void {
    if (
      signal.aborted ||
      this.stopping ||
      generation !== this.generation
    ) {
      throw new SidecarSupervisorError(
        "sidecar_start_cancelled",
        "sidecar_unavailable"
      );
    }
  }

  private async launch(
    generation: number,
    signal: AbortSignal
  ): Promise<VerifiedRendererAssets> {
    this.starting = true;
    this.state = { kind: "verifying" };
    let launch: PrivateLaunchFiles | null = null;
    let child: RetainedSidecarChild | null = null;
    let executable: VerifiedExecutableLaunchCapability | null = null;
    let expectedExecutableDigest: string | null = null;
    try {
      const resources = await this.dependencies.verifyResources();
      this.throwIfLaunchCancelled(generation, signal);
      const sidecar = resources.files.get(this.config.sidecarRelativePath);
      if (sidecar === undefined) {
        throw new SidecarSupervisorError(
          "verified_sidecar_missing",
          "sidecar_unverified"
        );
      }
      executable = await this.dependencies.acquireVerifiedExecutable(
        resources,
        this.config.sidecarRelativePath
      );
      this.throwIfLaunchCancelled(generation, signal);
      if (
        executable.resource.path !== sidecar.path ||
        executable.resource.size !== sidecar.size ||
        executable.resource.sha256 !== sidecar.sha256
      ) {
        throw new SidecarSupervisorError(
          "verified_executable_capability_mismatch",
          "sidecar_unverified"
        );
      }
      const profile = await this.dependencies.resolveProfile();
      this.throwIfLaunchCancelled(generation, signal);
      const initialLease = await this.dependencies.inspectLease(profile);
      this.throwIfLaunchCancelled(generation, signal);
      if (initialLease.status !== "available") {
        throw recoveryForLease(initialLease);
      }
      const parent = await this.dependencies.parentIdentity();
      this.throwIfLaunchCancelled(generation, signal);
      launch = await this.dependencies.createLaunchFiles({
        profile,
        parentPid: parent.pid,
        parentBirthMarker: parent.processBirthMarker,
        resourceManifestDigest: resources.manifestDigest
      });
      this.throwIfLaunchCancelled(generation, signal);
      expectedExecutableDigest = sidecar.sha256;
      this.state = { kind: "starting" };
      child = executable.spawn({
        args: [launch.bootstrapPath],
        options: {
          shell: false,
          detached: false,
          stdio: ["ignore", "pipe", "pipe"],
          env: minimalEnvironment(this.config.environment)
        }
      });
      let terminal: SidecarSupervisorError | null = null;
      let rejectTerminal!: (error: SidecarSupervisorError) => void;
      const terminalPromise = new Promise<never>((_resolve, reject) => {
        rejectTerminal = reject;
      });
      const reportTerminal = (error: SidecarSupervisorError): void => {
        if (terminal !== null) {
          return;
        }
        terminal = error;
        rejectTerminal(error);
        const current = this.active;
        if (current !== null && current.child === child && current.verified) {
          void this.handleUnexpectedExit(
            generation,
            child?.exitCode ?? null,
            child?.signalCode ?? null
          );
        }
      };
      child.once("error", () => {
        reportTerminal(
          new SidecarSupervisorError(
            "sidecar_spawn_failed",
            "sidecar_unavailable"
          )
        );
      });
      child.once(
        "exit",
        (_code: number | null, _exitSignal: NodeJS.Signals | null) => {
          reportTerminal(
            new SidecarSupervisorError(
              "sidecar_exited_before_readiness",
              "sidecar_unavailable"
            )
          );
        }
      );
      if (child.pid === undefined || child.pid <= 0) {
        throw new SidecarSupervisorError(
          "sidecar_spawn_identity_missing",
          "sidecar_unverified"
        );
      }
      installBoundedLogReader(
        child.stdout,
        "stdout",
        [launch.apiToken, launch.launchNonce],
        this.dependencies.log
      );
      installBoundedLogReader(
        child.stderr,
        "stderr",
        [launch.apiToken, launch.launchNonce],
        this.dependencies.log
      );
      const active: ActiveSidecar = {
        child,
        executable,
        executableClosed: false,
        launch,
        profile,
        resources,
        baseUrl: "",
        processIdentity: null,
        readiness: null,
        readinessIdentity: null,
        verified: false
      };
      this.active = active;
      const spawnedIdentity = await Promise.race([
        this.dependencies.inspectProcess(child.pid),
        terminalPromise
      ]);
      this.throwIfLaunchCancelled(generation, signal);
      if (
        spawnedIdentity === null ||
        spawnedIdentity.pid !== child.pid ||
        spawnedIdentity.executableDigest !== expectedExecutableDigest
      ) {
        throw new SidecarSupervisorError(
          "sidecar_process_identity_unverified",
          "sidecar_unverified"
        );
      }
      active.processIdentity = spawnedIdentity;
      try {
        await this.closeActiveExecutable(active);
      } catch {
        throw new SidecarSupervisorError(
          "verified_executable_close_failed",
          "sidecar_unverified"
        );
      }
      this.throwIfLaunchCancelled(generation, signal);
      const capturedReadiness = await Promise.race([
        this.dependencies.waitForReadiness({
          path: launch.readinessPath,
          child,
          timeoutMs: this.config.readinessTimeoutMs,
          signal
        }),
        terminalPromise
      ]);
      const readiness = capturedReadiness.readiness;
      this.throwIfLaunchCancelled(generation, signal);
      if (terminal !== null) {
        throw terminal;
      }
      const processIdentity = await this.dependencies.inspectProcess(
        child.pid
      );
      this.throwIfLaunchCancelled(generation, signal);
      if (terminal !== null) {
        throw terminal;
      }
      if (
        processIdentity === null ||
        processIdentity.executableDigest !== expectedExecutableDigest
      ) {
        throw new SidecarSupervisorError(
          "sidecar_process_identity_unverified",
          "sidecar_unverified"
        );
      }
      const postLaunchLease = await this.dependencies.inspectLease(profile);
      this.throwIfLaunchCancelled(generation, signal);
      if (terminal !== null) {
        throw terminal;
      }
      const baseUrl = validateLocalReadinessEvidence(
        active,
        readiness,
        processIdentity,
        postLaunchLease,
        this.config.sidecarVersion,
        expectedExecutableDigest
      );
      active.processIdentity = processIdentity;
      active.readiness = readiness;
      active.readinessIdentity = capturedReadiness.identity;
      active.baseUrl = baseUrl;
      const apiReadiness = await this.dependencies.requestReadiness({
        baseUrl,
        apiToken: launch.apiToken
      });
      this.throwIfLaunchCancelled(generation, signal);
      if (terminal !== null) {
        throw terminal;
      }
      validateAuthenticatedReadiness(
        active,
        apiReadiness,
        this.config.sidecarVersion
      );
      active.verified = true;
      this.state = {
        kind: "ready",
        profileId: profile.profileId,
        baseUrl: active.baseUrl,
        sidecarVersion: this.config.sidecarVersion
      };
      return resources.rendererAssets;
    } catch (error) {
      let surfacedError: unknown =
        this.stopping || signal.aborted || generation !== this.generation
          ? new SidecarSupervisorError(
              "sidecar_start_cancelled",
              "sidecar_unavailable"
            )
          : error;
      const active = this.active?.child === child ? this.active : null;
      if (active !== null) {
        let terminationFailure: unknown = null;
        try {
          await this.terminateActive(active, false);
        } catch (terminationError) {
          terminationFailure = terminationError;
          this.dependencies.log(
            `[sidecar:supervisor] ${fixedErrorCode(terminationError)}`
          );
        }
        try {
          await this.closeActiveExecutable(active);
        } catch {
          surfacedError = new SidecarSupervisorError(
            "verified_executable_close_failed",
            "sidecar_unverified"
          );
        }
        if (
          terminationFailure instanceof SidecarSupervisorError &&
          (terminationFailure.code === "sidecar_cleanup_failed" ||
            terminationFailure.code ===
              "verified_executable_close_failed")
        ) {
          surfacedError = terminationFailure;
        }
      } else {
        let cleanupFailed = false;
        let closeFailed = false;
        if (launch !== null) {
          try {
            await launch.cleanup();
          } catch {
            cleanupFailed = true;
          }
        }
        if (executable !== null) {
          try {
            await executable.close();
          } catch {
            closeFailed = true;
          }
        }
        if (cleanupFailed) {
          surfacedError = new SidecarSupervisorError(
            "sidecar_cleanup_failed",
            "sidecar_unavailable"
          );
        } else if (closeFailed) {
          surfacedError = new SidecarSupervisorError(
            "verified_executable_close_failed",
            "sidecar_unverified"
          );
        }
      }
      const recoveryReason =
        surfacedError instanceof SidecarSupervisorError
          ? surfacedError.recoveryReason
          : fixedErrorCode(surfacedError).startsWith("resource_")
            ? "sidecar_unverified"
            : "sidecar_unavailable";
      if (!this.stopping) {
        this.state = {
          kind: "recovery",
          reason: recoveryReason,
          detail: fixedErrorCode(surfacedError)
        };
      }
      throw surfacedError;
    } finally {
      this.starting = false;
    }
  }

  private async handleUnexpectedExit(
    generation: number,
    _exitCode: number | null,
    _signal: NodeJS.Signals | null
  ): Promise<void> {
    const active = this.active;
    if (active === null || generation !== this.generation) {
      return;
    }
    if (this.stopping) {
      return;
    }
    try {
      await this.finalizeExitedActive(active);
    } catch (error) {
      this.dependencies.log(
        `[sidecar:supervisor] ${fixedErrorCode(error)}`
      );
      return;
    }
    this.state = {
      kind: "recovery",
      reason: "reconciliation_required",
      detail: "unexpected_exit_reconciliation_required"
    };
  }

  async stop(): Promise<void> {
    if (this.stopPromise !== null) {
      return this.stopPromise;
    }
    this.stopping = true;
    this.generation += 1;
    this.launchAbort?.abort();
    this.state = { kind: "stopping" };
    const start = this.startPromise;
    const operation = this.stopAfterStartSettles(start);
    this.stopPromise = operation;
    void operation.then(
      () => {
        if (this.stopPromise === operation) {
          this.stopPromise = null;
        }
      },
      () => {
        if (this.stopPromise === operation) {
          this.stopPromise = null;
        }
      }
    );
    return operation;
  }

  private async stopAfterStartSettles(
    start: Promise<VerifiedRendererAssets> | null
  ): Promise<void> {
    await start?.catch(() => undefined);
    const active = this.active;
    if (active === null) {
      return;
    }
    await this.terminateActive(active, active.verified);
  }

  private childHasExited(child: RetainedSidecarChild): boolean {
    return child.exitCode !== null || child.signalCode !== null;
  }

  private terminationError(code: string): SidecarSupervisorError {
    this.state = {
      kind: "recovery",
      reason: "sidecar_unavailable",
      detail: code
    };
    return new SidecarSupervisorError(code, "sidecar_unavailable");
  }

  private async waitForTerminationStage(
    child: RetainedSidecarChild
  ): Promise<boolean> {
    try {
      const exited = await this.dependencies.waitForExit(
        child,
        this.config.shutdownTimeoutMs
      );
      return exited || this.childHasExited(child);
    } catch {
      this.dependencies.log(
        "[sidecar:supervisor] sidecar_exit_wait_failed"
      );
      return this.childHasExited(child);
    }
  }

  private async reattestTerminationAuthority(
    active: ActiveSidecar
  ): Promise<boolean> {
    const expected = active.processIdentity;
    const pid = active.child.pid;
    if (expected === null || pid === undefined || pid !== expected.pid) {
      return false;
    }
    let current: SidecarProcessIdentity | null;
    try {
      current = await this.dependencies.inspectProcess(pid);
    } catch {
      return false;
    }
    if (
      current === null ||
      current.pid !== expected.pid ||
      current.processBirthMarker !== expected.processBirthMarker ||
      current.executableDigest !== expected.executableDigest ||
      (expected.ownerDigest !== undefined &&
        current.ownerDigest !== expected.ownerDigest)
    ) {
      return false;
    }
    const readiness = active.readiness;
    if (readiness === null) {
      return true;
    }
    let lease: ProfileLeaseEvidence;
    try {
      lease = await this.dependencies.inspectLease(active.profile);
    } catch {
      return false;
    }
    const leaseCurrent = lease.current;
    return (
      lease.status === "attach_desktop" &&
      leaseCurrent !== undefined &&
      leaseCurrent.management === "desktop" &&
      leaseCurrent.profileId === active.profile.profileId &&
      leaseCurrent.pid === expected.pid &&
      leaseCurrent.processBirthMarker === expected.processBirthMarker &&
      leaseCurrent.executableDigest === expected.executableDigest &&
      leaseCurrent.launchNonceDigest ===
        active.launch.launchNonceDigest &&
      leaseCurrent.baseUrl === active.baseUrl &&
      leaseCurrent.version === this.config.sidecarVersion
    );
  }

  private async finalizeExitedActive(active: ActiveSidecar): Promise<void> {
    let cleanupFailed = false;
    let closeFailed = false;
    try {
      await active.launch.cleanup(active.readinessIdentity ?? undefined);
    } catch {
      cleanupFailed = true;
    }
    try {
      await this.closeActiveExecutable(active);
    } catch {
      closeFailed = true;
    }
    if (this.active === active) {
      this.active = null;
    }
    if (cleanupFailed) {
      throw this.terminationError("sidecar_cleanup_failed");
    }
    if (closeFailed) {
      throw this.terminationError(
        "verified_executable_close_failed"
      );
    }
  }

  private async closeActiveExecutable(
    active: ActiveSidecar
  ): Promise<void> {
    if (active.executableClosed) {
      return;
    }
    await active.executable.close();
    active.executableClosed = true;
  }

  private async terminateActive(
    active: ActiveSidecar,
    requestGraceful: boolean
  ): Promise<void> {
    if (this.childHasExited(active.child)) {
      await this.finalizeExitedActive(active);
      return;
    }

    if (requestGraceful) {
      try {
        await this.dependencies.requestShutdown({
          baseUrl: active.baseUrl,
          apiToken: active.launch.apiToken
        });
      } catch {
        // The exit wait below is mandatory even when the request failed.
        this.dependencies.log(
          "[sidecar:supervisor] authenticated_shutdown_failed"
        );
      }
      if (await this.waitForTerminationStage(active.child)) {
        await this.finalizeExitedActive(active);
        return;
      }
    }

    if (!(await this.reattestTerminationAuthority(active))) {
      throw this.terminationError(
        "sidecar_termination_identity_unverified"
      );
    }
    const termAccepted = active.child.kill("SIGTERM");
    if (await this.waitForTerminationStage(active.child)) {
      await this.finalizeExitedActive(active);
      return;
    }
    if (!termAccepted) {
      throw this.terminationError(
        "sidecar_termination_signal_rejected"
      );
    }

    if (!(await this.reattestTerminationAuthority(active))) {
      throw this.terminationError(
        "sidecar_termination_identity_unverified"
      );
    }
    const killAccepted = active.child.kill("SIGKILL");
    if (await this.waitForTerminationStage(active.child)) {
      await this.finalizeExitedActive(active);
      return;
    }
    if (!killAccepted) {
      throw this.terminationError(
        "sidecar_termination_signal_rejected"
      );
    }
    throw this.terminationError("sidecar_termination_unconfirmed");
  }
}

function loopbackBaseUrl(value: string): string {
  const parsed = new URL(value);
  if (
    parsed.protocol !== "http:" ||
    parsed.hostname !== "127.0.0.1" ||
    parsed.port === "" ||
    parsed.username !== "" ||
    parsed.password !== "" ||
    parsed.search !== "" ||
    parsed.hash !== ""
  ) {
    throw new Error("desktop_base_url_untrusted");
  }
  return `${parsed.origin}/`;
}

async function requestBoundedJson(
  method: "GET" | "POST",
  baseUrl: string,
  path: string,
  apiToken: string,
  timeoutMs: number
): Promise<{ statusCode: number; payload: unknown }> {
  const root = loopbackBaseUrl(baseUrl);
  const url = new URL(path, root);
  return new Promise((resolvePromise, rejectPromise) => {
    let absoluteTimeout: NodeJS.Timeout | undefined;
    const clearDeadline = (): void => {
      if (absoluteTimeout !== undefined) {
        clearTimeout(absoluteTimeout);
      }
    };
    const request = httpRequest(
      url,
      {
        method,
        headers: {
          Authorization: `Bearer ${apiToken}`,
          "Content-Length": "0"
        },
        timeout: timeoutMs
      },
      (response) => {
        const chunks: Buffer[] = [];
        let bytes = 0;
        response.on("data", (chunk: Buffer) => {
          bytes += chunk.byteLength;
          if (bytes > HTTP_RESPONSE_BYTES) {
            request.destroy(new Error("desktop_http_response_too_large"));
            return;
          }
          chunks.push(chunk);
        });
        response.on("end", () => {
          clearDeadline();
          try {
            resolvePromise({
              statusCode: response.statusCode ?? 0,
              payload: JSON.parse(Buffer.concat(chunks).toString("utf8"))
            });
          } catch (error) {
            rejectPromise(error);
          }
        });
      }
    );
    absoluteTimeout = setTimeout(() => {
      request.destroy(new Error("desktop_http_timeout"));
    }, timeoutMs);
    request.on("timeout", () => {
      request.destroy(new Error("desktop_http_timeout"));
    });
    request.on("close", clearDeadline);
    request.on("error", (error) => {
      clearDeadline();
      rejectPromise(error);
    });
    request.end();
  });
}

export async function waitForRetainedChildExit(
  child: RetainedSidecarChild,
  timeoutMs: number
): Promise<boolean> {
  if (child.exitCode !== null || child.signalCode !== null) {
    return true;
  }
  return new Promise((resolvePromise) => {
    let settled = false;
    const onExit = (): void => {
      if (!settled) {
        settled = true;
        clearTimeout(timer);
        resolvePromise(true);
      }
    };
    const timer = setTimeout(() => {
      if (!settled) {
        settled = true;
        child.removeListener?.("exit", onExit);
        resolvePromise(false);
      }
    }, timeoutMs);
    child.once("exit", onExit);
  });
}

async function sha256File(path: string): Promise<string> {
  return new Promise((resolvePromise, rejectPromise) => {
    const digest = createHash("sha256");
    const stream = createReadStream(path);
    stream.on("data", (chunk) => digest.update(chunk));
    stream.on("error", rejectPromise);
    stream.on("end", () => resolvePromise(digest.digest("hex")));
  });
}

export async function inspectNodeProcess(
  pid: number
): Promise<SidecarProcessIdentity | null> {
  if (
    process.platform !== "linux" ||
    !Number.isSafeInteger(pid) ||
    pid <= 0
  ) {
    return null;
  }
  try {
    const procRoot = `/proc/${pid}`;
    const [status, stat, executableLink] = await Promise.all([
      readFile(`${procRoot}/status`, "utf8"),
      readFile(`${procRoot}/stat`, "utf8"),
      readlink(`${procRoot}/exe`)
    ]);
    const uidLine = status
      .split("\n")
      .find((line) => line.startsWith("Uid:"));
    const uid = uidLine?.trim().split(/\s+/)[1];
    const closeParen = stat.lastIndexOf(")");
    const tail = stat.slice(closeParen + 2).trim().split(/\s+/);
    const startTicks = tail[19];
    if (uid === undefined || startTicks === undefined) {
      return null;
    }
    const executable = await realpath(executableLink);
    return {
      pid,
      ownerDigest: createHash("sha256")
        .update(`uid:${Number(uid)}`)
        .digest("hex"),
      processBirthMarker: `proc-start-ticks:${startTicks}`,
      executableDigest: await sha256File(executable)
    };
  } catch {
    return null;
  }
}

function normalizedLeaseBaseUrl(value: string): string | null {
  try {
    return loopbackBaseUrl(value);
  } catch {
    return null;
  }
}

export async function inspectProfileLeaseMetadata(
  profile: ResolvedPrivateProfile,
  expectedVersion: string,
  adapter: PrivateFilePlatformAdapter,
  processInspector: (
    pid: number
  ) => Promise<SidecarProcessIdentity | null>
): Promise<ProfileLeaseEvidence> {
  const metadataPath = `${profile.leaseControlRoot}/runtime-profile.json`;
  let raw: unknown;
  try {
    raw = await readPrivateJsonArtifact(metadataPath, adapter);
  } catch (error) {
    if (
      error instanceof Error &&
      error.message === "private_artifact_missing"
    ) {
      return { status: "available" };
    }
    return {
      status: "stale_unverified",
      detail: "lease_metadata_unreadable"
    };
  }
  const parsed = leaseMetadataSchema.safeParse(raw);
  if (!parsed.success) {
    return {
      status: "stale_unverified",
      detail: "lease_metadata_invalid"
    };
  }
  const metadata = parsed.data;
  const baseUrl = normalizedLeaseBaseUrl(metadata.base_url);
  const processIdentity = await processInspector(metadata.pid);
  if (
    baseUrl === null ||
    processIdentity === null ||
    processIdentity.pid !== metadata.pid ||
    processIdentity.ownerDigest === undefined ||
    !constantTimeEqual(processIdentity.ownerDigest, metadata.owner_digest) ||
    !constantTimeEqual(
      processIdentity.processBirthMarker,
      metadata.process_birth_marker
    ) ||
    !constantTimeEqual(
      processIdentity.executableDigest,
      metadata.executable_digest
    ) ||
    metadata.profile_id !== profile.profileId
  ) {
    return {
      status: "foreign_or_unrelated",
      detail: "lease_process_identity_unverified"
    };
  }
  const current: ProfileLeaseCurrent = {
    profileId: metadata.profile_id,
    management: metadata.management,
    ownerDigest: metadata.owner_digest,
    pid: metadata.pid,
    processBirthMarker: metadata.process_birth_marker,
    executableDigest: metadata.executable_digest,
    launchNonceDigest: metadata.launch_nonce_digest,
    baseUrl,
    version: metadata.version
  };
  if (metadata.version !== expectedVersion) {
    return {
      status: "version_conflict",
      current,
      detail: "runtime_version_mismatch"
    };
  }
  return {
    status:
      metadata.management === "desktop"
        ? "attach_desktop"
        : "offer_desktop_takeover",
    current
  };
}

export interface NodeSupervisorDependencyInput {
  resourceVerification: VerifyResourceManifestInput;
  profile: PrivateProfileInput;
  sidecarVersion: string;
  readinessPollMs?: number;
  privateFileAdapter?: PrivateFilePlatformAdapter;
  processInspector?: (
    pid: number
  ) => Promise<SidecarProcessIdentity | null>;
  log?(line: string): void;
}

export function createNodeSupervisorDependencies(
  input: NodeSupervisorDependencyInput
): SidecarSupervisorDependencies {
  const adapter =
    input.privateFileAdapter ?? createNodePrivateFileAdapter();
  const inspectProcess = input.processInspector ?? inspectNodeProcess;
  return {
    verifyResources: () =>
      verifyResourceManifest(input.resourceVerification),
    async acquireVerifiedExecutable() {
      throw new SidecarSupervisorError(
        "verified_executable_launch_unqualified",
        "sidecar_unverified"
      );
    },
    resolveProfile: () => resolvePrivateProfile(input.profile, adapter),
    inspectLease: (profile) =>
      inspectProfileLeaseMetadata(
        profile,
        input.sidecarVersion,
        adapter,
        inspectProcess
      ),
    async parentIdentity(): Promise<DesktopParentIdentity> {
      const identity = await inspectProcess(process.pid);
      if (identity === null) {
        throw new SidecarSupervisorError(
          "desktop_parent_identity_unqualified",
          "sidecar_unverified"
        );
      }
      return {
        pid: identity.pid,
        processBirthMarker: identity.processBirthMarker
      };
    },
    createLaunchFiles: (launchInput) =>
      createPrivateLaunchFiles({
        ...launchInput,
        platformAdapter: adapter
      }),
    async waitForReadiness({
      path,
      child,
      timeoutMs,
      signal
    }): Promise<CapturedSidecarReadiness> {
      const deadline = Date.now() + timeoutMs;
      while (Date.now() <= deadline) {
        if (signal.aborted) {
          throw new SidecarSupervisorError(
            "sidecar_start_cancelled",
            "sidecar_unavailable"
          );
        }
        if (child.exitCode !== null || child.signalCode !== null) {
          throw new SidecarSupervisorError(
            "sidecar_exited_before_readiness",
            "sidecar_unavailable"
          );
        }
        try {
          return await readSidecarReadiness(path, adapter);
        } catch (error) {
          if (
            !(error instanceof Error) ||
            error.message !== "private_artifact_missing"
          ) {
            throw error;
          }
        }
        await new Promise<void>((resolvePromise, rejectPromise) => {
          const onAbort = (): void => {
            clearTimeout(timer);
            rejectPromise(
              new SidecarSupervisorError(
                "sidecar_start_cancelled",
                "sidecar_unavailable"
              )
            );
          };
          const timer = setTimeout(() => {
            signal.removeEventListener("abort", onAbort);
            resolvePromise();
          }, input.readinessPollMs ?? 25);
          signal.addEventListener("abort", onAbort, { once: true });
        });
      }
      throw new SidecarSupervisorError(
        "sidecar_readiness_timeout",
        "sidecar_unavailable"
      );
    },
    inspectProcess,
    async requestReadiness({
      baseUrl,
      apiToken
    }): Promise<AuthenticatedDesktopReadiness> {
      const response = await requestBoundedJson(
        "GET",
        baseUrl,
        "/api/desktop/readiness",
        apiToken,
        5_000
      );
      const parsed = authenticatedReadinessSchema.safeParse(response.payload);
      if (response.statusCode !== 200 || !parsed.success) {
        throw new SidecarSupervisorError(
          "authenticated_readiness_invalid",
          "sidecar_unverified"
        );
      }
      return parsed.data;
    },
    async requestShutdown({ baseUrl, apiToken }): Promise<void> {
      const response = await requestBoundedJson(
        "POST",
        baseUrl,
        "/api/desktop/shutdown",
        apiToken,
        5_000
      );
      const parsed = z
        .object({
          schema: z.literal("kestrel.desktop.shutdown.v1"),
          accepted: z.literal(true)
        })
        .strict()
        .safeParse(response.payload);
      if (response.statusCode !== 202 || !parsed.success) {
        throw new Error("authenticated_shutdown_rejected");
      }
    },
    waitForExit: waitForRetainedChildExit,
    log: input.log ?? (() => undefined)
  };
}
