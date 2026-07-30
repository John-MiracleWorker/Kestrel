import { spawnSync } from "node:child_process";
import { existsSync, readdirSync, readFileSync, rmSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { runInNewContext } from "node:vm";
import { describe, expect, it } from "vitest";

const workspace = fileURLToPath(new URL("..", import.meta.url));
const dist = fileURLToPath(new URL("../dist", import.meta.url));

describe("desktop build boundary", () => {
  it("emits one sandbox-compatible preload and reviewed main modules only", () => {
    rmSync(dist, { force: true, recursive: true });

    const build = spawnSync("npm", ["run", "build"], {
      cwd: workspace,
      encoding: "utf8"
    });

    expect(build.status, `${build.stdout}\n${build.stderr}`).toBe(0);
    expect(existsSync(dist)).toBe(true);
    expect(readdirSync(dist, { recursive: true }).sort()).toEqual([
      "contracts.d.ts",
      "contracts.js",
      "main",
      "main.d.ts",
      "main.js",
      "main/api-session.d.ts",
      "main/api-session.js",
      "main/ipc.d.ts",
      "main/ipc.js",
      "main/private-files.d.ts",
      "main/private-files.js",
      "main/protocol.d.ts",
      "main/protocol.js",
      "main/resource-manifest.d.ts",
      "main/resource-manifest.js",
      "main/security.d.ts",
      "main/security.js",
      "main/sidecar-supervisor.d.ts",
      "main/sidecar-supervisor.js",
      "main/window.d.ts",
      "main/window.js",
      "preload.d.ts",
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
  });
});
