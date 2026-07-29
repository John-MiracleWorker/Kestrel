import { spawnSync } from "node:child_process";
import { existsSync, readdirSync, rmSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const workspace = fileURLToPath(new URL("..", import.meta.url));
const dist = fileURLToPath(new URL("../dist", import.meta.url));

describe("desktop build boundary", () => {
  it("emits contracts and declarations without Electron entrypoints or renderer assets", () => {
    rmSync(dist, { force: true, recursive: true });

    const build = spawnSync("npm", ["run", "build"], {
      cwd: workspace,
      encoding: "utf8"
    });

    expect(build.status, `${build.stdout}\n${build.stderr}`).toBe(0);
    expect(existsSync(dist)).toBe(true);
    expect(readdirSync(dist).sort()).toEqual(["contracts.d.ts", "contracts.js"]);
  });
});
