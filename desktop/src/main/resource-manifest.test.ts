import {
  createHash,
  generateKeyPairSync,
  sign,
  type KeyObject
} from "node:crypto";
import {
  mkdtemp,
  mkdir,
  readFile,
  realpath,
  rm,
  symlink,
  writeFile
} from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  canonicalResourceManifestBytes,
  validatePortableResourcePaths,
  verifyDeveloperResourceManifest,
  verifyResourceManifest,
  type PackagedResourceIdentity,
  type ResourceManifest
} from "./resource-manifest";

function sha256(bytes: Uint8Array): string {
  return createHash("sha256").update(bytes).digest("hex");
}

const developerIdentity = Object.freeze({
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
} satisfies PackagedResourceIdentity);

const releaseSbom = Buffer.from("release-sbom");
const releaseIdentity = Object.freeze({
  buildMode: "release",
  keyId: "release",
  sourceCommit: "b".repeat(40),
  appVersion: "0.5.0",
  platform: "darwin",
  architecture: "arm64",
  pythonLockSha256: "5".repeat(64),
  desktopNpmLockSha256: "6".repeat(64),
  webNpmLockSha256: "7".repeat(64),
  sbomSha256: sha256(releaseSbom)
} satisfies PackagedResourceIdentity);

const releaseManifestMetadata = Object.freeze({
  schema: "kestrel.desktop.resources.v1",
  build_mode: "release",
  key_id: "release",
  source_commit: releaseIdentity.sourceCommit,
  app_version: releaseIdentity.appVersion,
  platform: releaseIdentity.platform,
  architecture: releaseIdentity.architecture,
  python_lock_sha256: releaseIdentity.pythonLockSha256,
  desktop_npm_lock_sha256: releaseIdentity.desktopNpmLockSha256,
  web_npm_lock_sha256: releaseIdentity.webNpmLockSha256,
  sbom_sha256: releaseIdentity.sbomSha256
} satisfies Omit<ResourceManifest, "files">);

describe("portable resource paths", () => {
  it.each([
    ["Café/first.bin", "Cafe\u0301/second.bin"],
    ["Ａrtifact/first.bin", "Artifact/second.bin"]
  ])(
    "rejects NFKC-casefolded component collisions between %s and %s",
    (left, right) => {
      expect(() =>
        validatePortableResourcePaths([left, right])
      ).toThrowError(
        expect.objectContaining({ code: "resource_path_untrusted" })
      );
    }
  );
});

describe("signed desktop resource manifest", () => {
  let root: string;
  let manifestPath: string;
  let signaturePath: string;
  let sidecarPath: string;
  let rendererPath: string;
  let credentialPaths: Record<string, string>;

  beforeEach(async () => {
    root = await mkdtemp(join(tmpdir(), "kestrel-resources-"));
    manifestPath = join(root, "kestrel-resource-manifest.json");
    signaturePath = join(root, "kestrel-resource-manifest.sig");
    sidecarPath = join(root, "sidecar", "kestrel-desktop-sidecar");
    rendererPath = join(root, "web", "dist", "index.html");
    credentialPaths = Object.fromEntries(
      ["index.html", "form.js", "styles.css", "preload.js"].map(
        (name) => [
          name,
          join(root, "desktop", "dist", "credential", name)
        ]
      )
    );
    await mkdir(join(root, "sidecar"), { recursive: true });
    await mkdir(join(root, "web", "dist"), { recursive: true });
    await mkdir(
      join(root, "desktop", "dist", "credential"),
      { recursive: true }
    );
    await writeFile(sidecarPath, "verified-sidecar");
    await writeFile(rendererPath, "<h1>Kestrel</h1>");
    await writeFile(
      credentialPaths["index.html"]!,
      "<form>Credential</form>"
    );
    await writeFile(credentialPaths["form.js"]!, "export {};");
    await writeFile(
      credentialPaths["styles.css"]!,
      "body { color: #111; }"
    );
    await writeFile(
      credentialPaths["preload.js"]!,
      "require(\"electron\");"
    );
    await writeFile(join(root, "sbom.cdx.json"), releaseSbom);
  });

  afterEach(async () => {
    await rm(root, { force: true, recursive: true });
  });

  async function signedFixture(): Promise<{
    manifest: ResourceManifest;
    publicKey: KeyObject;
  }> {
    const sidecar = Buffer.from("verified-sidecar");
    const renderer = Buffer.from("<h1>Kestrel</h1>");
    const credentials = {
      "index.html": Buffer.from("<form>Credential</form>"),
      "form.js": Buffer.from("export {};"),
      "styles.css": Buffer.from("body { color: #111; }"),
      "preload.js": Buffer.from('require("electron");')
    };
    const manifest: ResourceManifest = {
      ...releaseManifestMetadata,
      files: {
        "sidecar/kestrel-desktop-sidecar": {
          size: sidecar.byteLength,
          sha256: sha256(sidecar)
        },
        "web/dist/index.html": {
          size: renderer.byteLength,
          sha256: sha256(renderer)
        },
        ...Object.fromEntries(
          Object.entries(credentials).map(([name, bytes]) => [
            `desktop/dist/credential/${name}`,
            {
              size: bytes.byteLength,
              sha256: sha256(bytes)
            }
          ])
        ),
        "sbom.cdx.json": {
          size: releaseSbom.byteLength,
          sha256: sha256(releaseSbom)
        }
      }
    };
    const bytes = canonicalResourceManifestBytes(manifest);
    const { privateKey, publicKey } = generateKeyPairSync("ed25519");
    await writeFile(manifestPath, bytes);
    await writeFile(signaturePath, sign(null, bytes, privateKey));
    return { manifest, publicKey };
  }

  it("verifies canonical Ed25519 bytes and every required file size and digest", async () => {
    const { publicKey } = await signedFixture();

    const verified = await verifyResourceManifest({
      resourceRoot: root,
      manifestPath,
      signaturePath,
      trustedKeys: new Map([["release", publicKey]]),
      requiredFiles: [
        "sidecar/kestrel-desktop-sidecar",
        "web/dist/index.html"
      ],
      expectedIdentity: releaseIdentity
    });

    expect(verified.manifestDigest).toMatch(/^sha256:[0-9a-f]{64}$/);
    expect(verified.files.get("sidecar/kestrel-desktop-sidecar")).toEqual({
      path: await realpath(sidecarPath),
      size: 16,
      sha256: sha256(Buffer.from("verified-sidecar"))
    });
  });

  it("binds expanded provenance and accepts developer keys only through the explicit verifier", async () => {
    const sbom = Buffer.from("developer-sbom");
    const sbomPath = join(root, "sbom.cdx.json");
    await writeFile(sbomPath, sbom);
    const manifest = {
      schema: "kestrel.desktop.resources.v1",
      build_mode: "developer",
      key_id: "developer",
      source_commit: developerIdentity.sourceCommit,
      app_version: developerIdentity.appVersion,
      platform: developerIdentity.platform,
      architecture: developerIdentity.architecture,
      python_lock_sha256: developerIdentity.pythonLockSha256,
      desktop_npm_lock_sha256:
        developerIdentity.desktopNpmLockSha256,
      web_npm_lock_sha256: developerIdentity.webNpmLockSha256,
      sbom_sha256: sha256(sbom),
      files: {
        "sidecar/kestrel-desktop-sidecar": {
          size: 16,
          sha256: sha256(Buffer.from("verified-sidecar"))
        },
        "web/dist/index.html": {
          size: 16,
          sha256: sha256(Buffer.from("<h1>Kestrel</h1>"))
        },
        ...Object.fromEntries(
          Object.entries(credentialPaths).map(([name]) => {
            const content =
              name === "index.html"
                ? Buffer.from("<form>Credential</form>")
                : name === "form.js"
                  ? Buffer.from("export {};")
                  : name === "styles.css"
                    ? Buffer.from("body { color: #111; }")
                    : Buffer.from('require("electron");');
            return [
              `desktop/dist/credential/${name}`,
              {
                size: content.byteLength,
                sha256: sha256(content)
              }
            ];
          })
        ),
        "sbom.cdx.json": {
          size: sbom.byteLength,
          sha256: sha256(sbom)
        }
      }
    } satisfies ResourceManifest;
    const canonical = canonicalResourceManifestBytes(manifest);
    const { privateKey, publicKey } = generateKeyPairSync("ed25519");
    await writeFile(manifestPath, canonical);
    await writeFile(signaturePath, sign(null, canonical, privateKey));
    const input = {
      resourceRoot: root,
      manifestPath,
      signaturePath,
      trustedKeys: new Map([["developer", publicKey]]),
      requiredFiles: [
        "sidecar/kestrel-desktop-sidecar",
        "web/dist/index.html",
        "sbom.cdx.json"
      ],
      expectedIdentity: {
        ...developerIdentity,
        sbomSha256: sha256(sbom)
      }
    };

    await expect(
      verifyResourceManifest(input)
    ).rejects.toMatchObject({
      code: "resource_build_mode_untrusted"
    });
    await expect(
      verifyDeveloperResourceManifest(input)
    ).resolves.toMatchObject({
      manifest: {
        build_mode: "developer",
        key_id: "developer"
      }
    });
  });

  it("rejects a signed manifest that omits or adds a staged payload file", async () => {
    const { publicKey } = await signedFixture();
    await writeFile(join(root, "unlisted.bin"), "extra");

    await expect(
      verifyResourceManifest({
        resourceRoot: root,
        manifestPath,
        signaturePath,
        trustedKeys: new Map([["release", publicKey]]),
        requiredFiles: [
          "sidecar/kestrel-desktop-sidecar",
          "web/dist/index.html"
        ],
        expectedIdentity: releaseIdentity
      })
    ).rejects.toMatchObject({ code: "resource_payload_coverage_mismatch" });
  });

  it("rejects control-character payload names and non-root control aliases", async () => {
    const { publicKey } = await signedFixture();
    const input = {
      resourceRoot: root,
      manifestPath,
      signaturePath,
      trustedKeys: new Map([["release", publicKey]]),
      requiredFiles: [
        "sidecar/kestrel-desktop-sidecar",
        "web/dist/index.html"
      ],
      expectedIdentity: releaseIdentity
    };
    const controlPath = join(root, "control-\n.bin");
    await writeFile(controlPath, "unsafe");
    await expect(
      verifyResourceManifest(input)
    ).rejects.toMatchObject({ code: "resource_path_untrusted" });
    await rm(controlPath);

    const manifestAlias = join(root, "manifest-alias.json");
    await writeFile(manifestAlias, await readFile(manifestPath));
    await expect(
      verifyResourceManifest({
        ...input,
        manifestPath: manifestAlias
      })
    ).rejects.toMatchObject({ code: "resource_path_untrusted" });
  });

  it("retains defensive snapshots of only signed renderer bytes", async () => {
    const { publicKey } = await signedFixture();
    const verified = await verifyResourceManifest({
      resourceRoot: root,
      manifestPath,
      signaturePath,
      trustedKeys: new Map([["release", publicKey]]),
      requiredFiles: [
        "sidecar/kestrel-desktop-sidecar",
        "web/dist/index.html"
      ],
      expectedIdentity: releaseIdentity
    });
    const rendererAssets = verified.rendererAssets;
    const first = rendererAssets.read("index.html");
    expect(Buffer.from(first ?? [])).toEqual(Buffer.from("<h1>Kestrel</h1>"));
    if (first !== undefined) {
      first.fill(0);
    }
    await writeFile(rendererPath, "<h1>Tampered after verify</h1>");
    expect(Buffer.from(rendererAssets.read("index.html") ?? [])).toEqual(
      Buffer.from("<h1>Kestrel</h1>")
    );
    expect(rendererAssets.read("../sidecar/kestrel-desktop-sidecar")).toBeUndefined();
    expect(rendererAssets.read("missing.js")).toBeUndefined();
  });

  it("captures a separate immutable credential snapshot from only the exact manifest prefix", async () => {
    const { publicKey } = await signedFixture();
    const requiredCredentials = [
      "desktop/dist/credential/index.html",
      "desktop/dist/credential/form.js",
      "desktop/dist/credential/styles.css",
      "desktop/dist/credential/preload.js"
    ];
    const verified = await verifyResourceManifest({
      resourceRoot: root,
      manifestPath,
      signaturePath,
      trustedKeys: new Map([["release", publicKey]]),
      requiredFiles: [
        "sidecar/kestrel-desktop-sidecar",
        "web/dist/index.html",
        ...requiredCredentials
      ],
      expectedIdentity: releaseIdentity
    });

    const credentials = verified.credentialAssets;
    const first = credentials.read("index.html");
    expect(Buffer.from(first ?? [])).toEqual(
      Buffer.from("<form>Credential</form>")
    );
    first?.fill(0);
    await writeFile(
      credentialPaths["index.html"]!,
      "<form>Tampered after verification</form>"
    );

    expect(
      Buffer.from(credentials.read("index.html") ?? [])
    ).toEqual(Buffer.from("<form>Credential</form>"));
    expect(credentials.read("../preload.js")).toBeUndefined();
    expect(credentials.read("web/dist/index.html")).toBeUndefined();
    expect(
      verified.rendererAssets.read(
        "../desktop/dist/credential/index.html"
      )
    ).toBeUndefined();
  });

  it("rejects signed renderer declarations that exceed per-file or aggregate snapshot bounds", async () => {
    const { privateKey, publicKey } = generateKeyPairSync("ed25519");
    const baseFiles = {
      "sidecar/kestrel-desktop-sidecar": {
        size: 16,
        sha256: sha256(Buffer.from("verified-sidecar"))
      },
      "sbom.cdx.json": {
        size: releaseSbom.byteLength,
        sha256: sha256(releaseSbom)
      }
    };
    const writeSigned = async (manifest: ResourceManifest): Promise<void> => {
      const bytes = canonicalResourceManifestBytes(manifest);
      await writeFile(manifestPath, bytes);
      await writeFile(signaturePath, sign(null, bytes, privateKey));
    };

    await writeSigned({
      ...releaseManifestMetadata,
      files: {
        ...baseFiles,
        "web/dist/oversized.wasm": {
          size: Number.MAX_SAFE_INTEGER,
          sha256: "1".repeat(64)
        }
      }
    });
    await expect(
      verifyResourceManifest({
        resourceRoot: root,
        manifestPath,
        signaturePath,
        trustedKeys: new Map([["release", publicKey]]),
        requiredFiles: [
          "sidecar/kestrel-desktop-sidecar",
          "web/dist/oversized.wasm"
        ],
        expectedIdentity: releaseIdentity
      })
    ).rejects.toMatchObject({ code: "renderer_asset_too_large" });

    const manyRendererFiles = Object.fromEntries(
      Array.from({ length: 32 }, (_unused, index) => [
        `web/dist/assets/chunk-${index}.wasm`,
        { size: 8 * 1024 * 1024, sha256: "2".repeat(64) }
      ])
    );
    await writeSigned({
      ...releaseManifestMetadata,
      files: {
        ...baseFiles,
        ...manyRendererFiles
      }
    });
    await expect(
      verifyResourceManifest({
        resourceRoot: root,
        manifestPath,
        signaturePath,
        trustedKeys: new Map([["release", publicKey]]),
        requiredFiles: [
          "sidecar/kestrel-desktop-sidecar",
          "web/dist/assets/chunk-0.wasm"
        ],
        expectedIdentity: releaseIdentity
      })
    ).rejects.toMatchObject({ code: "renderer_snapshot_too_large" });
  });

  it("refuses a sidecar whose digest differs from the signed manifest", async () => {
    const { publicKey } = await signedFixture();
    await writeFile(sidecarPath, "tampered-sidecar");

    await expect(
      verifyResourceManifest({
        resourceRoot: root,
        manifestPath,
        signaturePath,
        trustedKeys: new Map([["release", publicKey]]),
        requiredFiles: [
          "sidecar/kestrel-desktop-sidecar",
          "web/dist/index.html"
        ],
        expectedIdentity: releaseIdentity
      })
    ).rejects.toMatchObject({ code: "resource_digest_mismatch" });
  });

  it("rejects unsigned, non-canonical, unknown-key, and symlink-escaped resources", async () => {
    const { manifest, publicKey } = await signedFixture();
    const trustedKeys = new Map([["release", publicKey]]);
    const requiredFiles = [
      "sidecar/kestrel-desktop-sidecar",
      "web/dist/index.html"
    ];

    await writeFile(signaturePath, Buffer.alloc(64));
    await expect(
      verifyResourceManifest({
        resourceRoot: root,
        manifestPath,
        signaturePath,
        trustedKeys,
        requiredFiles,
        expectedIdentity: releaseIdentity
      })
    ).rejects.toMatchObject({ code: "resource_signature_invalid" });

    const pretty = Buffer.from(JSON.stringify(manifest, null, 2), "utf8");
    const { privateKey: secondPrivate, publicKey: secondPublic } =
      generateKeyPairSync("ed25519");
    await writeFile(manifestPath, pretty);
    await writeFile(signaturePath, sign(null, pretty, secondPrivate));
    await expect(
      verifyResourceManifest({
        resourceRoot: root,
        manifestPath,
        signaturePath,
        trustedKeys: new Map([["release", secondPublic]]),
        requiredFiles,
        expectedIdentity: releaseIdentity
      })
    ).rejects.toMatchObject({ code: "resource_manifest_not_canonical" });

    const canonical = canonicalResourceManifestBytes(manifest);
    await writeFile(manifestPath, canonical);
    await writeFile(signaturePath, sign(null, canonical, secondPrivate));
    await expect(
      verifyResourceManifest({
        resourceRoot: root,
        manifestPath,
        signaturePath,
        trustedKeys: new Map(),
        requiredFiles,
        expectedIdentity: releaseIdentity
      })
    ).rejects.toMatchObject({ code: "resource_signing_key_untrusted" });

    const { publicKey: rsaPublic } = generateKeyPairSync("rsa", {
      modulusLength: 2_048
    });
    await expect(
      verifyResourceManifest({
        resourceRoot: root,
        manifestPath,
        signaturePath,
        trustedKeys: new Map([["release", rsaPublic]]),
        requiredFiles,
        expectedIdentity: releaseIdentity
      })
    ).rejects.toMatchObject({ code: "resource_signing_key_untrusted" });

    const outside = join(root, "..", `outside-${Date.now()}`);
    await writeFile(outside, "verified-sidecar");
    await rm(sidecarPath);
    await symlink(outside, sidecarPath);
    await expect(
      verifyResourceManifest({
        resourceRoot: root,
        manifestPath,
        signaturePath,
        trustedKeys: new Map([["release", secondPublic]]),
        requiredFiles,
        expectedIdentity: releaseIdentity
      })
    ).rejects.toMatchObject({ code: "resource_path_untrusted" });
    await rm(outside, { force: true });
  });

  it("requires the signed manifest and signature to be contained resources", async () => {
    const { manifest } = await signedFixture();
    const outsideRoot = await mkdtemp(join(tmpdir(), "kestrel-manifest-outside-"));
    try {
      const bytes = canonicalResourceManifestBytes(manifest);
      const { privateKey, publicKey } = generateKeyPairSync("ed25519");
      const outsideManifest = join(
        outsideRoot,
        "kestrel-resource-manifest.json"
      );
      const outsideSignature = join(
        outsideRoot,
        "kestrel-resource-manifest.sig"
      );
      await writeFile(outsideManifest, bytes);
      await writeFile(outsideSignature, sign(null, bytes, privateKey));

      await expect(
        verifyResourceManifest({
          resourceRoot: root,
          manifestPath: outsideManifest,
          signaturePath: outsideSignature,
          trustedKeys: new Map([["release", publicKey]]),
          requiredFiles: [
            "sidecar/kestrel-desktop-sidecar",
            "web/dist/index.html"
          ],
          expectedIdentity: releaseIdentity
        })
      ).rejects.toMatchObject({ code: "resource_path_untrusted" });
    } finally {
      await rm(outsideRoot, { force: true, recursive: true });
    }
  });

  it("uses locale-independent Unicode code-point ordering from the shared golden vector", async () => {
    const fixture = JSON.parse(
      await readFile(
        join(
          import.meta.dirname,
          "../../../tests/fixtures/desktop-canonical-vectors.json"
        ),
        "utf8"
      )
    ) as {
      manifest: {
        canonical_utf8_hex: string;
        value: ResourceManifest;
      };
    };

    expect(
      canonicalResourceManifestBytes(fixture.manifest.value).toString("hex")
    ).toBe(fixture.manifest.canonical_utf8_hex);
  });
});
