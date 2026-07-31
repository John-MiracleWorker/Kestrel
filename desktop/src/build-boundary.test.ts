import { spawnSync } from "node:child_process";
import { existsSync, readdirSync, readFileSync, rmSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { runInNewContext } from "node:vm";
import { describe, expect, it } from "vitest";

const workspace = fileURLToPath(new URL("..", import.meta.url));
const dist = fileURLToPath(new URL("../dist", import.meta.url));

describe("desktop build boundary", () => {
  it("emits separate sandbox-compatible primary and credential bundles with reviewed main modules only", () => {
    rmSync(dist, { force: true, recursive: true });

    const build = spawnSync("npm", ["run", "build"], {
      cwd: workspace,
      encoding: "utf8"
    });

    expect(build.status, `${build.stdout}\n${build.stderr}`).toBe(0);
    expect(existsSync(dist)).toBe(true);
    expect(readdirSync(dist, { recursive: true }).sort()).toEqual([
      "contracts.js",
      "credential",
      "credential/form.js",
      "credential/index.html",
      "credential/preload.js",
      "credential/styles.css",
      "main",
      "main.js",
      "main/api-session.js",
      "main/app-route.js",
      "main/build-trust.js",
      "main/credential-api.js",
      "main/credential-window.js",
      "main/developer-runtime.js",
      "main/directory-smoke.js",
      "main/ipc.js",
      "main/private-files.js",
      "main/protocol.js",
      "main/resource-manifest.js",
      "main/security.js",
      "main/sidecar-supervisor.js",
      "main/window.js",
      "preload.js"
    ]);

    const packageJson = JSON.parse(
      readFileSync(new URL("../package.json", import.meta.url), "utf8")
    ) as { main?: string };
    expect(packageJson.main).toBe("dist/main.js");
    expect(existsSync(fileURLToPath(new URL("../dist/main.js", import.meta.url)))).toBe(
      true
    );
    const preload = readFileSync(
      fileURLToPath(new URL("../dist/preload.js", import.meta.url)),
      "utf8"
    );
    expect(preload).not.toMatch(/\bimport\s/);
    const requires = [
      ...preload.matchAll(/\brequire\((["'][^"']+["'])\)/g)
    ].map((match) => match[1]);
    expect([...preload.matchAll(/\brequire\(/g)]).toHaveLength(1);
    expect(requires).toEqual(['"electron"']);
    expect(preload).not.toContain("sourceMappingURL");
    expect(preload).not.toContain("ipcRenderer:");
    expect(preload).not.toContain("apiToken");

    const credentialPreload = readFileSync(
      fileURLToPath(
        new URL(
          "../dist/credential/preload.js",
          import.meta.url
        )
      ),
      "utf8"
    );
    expect(credentialPreload).not.toMatch(/\bimport\s/);
    expect([
      ...credentialPreload.matchAll(/\brequire\(/g)
    ]).toHaveLength(1);
    expect([
      ...credentialPreload.matchAll(
        /\brequire\((["'][^"']+["'])\)/g
      )
    ].map((match) => match[1])).toEqual(['"electron"']);
    expect(credentialPreload).not.toContain(
      "sourceMappingURL"
    );
    expect(credentialPreload).not.toContain("kestrelDesktop");
    expect(credentialPreload).not.toContain("apiToken");
    expect(credentialPreload).not.toContain(
      "X-Kestrel-Desktop-Credential-Capability"
    );
    expect(credentialPreload).not.toContain("localStorage");
    expect(credentialPreload).not.toContain("sessionStorage");
    expect(credentialPreload).not.toMatch(/\bfetch\s*\(/);
    expect(credentialPreload).not.toContain("XMLHttpRequest");

    const credentialHtml = readFileSync(
      fileURLToPath(
        new URL(
          "../dist/credential/index.html",
          import.meta.url
        )
      ),
      "utf8"
    );
    const credentialForm = readFileSync(
      fileURLToPath(
        new URL("../dist/credential/form.js", import.meta.url)
      ),
      "utf8"
    );
    const credentialStyles = readFileSync(
      fileURLToPath(
        new URL("../dist/credential/styles.css", import.meta.url)
      ),
      "utf8"
    );
    const credentialInput = credentialHtml.match(
      /<input\b(?=[^>]*\bid=["']credential-value["'])(?=[^>]*\btype=["']password["'])(?=[^>]*\bautocomplete=["']off["'])[^>]*>/i
    );
    expect(credentialInput).not.toBeNull();
    expect(credentialInput?.[0]).not.toMatch(/\bvalue\s*=/i);
    const scripts = [
      ...credentialHtml.matchAll(
        /<script\b([^>]*)>([\s\S]*?)<\/script>/gi
      )
    ];
    expect(scripts).toHaveLength(1);
    expect(scripts[0]?.[1]).toMatch(
      /\bsrc=["']\.\/form\.js["']/
    );
    expect(scripts[0]?.[2]?.trim()).toBe("");
    expect(credentialHtml).toMatch(
      /<link\b(?=[^>]*\brel=["']stylesheet["'])(?=[^>]*\bhref=["']\.\/styles\.css["'])[^>]*>/i
    );

    for (const artifact of [
      credentialHtml,
      credentialForm,
      credentialStyles,
      credentialPreload
    ]) {
      expect(artifact).not.toContain("sourceMappingURL");
      expect(artifact).not.toContain(
        "credential-dist-private-sentinel"
      );
      expect(artifact).not.toContain("desktop-test-token-花");
      expect(artifact).not.toContain(
        "X-Kestrel-Desktop-Credential-Capability"
      );
    }
    for (const forbidden of [
      /\bfetch\s*\(/,
      /\bXMLHttpRequest\b/,
      /\bWebSocket\b/,
      /\blocalStorage\b/,
      /\bsessionStorage\b/,
      /\bindexedDB\b/,
      /\bconsole\./,
      /\brequire\s*\(/,
      /\bprocess\./
    ]) {
      expect(credentialForm).not.toMatch(forbidden);
    }

    const sandbox: {
      module: { exports: Record<string, unknown> };
      exports: Record<string, unknown>;
      TextEncoder: typeof TextEncoder;
      URL: typeof URL;
      __required?: string[];
      __exposed?: Array<[string, unknown]>;
    } = {
      module: { exports: {} },
      exports: {},
      TextEncoder,
      URL
    };
    runInNewContext(
      `
        globalThis.__required = [];
        globalThis.__exposed = [];
        globalThis.require = (name) => {
          globalThis.__required.push(name);
          if (name !== "electron") {
            throw new Error("unsupported preload require");
          }
          return {
            contextBridge: {
              exposeInMainWorld(name, value) {
                globalThis.__exposed.push([name, value]);
              }
            },
            ipcRenderer: {
              invoke: async () => ({
                ok: false,
                error: { code: "desktop_feature_unavailable" }
              }),
              sendSync: () => ({
                ok: true,
                value: {
                  marker: {
                    schema: "kestrel.desktop.runtime.v1",
                    baseUrl: "http://127.0.0.1:43123/",
                    generation: 7
                  }
                }
              }),
              on() { return this; },
              removeListener() { return this; }
            }
          };
        };
        ${preload}
      `,
      sandbox,
      { timeout: 5_000 }
    );
    const exposed = new Map(sandbox.__exposed);
    expect(sandbox.__required).toEqual(["electron"]);
    expect([...exposed.keys()].sort()).toEqual([
      "kestrelDesktop",
      "kestrelDesktopRuntime"
    ]);
    const bridge = exposed.get("kestrelDesktop") as Record<
      string,
      unknown
    >;
    expect(Reflect.ownKeys(bridge).sort()).toEqual([
      "chooseProjectFolder",
      "chooseStorageFolder",
      "connection",
      "exportSupportBundle",
      "getAppVersion",
      "getUpdateStatus",
      "openCredentialDialog",
      "openExternalUrl",
      "performRecoveryAction",
      "subscribeLifecycle",
      "subscribeUpdateStatus"
    ]);
    expect(Object.isFrozen(bridge)).toBe(true);
    expect(bridge).not.toHaveProperty("invoke");
    expect(bridge).not.toHaveProperty("ipcRenderer");
    expect(exposed.get("kestrelDesktopRuntime")).toEqual({
      schema: "kestrel.desktop.runtime.v1",
      baseUrl: "http://127.0.0.1:43123/",
      generation: 7
    });
    expect(Object.isFrozen(exposed.get("kestrelDesktopRuntime"))).toBe(
      true
    );

    const credentialSandbox: {
      module: { exports: Record<string, unknown> };
      exports: Record<string, unknown>;
      TextEncoder: typeof TextEncoder;
      __required?: string[];
      __exposed?: Array<[string, unknown]>;
    } = {
      module: { exports: {} },
      exports: {},
      TextEncoder
    };
    runInNewContext(
      `
        globalThis.__required = [];
        globalThis.__exposed = [];
        globalThis.require = (name) => {
          globalThis.__required.push(name);
          if (name !== "electron") {
            throw new Error("unsupported credential preload require");
          }
          return {
            contextBridge: {
              exposeInMainWorld(name, value) {
                globalThis.__exposed.push([name, value]);
              }
            },
            ipcRenderer: {
              invoke: async () => ({
                ok: false,
                error: { code: "desktop_feature_unavailable" }
              })
            }
          };
        };
        ${credentialPreload}
      `,
      credentialSandbox,
      { timeout: 5_000 }
    );
    const credentialExposed = new Map(
      credentialSandbox.__exposed
    );
    expect(credentialSandbox.__required).toEqual(["electron"]);
    expect([...credentialExposed.keys()]).toEqual([
      "kestrelCredential"
    ]);
    const credentialBridge = credentialExposed.get(
      "kestrelCredential"
    ) as Record<string, unknown>;
    expect(Reflect.ownKeys(credentialBridge).sort()).toEqual([
      "cancel",
      "getContext",
      "submit"
    ]);
    expect(Object.isFrozen(credentialBridge)).toBe(true);
    expect(credentialBridge).not.toHaveProperty("invoke");
    expect(credentialBridge).not.toHaveProperty("ipcRenderer");
    expect(credentialBridge).not.toHaveProperty(
      "openCredentialDialog"
    );
  });
});
