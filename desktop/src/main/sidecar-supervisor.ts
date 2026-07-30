import { createHash, timingSafeEqual } from "node:crypto";
import { createReadStream } from "node:fs";
import {
  lstat,
  readFile,
  readlink,
  realpath
} from "node:fs/promises";
import { request as httpRequest } from "node:http";
import { posix } from "node:path";
import { StringDecoder } from "node:string_decoder";
import type { Readable } from "node:stream";
import { z } from "zod";
import type {
  DesktopApiSessionActivation,
  DesktopApiSessionAuthority
} from "./api-session.js";
import { deriveDesktopCredentialCapability } from "./credential-api.js";
import {
  createNodePrivateFileAdapter,
  createPrivateLaunchFiles,
  readPrivateJsonArtifact,
  readSidecarFailure,
  readSidecarReadiness,
  resolvePrivateProfile,
  type CapturedPrivateFileIdentity,
  type CapturedSidecarFailure,
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
  type VerifiedCredentialAssets,
  type VerifiedRendererAssets,
  type VerifiedResourceFile,
  type VerifiedResourceSet,
  type VerifyResourceManifestInput
} from "./resource-manifest.js";

export interface VerifiedDesktopSessionResources {
  readonly rendererAssets: VerifiedRendererAssets;
  readonly credentialAssets: VerifiedCredentialAssets;
  readonly credentialPreloadPath: string;
}

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
const DBUS_SESSION_ADDRESS_CHARACTERS = 2_048;
const XDG_RUNTIME_DIRECTORY_CHARACTERS = 1_024;
const RECOVERY_RETRY_LIMIT = 2;
const RECOVERY_RETRY_WINDOW_MS = 60_000;
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
const recoveryReasonCodeSchema = z.enum([
  "payload_verification_failed",
  "profile_conflict",
  "state_incompatible",
  "state_corrupt",
  "memvid_reopen_failed",
  "sidecar_crash_loop",
  "pending_high_risk_approval",
  "ambiguous_provider_attempt",
  "credential_backend_unavailable",
  "recovery_inspection_unavailable"
]);
type RecoveryReasonCode = z.infer<typeof recoveryReasonCodeSchema>;
const blockingRecoveryReasonCodes: ReadonlySet<RecoveryReasonCode> =
  new Set([
    "payload_verification_failed",
    "profile_conflict",
    "state_incompatible",
    "state_corrupt",
    "memvid_reopen_failed",
    "sidecar_crash_loop",
    "pending_high_risk_approval",
    "ambiguous_provider_attempt",
    "recovery_inspection_unavailable"
  ]);
const authenticatedRecoveryReportSchema = z
  .object({
    schema: z.literal("kestrel.desktop.recovery.v1"),
    can_auto_resume: z.boolean(),
    reasons: z.array(recoveryReasonCodeSchema).max(16),
    blockers: z.array(recoveryReasonCodeSchema).max(16),
    actions: z.tuple([
      z.literal("inspect"),
      z.literal("export_support_bundle"),
      z.literal("retry_readiness")
    ]),
    state: z
      .object({
        integrity: z.enum(["ok", "error"]),
        schema_version: z.number().int().nonnegative().nullable(),
        writable: z.boolean()
      })
      .strict(),
    memory: z.object({ ready: z.boolean() }).strict(),
    approvals: z
      .object({
        pending_high_risk: z.number().int().min(0).max(1_000)
      })
      .strict(),
    routing: z
      .object({
        ambiguous_provider_attempts: z
          .number()
          .int()
          .min(0)
          .max(1_000)
      })
      .strict(),
    credential_storage: z
      .object({
        state: z.enum([
          "available",
          "session_only",
          "locked_vault_required",
          "unavailable"
        ])
      })
      .strict()
  })
  .strict()
  .superRefine((report, context) => {
    if (report.can_auto_resume !== (report.blockers.length === 0)) {
      context.addIssue({
        code: "custom",
        message: "recovery_resume_blocker_mismatch"
      });
    }
    const reasons = new Set(report.reasons);
    if (report.blockers.some((blocker) => !reasons.has(blocker))) {
      context.addIssue({
        code: "custom",
        message: "recovery_blocker_missing_reason"
      });
    }
    if (
      report.reasons.some(
        (reason) =>
          blockingRecoveryReasonCodes.has(reason) &&
          !report.blockers.includes(reason)
      )
    ) {
      context.addIssue({
        code: "custom",
        message: "recovery_reason_missing_blocker"
      });
    }
    if (
      reasons.size !== report.reasons.length ||
      new Set(report.blockers).size !== report.blockers.length
    ) {
      context.addIssue({
        code: "custom",
        message: "recovery_reason_duplicate"
      });
    }
    const requiredReasons = [
      ...(report.state.integrity === "error"
        ? (["state_corrupt"] as const)
        : []),
      ...(!report.memory.ready
        ? (["memvid_reopen_failed"] as const)
        : []),
      ...(report.approvals.pending_high_risk > 0
        ? (["pending_high_risk_approval"] as const)
        : []),
      ...(report.routing.ambiguous_provider_attempts > 0
        ? (["ambiguous_provider_attempt"] as const)
        : []),
      ...(["locked_vault_required", "unavailable"].includes(
        report.credential_storage.state
      )
        ? (["credential_backend_unavailable"] as const)
        : [])
    ];
    if (requiredReasons.some((reason) => !reasons.has(reason))) {
      context.addIssue({
        code: "custom",
        message: "recovery_required_reason_missing"
      });
    }
  });
const authenticatedRecoveryRetryResultSchema = z
  .object({
    schema: z.literal(
      "kestrel.desktop.recovery-retry-result.v1"
    ),
    accepted: z.boolean(),
    report: authenticatedRecoveryReportSchema
  })
  .strict()
  .superRefine((result, context) => {
    if (result.accepted !== result.report.can_auto_resume) {
      context.addIssue({
        code: "custom",
        message: "recovery_retry_acceptance_mismatch"
      });
    }
  });
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

export type AuthenticatedDesktopRecoveryReport = z.infer<
  typeof authenticatedRecoveryReportSchema
>;

export type AuthenticatedDesktopRecoveryRetryResult = z.infer<
  typeof authenticatedRecoveryRetryResultSchema
>;

export type SidecarRecoveryRetryResult =
  | Readonly<{ accepted: true }>
  | Readonly<{
      accepted: false;
      reason:
        | "not_in_recovery"
        | "recovery_blocked"
        | "retry_rate_limited"
        | "retry_failed";
    }>;

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
    | "developer_reverified_path"
    | "test_verified_handle";
  spawn(request: VerifiedExecutableSpawnRequest): RetainedSidecarChild;
  close(): Promise<void>;
}

export interface SidecarSupervisorDependencies {
  apiSession: Pick<
    DesktopApiSessionAuthority,
    "activate" | "deactivate"
  >;
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
  readStartupFailure?(input: {
    path: string;
    apiToken: string;
    launchNonceDigest: string;
    profileId: string;
    resourceManifestDigest: string;
    sidecarVersion: string;
  }): Promise<CapturedSidecarFailure | null>;
  inspectProcess(pid: number): Promise<SidecarProcessIdentity | null>;
  inspectRetainedChild?(
    child: RetainedSidecarChild,
    expectedExecutableDigest: string
  ): Promise<SidecarProcessIdentity | null>;
  requestReadiness(input: {
    baseUrl: string;
    apiToken: string;
  }): Promise<AuthenticatedDesktopReadiness>;
  requestRecovery(input: {
    baseUrl: string;
    apiToken: string;
  }): Promise<AuthenticatedDesktopRecoveryReport>;
  requestRecoveryRetry(input: {
    baseUrl: string;
    apiToken: string;
  }): Promise<AuthenticatedDesktopRecoveryRetryResult>;
  requestShutdown(input: {
    baseUrl: string;
    apiToken: string;
  }): Promise<void>;
  waitForExit(
    child: RetainedSidecarChild,
    timeoutMs: number
  ): Promise<boolean>;
  now(): number;
  log(line: string): void;
}

export interface SidecarSupervisorConfig {
  sidecarRelativePath: string;
  sidecarVersion: string;
  readinessTimeoutMs: number;
  shutdownTimeoutMs: number;
  platform: NodeJS.Platform;
  environment: Readonly<Record<string, string | undefined>>;
}

export type SidecarRecoveryReason =
  | "sidecar_unavailable"
  | "sidecar_unverified"
  | "payload_verification_failed"
  | "profile_conflict"
  | "version_incompatible"
  | "state_incompatible"
  | "state_corrupt"
  | "memvid_reopen_failed"
  | "sidecar_crash_loop"
  | "credential_backend_unavailable"
  | "reconciliation_required";

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
      reason: SidecarRecoveryReason;
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
  failureIdentity: CapturedPrivateFileIdentity | null;
  startupSettled: boolean;
  verified: boolean;
  finalization: Promise<void> | null;
}

export class SidecarSupervisorError extends Error {
  constructor(
    readonly code: string,
    readonly recoveryReason: SidecarRecoveryReason
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

function hasControlCharacters(value: string): boolean {
  return /[\u0000-\u001f\u007f-\u009f]/.test(value);
}

function validDbusAddressValue(value: string): boolean {
  for (let index = 0; index < value.length; index += 1) {
    const character = value[index]!;
    if (/[A-Za-z0-9_./*-]/.test(character)) {
      continue;
    }
    if (
      character !== "%" ||
      index + 2 >= value.length ||
      !/^[0-9a-fA-F]{2}$/.test(value.slice(index + 1, index + 3))
    ) {
      return false;
    }
    const decoded = Number.parseInt(
      value.slice(index + 1, index + 3),
      16
    );
    if (decoded <= 0x1f || decoded === 0x7f) {
      return false;
    }
    index += 2;
  }
  return true;
}

function validUnixDbusEntry(value: string): boolean {
  if (!value.startsWith("unix:")) {
    return false;
  }
  const parameters = value.slice("unix:".length).split(",");
  if (parameters.length === 0) {
    return false;
  }
  const parsed = new Map<string, string>();
  for (const parameter of parameters) {
    const separator = parameter.indexOf("=");
    if (
      separator <= 0 ||
      separator === parameter.length - 1
    ) {
      return false;
    }
    const name = parameter.slice(0, separator);
    const entryValue = parameter.slice(separator + 1);
    if (
      !["path", "abstract", "guid"].includes(name) ||
      parsed.has(name)
    ) {
      return false;
    }
    parsed.set(name, entryValue);
  }
  const path = parsed.get("path");
  const abstract = parsed.get("abstract");
  if (
    (path === undefined) === (abstract === undefined) ||
    (path !== undefined &&
      (!path.startsWith("/") || !validDbusAddressValue(path))) ||
    (abstract !== undefined &&
      !validDbusAddressValue(abstract))
  ) {
    return false;
  }
  const guid = parsed.get("guid");
  return guid === undefined || /^[0-9a-fA-F]{32}$/.test(guid);
}

function validDbusSessionAddress(value: string): boolean {
  return (
    value.length > 0 &&
    value.length <= DBUS_SESSION_ADDRESS_CHARACTERS &&
    !hasControlCharacters(value) &&
    value.split(";").every(validUnixDbusEntry)
  );
}

function validXdgRuntimeDirectory(value: string): boolean {
  return (
    value.length > 1 &&
    value.length <= XDG_RUNTIME_DIRECTORY_CHARACTERS &&
    !hasControlCharacters(value) &&
    posix.isAbsolute(value) &&
    posix.normalize(value) === value
  );
}

function minimalEnvironment(
  source: Readonly<Record<string, string | undefined>>,
  platform: NodeJS.Platform
): Record<string, string> {
  const environment: Record<string, string> = {};
  for (const name of safeEnvironmentNames) {
    const value = source[name];
    if (value !== undefined && value.length > 0 && !value.includes("\0")) {
      environment[name] = value;
    }
  }
  if (platform === "linux") {
    const dbusAddress = source.DBUS_SESSION_BUS_ADDRESS;
    if (
      dbusAddress !== undefined &&
      validDbusSessionAddress(dbusAddress)
    ) {
      environment.DBUS_SESSION_BUS_ADDRESS = dbusAddress;
    }
    const runtimeDirectory = source.XDG_RUNTIME_DIR;
    if (
      runtimeDirectory !== undefined &&
      validXdgRuntimeDirectory(runtimeDirectory)
    ) {
      environment.XDG_RUNTIME_DIR = runtimeDirectory;
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

const payloadVerificationErrorCodes = new Set([
  "credential_asset_too_large",
  "credential_snapshot_too_large",
  "renderer_asset_too_large",
  "renderer_snapshot_too_large",
  "resource_digest_mismatch",
  "resource_manifest_invalid",
  "resource_manifest_not_canonical",
  "resource_manifest_too_large",
  "resource_missing",
  "resource_path_untrusted",
  "resource_build_mode_untrusted",
  "resource_identity_mismatch",
  "resource_payload_coverage_mismatch",
  "resource_sbom_mismatch",
  "resource_signature_invalid",
  "resource_signature_too_large",
  "resource_signing_key_untrusted"
]);

function isPayloadVerificationError(error: unknown): boolean {
  return payloadVerificationErrorCodes.has(fixedErrorCode(error));
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
  private currentState: SidecarSupervisorState = Object.freeze({
    kind: "recovery",
    reason: "sidecar_unavailable",
    detail: "not_started"
  });
  private readonly stateListeners = new Set<
    (state: SidecarSupervisorState) => void
  >();
  private active: ActiveSidecar | null = null;
  private starting = false;
  private stopping = false;
  private generation = 0;
  private startPromise: Promise<VerifiedDesktopSessionResources> | null =
    null;
  private stopPromise: Promise<void> | null = null;
  private launchAbort: AbortController | null = null;
  private recoveryRetryPromise:
    | Promise<SidecarRecoveryRetryResult>
    | null = null;
  private recoveryRetryTimes: number[] = [];

  constructor(
    private readonly config: SidecarSupervisorConfig,
    private readonly dependencies: SidecarSupervisorDependencies
  ) {}

  get state(): SidecarSupervisorState {
    return this.currentState;
  }

  subscribe(
    listener: (state: SidecarSupervisorState) => void
  ): () => void {
    this.stateListeners.add(listener);
    let subscribed = true;
    return (): void => {
      if (!subscribed) {
        return;
      }
      subscribed = false;
      this.stateListeners.delete(listener);
    };
  }

  enterReconciliationRequired(): void {
    if (
      this.currentState.kind === "recovery" &&
      this.currentState.reason === "reconciliation_required"
    ) {
      return;
    }
    if (
      this.currentState.kind !== "ready" ||
      this.active === null ||
      !this.active.verified ||
      this.stopping
    ) {
      return;
    }
    this.dependencies.apiSession.deactivate(this.generation);
    this.transition({
      kind: "recovery",
      reason: "reconciliation_required",
      detail: "credential_mutation_reconciliation_required"
    });
  }

  retryReadiness(): Promise<SidecarRecoveryRetryResult> {
    if (this.recoveryRetryPromise !== null) {
      return this.recoveryRetryPromise;
    }
    if (this.currentState.kind !== "recovery") {
      return Promise.resolve({
        accepted: false,
        reason: "not_in_recovery"
      });
    }
    const now = this.dependencies.now();
    const threshold = now - RECOVERY_RETRY_WINDOW_MS;
    this.recoveryRetryTimes = this.recoveryRetryTimes.filter(
      (attemptedAt) => attemptedAt > threshold
    );
    if (this.recoveryRetryTimes.length >= RECOVERY_RETRY_LIMIT) {
      this.transition({
        kind: "recovery",
        reason: "sidecar_crash_loop",
        detail: "recovery_retry_rate_limited"
      });
      return Promise.resolve({
        accepted: false,
        reason: "retry_rate_limited"
      });
    }
    this.recoveryRetryTimes.push(now);
    const operation = this.performRecoveryRetry();
    this.recoveryRetryPromise = operation;
    void operation.then(
      () => {
        if (this.recoveryRetryPromise === operation) {
          this.recoveryRetryPromise = null;
        }
      },
      () => {
        if (this.recoveryRetryPromise === operation) {
          this.recoveryRetryPromise = null;
        }
      }
    );
    return operation;
  }

  private async performRecoveryRetry(): Promise<SidecarRecoveryRetryResult> {
    const state = this.currentState;
    if (state.kind !== "recovery") {
      return {
        accepted: false,
        reason: "not_in_recovery"
      };
    }
    const active = this.active;
    if (
      active !== null &&
      active.verified &&
      active.baseUrl !== "" &&
      !this.childHasExited(active.child) &&
      state.detail !==
        "credential_mutation_reconciliation_required"
    ) {
      const generation = this.generation;
      try {
        const retry =
          await this.dependencies.requestRecoveryRetry({
            baseUrl: active.baseUrl,
            apiToken: active.launch.apiToken
          });
        if (
          this.active !== active ||
          this.generation !== generation ||
          this.currentState.kind !== "recovery"
        ) {
          return {
            accepted: false,
            reason: "retry_failed"
          };
        }
        if (!retry.accepted || !retry.report.can_auto_resume) {
          this.transitionBlockedRecovery(retry.report.blockers);
          return {
            accepted: false,
            reason: "recovery_blocked"
          };
        }
        const readiness =
          await this.dependencies.requestReadiness({
            baseUrl: active.baseUrl,
            apiToken: active.launch.apiToken
          });
        if (
          this.active !== active ||
          this.generation !== generation ||
          this.currentState.kind !== "recovery"
        ) {
          return {
            accepted: false,
            reason: "retry_failed"
          };
        }
        validateAuthenticatedReadiness(
          active,
          readiness,
          this.config.sidecarVersion
        );
        this.dependencies.apiSession.activate({
          baseUrl: active.baseUrl,
          apiToken: active.launch.apiToken,
          credentialCapability:
            deriveDesktopCredentialCapability(
              active.launch.apiToken,
              active.launch.launchNonce
            ),
          generation
        });
        this.transition({
          kind: "ready",
          profileId: active.profile.profileId,
          baseUrl: active.baseUrl,
          sidecarVersion: this.config.sidecarVersion
        });
        return { accepted: true };
      } catch {
        return {
          accepted: false,
          reason: "retry_failed"
        };
      }
    }
    try {
      if (this.active !== null || this.startPromise !== null) {
        await this.stop();
      }
      await this.start();
      if (this.currentState.kind !== "ready") {
        return {
          accepted: false,
          reason: "recovery_blocked"
        };
      }
      return { accepted: true };
    } catch {
      return {
        accepted: false,
        reason: "retry_failed"
      };
    }
  }

  private transitionBlockedRecovery(
    blockers: readonly string[]
  ): void {
    const blocker = blockers[0] ?? "recovery_inspection_unavailable";
    const reason: SidecarRecoveryReason =
      blocker === "state_incompatible"
        ? "state_incompatible"
        : blocker === "state_corrupt"
          ? "state_corrupt"
          : blocker === "memvid_reopen_failed"
            ? "memvid_reopen_failed"
            : blocker === "profile_conflict"
              ? "profile_conflict"
              : blocker === "sidecar_crash_loop"
                ? "sidecar_crash_loop"
                : "reconciliation_required";
    this.transition({
      kind: "recovery",
      reason,
      detail: `recovery_blocked_${blocker}`.slice(0, 160)
    });
  }

  private transition(state: SidecarSupervisorState): void {
    const snapshot = Object.freeze({ ...state }) as SidecarSupervisorState;
    this.currentState = snapshot;
    for (const listener of [...this.stateListeners]) {
      try {
        listener(snapshot);
      } catch {
        // A projection observer cannot damage lifecycle authority.
      }
    }
  }

  async start(): Promise<VerifiedDesktopSessionResources> {
    if (this.startPromise !== null || this.active !== null) {
      throw new SidecarSupervisorError(
        "sidecar_already_started",
        "sidecar_unavailable"
      );
    }
    this.dependencies.apiSession.deactivate();
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
  ): Promise<VerifiedDesktopSessionResources> {
    this.starting = true;
    this.transition({ kind: "verifying" });
    let launch: PrivateLaunchFiles | null = null;
    let child: RetainedSidecarChild | null = null;
    let executable: VerifiedExecutableLaunchCapability | null = null;
    let expectedExecutableDigest: string | null = null;
    let retainedActive: ActiveSidecar | null = null;
    try {
      const resources = await this.dependencies.verifyResources();
      this.throwIfLaunchCancelled(generation, signal);
      const sidecar = resources.files.get(this.config.sidecarRelativePath);
      if (sidecar === undefined) {
        throw new SidecarSupervisorError(
          "verified_sidecar_missing",
          "payload_verification_failed"
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
        assuranceMode: resources.manifest.build_mode,
        parentPid: parent.pid,
        parentBirthMarker: parent.processBirthMarker,
        resourceManifestDigest: resources.manifestDigest
      });
      const credentialCapability =
        deriveDesktopCredentialCapability(
          launch.apiToken,
          launch.launchNonce
        );
      this.throwIfLaunchCancelled(generation, signal);
      expectedExecutableDigest = sidecar.sha256;
      this.transition({ kind: "starting" });
      child = executable.spawn({
        args: [launch.bootstrapPath],
        options: {
          shell: false,
          detached: false,
          stdio: ["ignore", "pipe", "pipe"],
          env: minimalEnvironment(
            this.config.environment,
            this.config.platform
          )
        }
      });
      let terminal: SidecarSupervisorError | null = null;
      let rejectTerminal!: (error: SidecarSupervisorError) => void;
      const terminalPromise = new Promise<never>((_resolve, reject) => {
        rejectTerminal = reject;
      });
      const reportTerminal = (error: SidecarSupervisorError): void => {
        if (terminal === null) {
          terminal = error;
          rejectTerminal(error);
        }
        const current = this.active;
        if (
          current !== null &&
          current.child === child &&
          (current.verified || current.startupSettled) &&
          this.childHasExited(current.child)
        ) {
          this.armConfirmedExit(current, generation);
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
        [
          launch.apiToken,
          launch.launchNonce,
          credentialCapability
        ],
        this.dependencies.log
      );
      installBoundedLogReader(
        child.stderr,
        "stderr",
        [
          launch.apiToken,
          launch.launchNonce,
          credentialCapability
        ],
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
        failureIdentity: null,
        startupSettled: false,
        verified: false,
        finalization: null
      };
      retainedActive = active;
      this.active = active;
      const spawnedIdentity = await Promise.race([
        this.inspectRetainedChildOrProcess(
          child,
          expectedExecutableDigest
        ),
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
      const processIdentity = await this.inspectRetainedChildOrProcess(
        child,
        expectedExecutableDigest
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
      const credentialPreload = resources.files.get(
        "desktop/dist/credential/preload.js"
      );
      if (credentialPreload === undefined) {
        throw new SidecarSupervisorError(
          "verified_credential_preload_missing",
          "sidecar_unverified"
        );
      }
      active.verified = true;
      const desktopResources = Object.freeze({
        rendererAssets: resources.rendererAssets,
        credentialAssets: resources.credentialAssets,
        credentialPreloadPath: credentialPreload.path
      });
      const recovery =
        await this.dependencies.requestRecovery({
          baseUrl: active.baseUrl,
          apiToken: launch.apiToken
        });
      this.throwIfLaunchCancelled(generation, signal);
      if (terminal !== null) {
        throw terminal;
      }
      if (!recovery.can_auto_resume) {
        this.transitionBlockedRecovery(recovery.blockers);
        return desktopResources;
      }
      const apiSessionActivation: DesktopApiSessionActivation = {
        baseUrl: active.baseUrl,
        apiToken: launch.apiToken,
        credentialCapability,
        generation
      };
      this.dependencies.apiSession.activate(apiSessionActivation);
      this.transition({
        kind: "ready",
        profileId: profile.profileId,
        baseUrl: active.baseUrl,
        sidecarVersion: this.config.sidecarVersion
      });
      return desktopResources;
    } catch (error) {
      this.dependencies.apiSession.deactivate(generation);
      let surfacedError: unknown =
        this.stopping || signal.aborted || generation !== this.generation
          ? new SidecarSupervisorError(
              "sidecar_start_cancelled",
              "sidecar_unavailable"
            )
          : error;
      const active =
        retainedActive?.child === child ? retainedActive : null;
      if (
        active !== null &&
        active.readiness === null &&
        launch !== null &&
        !this.stopping &&
        !signal.aborted &&
        generation === this.generation &&
        this.dependencies.readStartupFailure !== undefined
      ) {
        try {
          const captured =
            await this.dependencies.readStartupFailure({
              path: launch.failurePath,
              apiToken: launch.apiToken,
              launchNonceDigest: launch.launchNonceDigest,
              profileId: active.profile.profileId,
              resourceManifestDigest:
                active.resources.manifestDigest,
              sidecarVersion: this.config.sidecarVersion
            });
          if (captured !== null) {
            active.failureIdentity = captured.identity;
            surfacedError = new SidecarSupervisorError(
              captured.failure.reason,
              captured.failure.reason
            );
          }
        } catch (failureError) {
          surfacedError =
            failureError instanceof SidecarSupervisorError
              ? failureError
              : new SidecarSupervisorError(
                  "sidecar_failure_invalid",
                  "sidecar_unverified"
                );
        }
      }
      if (active !== null) {
        active.startupSettled = true;
      }
      if (active !== null) {
        try {
          await this.terminateActive(active, false);
        } catch (terminationError) {
          this.dependencies.log(
            `[sidecar:supervisor] ${fixedErrorCode(terminationError)}`
          );
          surfacedError = terminationError;
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
          : isPayloadVerificationError(surfacedError)
            ? "payload_verification_failed"
            : "sidecar_unavailable";
      if (!this.stopping) {
        this.transition({
          kind: "recovery",
          reason: recoveryReason,
          detail: fixedErrorCode(surfacedError)
        });
      }
      throw surfacedError;
    } finally {
      this.starting = false;
    }
  }

  private armConfirmedExit(
    active: ActiveSidecar,
    generation: number
  ): void {
    if (active.finalization !== null) {
      return;
    }
    this.dependencies.apiSession.deactivate(generation);
    const wasVerified = active.verified;
    if (
      wasVerified &&
      generation === this.generation &&
      !this.stopping
    ) {
      this.transition({
        kind: "recovery",
        reason: "reconciliation_required",
        detail: "unexpected_exit_reconciliation_required"
      });
    }
    const finalization = this.finalizeExitedActive(active);
    void this.completeConfirmedExit(active, finalization);
  }

  private async completeConfirmedExit(
    active: ActiveSidecar,
    finalization: Promise<void>
  ): Promise<void> {
    try {
      await finalization;
    } catch (error) {
      this.dependencies.log(
        `[sidecar:supervisor] ${fixedErrorCode(error)}`
      );
      return;
    }
    if (
      this.active !== active
    ) {
      return;
    }
    this.releaseFinalizedActive(active);
  }

  async stop(): Promise<void> {
    if (this.stopPromise !== null) {
      return this.stopPromise;
    }
    this.dependencies.apiSession.deactivate(this.generation);
    this.stopping = true;
    this.generation += 1;
    this.launchAbort?.abort();
    this.transition({ kind: "stopping" });
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
    start: Promise<VerifiedDesktopSessionResources> | null
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
    this.transition({
      kind: "recovery",
      reason: "sidecar_unavailable",
      detail: code
    });
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
      current = await this.inspectRetainedChildOrProcess(
        active.child,
        expected.executableDigest
      );
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

  private inspectRetainedChildOrProcess(
    child: RetainedSidecarChild,
    expectedExecutableDigest: string
  ): Promise<SidecarProcessIdentity | null> {
    const pid = child.pid;
    if (pid === undefined || pid <= 0) {
      return Promise.resolve(null);
    }
    return this.dependencies.inspectRetainedChild !== undefined
      ? this.dependencies.inspectRetainedChild(
          child,
          expectedExecutableDigest
        )
      : this.dependencies.inspectProcess(pid);
  }

  private finalizeExitedActive(active: ActiveSidecar): Promise<void> {
    if (active.finalization === null) {
      active.finalization = this.performFinalization(active);
    }
    return active.finalization;
  }

  private async performFinalization(active: ActiveSidecar): Promise<void> {
    let cleanupFailed = false;
    let closeFailed = false;
    try {
      await active.launch.cleanup(
        active.readinessIdentity ?? undefined,
        active.failureIdentity ?? undefined
      );
    } catch {
      cleanupFailed = true;
    }
    try {
      await this.closeActiveExecutable(active);
    } catch {
      closeFailed = true;
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

  private releaseFinalizedActive(active: ActiveSidecar): void {
    if (this.active === active) {
      this.active = null;
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
      this.releaseFinalizedActive(active);
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
        this.releaseFinalizedActive(active);
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
      this.releaseFinalizedActive(active);
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
      this.releaseFinalizedActive(active);
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
  timeoutMs: number,
  body = ""
): Promise<{ statusCode: number; payload: unknown }> {
  const root = loopbackBaseUrl(baseUrl);
  const url = new URL(path, root);
  return new Promise((resolvePromise, rejectPromise) => {
    let absoluteTimeout: NodeJS.Timeout | undefined;
    let settled = false;
    const clearDeadline = (): void => {
      if (absoluteTimeout !== undefined) {
        clearTimeout(absoluteTimeout);
      }
    };
    const rejectOnce = (error: Error): void => {
      if (settled) {
        return;
      }
      settled = true;
      clearDeadline();
      rejectPromise(error);
    };
    const resolveOnce = (value: {
      statusCode: number;
      payload: unknown;
    }): void => {
      if (settled) {
        return;
      }
      settled = true;
      clearDeadline();
      resolvePromise(value);
    };
    const request = httpRequest(
      url,
      {
        method,
        headers: {
          Authorization: `Bearer ${apiToken}`,
          "Content-Length": String(
            Buffer.byteLength(body, "utf8")
          ),
          ...(body === ""
            ? {}
            : { "Content-Type": "application/json" })
        },
        timeout: timeoutMs
      },
      (response) => {
        const chunks: Buffer[] = [];
        let bytes = 0;
        response.on("data", (chunk: Buffer) => {
          if (settled) {
            return;
          }
          bytes += chunk.byteLength;
          if (bytes > HTTP_RESPONSE_BYTES) {
            rejectOnce(
              new Error("desktop_http_response_too_large")
            );
            response.destroy();
            request.destroy();
            return;
          }
          chunks.push(chunk);
        });
        response.on("end", () => {
          if (settled) {
            return;
          }
          try {
            resolveOnce({
              statusCode: response.statusCode ?? 0,
              payload: JSON.parse(Buffer.concat(chunks).toString("utf8"))
            });
          } catch (error) {
            rejectOnce(
              error instanceof Error
                ? error
                : new Error("desktop_http_json_invalid")
            );
          }
        });
        response.on("aborted", () => {
          rejectOnce(new Error("desktop_http_response_aborted"));
        });
        response.on("error", rejectOnce);
      }
    );
    absoluteTimeout = setTimeout(() => {
      rejectOnce(new Error("desktop_http_timeout"));
      request.destroy();
    }, timeoutMs);
    request.on("timeout", () => {
      rejectOnce(new Error("desktop_http_timeout"));
      request.destroy();
    });
    request.on("close", clearDeadline);
    request.on("error", rejectOnce);
    request.end(body);
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
  apiSession: Pick<
    DesktopApiSessionAuthority,
    "activate" | "deactivate"
  >;
  resourceVerification: VerifyResourceManifestInput;
  resourceVerifier?: (
    input: VerifyResourceManifestInput
  ) => Promise<VerifiedResourceSet>;
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
    apiSession: input.apiSession,
    verifyResources: () =>
      (input.resourceVerifier ?? verifyResourceManifest)(
        input.resourceVerification
      ),
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
    async readStartupFailure({
      path,
      apiToken,
      launchNonceDigest,
      profileId,
      resourceManifestDigest,
      sidecarVersion
    }): Promise<CapturedSidecarFailure | null> {
      try {
        return await readSidecarFailure(
          path,
          {
            apiToken,
            launchNonceDigest,
            profileId,
            resourceManifestDigest,
            sidecarVersion
          },
          adapter
        );
      } catch (error) {
        if (
          error instanceof Error &&
          error.message === "private_artifact_missing"
        ) {
          return null;
        }
        throw new SidecarSupervisorError(
          "sidecar_failure_invalid",
          "sidecar_unverified"
        );
      }
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
    async requestRecovery({
      baseUrl,
      apiToken
    }): Promise<AuthenticatedDesktopRecoveryReport> {
      const response = await requestBoundedJson(
        "GET",
        baseUrl,
        "/api/desktop/recovery",
        apiToken,
        5_000
      );
      const parsed =
        authenticatedRecoveryReportSchema.safeParse(
          response.payload
        );
      if (response.statusCode !== 200 || !parsed.success) {
        throw new SidecarSupervisorError(
          "authenticated_recovery_invalid",
          "sidecar_unverified"
        );
      }
      return parsed.data;
    },
    async requestRecoveryRetry({
      baseUrl,
      apiToken
    }): Promise<AuthenticatedDesktopRecoveryRetryResult> {
      const body = JSON.stringify({
        schema: "kestrel.desktop.recovery-retry.v1",
        action: "retry_readiness"
      });
      const response = await requestBoundedJson(
        "POST",
        baseUrl,
        "/api/desktop/recovery/retry",
        apiToken,
        5_000,
        body
      );
      const parsed =
        authenticatedRecoveryRetryResultSchema.safeParse(
          response.payload
        );
      if (response.statusCode !== 200 || !parsed.success) {
        throw new SidecarSupervisorError(
          "authenticated_recovery_retry_invalid",
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
    now: () => Date.now(),
    log: input.log ?? (() => undefined)
  };
}
