import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const BUILDER_NAME = "electron-builder";
const BUILDER_VERSION = "26.15.3";
const REVIEWED_ADVISORY_LABEL = "GHSA-mh99-v99m-4gvg / CVE-2026-14257";
const MAX_AUDIT_OUTPUT_BYTES = 16 * 1024 * 1024;
const SCRIPT_DIRECTORY = dirname(fileURLToPath(import.meta.url));
const DESKTOP_DIRECTORY = resolve(SCRIPT_DIRECTORY, "..");

// This exception is valid only for the developer-only directory build whose
// inputs are exact-clean Git source, a literal reviewed builder config, and a
// bounded verified local resource stage. Any broader input or release path
// requires a new review instead of widening this allowlist.
const REVIEWED_ADVISORY = Object.freeze({
  source: 1124334,
  name: "brace-expansion",
  dependency: "brace-expansion",
  title:
    "brace-expansion: DoS via unbounded expansion length causing an out-of-memory process crash",
  url: "https://github.com/advisories/GHSA-mh99-v99m-4gvg",
  severity: "high",
  cwe: ["CWE-400", "CWE-770"],
  cvss: {
    score: 7.5,
    vectorString: "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:H"
  },
  range: "<=5.0.7"
});

function fail(message) {
  throw new Error(`desktop dependency audit rejected: ${message}`);
}

function isPlainObject(value) {
  return (
    value !== null &&
    typeof value === "object" &&
    !Array.isArray(value) &&
    (Object.getPrototypeOf(value) === Object.prototype ||
      Object.getPrototypeOf(value) === null)
  );
}

function requireObject(value, label) {
  if (!isPlainObject(value)) {
    fail(`${label} must be an object`);
  }
  return value;
}

function requireString(value, label) {
  if (typeof value !== "string" || value.length === 0) {
    fail(`${label} must be a non-empty string`);
  }
  return value;
}

function requireStringArray(value, label) {
  if (
    !Array.isArray(value) ||
    value.length === 0 ||
    value.some((entry) => typeof entry !== "string" || entry.length === 0) ||
    new Set(value).size !== value.length
  ) {
    fail(`${label} must be a non-empty array of unique strings`);
  }
  return value;
}

function canonicalJson(value) {
  if (Array.isArray(value)) {
    return `[${value.map((entry) => canonicalJson(entry)).join(",")}]`;
  }
  if (isPlainObject(value)) {
    return `{${Object.keys(value)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`)
      .join(",")}}`;
  }
  return JSON.stringify(value);
}

function requireExactObject(actual, expected, label) {
  if (canonicalJson(actual) !== canonicalJson(expected)) {
    fail(`${label} does not match the exact reviewed advisory`);
  }
}

function vulnerabilityMetadata(report, label) {
  const root = requireObject(report, label);
  if (root.auditReportVersion !== 2) {
    fail(`${label}.auditReportVersion must be 2`);
  }
  const vulnerabilities = requireObject(root.vulnerabilities, `${label}.vulnerabilities`);
  const metadata = requireObject(root.metadata, `${label}.metadata`);
  const counts = requireObject(
    metadata.vulnerabilities,
    `${label}.metadata.vulnerabilities`
  );
  const countNames = ["info", "low", "moderate", "high", "critical", "total"];
  for (const countName of countNames) {
    const count = counts[countName];
    if (!Number.isSafeInteger(count) || count < 0) {
      fail(`${label}.metadata.vulnerabilities.${countName} must be a non-negative integer`);
    }
  }
  const severityTotal =
    counts.info + counts.low + counts.moderate + counts.high + counts.critical;
  if (counts.total !== severityTotal) {
    fail(`${label} vulnerability severity totals are inconsistent`);
  }
  if (counts.total !== Object.keys(vulnerabilities).length) {
    fail(`${label} vulnerability total does not match its vulnerability entries`);
  }
  return { counts, vulnerabilities };
}

function requireCleanAudit(report, label) {
  const { counts, vulnerabilities } = vulnerabilityMetadata(report, label);
  if (counts.total !== 0 || Object.keys(vulnerabilities).length !== 0) {
    fail(`${label} must be clean`);
  }
}

function dependencyMap(metadata, field) {
  const value = metadata[field];
  if (value === undefined) {
    return {};
  }
  const dependencies = requireObject(value, `package metadata ${field}`);
  for (const [name, version] of Object.entries(dependencies)) {
    requireString(name, `package metadata ${field} name`);
    requireString(version, `package metadata ${field}.${name}`);
  }
  return dependencies;
}

function requireMatchingDependencyMap(packageMap, lockMap, label) {
  if (canonicalJson(packageMap) !== canonicalJson(lockMap)) {
    fail(`package.json and package-lock.json ${label} must match exactly`);
  }
}

function resolveInstalledDependency(packages, fromPath, dependencyName) {
  let candidateParent = fromPath;
  while (true) {
    const candidate = candidateParent
      ? `${candidateParent}/node_modules/${dependencyName}`
      : `node_modules/${dependencyName}`;
    if (Object.hasOwn(packages, candidate)) {
      return candidate;
    }
    if (candidateParent.length === 0) {
      return null;
    }
    const nestedBoundary = candidateParent.lastIndexOf("/node_modules/");
    candidateParent =
      nestedBoundary === -1 ? "" : candidateParent.slice(0, nestedBoundary);
  }
}

function installedClosure(packages, rootPath) {
  if (!Object.hasOwn(packages, rootPath)) {
    fail(`direct dependency root ${rootPath} is missing from package-lock.json`);
  }
  const seen = new Set([rootPath]);
  const pending = [rootPath];
  while (pending.length > 0) {
    const currentPath = pending.shift();
    const current = requireObject(packages[currentPath], `lock package ${currentPath}`);
    const dependencyNames = new Set();
    for (const field of ["dependencies", "optionalDependencies", "peerDependencies"]) {
      const dependencies =
        current[field] === undefined
          ? {}
          : requireObject(current[field], `lock package ${currentPath}.${field}`);
      for (const dependencyName of Object.keys(dependencies)) {
        dependencyNames.add(dependencyName);
      }
    }
    for (const dependencyName of [...dependencyNames].sort()) {
      const installedPath = resolveInstalledDependency(
        packages,
        currentPath,
        dependencyName
      );
      if (installedPath === null || seen.has(installedPath)) {
        continue;
      }
      seen.add(installedPath);
      pending.push(installedPath);
    }
  }
  return seen;
}

function requireExactBuilderRoot(packageMetadata, lockMetadata) {
  const packageJson = requireObject(packageMetadata, "package.json");
  const lock = requireObject(lockMetadata, "package-lock.json");
  if (Object.hasOwn(packageJson, "overrides")) {
    fail("package.json overrides are forbidden for the reviewed builder graph");
  }
  const packageProduction = dependencyMap(packageJson, "dependencies");
  const packageOptional = dependencyMap(packageJson, "optionalDependencies");
  const packageDevelopment = dependencyMap(packageJson, "devDependencies");
  if (
    Object.hasOwn(packageProduction, BUILDER_NAME) ||
    Object.hasOwn(packageOptional, BUILDER_NAME)
  ) {
    fail(`${BUILDER_NAME} must not be a production dependency`);
  }
  if (packageDevelopment[BUILDER_NAME] !== BUILDER_VERSION) {
    fail(`${BUILDER_NAME} must be pinned exactly to ${BUILDER_VERSION}`);
  }

  if (lock.lockfileVersion !== 3) {
    fail("package-lock.json lockfileVersion must be 3");
  }
  const packages = requireObject(lock.packages, "package-lock.json packages");
  const lockRoot = requireObject(packages[""], "package-lock.json root package");
  const lockProduction = dependencyMap(lockRoot, "dependencies");
  const lockOptional = dependencyMap(lockRoot, "optionalDependencies");
  const lockDevelopment = dependencyMap(lockRoot, "devDependencies");
  requireMatchingDependencyMap(packageProduction, lockProduction, "dependencies");
  requireMatchingDependencyMap(packageOptional, lockOptional, "optionalDependencies");
  requireMatchingDependencyMap(packageDevelopment, lockDevelopment, "devDependencies");

  const builderPath = `node_modules/${BUILDER_NAME}`;
  const builderPackage = requireObject(packages[builderPath], `lock package ${builderPath}`);
  if (
    builderPackage.version !== BUILDER_VERSION ||
    builderPackage.dev !== true ||
    builderPackage.optional === true
  ) {
    fail(
      `${builderPath} must be the non-optional dev-only package ${BUILDER_VERSION}`
    );
  }

  const directRootNames = [
    ...new Set([
      ...Object.keys(packageProduction),
      ...Object.keys(packageOptional),
      ...Object.keys(packageDevelopment)
    ])
  ].sort();
  const rootClosures = new Map(
    directRootNames.map((rootName) => [
      rootName,
      installedClosure(packages, `node_modules/${rootName}`)
    ])
  );
  return { builderPath, packages, rootClosures };
}

function collectAdvisoryLeaves(vulnerabilities, vulnerabilityName, visiting = new Set()) {
  if (visiting.has(vulnerabilityName)) {
    return [];
  }
  const vulnerability = requireObject(
    vulnerabilities[vulnerabilityName],
    `vulnerability ${vulnerabilityName}`
  );
  if (!Array.isArray(vulnerability.via) || vulnerability.via.length === 0) {
    fail(`vulnerability ${vulnerabilityName}.via must be non-empty`);
  }
  const nextVisiting = new Set(visiting);
  nextVisiting.add(vulnerabilityName);
  const leaves = [];
  for (const via of vulnerability.via) {
    if (typeof via === "string") {
      if (!Object.hasOwn(vulnerabilities, via)) {
        fail(`vulnerability ${vulnerabilityName} references missing path ${via}`);
      }
      leaves.push(...collectAdvisoryLeaves(vulnerabilities, via, nextVisiting));
    } else {
      const advisory = requireObject(via, `vulnerability ${vulnerabilityName} advisory`);
      requireExactObject(advisory, REVIEWED_ADVISORY, "unreviewed advisory");
      leaves.push(advisory);
    }
  }
  return leaves;
}

function validateReviewedFamily(allReport, packageMetadata, lockMetadata) {
  const { counts, vulnerabilities } = vulnerabilityMetadata(
    allReport,
    "all-dependency audit"
  );
  const vulnerabilityNames = Object.keys(vulnerabilities).sort();
  if (vulnerabilityNames.length === 0) {
    return {
      exceptionUsed: false,
      reviewedAdvisory: null,
      vulnerabilityCount: 0
    };
  }
  if (
    counts.high !== vulnerabilityNames.length ||
    counts.info !== 0 ||
    counts.low !== 0 ||
    counts.moderate !== 0 ||
    counts.critical !== 0
  ) {
    fail("all-dependency audit may contain only high-severity reviewed paths");
  }

  const { builderPath, packages, rootClosures } = requireExactBuilderRoot(
    packageMetadata,
    lockMetadata
  );
  const builderClosure = rootClosures.get(BUILDER_NAME);
  if (builderClosure === undefined) {
    fail(`${BUILDER_NAME} direct dependency closure is missing`);
  }

  for (const vulnerabilityName of vulnerabilityNames) {
    const vulnerability = requireObject(
      vulnerabilities[vulnerabilityName],
      `vulnerability ${vulnerabilityName}`
    );
    if (
      vulnerability.name !== vulnerabilityName ||
      vulnerability.severity !== "high" ||
      typeof vulnerability.isDirect !== "boolean"
    ) {
      fail(
        `vulnerability ${vulnerabilityName} must retain its exact name and high severity`
      );
    }
    if (vulnerability.isDirect !== (vulnerabilityName === BUILDER_NAME)) {
      fail(
        `only the exact ${BUILDER_NAME} root may be a direct vulnerable dependency`
      );
    }
    const nodes = requireStringArray(
      vulnerability.nodes,
      `vulnerability ${vulnerabilityName}.nodes`
    );
    const leaves = collectAdvisoryLeaves(vulnerabilities, vulnerabilityName);
    if (leaves.length === 0) {
      fail(`vulnerability ${vulnerabilityName} has no reviewed advisory leaf`);
    }
    for (const nodePath of nodes) {
      const lockPackage = requireObject(
        packages[nodePath],
        `vulnerable lock package ${nodePath}`
      );
      if (lockPackage.dev !== true) {
        fail(`vulnerable lock package ${nodePath} is not dev-only`);
      }
      if (!builderClosure.has(nodePath)) {
        fail(`vulnerable lock package ${nodePath} is outside the builder closure`);
      }
      for (const [rootName, closure] of rootClosures) {
        if (rootName !== BUILDER_NAME && closure.has(nodePath)) {
          fail(
            `vulnerable lock package ${nodePath} is also reachable from direct root ${rootName}`
          );
        }
      }
    }
  }
  if (
    !Object.hasOwn(vulnerabilities, BUILDER_NAME) ||
    !vulnerabilities[BUILDER_NAME].nodes.includes(builderPath)
  ) {
    fail(`reviewed family must terminate at the exact direct ${BUILDER_NAME} root`);
  }

  return {
    exceptionUsed: true,
    reviewedAdvisory: REVIEWED_ADVISORY_LABEL,
    vulnerabilityCount: vulnerabilityNames.length
  };
}

export function validateAuditContract({
  productionReport,
  allReport,
  packageMetadata,
  lockMetadata
}) {
  requireCleanAudit(productionReport, "production audit");
  return validateReviewedFamily(allReport, packageMetadata, lockMetadata);
}

function runNpmAudit(argumentsList, label) {
  const npmExecutable = process.platform === "win32" ? "npm.cmd" : "npm";
  const result = spawnSync(npmExecutable, ["audit", "--json", ...argumentsList], {
    cwd: DESKTOP_DIRECTORY,
    encoding: "utf8",
    env: process.env,
    maxBuffer: MAX_AUDIT_OUTPUT_BYTES,
    shell: false,
    windowsHide: true
  });
  if (result.error) {
    fail(`${label} could not run npm audit: ${result.error.message}`);
  }
  if (result.signal !== null) {
    fail(`${label} npm audit was terminated by ${result.signal}`);
  }
  if (result.status !== 0 && result.status !== 1) {
    const detail = String(result.stderr ?? "").trim().slice(0, 1_000);
    fail(`${label} npm audit exited ${result.status}${detail ? `: ${detail}` : ""}`);
  }
  try {
    return {
      report: JSON.parse(result.stdout),
      status: result.status
    };
  } catch (error) {
    fail(`${label} npm audit returned invalid JSON: ${error.message}`);
  }
}

function readMetadata(filename) {
  const path = join(DESKTOP_DIRECTORY, filename);
  const source = readFileSync(path, { encoding: "utf8", flag: "r" });
  if (Buffer.byteLength(source, "utf8") > MAX_AUDIT_OUTPUT_BYTES) {
    fail(`${filename} exceeds the audit input limit`);
  }
  try {
    return JSON.parse(source);
  } catch (error) {
    fail(`${filename} is invalid JSON: ${error.message}`);
  }
}

function main() {
  const production = runNpmAudit(["--omit=dev"], "production audit");
  if (production.status !== 0) {
    fail("production audit must exit cleanly");
  }
  const allDependencies = runNpmAudit([], "all-dependency audit");
  const result = validateAuditContract({
    productionReport: production.report,
    allReport: allDependencies.report,
    packageMetadata: readMetadata("package.json"),
    lockMetadata: readMetadata("package-lock.json")
  });
  if (result.exceptionUsed) {
    console.warn(
      `REVIEWED DEVELOPMENT-ONLY EXCEPTION ACTIVE: ${result.reviewedAdvisory}; ` +
        `${result.vulnerabilityCount} npm audit paths are confined exclusively to ` +
        `${BUILDER_NAME}@${BUILDER_VERSION}. Production audit: 0.`
    );
  } else {
    console.log("Desktop production and all-dependency npm audits are clean.");
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    main();
  } catch (error) {
    console.error(error instanceof Error ? error.message : String(error));
    process.exitCode = 1;
  }
}
