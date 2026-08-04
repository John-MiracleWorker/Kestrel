import { spawnSync } from "node:child_process";
import {
  copyFile,
  mkdir,
  rm
} from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { join } from "node:path";
import { build } from "esbuild";

const desktopRoot = fileURLToPath(new URL("..", import.meta.url));
const distRoot = join(desktopRoot, "dist");
const executable =
  process.platform === "win32" ? "tsc.cmd" : "tsc";
const tscPath = join(
  desktopRoot,
  "node_modules",
  ".bin",
  executable
);

await rm(distRoot, { force: true, recursive: true });
const compiled = spawnSync(
  tscPath,
  [
    "--project",
    join(desktopRoot, "tsconfig.build.json"),
    "--pretty",
    "false"
  ],
  {
    cwd: desktopRoot,
    stdio: "inherit"
  }
);
if (compiled.status !== 0) {
  process.exit(compiled.status ?? 1);
}

await Promise.all([
  build({
    entryPoints: [join(desktopRoot, "src", "preload.ts")],
    bundle: true,
    format: "cjs",
    platform: "browser",
    target: "es2022",
    external: ["electron"],
    outfile: join(distRoot, "preload.js"),
    logLevel: "error",
    legalComments: "none",
    sourcemap: false
  }),
  build({
    entryPoints: [
      join(desktopRoot, "src", "credential", "preload.ts")
    ],
    bundle: true,
    format: "cjs",
    platform: "browser",
    target: "es2022",
    external: ["electron"],
    outfile: join(
      distRoot,
      "credential",
      "preload.js"
    ),
    logLevel: "error",
    legalComments: "none",
    sourcemap: false
  }),
  build({
    entryPoints: [
      join(desktopRoot, "src", "credential", "form.ts")
    ],
    bundle: true,
    format: "esm",
    platform: "browser",
    target: "es2022",
    outfile: join(distRoot, "credential", "form.js"),
    logLevel: "error",
    legalComments: "none",
    sourcemap: false
  })
]);

await mkdir(join(distRoot, "credential"), {
  recursive: true
});
await Promise.all(
  ["index.html", "styles.css"].map((name) =>
    copyFile(
      join(desktopRoot, "src", "credential", name),
      join(distRoot, "credential", name)
    )
  )
);
