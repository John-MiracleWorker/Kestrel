import { spawnSync } from "node:child_process";
import { existsSync, readdirSync, readFileSync, rmSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const workspace = fileURLToPath(new URL("..", import.meta.url));
const dist = fileURLToPath(new URL("../dist", import.meta.url));

describe("desktop build boundary", () => {
  it("emits the reviewed Electron main process without a preload or renderer assets", () => {
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
      "main/window.js"
    ]);

    const packageJson = JSON.parse(
      readFileSync(new URL("../package.json", import.meta.url), "utf8")
    ) as { main?: string };
    expect(packageJson.main).toBe("dist/main.js");
    expect(existsSync(fileURLToPath(new URL("../dist/main.js", import.meta.url)))).toBe(
      true
    );
    expect(
      readdirSync(dist, { recursive: true }).some((entry) =>
        String(entry).includes("preload")
      )
    ).toBe(false);
  });
});
