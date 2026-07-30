import { TextDecoder } from "node:util";
import { z } from "zod";

const MAX_PACKAGED_METADATA_BYTES = 64 * 1024;
const sha256Schema = z.string().regex(/^[0-9a-f]{64}$/);
const sourceCommitSchema = z.string().regex(/^[0-9a-f]{40}$/);
const appVersionSchema = z
  .string()
  .regex(/^[0-9A-Za-z][0-9A-Za-z.+-]{0,63}$/);
const buildModeSchema = z.enum(["developer", "release"]);
const smokeAuthoritySchema = z.enum([
  "developer_directory_smoke_v1",
  "disabled"
]);
const packagedBuildSchema = z
  .object({
    schema: z.literal("kestrel.desktop.packaged-build.v1"),
    build_mode: buildModeSchema,
    key_id: buildModeSchema,
    source_commit: sourceCommitSchema,
    app_version: appVersionSchema,
    platform: z.enum(["darwin", "linux", "win32"]),
    architecture: z.enum(["arm64", "x64"]),
    resource_root_relative: z.literal("kestrel"),
    smoke_authority: smokeAuthoritySchema,
    python_lock_sha256: sha256Schema,
    desktop_npm_lock_sha256: sha256Schema,
    web_npm_lock_sha256: sha256Schema,
    sbom_sha256: sha256Schema
  })
  .strict();
const packageSchema = z
  .object({
    version: appVersionSchema,
    kestrelDesktopBuild: packagedBuildSchema
  })
  .passthrough();

export interface PackagedBuildRuntime {
  appVersion: string;
  platform: NodeJS.Platform;
  architecture: string;
}

export interface PackagedBuildTrust {
  readonly buildMode: "developer" | "release";
  readonly keyId: "developer" | "release";
  readonly sourceCommit: string;
  readonly appVersion: string;
  readonly platform: "darwin" | "linux" | "win32";
  readonly architecture: "arm64" | "x64";
  readonly resourceRootRelative: "kestrel";
  readonly smokeAuthority:
    | "developer_directory_smoke_v1"
    | "disabled";
  readonly pythonLockSha256: string;
  readonly desktopNpmLockSha256: string;
  readonly webNpmLockSha256: string;
  readonly sbomSha256: string;
}

export function parsePackagedBuildTrust(
  packageBytes: Uint8Array,
  runtime: PackagedBuildRuntime
): PackagedBuildTrust {
  if (
    packageBytes.byteLength === 0 ||
    packageBytes.byteLength > MAX_PACKAGED_METADATA_BYTES
  ) {
    throw new Error("desktop_build_metadata_too_large");
  }
  let raw: unknown;
  try {
    const decoded = new TextDecoder("utf-8", { fatal: true }).decode(
      packageBytes
    );
    raw = JSON.parse(decoded);
  } catch {
    throw new Error("desktop_build_metadata_invalid");
  }
  if (
    raw === null ||
    typeof raw !== "object" ||
    !Object.hasOwn(raw, "kestrelDesktopBuild")
  ) {
    throw new Error("desktop_build_metadata_unavailable");
  }
  const result = packageSchema.safeParse(raw);
  if (!result.success) {
    throw new Error("desktop_build_metadata_invalid");
  }
  const metadata = result.data.kestrelDesktopBuild;
  if (metadata.build_mode !== metadata.key_id) {
    throw new Error("desktop_build_mode_key_mismatch");
  }
  if (
    (metadata.build_mode === "developer") !==
    (metadata.smoke_authority ===
      "developer_directory_smoke_v1")
  ) {
    throw new Error("desktop_build_smoke_authority_mismatch");
  }
  if (
    result.data.version !== metadata.app_version ||
    runtime.appVersion !== metadata.app_version ||
    runtime.platform !== metadata.platform ||
    runtime.architecture !== metadata.architecture
  ) {
    throw new Error("desktop_build_runtime_mismatch");
  }
  return Object.freeze({
    buildMode: metadata.build_mode,
    keyId: metadata.key_id,
    sourceCommit: metadata.source_commit,
    appVersion: metadata.app_version,
    platform: metadata.platform,
    architecture: metadata.architecture,
    resourceRootRelative: metadata.resource_root_relative,
    smokeAuthority: metadata.smoke_authority,
    pythonLockSha256: metadata.python_lock_sha256,
    desktopNpmLockSha256: metadata.desktop_npm_lock_sha256,
    webNpmLockSha256: metadata.web_npm_lock_sha256,
    sbomSha256: metadata.sbom_sha256
  });
}
