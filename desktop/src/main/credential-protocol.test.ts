import { describe, expect, it, vi } from "vitest";
import type {
  VerifiedCredentialAssets,
  VerifiedRendererAssets
} from "./resource-manifest";
import {
  CREDENTIAL_CONTENT_SECURITY_POLICY,
  appProtocolResponse,
  credentialProtocolResponse,
  registerKestrelProtocol
} from "./protocol";

const expectedCredentialCsp =
  "default-src 'none'; script-src 'self'; style-src 'self'; " +
  "img-src 'none'; connect-src 'none'; object-src 'none'; " +
  "base-uri 'none'; form-action 'none'; frame-ancestors 'none';";

class Assets
  implements VerifiedRendererAssets, VerifiedCredentialAssets
{
  readonly totalBytes: number;
  readonly reads: string[] = [];
  readonly #assets: Map<string, Uint8Array>;

  constructor(entries: Readonly<Record<string, string>>) {
    this.#assets = new Map(
      Object.entries(entries).map(([path, body]) => [
        path,
        Buffer.from(body)
      ])
    );
    this.totalBytes = [...this.#assets.values()].reduce(
      (total, bytes) => total + bytes.byteLength,
      0
    );
  }

  read(relativePath: string): Uint8Array | undefined {
    this.reads.push(relativePath);
    const bytes = this.#assets.get(relativePath);
    return bytes === undefined
      ? undefined
      : Uint8Array.from(bytes);
  }
}

function appAssets(): Assets {
  return new Assets({
    "index.html": "<h1>Primary</h1>",
    "assets/app.js": "export {};"
  });
}

function credentialAssets(): Assets {
  return new Assets({
    "index.html": "<form>Credential</form>",
    "form.js": "export {};",
    "styles.css": "body { color: #111; }",
    "preload.js": "require(\"electron\");"
  });
}

describe("immutable credential protocol host", () => {
  it("serves only the three reviewed renderer assets with the exact credential CSP", async () => {
    const assets = credentialAssets();
    expect(CREDENTIAL_CONTENT_SECURITY_POLICY).toBe(
      expectedCredentialCsp
    );

    const responses = await Promise.all(
      ["index.html", "form.js", "styles.css"].map((path) =>
        credentialProtocolResponse(
          {
            method: "GET",
            url: `kestrel://credential/${path}`
          },
          assets
        )
      )
    );
    expect(
      await Promise.all(
        responses.map((response: Response) => response.text())
      )
    ).toEqual([
      "<form>Credential</form>",
      "export {};",
      "body { color: #111; }"
    ]);
    for (const response of responses) {
      expect(response.status).toBe(200);
      expect(
        response.headers.get("Content-Security-Policy")
      ).toBe(expectedCredentialCsp);
      expect(
        response.headers.get("X-Content-Type-Options")
      ).toBe("nosniff");
    }

    for (const url of [
      "kestrel://credential/preload.js",
      "kestrel://credential/form.js.map",
      "kestrel://credential/private.env",
      "kestrel://credential/%2e%2e/index.html"
    ]) {
      const denied = await credentialProtocolResponse(
        { method: "GET", url },
        assets
      );
      expect([400, 404]).toContain(denied.status);
      expect(
        denied.headers.get("Content-Security-Policy")
      ).toBe(expectedCredentialCsp);
    }
  });

  it("keeps app and credential snapshots mutually unreachable", async () => {
    const primary = appAssets();
    const credential = credentialAssets();

    const appReadingCredential = await appProtocolResponse(
      {
        method: "GET",
        url: "kestrel://credential/index.html"
      },
      primary
    );
    const credentialReadingApp =
      await credentialProtocolResponse(
        {
          method: "GET",
          url: "kestrel://app/index.html"
        },
        credential
      );

    expect(appReadingCredential.status).toBe(400);
    expect(credentialReadingApp.status).toBe(400);
    expect(primary.reads).toEqual([]);
    expect(credential.reads).toEqual([]);
  });

  it("dispatches both exact hosts through one scheme handler and rejects every other host", async () => {
    let handler:
      | ((request: {
          method: string;
          url: string;
        }) => Promise<Response>)
      | undefined;
    const registrar = {
      handle: vi.fn(
        (
          scheme: string,
          next: (request: {
            method: string;
            url: string;
          }) => Promise<Response>
        ) => {
          expect(scheme).toBe("kestrel");
          handler = next;
        }
      )
    };
    const primary = appAssets();
    const credential = credentialAssets();

    registerKestrelProtocol(registrar, {
      rendererAssets: primary,
      credentialAssets: credential
    });

    expect(
      await (
        await handler!({
          method: "GET",
          url: "kestrel://app/index.html"
        })
      ).text()
    ).toBe("<h1>Primary</h1>");
    expect(
      await (
        await handler!({
          method: "GET",
          url: "kestrel://credential/index.html"
        })
      ).text()
    ).toBe("<form>Credential</form>");
    expect(
      (
        await handler!({
          method: "GET",
          url: "kestrel://other/index.html"
        })
      ).status
    ).toBe(400);
    expect(registrar.handle).toHaveBeenCalledOnce();
  });
});
