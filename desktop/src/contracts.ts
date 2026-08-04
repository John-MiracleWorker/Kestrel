import { z } from "zod";

const loopbackHosts = new Set(["127.0.0.1", "[::1]"]);

export const DESKTOP_REQUEST_BYTES = 4 * 1024;
export const DESKTOP_RESPONSE_BYTES = 32 * 1024;
export const DESKTOP_EVENT_BYTES = 16 * 1024;
export const DESKTOP_PATH_CHARACTERS = 4_096;
export const DESKTOP_URL_CHARACTERS = 2_048;
export const DESKTOP_CREDENTIAL_CONTEXT_BYTES = 2 * 1024;
export const DESKTOP_CREDENTIAL_REQUEST_BYTES = 20 * 1024;
export const DESKTOP_CREDENTIAL_VALUE_BYTES = 16 * 1024;

export const DESKTOP_APP_SCHEME = "kestrel";
export const DESKTOP_APP_HOST = "app";
export const DESKTOP_APP_ORIGIN =
  `${DESKTOP_APP_SCHEME}://${DESKTOP_APP_HOST}` as const;
export const DESKTOP_APP_ENTRY_URL =
  `${DESKTOP_APP_ORIGIN}/index.html` as const;
export const DESKTOP_CREDENTIAL_HOST = "credential";
export const DESKTOP_CREDENTIAL_ORIGIN =
  `${DESKTOP_APP_SCHEME}://${DESKTOP_CREDENTIAL_HOST}` as const;
export const DESKTOP_CREDENTIAL_ENTRY_URL =
  `${DESKTOP_CREDENTIAL_ORIGIN}/index.html` as const;

export const DESKTOP_IPC_CHANNELS = Object.freeze({
  runtimeBootstrap: "kestrel:desktop:runtime-bootstrap",
  connection: "kestrel:desktop:connection",
  chooseProjectFolder: "kestrel:desktop:choose-project-folder",
  chooseStorageFolder: "kestrel:desktop:choose-storage-folder",
  exportSupportBundle: "kestrel:desktop:export-support-bundle",
  appVersion: "kestrel:desktop:app-version",
  updateStatus: "kestrel:desktop:update-status",
  credentialDialog: "kestrel:desktop:credential-dialog",
  openExternalUrl: "kestrel:desktop:open-external-url",
  recoveryAction: "kestrel:desktop:recovery-action",
  lifecycleEvent: "kestrel:desktop:lifecycle",
  updateStatusEvent: "kestrel:desktop:update-status-changed"
} as const);

export const DESKTOP_CREDENTIAL_IPC_CHANNELS = Object.freeze({
  bootstrap: "kestrel:credential:bootstrap",
  submit: "kestrel:credential:submit",
  cancel: "kestrel:credential:cancel"
} as const);

export const desktopErrorCodeSchema = z.enum([
  "invalid_desktop_request",
  "invalid_desktop_response",
  "desktop_sender_untrusted",
  "desktop_feature_unavailable",
  "desktop_operation_failed",
  "desktop_operation_in_progress",
  "desktop_operation_ambiguous"
]);

export type DesktopErrorCode = z.infer<
  typeof desktopErrorCodeSchema
>;

export const desktopErrorSchema = z
  .object({
    code: desktopErrorCodeSchema
  })
  .strict();

export type DesktopError = Readonly<
  z.infer<typeof desktopErrorSchema>
>;

export function assertDesktopPlainData(value: unknown): void {
  const seen = new WeakSet<object>();
  const visit = (current: unknown): void => {
    if (
      current === null ||
      typeof current === "string" ||
      typeof current === "number" ||
      typeof current === "boolean"
    ) {
      return;
    }
    if (typeof current !== "object") {
      throw new Error("desktop_data_not_plain");
    }
    if (seen.has(current)) {
      throw new Error("desktop_data_not_plain");
    }
    seen.add(current);
    const isArray = Array.isArray(current);
    const prototype = Object.getPrototypeOf(current);
    if (
      (isArray && prototype !== Array.prototype) ||
      (!isArray &&
        prototype !== Object.prototype &&
        prototype !== null)
    ) {
      throw new Error("desktop_data_not_plain");
    }
    const keys = Reflect.ownKeys(current);
    const descriptors = Object.getOwnPropertyDescriptors(current);
    for (const key of keys) {
      if (typeof key !== "string") {
        throw new Error("desktop_data_not_plain");
      }
      if (isArray && key === "length") {
        continue;
      }
      const descriptor = descriptors[key];
      if (
        descriptor === undefined ||
        !("value" in descriptor)
      ) {
        throw new Error("desktop_data_not_plain");
      }
      visit(descriptor.value);
    }
  };
  visit(value);
}

export function desktopEnvelopeSchema<T extends z.ZodType>(
  value: T
): z.ZodType<
  | { ok: true; value: z.output<T> }
  | { ok: false; error: DesktopError }
> {
  return z.discriminatedUnion("ok", [
    z.object({ ok: z.literal(true), value }).strict(),
    z.object({
      ok: z.literal(false),
      error: desktopErrorSchema
    }).strict()
  ]) as z.ZodType<
    | { ok: true; value: z.output<T> }
    | { ok: false; error: DesktopError }
  >;
}

export const desktopLifecycleStateSchema = z.enum([
  "verifying",
  "starting",
  "ready",
  "stopping",
  "recovery"
]);

export const desktopRecoveryReasonSchema = z.enum([
  "sidecar_unavailable",
  "sidecar_unverified",
  "payload_verification_failed",
  "profile_conflict",
  "version_incompatible",
  "state_incompatible",
  "state_corrupt",
  "memvid_reopen_failed",
  "sidecar_crash_loop",
  "credential_backend_unavailable",
  "reconciliation_required"
]);

export const desktopRecoverySchema = z
  .object({
    reason: desktopRecoveryReasonSchema
  })
  .strict();

export function isLoopbackHttpUrl(value: string): boolean {
  try {
    const url = new URL(value);
    return (
      url.protocol === "http:" &&
      loopbackHosts.has(url.hostname) &&
      url.port !== "" &&
      url.username === "" &&
      url.password === "" &&
      url.pathname === "/" &&
      url.search === "" &&
      url.hash === "" &&
      url.href === value
    );
  } catch {
    return false;
  }
}

export function isExactDesktopRuntimeUrl(value: string): boolean {
  try {
    const url = new URL(value);
    return (
      url.protocol === "http:" &&
      url.hostname === "127.0.0.1" &&
      url.port !== "" &&
      url.username === "" &&
      url.password === "" &&
      url.pathname === "/" &&
      url.search === "" &&
      url.hash === "" &&
      url.href === value
    );
  } catch {
    return false;
  }
}

const loopbackBaseUrlSchema = z
  .string()
  .max(256)
  .refine(isExactDesktopRuntimeUrl);
const profileIdSchema = z.string().trim().min(1).max(120);
const versionSchema = z.string().trim().min(1).max(64);
const positiveGenerationSchema = z
  .number()
  .int()
  .positive()
  .safe();

export const desktopRuntimeMarkerSchema = z
  .object({
    schema: z.literal("kestrel.desktop.runtime.v1"),
    baseUrl: loopbackBaseUrlSchema,
    generation: positiveGenerationSchema
  })
  .strict();

export type DesktopRuntimeMarker = Readonly<
  z.infer<typeof desktopRuntimeMarkerSchema>
>;

const inactiveConnectionShape = {
  schema: z.literal("kestrel.desktop.connection.v1"),
  generation: z.null(),
  baseUrl: z.null(),
  profileId: z.null(),
  sidecarVersion: z.null()
} as const;

export const desktopConnectionSchema = z.discriminatedUnion("state", [
  z
    .object({
      schema: z.literal("kestrel.desktop.connection.v1"),
      state: z.literal("ready"),
      generation: positiveGenerationSchema,
      baseUrl: loopbackBaseUrlSchema,
      profileId: profileIdSchema,
      sidecarVersion: versionSchema,
      recovery: z.null()
    })
    .strict(),
  z
    .object({
      ...inactiveConnectionShape,
      state: z.literal("verifying"),
      recovery: z.null()
    })
    .strict(),
  z
    .object({
      ...inactiveConnectionShape,
      state: z.literal("starting"),
      recovery: z.null()
    })
    .strict(),
  z
    .object({
      ...inactiveConnectionShape,
      state: z.literal("stopping"),
      recovery: z.null()
    })
    .strict(),
  z
    .object({
      ...inactiveConnectionShape,
      state: z.literal("recovery"),
      recovery: desktopRecoverySchema
    })
    .strict()
]);

export type DesktopLifecycleState = z.infer<
  typeof desktopLifecycleStateSchema
>;
export type DesktopRecoveryReason = z.infer<
  typeof desktopRecoveryReasonSchema
>;
export type DesktopConnection = Readonly<
  z.infer<typeof desktopConnectionSchema>
>;

export const desktopFolderChoiceSchema = z.discriminatedUnion(
  "status",
  [
    z.object({ status: z.literal("cancelled") }).strict(),
    z
      .object({
        status: z.literal("selected"),
        path: z
          .string()
          .min(1)
          .max(DESKTOP_PATH_CHARACTERS)
          .refine((value) => !value.includes("\0")),
        displayLabel: z.string().trim().min(1).max(256)
      })
      .strict()
  ]
);

export type DesktopFolderChoice = Readonly<
  z.infer<typeof desktopFolderChoiceSchema>
>;

export const desktopSupportBundleResultSchema = z
  .object({
    status: z.literal("exported"),
    displayLabel: z.string().trim().min(1).max(256)
  })
  .strict();

export type DesktopSupportBundleResult = Readonly<
  z.infer<typeof desktopSupportBundleResultSchema>
>;

export const desktopAppVersionSchema = z
  .object({
    version: versionSchema
  })
  .strict();

export type DesktopAppVersion = Readonly<
  z.infer<typeof desktopAppVersionSchema>
>;

export const desktopUpdateStatusSchema = z.discriminatedUnion(
  "state",
  [
    z
      .object({
        schema: z.literal("kestrel.desktop.update.v1"),
        state: z.literal("unavailable"),
        reason: z.literal("not_configured")
      })
      .strict(),
    z
      .object({
        schema: z.literal("kestrel.desktop.update.v1"),
        state: z.literal("idle")
      })
      .strict(),
    z
      .object({
        schema: z.literal("kestrel.desktop.update.v1"),
        state: z.literal("checking")
      })
      .strict(),
    z
      .object({
        schema: z.literal("kestrel.desktop.update.v1"),
        state: z.literal("available"),
        version: versionSchema
      })
      .strict(),
    z
      .object({
        schema: z.literal("kestrel.desktop.update.v1"),
        state: z.literal("downloading"),
        version: versionSchema,
        progressPercent: z.number().min(0).max(100).finite()
      })
      .strict(),
    z
      .object({
        schema: z.literal("kestrel.desktop.update.v1"),
        state: z.literal("downloaded"),
        version: versionSchema
      })
      .strict(),
    z
      .object({
        schema: z.literal("kestrel.desktop.update.v1"),
        state: z.literal("error"),
        reason: z.enum([
          "metadata_unavailable",
          "verification_failed",
          "network_unavailable"
        ])
      })
      .strict()
  ]
);

export type DesktopUpdateStatus = Readonly<
  z.infer<typeof desktopUpdateStatusSchema>
>;

export const desktopCredentialProviderIdSchema = z.enum([
  "openai",
  "openrouter",
  "deepseek",
  "kimi",
  "ollama-cloud",
  "anthropic",
  "grok",
  "gemini"
]);

export type DesktopCredentialProviderId = z.infer<
  typeof desktopCredentialProviderIdSchema
>;

export const desktopCredentialIntentSchema = z
  .object({
    providerId: desktopCredentialProviderIdSchema,
    purpose: z.literal("provider_api_key")
  })
  .strict();

export type DesktopCredentialIntent = Readonly<
  z.infer<typeof desktopCredentialIntentSchema>
>;

export const desktopCredentialResultSchema =
  z.discriminatedUnion("status", [
    z
      .object({
        status: z.literal("stored"),
        secretRef: z
          .string()
          .min(1)
          .max(256)
          .regex(/^secret:\/\/[A-Za-z0-9._/-]+$/),
        validation: z.enum([
          "unverified",
          "valid",
          "invalid"
        ]),
        fingerprint: z.string().min(1).max(256)
      })
      .strict(),
    z.object({ status: z.literal("cancelled") }).strict()
  ]);

export type DesktopCredentialResult = Readonly<
  z.infer<typeof desktopCredentialResultSchema>
>;

export const desktopCredentialStateSchema =
  desktopCredentialResultSchema;
export type DesktopCredentialState = Readonly<
  z.infer<typeof desktopCredentialStateSchema>
>;

export const desktopCredentialContextSchema = z
  .object({
    schema: z.literal("kestrel.credential.context.v1"),
    providerId: desktopCredentialProviderIdSchema,
    providerLabel: z.string().min(1).max(120),
    inputLabel: z.string().min(1).max(160),
    maxUtf8Bytes: z.literal(DESKTOP_CREDENTIAL_VALUE_BYTES)
  })
  .strict();

export type DesktopCredentialContext = Readonly<
  z.infer<typeof desktopCredentialContextSchema>
>;

export const desktopCredentialBootstrapRequestSchema = z
  .object({
    schema: z.literal("kestrel.credential.bootstrap.v1")
  })
  .strict();

export const desktopCredentialSubmitRequestSchema = z
  .object({
    schema: z.literal("kestrel.credential.submit.v1"),
    valueBytes: z.instanceof(Uint8Array)
  })
  .strict();

export const desktopCredentialCancelRequestSchema = z
  .object({
    schema: z.literal("kestrel.credential.cancel.v1")
  })
  .strict();

export const desktopCredentialStoredResultSchema = z
  .object({ status: z.literal("stored") })
  .strict();

export const desktopCredentialCancelledResultSchema = z
  .object({ status: z.literal("cancelled") })
  .strict();

export const desktopExternalUrlPurposeSchema = z.enum([
  "documentation",
  "issues",
  "security",
  "release_notes"
]);

function exactExternalHttpsUrl(value: string): boolean {
  try {
    const authority =
      value.match(/^https:\/\/([^/]+)/i)?.[1] ?? "";
    const parsed = new URL(value);
    return (
      parsed.protocol === "https:" &&
      parsed.username === "" &&
      parsed.password === "" &&
      parsed.port === "" &&
      parsed.hash === "" &&
      !authority.includes("%") &&
      parsed.href === value
    );
  } catch {
    return false;
  }
}

export const desktopExternalUrlRequestSchema = z
  .object({
    purpose: desktopExternalUrlPurposeSchema,
    url: z
      .string()
      .min(1)
      .max(DESKTOP_URL_CHARACTERS)
      .refine(exactExternalHttpsUrl)
  })
  .strict();

export type DesktopExternalUrlRequest = Readonly<
  z.infer<typeof desktopExternalUrlRequestSchema>
>;

export const desktopExternalUrlResultSchema = z
  .object({ opened: z.literal(true) })
  .strict();

export type DesktopExternalUrlResult = Readonly<
  z.infer<typeof desktopExternalUrlResultSchema>
>;

export const desktopRecoveryActionRequestSchema = z
  .object({ action: z.literal("retry_readiness") })
  .strict();

export type DesktopRecoveryActionRequest = Readonly<
  z.infer<typeof desktopRecoveryActionRequestSchema>
>;

export const desktopRecoveryActionResultSchema =
  z.discriminatedUnion("accepted", [
    z.object({ accepted: z.literal(true) }).strict(),
    z
      .object({
        accepted: z.literal(false),
        reason: z.enum([
          "not_in_recovery",
          "recovery_blocked",
          "retry_rate_limited",
          "retry_failed"
        ])
      })
      .strict()
  ]);

export type DesktopRecoveryActionResult = Readonly<
  z.infer<typeof desktopRecoveryActionResultSchema>
>;

export const desktopRuntimeBootstrapRequestSchema = z
  .object({
    schema: z.literal(
      "kestrel.desktop.runtime-bootstrap.request.v1"
    )
  })
  .strict();
export const desktopConnectionRequestSchema = z
  .object({
    schema: z.literal("kestrel.desktop.connection.request.v1")
  })
  .strict();
export const desktopChooseProjectFolderRequestSchema = z
  .object({
    schema: z.literal(
      "kestrel.desktop.choose-project-folder.request.v1"
    )
  })
  .strict();
export const desktopChooseStorageFolderRequestSchema = z
  .object({
    schema: z.literal(
      "kestrel.desktop.choose-storage-folder.request.v1"
    )
  })
  .strict();
export const desktopExportSupportBundleRequestSchema = z
  .object({
    schema: z.literal(
      "kestrel.desktop.export-support-bundle.request.v1"
    )
  })
  .strict();
export const desktopAppVersionRequestSchema = z
  .object({
    schema: z.literal("kestrel.desktop.app-version.request.v1")
  })
  .strict();
export const desktopUpdateStatusRequestSchema = z
  .object({
    schema: z.literal("kestrel.desktop.update-status.request.v1")
  })
  .strict();
export const desktopCredentialDialogRequestSchema = z
  .object({
    schema: z.literal(
      "kestrel.desktop.credential-dialog.request.v1"
    ),
    intent: desktopCredentialIntentSchema
  })
  .strict();
export const desktopOpenExternalUrlRequestSchema = z
  .object({
    schema: z.literal(
      "kestrel.desktop.open-external-url.request.v1"
    ),
    request: desktopExternalUrlRequestSchema
  })
  .strict();
export const desktopRecoveryActionIpcRequestSchema = z
  .object({
    schema: z.literal(
      "kestrel.desktop.recovery-action.request.v1"
    ),
    request: desktopRecoveryActionRequestSchema
  })
  .strict();

export const desktopRuntimeBootstrapResultSchema = z
  .object({
    marker: desktopRuntimeMarkerSchema.nullable()
  })
  .strict();

export interface DesktopBridge {
  connection(): Promise<DesktopConnection>;
  chooseProjectFolder(): Promise<DesktopFolderChoice>;
  chooseStorageFolder(): Promise<DesktopFolderChoice>;
  exportSupportBundle(): Promise<DesktopSupportBundleResult>;
  getAppVersion(): Promise<DesktopAppVersion>;
  getUpdateStatus(): Promise<DesktopUpdateStatus>;
  openCredentialDialog(
    intent: DesktopCredentialIntent
  ): Promise<DesktopCredentialResult>;
  openExternalUrl(
    request: DesktopExternalUrlRequest
  ): Promise<DesktopExternalUrlResult>;
  performRecoveryAction(
    request: DesktopRecoveryActionRequest
  ): Promise<DesktopRecoveryActionResult>;
  subscribeLifecycle(
    listener: (connection: DesktopConnection) => void
  ): () => void;
  subscribeUpdateStatus(
    listener: (status: DesktopUpdateStatus) => void
  ): () => void;
}
