import { createHmac } from "node:crypto";
import { z } from "zod";
import {
  DESKTOP_CREDENTIAL_VALUE_BYTES,
  desktopCredentialProviderIdSchema,
  type DesktopCredentialProviderId,
  type DesktopCredentialResult,
  type DesktopErrorCode
} from "../contracts.js";

const CAPABILITY_CONTEXT =
  "kestrel.desktop.credential.write.v1\0";
const CREDENTIAL_RESPONSE_BYTES = 32 * 1024;
const DETERMINISTIC_REJECTION_STATUSES = new Set([
  400, 401, 403, 413, 415
]);
const localErrors = new WeakSet<object>();

export interface DesktopCredentialApiAuthority {
  readonly baseUrl: string;
  readonly apiToken: string;
  readonly credentialCapability: string;
  readonly generation: number;
}

export interface DesktopCredentialProviderAuthority {
  readonly providerId: DesktopCredentialProviderId;
  readonly label: string;
  readonly name: string;
  readonly secretId: string;
  readonly purpose: string;
}

interface CredentialFetch {
  (url: string, init: RequestInit): Promise<Response>;
}

export interface DesktopCredentialApiClient {
  storeProviderCredential(request: {
    providerId: DesktopCredentialProviderId;
    expectedGeneration: number;
    valueBytes: Uint8Array;
    signal?: AbortSignal;
  }): Promise<Extract<DesktopCredentialResult, { status: "stored" }>>;
}

const providerAuthorities = Object.freeze({
  openai: Object.freeze({
    providerId: "openai",
    label: "OpenAI",
    name: "OPENAI_API_KEY",
    secretId: "openai_api_key"
  }),
  openrouter: Object.freeze({
    providerId: "openrouter",
    label: "OpenRouter",
    name: "OPENROUTER_API_KEY",
    secretId: "openrouter_api_key"
  }),
  deepseek: Object.freeze({
    providerId: "deepseek",
    label: "DeepSeek",
    name: "DEEPSEEK_API_KEY",
    secretId: "deepseek_api_key"
  }),
  kimi: Object.freeze({
    providerId: "kimi",
    label: "Kimi",
    name: "MOONSHOT_API_KEY",
    secretId: "moonshot_api_key"
  }),
  "ollama-cloud": Object.freeze({
    providerId: "ollama-cloud",
    label: "Ollama Cloud",
    name: "OLLAMA_API_KEY",
    secretId: "ollama_api_key"
  }),
  anthropic: Object.freeze({
    providerId: "anthropic",
    label: "Anthropic",
    name: "ANTHROPIC_API_KEY",
    secretId: "anthropic_api_key"
  }),
  grok: Object.freeze({
    providerId: "grok",
    label: "Grok / xAI",
    name: "XAI_API_KEY",
    secretId: "xai_api_key"
  }),
  gemini: Object.freeze({
    providerId: "gemini",
    label: "Gemini",
    name: "GEMINI_API_KEY",
    secretId: "gemini_api_key"
  })
} as const);

function fixedError(code: DesktopErrorCode): Readonly<{
  code: DesktopErrorCode;
}> {
  const error = Object.freeze({ code });
  localErrors.add(error);
  return error;
}

export function deriveDesktopCredentialCapability(
  apiToken: string,
  launchNonce: string
): string {
  return createHmac("sha256", Buffer.from(apiToken, "utf8"))
    .update(
      Buffer.from(
        `${CAPABILITY_CONTEXT}${launchNonce}`,
        "utf8"
      )
    )
    .digest("hex");
}

export function credentialProviderAuthority(
  providerId: DesktopCredentialProviderId
): DesktopCredentialProviderAuthority {
  const parsed = desktopCredentialProviderIdSchema.safeParse(
    providerId
  );
  if (!parsed.success) {
    throw new Error("invalid_desktop_request");
  }
  const authority = providerAuthorities[parsed.data];
  return Object.freeze({
    ...authority,
    purpose: `Desktop provider API key for ${authority.label}.`
  });
}

function exactAuthority(
  raw: DesktopCredentialApiAuthority | null
): DesktopCredentialApiAuthority | null {
  if (
    raw === null ||
    typeof raw !== "object" ||
    typeof raw.baseUrl !== "string" ||
    typeof raw.apiToken !== "string" ||
    typeof raw.credentialCapability !== "string" ||
    !Number.isSafeInteger(raw.generation) ||
    raw.generation <= 0 ||
    raw.apiToken.length === 0 ||
    raw.apiToken.length > 4_096 ||
    raw.apiToken.trim() !== raw.apiToken ||
    /[\r\n]/.test(raw.apiToken) ||
    !/^[0-9a-f]{64}$/.test(raw.credentialCapability)
  ) {
    return null;
  }
  try {
    const parsed = new URL(raw.baseUrl);
    if (
      parsed.protocol !== "http:" ||
      parsed.hostname !== "127.0.0.1" ||
      parsed.port === "" ||
      parsed.username !== "" ||
      parsed.password !== "" ||
      parsed.pathname !== "/" ||
      parsed.search !== "" ||
      parsed.hash !== "" ||
      parsed.href !== raw.baseUrl
    ) {
      return null;
    }
  } catch {
    return null;
  }
  return raw;
}

function sameGeneration(
  authority: DesktopCredentialApiAuthority | null,
  expectedGeneration: number
): authority is DesktopCredentialApiAuthority {
  return (
    authority !== null &&
    authority.generation === expectedGeneration
  );
}

function serverMetadataSchema(
  provider: DesktopCredentialProviderAuthority
) {
  return z
    .object({
      schema: z.literal(
        "kestrel.desktop.credential-store.v1"
      ),
      provider_id: z.literal(provider.providerId),
      id: z.literal(provider.secretId),
      name: z.literal(provider.name),
      purpose: z.literal(provider.purpose),
      secret_ref: z.literal(
        `secret://${provider.secretId}` as const
      ),
      configured: z.literal(true),
      validated: z.literal(false),
      fingerprint: z
        .string()
        .regex(/^sha256:[0-9a-f]{12}$/),
      source: z.literal("broker")
    })
    .strict();
}

function cancelResponseBody(response: Response): void {
  try {
    const cancellation = response.body?.cancel();
    if (cancellation !== undefined) {
      void cancellation.catch(() => undefined);
    }
  } catch {
    // Invalid responses remain fixed even if stream cancellation fails.
  }
}

function awaitWithAbort<T>(
  pending: Promise<T>,
  signal: AbortSignal | undefined
): Promise<T> {
  if (signal === undefined) {
    return pending;
  }
  if (signal.aborted) {
    return Promise.reject(
      fixedError("desktop_operation_ambiguous")
    );
  }
  return new Promise<T>((resolve, reject) => {
    let settled = false;
    const finish = (
      callback: () => void
    ): void => {
      if (settled) {
        return;
      }
      settled = true;
      signal.removeEventListener("abort", onAbort);
      callback();
    };
    const onAbort = (): void => {
      finish(() => {
        reject(fixedError("desktop_operation_ambiguous"));
      });
    };
    signal.addEventListener("abort", onAbort, {
      once: true
    });
    pending.then(
      (value) => {
        finish(() => {
          resolve(value);
        });
      },
      (error: unknown) => {
        finish(() => {
          reject(error);
        });
      }
    );
  });
}

async function boundedJson(
  response: Response,
  signal: AbortSignal | undefined
): Promise<unknown> {
  const contentLength = response.headers.get("Content-Length");
  if (
    contentLength !== null &&
    (!/^\d+$/.test(contentLength) ||
      Number(contentLength) > CREDENTIAL_RESPONSE_BYTES)
  ) {
    cancelResponseBody(response);
    throw fixedError("invalid_desktop_response");
  }
  const body = response.body;
  if (body === null) {
    throw fixedError("invalid_desktop_response");
  }
  const reader = body.getReader();
  const decoder = new TextDecoder("utf-8", {
    fatal: true
  });
  let totalBytes = 0;
  let text = "";
  try {
    while (true) {
      const part = await awaitWithAbort(
        reader.read(),
        signal
      );
      if (part.done) {
        break;
      }
      if (!(part.value instanceof Uint8Array)) {
        void reader.cancel().catch(() => undefined);
        throw fixedError("invalid_desktop_response");
      }
      totalBytes += part.value.byteLength;
      if (totalBytes > CREDENTIAL_RESPONSE_BYTES) {
        void reader.cancel().catch(() => undefined);
        throw fixedError("invalid_desktop_response");
      }
      text += decoder.decode(part.value, {
        stream: true
      });
    }
    text += decoder.decode();
    return JSON.parse(text);
  } catch {
    try {
      void reader.cancel().catch(() => undefined);
    } catch {
      // The fixed response error still wins.
    }
    throw fixedError("invalid_desktop_response");
  } finally {
    reader.releaseLock();
  }
}

export function createDesktopCredentialApiClient(options: {
  readAuthority():
    | DesktopCredentialApiAuthority
    | null;
  fetch: CredentialFetch;
}): DesktopCredentialApiClient {
  const client: DesktopCredentialApiClient = {
    async storeProviderCredential(request) {
      const provider = credentialProviderAuthority(
        request.providerId
      );
      const input = request.valueBytes;
      if (
        Object.getPrototypeOf(input) !==
          Uint8Array.prototype ||
        input.byteLength === 0 ||
        input.byteLength > DESKTOP_CREDENTIAL_VALUE_BYTES ||
        !Number.isSafeInteger(request.expectedGeneration) ||
        request.expectedGeneration <= 0
      ) {
        if (
          input instanceof Uint8Array &&
          !Buffer.isBuffer(input)
        ) {
          input.fill(0);
        }
        throw fixedError("invalid_desktop_request");
      }
      const body = Uint8Array.from(input);
      let dispatched = false;
      const signal = request.signal;
      const scrubOnAbort = (): void => {
        body.fill(0);
        input.fill(0);
      };
      signal?.addEventListener("abort", scrubOnAbort, {
        once: true
      });
      try {
        if (signal?.aborted === true) {
          throw fixedError("desktop_operation_failed");
        }
        const initial = exactAuthority(
          options.readAuthority()
        );
        if (
          !sameGeneration(
            initial,
            request.expectedGeneration
          )
        ) {
          throw fixedError("desktop_operation_failed");
        }
        dispatched = true;
        let response: Response;
        try {
          response = await awaitWithAbort(
            options.fetch(
              `${initial.baseUrl}api/desktop/credentials/providers/${provider.providerId}`,
              {
                method: "POST",
                redirect: "error",
                cache: "no-store",
                signal,
                headers: {
                  Authorization: `Bearer ${initial.apiToken}`,
                  "Content-Type": "application/octet-stream",
                  "X-Kestrel-Desktop-Credential-Capability":
                    initial.credentialCapability
                },
                body
              }
            ),
            signal
          );
        } catch {
          throw fixedError("desktop_operation_ambiguous");
        }
        const current = exactAuthority(
          options.readAuthority()
        );
        if (
          !sameGeneration(
            current,
            request.expectedGeneration
          ) ||
          current.baseUrl !== initial.baseUrl ||
          current.apiToken !== initial.apiToken ||
          current.credentialCapability !==
            initial.credentialCapability
        ) {
          throw fixedError("desktop_operation_ambiguous");
        }
        if (!response.ok) {
          cancelResponseBody(response);
          throw fixedError(
            DETERMINISTIC_REJECTION_STATUSES.has(
              response.status
            )
              ? "desktop_operation_failed"
              : "desktop_operation_ambiguous"
          );
        }
        let parsed:
          | ReturnType<
              ReturnType<typeof serverMetadataSchema>["safeParse"]
            >
          | undefined;
        try {
          parsed = serverMetadataSchema(provider).safeParse(
            await boundedJson(response, signal)
          );
        } catch {
          throw fixedError("desktop_operation_ambiguous");
        }
        if (!parsed.success) {
          throw fixedError("desktop_operation_ambiguous");
        }
        return Object.freeze({
          status: "stored" as const,
          secretRef: parsed.data.secret_ref,
          validation: "unverified" as const,
          fingerprint: parsed.data.fingerprint
        });
      } catch (error) {
        if (
          error !== null &&
          typeof error === "object" &&
          localErrors.has(error)
        ) {
          throw error;
        }
        throw fixedError(
          dispatched
            ? "desktop_operation_ambiguous"
            : "desktop_operation_failed"
        );
      } finally {
        signal?.removeEventListener(
          "abort",
          scrubOnAbort
        );
        body.fill(0);
        input.fill(0);
      }
    }
  };
  return Object.freeze(client);
}
