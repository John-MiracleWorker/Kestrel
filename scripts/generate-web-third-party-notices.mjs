#!/usr/bin/env node

import { execFileSync } from "node:child_process";
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

const sections = [...packages.values()]
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
  "every JavaScript package bundled into the Kestrel web workbench.",
  "",
  `Production packages: ${packages.size}`,
  "",
  ...sections,
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
  console.log(`verified ${relative(REPOSITORY_ROOT, outputPath)} (${packages.size} packages)`);
} else {
  mkdirSync(dirname(outputPath), { recursive: true });
  writeFileSync(outputPath, generated, "utf8");
  console.log(`wrote ${relative(REPOSITORY_ROOT, outputPath)} (${packages.size} packages)`);
}
