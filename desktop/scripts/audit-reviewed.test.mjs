import { describe, expect, it } from "vitest";

import { validateAuditContract } from "./audit-reviewed.mjs";

const ADVISORY = Object.freeze({
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

function zeroReport() {
  return {
    auditReportVersion: 2,
    vulnerabilities: {},
    metadata: {
      vulnerabilities: {
        info: 0,
        low: 0,
        moderate: 0,
        high: 0,
        critical: 0,
        total: 0
      }
    }
  };
}

function packageMetadata() {
  return {
    name: "kestrel-desktop",
    version: "0.5.0",
    private: true,
    dependencies: {},
    devDependencies: {
      "electron-builder": "26.15.3"
    }
  };
}

function lockMetadata() {
  return {
    name: "kestrel-desktop",
    version: "0.5.0",
    lockfileVersion: 3,
    requires: true,
    packages: {
      "": {
        name: "kestrel-desktop",
        version: "0.5.0",
        dependencies: {},
        devDependencies: {
          "electron-builder": "26.15.3"
        }
      },
      "node_modules/electron-builder": {
        version: "26.15.3",
        dev: true,
        dependencies: {
          minimatch: "10.0.3"
        }
      },
      "node_modules/minimatch": {
        version: "10.0.3",
        dev: true,
        dependencies: {
          "brace-expansion": "2.0.2"
        }
      },
      "node_modules/brace-expansion": {
        version: "2.0.2",
        dev: true
      }
    }
  };
}

function reviewedReport() {
  return {
    auditReportVersion: 2,
    vulnerabilities: {
      "brace-expansion": {
        name: "brace-expansion",
        severity: "high",
        isDirect: false,
        via: [{ ...ADVISORY }],
        effects: ["minimatch"],
        range: "<=5.0.7",
        nodes: ["node_modules/brace-expansion"],
        fixAvailable: false
      },
      minimatch: {
        name: "minimatch",
        severity: "high",
        isDirect: false,
        via: ["brace-expansion"],
        effects: ["electron-builder"],
        range: "1.0.0 - 10.0.3",
        nodes: ["node_modules/minimatch"],
        fixAvailable: false
      },
      "electron-builder": {
        name: "electron-builder",
        severity: "high",
        isDirect: true,
        via: ["minimatch"],
        effects: [],
        range: "26.15.3",
        nodes: ["node_modules/electron-builder"],
        fixAvailable: false
      }
    },
    metadata: {
      vulnerabilities: {
        info: 0,
        low: 0,
        moderate: 0,
        high: 3,
        critical: 0,
        total: 3
      }
    }
  };
}

function validate(overrides = {}) {
  return validateAuditContract({
    productionReport: zeroReport(),
    allReport: reviewedReport(),
    packageMetadata: packageMetadata(),
    lockMetadata: lockMetadata(),
    ...overrides
  });
}

describe("reviewed desktop dependency audit contract", () => {
  it("accepts a completely clean production and all-dependency audit", () => {
    const result = validate({ allReport: zeroReport() });

    expect(result).toEqual({
      exceptionUsed: false,
      reviewedAdvisory: null,
      vulnerabilityCount: 0
    });
  });

  it("accepts only the reviewed advisory family inside the exact builder closure", () => {
    const result = validate();

    expect(result).toEqual({
      exceptionUsed: true,
      reviewedAdvisory: "GHSA-mh99-v99m-4gvg / CVE-2026-14257",
      vulnerabilityCount: 3
    });
  });

  it("accepts npm propagation cycles when every path family still resolves to the reviewed leaf", () => {
    const allReport = reviewedReport();
    allReport.vulnerabilities.minimatch.via.push("electron-builder");

    expect(validate({ allReport })).toEqual({
      exceptionUsed: true,
      reviewedAdvisory: "GHSA-mh99-v99m-4gvg / CVE-2026-14257",
      vulnerabilityCount: 3
    });
  });

  it("rejects any additional advisory leaf", () => {
    const allReport = reviewedReport();
    allReport.vulnerabilities.minimatch.via.push({
      ...ADVISORY,
      source: 9999999,
      name: "minimatch",
      dependency: "minimatch",
      title: "a second advisory",
      url: "https://github.com/advisories/GHSA-xxxx-yyyy-zzzz"
    });

    expect(() => validate({ allReport })).toThrow(/unreviewed advisory/i);
  });

  it("rejects a nonzero production audit or a production dependency path", () => {
    const productionReport = reviewedReport();
    expect(() => validate({ productionReport })).toThrow(/production audit must be clean/i);

    const packageJson = packageMetadata();
    packageJson.dependencies["electron-builder"] = "26.15.3";
    expect(() => validate({ packageMetadata: packageJson })).toThrow(
      /must not be a production dependency/i
    );

    const lock = lockMetadata();
    lock.packages["node_modules/brace-expansion"].dev = false;
    expect(() => validate({ lockMetadata: lock })).toThrow(/not dev-only/i);
  });

  it.each([
    ["pin", (context) => (context.packageMetadata.devDependencies["electron-builder"] = "^26.15.3")],
    [
      "url",
      (context) =>
        (context.allReport.vulnerabilities["brace-expansion"].via[0].url =
          "https://example.invalid/GHSA-mh99-v99m-4gvg")
    ],
    [
      "severity",
      (context) =>
        (context.allReport.vulnerabilities["brace-expansion"].via[0].severity = "moderate")
    ],
    [
      "name",
      (context) =>
        (context.allReport.vulnerabilities["brace-expansion"].via[0].name =
          "not-brace-expansion")
    ]
  ])("rejects altered reviewed %s metadata", (_field, mutate) => {
    const context = {
      packageMetadata: packageMetadata(),
      allReport: reviewedReport()
    };
    mutate(context);

    expect(() => validate(context)).toThrow();
  });

  it("rejects a vulnerable path reachable from another direct dependency root", () => {
    const packageJson = packageMetadata();
    packageJson.devDependencies["other-tool"] = "1.0.0";
    const lock = lockMetadata();
    lock.packages[""].devDependencies["other-tool"] = "1.0.0";
    lock.packages["node_modules/other-tool"] = {
      version: "1.0.0",
      dev: true,
      dependencies: {
        minimatch: "10.0.3"
      }
    };

    expect(() =>
      validate({
        packageMetadata: packageJson,
        lockMetadata: lock
      })
    ).toThrow(/also reachable from direct root other-tool/i);
  });
});
