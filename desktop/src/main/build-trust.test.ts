import { describe, expect, it } from "vitest";
import {
  parsePackagedBuildTrust,
  type PackagedBuildRuntime
} from "./build-trust";

const runtime: PackagedBuildRuntime = Object.freeze({
  appVersion: "0.5.0",
  platform: "darwin",
  architecture: "arm64"
});

function packageBytes(
  buildMode: "developer" | "release",
  keyId: "developer" | "release" = buildMode
): Buffer {
  return Buffer.from(
    JSON.stringify({
      name: "kestrel-desktop",
      version: "0.5.0",
      kestrelDesktopBuild: {
        schema: "kestrel.desktop.packaged-build.v1",
        build_mode: buildMode,
        key_id: keyId,
        source_commit: "a".repeat(40),
        app_version: "0.5.0",
        platform: "darwin",
        architecture: "arm64",
        python_lock_sha256: "1".repeat(64),
        desktop_npm_lock_sha256: "2".repeat(64),
        web_npm_lock_sha256: "3".repeat(64),
        sbom_sha256: "4".repeat(64)
      }
    }),
    "utf8"
  );
}

describe("immutable packaged build trust", () => {
  it("projects exact manifest identity from packaged metadata", () => {
    const trust = parsePackagedBuildTrust(
      packageBytes("developer"),
      runtime
    );

    expect(trust).toEqual({
      buildMode: "developer",
      keyId: "developer",
      sourceCommit: "a".repeat(40),
      appVersion: "0.5.0",
      platform: "darwin",
      architecture: "arm64",
      pythonLockSha256: "1".repeat(64),
      desktopNpmLockSha256: "2".repeat(64),
      webNpmLockSha256: "3".repeat(64),
      sbomSha256: "4".repeat(64)
    });
    expect(Object.isFrozen(trust)).toBe(true);
  });

  it("fails closed when packaged metadata is absent or runtime identity drifts", () => {
    expect(() =>
      parsePackagedBuildTrust(
        Buffer.from(
          JSON.stringify({
            name: "kestrel-desktop",
            version: "0.5.0"
          })
        ),
        runtime
      )
    ).toThrow("desktop_build_metadata_unavailable");

    expect(() =>
      parsePackagedBuildTrust(packageBytes("release"), {
        ...runtime,
        architecture: "x64"
      })
    ).toThrow("desktop_build_runtime_mismatch");
  });

  it("rejects mode-key mismatch and accessor-free JSON ignores environment and argv", () => {
    const previousEnvironment = process.env.KESTREL_DESKTOP_BUILD_MODE;
    const previousArguments = [...process.argv];
    process.env.KESTREL_DESKTOP_BUILD_MODE = "developer";
    process.argv.push("--kestrel-desktop-build-mode=developer");
    try {
      expect(() =>
        parsePackagedBuildTrust(
          packageBytes("release", "developer"),
          runtime
        )
      ).toThrow("desktop_build_mode_key_mismatch");
      expect(
        parsePackagedBuildTrust(packageBytes("release"), runtime)
      ).toMatchObject({
        buildMode: "release",
        keyId: "release"
      });
    } finally {
      process.argv = previousArguments;
      if (previousEnvironment === undefined) {
        delete process.env.KESTREL_DESKTOP_BUILD_MODE;
      } else {
        process.env.KESTREL_DESKTOP_BUILD_MODE = previousEnvironment;
      }
    }
  });

  it("rejects unknown metadata fields and oversized package input", () => {
    const packageJson = JSON.parse(
      packageBytes("release").toString("utf8")
    ) as Record<string, unknown>;
    (
      packageJson.kestrelDesktopBuild as Record<string, unknown>
    ).unexpected = true;

    expect(() =>
      parsePackagedBuildTrust(
        Buffer.from(JSON.stringify(packageJson)),
        runtime
      )
    ).toThrow("desktop_build_metadata_invalid");
    expect(() =>
      parsePackagedBuildTrust(Buffer.alloc(65 * 1024, 0x20), runtime)
    ).toThrow("desktop_build_metadata_too_large");
  });
});
