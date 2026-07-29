import {
  mkdtemp,
  mkdir,
  realpath,
  symlink,
  writeFile
} from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  APP_CONTENT_SECURITY_POLICY,
  appProtocolResponse,
  registerAppProtocol,
  registerKestrelScheme,
  resolveAppAsset
} from "./protocol";

const expectedCsp =
  "default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; " +
  "img-src 'self' data: blob:; connect-src http://127.0.0.1:*; object-src 'none'; " +
  "base-uri 'none'; form-action 'none'; frame-ancestors 'none';";

describe("private app protocol", () => {
  let testRoot: string;
  let rendererRoot: string;

  beforeEach(async () => {
    testRoot = await mkdtemp(join(tmpdir(), "kestrel-protocol-"));
    rendererRoot = join(testRoot, "web", "dist");
    await mkdir(join(rendererRoot, "assets"), { recursive: true });
    await writeFile(join(rendererRoot, "index.html"), "<h1>Kestrel</h1>");
    await writeFile(join(rendererRoot, "assets", "app.js"), "export {};");
  });

  afterEach(async () => {
    const { rm } = await import("node:fs/promises");
    await rm(testRoot, { force: true, recursive: true });
  });

  it("serves only normalized files beneath the renderer root", async () => {
    await expect(
      resolveAppAsset("../secrets.json", rendererRoot)
    ).rejects.toThrow();
    await expect(
      resolveAppAsset("%2e%2e/secrets.json", rendererRoot)
    ).rejects.toThrow();
    await expect(
      resolveAppAsset("%252e%252e%252fsecrets.json", rendererRoot)
    ).rejects.toThrow();
    await expect(
      resolveAppAsset("assets%5capp.js", rendererRoot)
    ).rejects.toThrow();
    await expect(resolveAppAsset("assets\u0000app.js", rendererRoot)).rejects.toThrow();
    await expect(resolveAppAsset("assets/%ZZ.js", rendererRoot)).rejects.toThrow();
  });

  it("maps routes intentionally and admits only reviewed asset types", async () => {
    const indexPath = await realpath(join(rendererRoot, "index.html"));
    const scriptPath = await realpath(join(rendererRoot, "assets", "app.js"));
    await expect(resolveAppAsset("mission", rendererRoot)).resolves.toBe(
      indexPath
    );
    await expect(resolveAppAsset("/", rendererRoot)).resolves.toBe(
      indexPath
    );
    await expect(resolveAppAsset("assets/app.js", rendererRoot)).resolves.toBe(
      scriptPath
    );
    await writeFile(join(rendererRoot, "assets", "app.js.map"), "{}");
    await writeFile(join(rendererRoot, "private.env"), "secret");
    await expect(
      resolveAppAsset("assets/app.js.map", rendererRoot)
    ).rejects.toThrow();
    await expect(resolveAppAsset("private.env", rendererRoot)).rejects.toThrow();
  });

  it("rejects a symlink that escapes the real renderer root", async () => {
    const outside = join(testRoot, "outside.js");
    await writeFile(outside, "secret");
    await symlink(outside, join(rendererRoot, "assets", "escape.js"));

    await expect(
      resolveAppAsset("assets/escape.js", rendererRoot)
    ).rejects.toThrow();
  });

  it("attaches the exact CSP and safe media types to success and error responses", async () => {
    expect(APP_CONTENT_SECURITY_POLICY).toBe(expectedCsp);

    const html = await appProtocolResponse(
      { method: "GET", url: "kestrel://app/index.html" },
      rendererRoot
    );
    const script = await appProtocolResponse(
      { method: "GET", url: "kestrel://app/assets/app.js" },
      rendererRoot
    );
    const missing = await appProtocolResponse(
      { method: "GET", url: "kestrel://app/assets/missing.js" },
      rendererRoot
    );
    const malformed = await appProtocolResponse(
      { method: "GET", url: "kestrel://app/assets/%ZZ.js" },
      rendererRoot
    );
    const methodDenied = await appProtocolResponse(
      { method: "POST", url: "kestrel://app/index.html" },
      rendererRoot
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
    expect(await html.text()).toBe("<h1>Kestrel</h1>");
    expect(script.headers.get("Content-Type")).toBe(
      "text/javascript; charset=utf-8"
    );
    expect(missing.status).toBe(404);
    expect(missing.headers.get("Content-Type")).toBe(
      "text/plain; charset=utf-8"
    );
    expect(malformed.status).toBe(400);
    expect(methodDenied.status).toBe(405);
  });

  it("rejects non-app origins before reading any asset", async () => {
    const reader = vi.fn(async (_path: string) => new Uint8Array());
    const response = await appProtocolResponse(
      { method: "GET", url: "kestrel://other/index.html" },
      rendererRoot,
      reader
    );

    expect(response.status).toBe(400);
    expect(reader).not.toHaveBeenCalled();
  });

  it("rejects URL-level traversal before URL normalization can hide it", async () => {
    const response = await appProtocolResponse(
      {
        method: "GET",
        url: "kestrel://app/%2e%2e/assets/app.js"
      },
      rendererRoot
    );

    expect(response.status).toBe(400);
  });

  it("registers the scheme as privileged and binds exactly one app handler", async () => {
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

    registerAppProtocol(protocol, rendererRoot);
    const response = await handler?.({
      method: "GET",
      url: "kestrel://app/index.html"
    });

    expect(protocol.handle).toHaveBeenCalledOnce();
    expect(response?.status).toBe(200);
  });
});
