#!/usr/bin/env node

import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  mkdirSync,
  readFileSync,
  readdirSync,
  realpathSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { dirname, isAbsolute, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url));
const REPOSITORY_ROOT = resolve(SCRIPT_DIR, "..");
const WEB_DIR = join(REPOSITORY_ROOT, "web");
const NODE_MODULES_DIR = realpathSync(join(WEB_DIR, "node_modules"));
const DEFAULT_OUTPUT = join(WEB_DIR, "public", "THIRD_PARTY_NOTICES.txt");
const LICENSE_NAME = /^(licen[cs]e|copying|copyright)(?:[-._].*)?$/i;
const GOOGLE_FONTS_COMMIT = "7ff85c87f93ea6cca5f41c69f2e4edcb90240f26";
const BUNDLED_ASSETS = [
  {
    name: "Fraunces Latin variable font",
    path: join(WEB_DIR, "public", "fonts", "fraunces-latin-variable.woff2"),
    sha256: "48282a415ec22e31beaf0a0666e6fae0c8cbddcd0b1f6e729f27c3ade8a64e43",
    source:
      "https://fonts.gstatic.com/s/fraunces/v38/6NU78FyLNQOQZAnv9bYEvDiIdE9Ea92uemAk_WBq8U_9v0c2Wa0KxC9TeP2Xz5c.woff2",
    upstream: "https://github.com/undercasetype/Fraunces",
    license: "SIL Open Font License 1.1",
    licensePath: join(WEB_DIR, "public", "fonts", "OFL-Fraunces.txt"),
    licenseSha256:
      "6732d6cc72c5d09292ff754dc1f39d9ea14918f74e87a17afa3f00a5120c3d48",
  },
  {
    name: "Atkinson Hyperlegible Next Latin variable font",
    path: join(
      WEB_DIR,
      "public",
      "fonts",
      "atkinson-hyperlegible-next-latin-variable.woff2",
    ),
    sha256: "1e4cea71d75ec427581d6259fc07148a2e60d60d16cabf4b4f5360487b3f9dc3",
    source:
      "https://fonts.gstatic.com/s/atkinsonhyperlegiblenext/v7/NaPNcYPdHfdVxJw0IfIP0lvYFqijb-UxCtm5_wdGseiJn3q0pkZ_.woff2",
    upstream: "https://github.com/googlefonts/atkinson-hyperlegible",
    license: "SIL Open Font License 1.1",
    licensePath: join(
      WEB_DIR,
      "public",
      "fonts",
      "OFL-Atkinson-Hyperlegible-Next.txt",
    ),
    licenseSha256:
      "09636801ed3e868736cc359bb1c819c5ef76529cbb41473cb1f602ef166dad0a",
  },
];

const args = process.argv.slice(2);
const checkOnly = args.includes("--check");
const outputFlag = args.indexOf("--output");
if (args.some((arg, index) => arg.startsWith("--") && arg !== "--check" && index !== outputFlag)) {
  throw new Error("usage: generate-web-third-party-notices.mjs [--check] [--output PATH]");
}
if (outputFlag !== -1 && !args[outputFlag + 1]) {
  throw new Error("--output requires a path");
}
const outputPath = outputFlag === -1
  ? DEFAULT_OUTPUT
  : resolve(REPOSITORY_ROOT, args[outputFlag + 1]);

const dependencyTree = JSON.parse(
  execFileSync(
    "npm",
    ["ls", "--prefix", WEB_DIR, "--omit=dev", "--all", "--json", "--long"],
    { encoding: "utf8", maxBuffer: 32 * 1024 * 1024 },
  ),
);

const packages = new Map();

function packageLicenseFiles(packagePath) {
  return readdirSync(packagePath)
    .filter((name) => LICENSE_NAME.test(name))
    .filter((name) => statSync(join(packagePath, name)).isFile())
    .sort((left, right) => left.localeCompare(right));
}

function visitDependencies(dependencies) {
  for (const dependency of Object.values(dependencies ?? {})) {
    if (!dependency || typeof dependency !== "object") {
      throw new Error("npm returned an invalid production dependency node");
    }
    const { name, version, path } = dependency;
    if (typeof name !== "string" || typeof version !== "string" || typeof path !== "string") {
      throw new Error("npm dependency is missing name, version, or path");
    }
    const packagePath = realpathSync(path);
    const packageRelative = relative(NODE_MODULES_DIR, packagePath);
    if (
      !packageRelative
      || packageRelative === ".."
      || packageRelative.startsWith(`..${sep}`)
      || isAbsolute(packageRelative)
    ) {
      throw new Error(`dependency path escapes web/node_modules: ${name}@${version}`);
    }
    const packageJson = JSON.parse(readFileSync(join(packagePath, "package.json"), "utf8"));
    if (packageJson.name !== name || packageJson.version !== version) {
      throw new Error(`npm/package.json identity mismatch for ${name}@${version}`);
    }
    const declaredLicense = packageJson.license ?? dependency.license;
    if (typeof declaredLicense !== "string" || !declaredLicense.trim()) {
      throw new Error(`missing declared license for ${name}@${version}`);
    }
    const licenseFiles = packageLicenseFiles(packagePath);
    if (licenseFiles.length === 0) {
      throw new Error(`missing full license text for ${name}@${version}`);
    }
    const licenseTexts = licenseFiles.map((licenseFile) => ({
      name: licenseFile,
      text: readFileSync(join(packagePath, licenseFile), "utf8")
        .replaceAll("\r\n", "\n")
        .trimEnd(),
    }));
    if (licenseTexts.some(({ text }) => !text)) {
      throw new Error(`empty license text for ${name}@${version}`);
    }
    const key = `${name}@${version}`;
    const candidate = {
      name,
      version,
      declaredLicense: declaredLicense.trim(),
      licenseTexts,
    };
    const existing = packages.get(key);
    if (existing && JSON.stringify(existing) !== JSON.stringify(candidate)) {
      throw new Error(`inconsistent duplicate license material for ${key}`);
    }
    packages.set(key, candidate);
    visitDependencies(dependency.dependencies);
  }
}

visitDependencies(dependencyTree.dependencies);
if (packages.size === 0) {
  throw new Error("no production web dependencies were discovered");
}

function sha256(contents) {
  return createHash("sha256").update(contents).digest("hex");
}

const assetSections = BUNDLED_ASSETS.map((asset) => {
  const contents = readFileSync(asset.path);
  const digest = sha256(contents);
  if (digest !== asset.sha256) {
    throw new Error(
      `bundled asset hash mismatch for ${relative(REPOSITORY_ROOT, asset.path)}: ${digest}`,
    );
  }
  const licenseContents = readFileSync(asset.licensePath);
  const licenseDigest = sha256(licenseContents);
  if (licenseDigest !== asset.licenseSha256) {
    throw new Error(
      `font license hash mismatch for ${relative(REPOSITORY_ROOT, asset.licensePath)}: ${licenseDigest}`,
    );
  }
  const licenseText = licenseContents.toString("utf8").replaceAll("\r\n", "\n").trimEnd();
  if (!licenseText) {
    throw new Error(`empty font license text for ${asset.name}`);
  }
  return [
    "=".repeat(79),
    asset.name,
    `Bundled file: ${relative(REPOSITORY_ROOT, asset.path)}`,
    `SHA-256: ${asset.sha256}`,
    `Source: ${asset.source}`,
    `Upstream: ${asset.upstream}`,
    `Google Fonts source commit: ${GOOGLE_FONTS_COMMIT}`,
    `Declared license: ${asset.license}`,
    `License file: ${relative(REPOSITORY_ROOT, asset.licensePath)}`,
    "-".repeat(79),
    licenseText,
  ].join("\n");
});

const packageSections = [...packages.values()]
  .sort((left, right) =>
    left.name.localeCompare(right.name) || left.version.localeCompare(right.version),
  )
  .map((entry) => {
    const texts = entry.licenseTexts
      .map(({ name, text }) => `License file: ${name}\n\n${text}`)
      .join("\n\n");
    return [
      "=".repeat(79),
      `${entry.name}@${entry.version}`,
      `Declared license: ${entry.declaredLicense}`,
      "-".repeat(79),
      texts,
    ].join("\n");
  });

const generated = [
  "Kestrel Web Workbench - Third-Party Notices",
  "",
  "This file is generated from the exact production dependency graph in",
  "web/package-lock.json. It contains the complete license files distributed by",
  "every JavaScript package and local font asset bundled into the Kestrel web",
  "workbench. Asset hashes are checked before this notice can be generated.",
  "",
  `Production packages: ${packages.size}`,
  `Bundled font assets: ${BUNDLED_ASSETS.length}`,
  "",
  ...assetSections,
  ...packageSections,
  "",
].join("\n");

if (checkOnly) {
  let existing;
  try {
    existing = readFileSync(outputPath, "utf8");
  } catch {
    throw new Error(`third-party notice is missing: ${relative(REPOSITORY_ROOT, outputPath)}`);
  }
  if (existing !== generated) {
    throw new Error(
      `third-party notice is stale: run node scripts/generate-web-third-party-notices.mjs`,
    );
  }
  console.log(
    `verified ${relative(REPOSITORY_ROOT, outputPath)} (${packages.size} packages, ${BUNDLED_ASSETS.length} font assets)`,
  );
} else {
  mkdirSync(dirname(outputPath), { recursive: true });
  writeFileSync(outputPath, generated, "utf8");
  console.log(
    `wrote ${relative(REPOSITORY_ROOT, outputPath)} (${packages.size} packages, ${BUNDLED_ASSETS.length} font assets)`,
  );
}
