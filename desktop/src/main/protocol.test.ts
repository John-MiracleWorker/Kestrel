import { describe, expect, it, vi } from "vitest";
import type { VerifiedRendererAssets } from "./resource-manifest";
import {
  APP_CONTENT_SECURITY_POLICY,
  appProtocolResponse,
  registerAppProtocol,
  registerKestrelScheme
} from "./protocol";

const expectedCsp =
  "default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; " +
  "img-src 'self' data: blob:; connect-src http://127.0.0.1:*; object-src 'none'; " +
  "base-uri 'none'; form-action 'none'; frame-ancestors 'none';";

class TestRendererAssets implements VerifiedRendererAssets {
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
    return bytes === undefined ? undefined : Uint8Array.from(bytes);
  }
}

function rendererAssets(): TestRendererAssets {
  return new TestRendererAssets({
    "index.html": "<h1>Kestrel</h1>",
    "assets/app.js": "export {};"
  });
}

describe("private app protocol", () => {
  it("rejects ambiguous or traversing URL paths before snapshot lookup", async () => {
    const assets = rendererAssets();
    for (const url of [
      "kestrel://app/%2e%2e/secrets.json",
      "kestrel://app/%252e%252e%252fsecrets.json",
      "kestrel://app/assets%5capp.js",
      "kestrel://app/assets/%00app.js",
      "kestrel://app/assets/%ZZ.js"
    ]) {
      const response = await appProtocolResponse({ method: "GET", url }, assets);
      expect(response.status).toBe(400);
    }
    expect(assets.reads).toEqual([]);
  });

  it("maps routes intentionally and admits only reviewed asset types", async () => {
    const assets = rendererAssets();
    const mission = await appProtocolResponse(
      { method: "GET", url: "kestrel://app/mission" },
      assets
    );
    const root = await appProtocolResponse(
      { method: "GET", url: "kestrel://app/" },
      assets
    );
    const script = await appProtocolResponse(
      { method: "GET", url: "kestrel://app/assets/app.js" },
      assets
    );
    const sourceMap = await appProtocolResponse(
      { method: "GET", url: "kestrel://app/assets/app.js.map" },
      assets
    );
    const privateFile = await appProtocolResponse(
      { method: "GET", url: "kestrel://app/private.env" },
      assets
    );

    expect(await mission.text()).toBe("<h1>Kestrel</h1>");
    expect(await root.text()).toBe("<h1>Kestrel</h1>");
    expect(await script.text()).toBe("export {};");
    expect(sourceMap.status).toBe(400);
    expect(privateFile.status).toBe(400);
  });

  it("rejects every reviewed path absent from the verified snapshot", async () => {
    const assets = rendererAssets();
    const missing = await appProtocolResponse(
      { method: "GET", url: "kestrel://app/assets/not-signed.js" },
      assets
    );

    expect(missing.status).toBe(404);
    expect(assets.reads).toEqual(["assets/not-signed.js"]);
  });

  it("attaches the exact CSP and safe media types to success and error responses", async () => {
    const assets = rendererAssets();
    expect(APP_CONTENT_SECURITY_POLICY).toBe(expectedCsp);
    const html = await appProtocolResponse(
      { method: "GET", url: "kestrel://app/index.html" },
      assets
    );
    const script = await appProtocolResponse(
      { method: "GET", url: "kestrel://app/assets/app.js" },
      assets
    );
    const missing = await appProtocolResponse(
      { method: "GET", url: "kestrel://app/assets/missing.js" },
      assets
    );
    const malformed = await appProtocolResponse(
      { method: "GET", url: "kestrel://app/assets/%ZZ.js" },
      assets
    );
    const methodDenied = await appProtocolResponse(
      { method: "POST", url: "kestrel://app/index.html" },
      assets
    );

    for (const response of [
      html,
      script,
      missing,
      malformed,
      methodDenied
    ]) {
      expect(response.headers.get("Content-Security-Policy")).toBe(expectedCsp);
      expect(response.headers.get("X-Content-Type-Options")).toBe("nosniff");
    }
    expect(html.status).toBe(200);
    expect(html.headers.get("Content-Type")).toBe("text/html; charset=utf-8");
    expect(script.headers.get("Content-Type")).toBe(
      "text/javascript; charset=utf-8"
    );
    expect(missing.status).toBe(404);
    expect(malformed.status).toBe(400);
    expect(methodDenied.status).toBe(405);
  });

  it("rejects non-app origins before reading any asset", async () => {
    const assets = rendererAssets();
    const response = await appProtocolResponse(
      { method: "GET", url: "kestrel://other/index.html" },
      assets
    );

    expect(response.status).toBe(400);
    expect(assets.reads).toEqual([]);
  });

  it("serves a defensive copy returned by the verified source", async () => {
    const original = Buffer.from("<h1>Verified snapshot</h1>");
    const source: VerifiedRendererAssets = {
      totalBytes: original.byteLength,
      read: (relativePath) =>
        relativePath === "index.html"
          ? Uint8Array.from(original)
          : undefined
    };
    const response = await appProtocolResponse(
      { method: "GET", url: "kestrel://app/mission" },
      source
    );
    original.fill(0);

    expect(response.status).toBe(200);
    expect(await response.text()).toBe("<h1>Verified snapshot</h1>");
  });

  it("registers the scheme as privileged and binds exactly one verified handler", async () => {
    const privilegeCalls: unknown[] = [];
    registerKestrelScheme({
      registerSchemesAsPrivileged: (schemes) => {
        privilegeCalls.push(schemes);
      }
    });
    expect(privilegeCalls).toEqual([
      [
        {
          scheme: "kestrel",
          privileges: {
            standard: true,
            secure: true,
            supportFetchAPI: true,
            stream: true
          }
        }
      ]
    ]);

    let handler:
      | ((request: { method: string; url: string }) => Promise<Response>)
      | undefined;
    const protocol = {
      handle: vi.fn(
        (
          scheme: string,
          registered: (request: {
            method: string;
            url: string;
          }) => Promise<Response>
        ) => {
          expect(scheme).toBe("kestrel");
          handler = registered;
        }
      )
    };
    const assets = rendererAssets();

    registerAppProtocol(protocol, assets);
    const response = await handler?.({
      method: "GET",
      url: "kestrel://app/index.html"
    });

    expect(protocol.handle).toHaveBeenCalledOnce();
    expect(response?.status).toBe(200);
    expect(assets.reads).toEqual(["index.html"]);
  });
});
