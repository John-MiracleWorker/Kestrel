import { readFile } from "node:fs/promises";
import { join } from "node:path";
import { describe, expect, it, vi } from "vitest";
import {
  createDesktopCredentialApiClient,
  credentialProviderAuthority,
  deriveDesktopCredentialCapability,
  type DesktopCredentialApiAuthority
} from "./credential-api";

const providers = [
  ["openai", "OpenAI", "OPENAI_API_KEY", "openai_api_key"],
  [
    "openrouter",
    "OpenRouter",
    "OPENROUTER_API_KEY",
    "openrouter_api_key"
  ],
  [
    "deepseek",
    "DeepSeek",
    "DEEPSEEK_API_KEY",
    "deepseek_api_key"
  ],
  ["kimi", "Kimi", "MOONSHOT_API_KEY", "moonshot_api_key"],
  [
    "ollama-cloud",
    "Ollama Cloud",
    "OLLAMA_API_KEY",
    "ollama_api_key"
  ],
  [
    "anthropic",
    "Anthropic",
    "ANTHROPIC_API_KEY",
    "anthropic_api_key"
  ],
  ["grok", "Grok / xAI", "XAI_API_KEY", "xai_api_key"],
  ["gemini", "Gemini", "GEMINI_API_KEY", "gemini_api_key"]
] as const;

function authority(
  overrides: Partial<DesktopCredentialApiAuthority> = {}
): DesktopCredentialApiAuthority {
  return {
    baseUrl: "http://127.0.0.1:43123/",
    apiToken: "desktop-api-token",
    credentialCapability: "a".repeat(64),
    generation: 7,
    ...overrides
  };
}

function serverMetadata(
  providerId: (typeof providers)[number][0]
): Record<string, unknown> {
  const provider = providers.find(([id]) => id === providerId)!;
  return {
    schema: "kestrel.desktop.credential-store.v1",
    provider_id: providerId,
    id: provider[3],
    name: provider[2],
    purpose: `Desktop provider API key for ${provider[1]}.`,
    secret_ref: `secret://${provider[3]}`,
    configured: true,
    validated: false,
    fingerprint: "sha256:0123456789ab",
    source: "broker"
  };
}

function jsonResponse(
  value: unknown,
  status = 200
): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" }
  });
}

describe("main-owned Desktop credential API", () => {
  it("derives the exact cross-language HMAC capability vector", async () => {
    const fixture = JSON.parse(
      await readFile(
        join(
          import.meta.dirname,
          "../../../tests/fixtures/desktop-canonical-vectors.json"
        ),
        "utf8"
      )
    ) as {
      credential_capability: {
        api_token: string;
        launch_nonce: string;
        message_utf8_hex: string;
        hmac_sha256_hex: string;
      };
    };
    const vector = fixture.credential_capability;
    expect(
      Buffer.from(
        `kestrel.desktop.credential.write.v1\0${vector.launch_nonce}`,
        "utf8"
      ).toString("hex")
    ).toBe(vector.message_utf8_hex);
    expect(
      deriveDesktopCredentialCapability(
        vector.api_token,
        vector.launch_nonce
      )
    ).toBe(vector.hmac_sha256_hex);
  });

  it("owns all eight canonical provider identities and purpose text", () => {
    for (const [providerId, label, name, secretId] of providers) {
      expect(credentialProviderAuthority(providerId)).toEqual({
        providerId,
        label,
        name,
        secretId,
        purpose: `Desktop provider API key for ${label}.`
      });
    }
    for (const providerId of [
      "mock",
      "ollama",
      "lm-studio",
      "codex-cli",
      "openai-compatible",
      "OPENAI",
      "custom"
    ]) {
      expect(() =>
        credentialProviderAuthority(providerId as never)
      ).toThrow("invalid_desktop_request");
    }
  });

  it.each(providers)(
    "posts %s raw bytes once to the fixed provider route with hidden authority",
    async (providerId, _label, _name, secretId) => {
      const active = authority();
      const input = new TextEncoder().encode(
        `private-${providerId}-sentinel`
      );
      let capturedBody: Uint8Array | undefined;
      const fetch = vi.fn(
        async (url: string, init: RequestInit) => {
          capturedBody = init.body as Uint8Array;
          expect(url).toBe(
            `http://127.0.0.1:43123/api/desktop/credentials/providers/${providerId}`
          );
          expect(init).toMatchObject({
            method: "POST",
            redirect: "error",
            cache: "no-store"
          });
          expect(init.headers).toEqual({
            Authorization: "Bearer desktop-api-token",
            "Content-Type": "application/octet-stream",
            "X-Kestrel-Desktop-Credential-Capability":
              "a".repeat(64)
          });
          expect(
            new TextDecoder().decode(capturedBody)
          ).toBe(`private-${providerId}-sentinel`);
          return jsonResponse(serverMetadata(providerId));
        }
      );
      const client = createDesktopCredentialApiClient({
        readAuthority: () => active,
        fetch
      });

      await expect(
        client.storeProviderCredential({
          providerId,
          expectedGeneration: 7,
          valueBytes: input
        })
      ).resolves.toEqual({
        status: "stored",
        secretRef: `secret://${secretId}`,
        validation: "unverified",
        fingerprint: "sha256:0123456789ab"
      });

      expect(fetch).toHaveBeenCalledTimes(1);
      expect([...input].every((value) => value === 0)).toBe(true);
      expect(
        [...(capturedBody ?? [])].every((value) => value === 0)
      ).toBe(true);
      expect(JSON.stringify(client)).not.toContain(
        "desktop-api-token"
      );
    }
  );

  it("rejects generation drift before dispatch without sending or clearing an unrelated authority", async () => {
    const fetch = vi.fn();
    const input = new TextEncoder().encode("never-dispatched");
    const client = createDesktopCredentialApiClient({
      readAuthority: () => authority({ generation: 8 }),
      fetch
    });

    await expect(
      client.storeProviderCredential({
        providerId: "openai",
        expectedGeneration: 7,
        valueBytes: input
      })
    ).rejects.toMatchObject({
      code: "desktop_operation_failed"
    });
    expect(fetch).not.toHaveBeenCalled();
    expect([...input].every((value) => value === 0)).toBe(true);
  });

  it("marks post-dispatch generation drift and transport failure ambiguous without retry", async () => {
    for (const mode of ["generation", "transport"] as const) {
      let current = authority();
      const input = new TextEncoder().encode(
        `ambiguous-${mode}-sentinel`
      );
      let capturedBody: Uint8Array | undefined;
      const fetch = vi.fn(
        async (_url: string, init: RequestInit) => {
          capturedBody = init.body as Uint8Array;
          if (mode === "transport") {
            throw new Error("native-network-secret");
          }
          current = authority({ generation: 8 });
          return jsonResponse(serverMetadata("openai"));
        }
      );
      const client = createDesktopCredentialApiClient({
        readAuthority: () => current,
        fetch
      });

      let caught: unknown;
      try {
        await client.storeProviderCredential({
          providerId: "openai",
          expectedGeneration: 7,
          valueBytes: input
        });
      } catch (error) {
        caught = error;
      }
      expect(caught).toEqual({
        code: "desktop_operation_ambiguous"
      });
      expect(JSON.stringify(caught)).not.toContain("secret");
      expect(fetch).toHaveBeenCalledTimes(1);
      expect([...input].every((value) => value === 0)).toBe(true);
      expect(
        [...(capturedBody ?? [])].every((value) => value === 0)
      ).toBe(true);
    }
  });

  it("treats mismatched or secret-bearing post-dispatch metadata as ambiguous", async () => {
    const invalidResponses = [
      {
        ...serverMetadata("openai"),
        name: "RENDERER_CHOSEN_NAME"
      },
      {
        ...serverMetadata("openai"),
        provider_id: "grok"
      },
      {
        ...serverMetadata("openai"),
        validated: true
      },
      {
        ...serverMetadata("openai"),
        value: "response-secret"
      },
      {
        ...serverMetadata("openai"),
        fingerprint: "x".repeat(40_000)
      }
    ];
    for (const metadata of invalidResponses) {
      const input = new TextEncoder().encode("invalid-response");
      const client = createDesktopCredentialApiClient({
        readAuthority: () => authority(),
        fetch: async () => jsonResponse(metadata)
      });
      await expect(
        client.storeProviderCredential({
          providerId: "openai",
          expectedGeneration: 7,
          valueBytes: input
        })
      ).rejects.toMatchObject({
        code: "desktop_operation_ambiguous"
      });
      expect([...input].every((value) => value === 0)).toBe(true);
    }
  });

  it("keeps exact pre-store HTTP rejections deterministic and all uncertain statuses ambiguous without retry", async () => {
    for (const status of [400, 401, 403, 413, 415]) {
      const fetch = vi.fn(async () =>
        jsonResponse({ detail: "fixed_rejection" }, status)
      );
      const client = createDesktopCredentialApiClient({
        readAuthority: () => authority(),
        fetch
      });

      await expect(
        client.storeProviderCredential({
          providerId: "openai",
          expectedGeneration: 7,
          valueBytes: new TextEncoder().encode(
            `deterministic-${status}`
          )
        })
      ).rejects.toEqual({
        code: "desktop_operation_failed"
      });
      expect(fetch).toHaveBeenCalledOnce();
    }

    for (const status of [409, 500, 503]) {
      const fetch = vi.fn(async () =>
        jsonResponse({ detail: "must-not-cross" }, status)
      );
      const client = createDesktopCredentialApiClient({
        readAuthority: () => authority(),
        fetch
      });

      await expect(
        client.storeProviderCredential({
          providerId: "openai",
          expectedGeneration: 7,
          valueBytes: new TextEncoder().encode(
            `ambiguous-${status}`
          )
        })
      ).rejects.toEqual({
        code: "desktop_operation_ambiguous"
      });
      expect(fetch).toHaveBeenCalledOnce();
    }
  });
});
