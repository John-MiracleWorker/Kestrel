import {
  createHash,
  generateKeyPairSync,
  sign,
  type KeyObject
} from "node:crypto";
import {
  mkdtemp,
  mkdir,
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
  verifyResourceManifest,
  type ResourceManifest
} from "./resource-manifest";

function sha256(bytes: Uint8Array): string {
  return createHash("sha256").update(bytes).digest("hex");
}

describe("signed desktop resource manifest", () => {
  let root: string;
  let manifestPath: string;
  let signaturePath: string;
  let sidecarPath: string;
  let rendererPath: string;

  beforeEach(async () => {
    root = await mkdtemp(join(tmpdir(), "kestrel-resources-"));
    manifestPath = join(root, "kestrel-resource-manifest.json");
    signaturePath = join(root, "kestrel-resource-manifest.sig");
    sidecarPath = join(root, "sidecar", "kestrel-desktop-sidecar");
    rendererPath = join(root, "web", "dist", "index.html");
    await mkdir(join(root, "sidecar"), { recursive: true });
    await mkdir(join(root, "web", "dist"), { recursive: true });
    await writeFile(sidecarPath, "verified-sidecar");
    await writeFile(rendererPath, "<h1>Kestrel</h1>");
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
    const manifest: ResourceManifest = {
      schema: "kestrel.desktop.resources.v1",
      key_id: "ephemeral-test",
      files: {
        "sidecar/kestrel-desktop-sidecar": {
          size: sidecar.byteLength,
          sha256: sha256(sidecar)
        },
        "web/dist/index.html": {
          size: renderer.byteLength,
          sha256: sha256(renderer)
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
      trustedKeys: new Map([["ephemeral-test", publicKey]]),
      requiredFiles: [
        "sidecar/kestrel-desktop-sidecar",
        "web/dist/index.html"
      ]
    });

    expect(verified.manifestDigest).toMatch(/^sha256:[0-9a-f]{64}$/);
    expect(verified.files.get("sidecar/kestrel-desktop-sidecar")).toEqual({
      path: await realpath(sidecarPath),
      size: 16,
      sha256: sha256(Buffer.from("verified-sidecar"))
    });
  });

  it("refuses a sidecar whose digest differs from the signed manifest", async () => {
    const { publicKey } = await signedFixture();
    await writeFile(sidecarPath, "tampered-sidecar");

    await expect(
      verifyResourceManifest({
        resourceRoot: root,
        manifestPath,
        signaturePath,
        trustedKeys: new Map([["ephemeral-test", publicKey]]),
        requiredFiles: [
          "sidecar/kestrel-desktop-sidecar",
          "web/dist/index.html"
        ]
      })
    ).rejects.toMatchObject({ code: "resource_digest_mismatch" });
  });

  it("rejects unsigned, non-canonical, unknown-key, and symlink-escaped resources", async () => {
    const { manifest, publicKey } = await signedFixture();
    const trustedKeys = new Map([["ephemeral-test", publicKey]]);
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
        requiredFiles
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
        trustedKeys: new Map([["ephemeral-test", secondPublic]]),
        requiredFiles
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
        requiredFiles
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
        trustedKeys: new Map([["ephemeral-test", rsaPublic]]),
        requiredFiles
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
        trustedKeys: new Map([["ephemeral-test", secondPublic]]),
        requiredFiles
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
          trustedKeys: new Map([["ephemeral-test", publicKey]]),
          requiredFiles: [
            "sidecar/kestrel-desktop-sidecar",
            "web/dist/index.html"
          ]
        })
      ).rejects.toMatchObject({ code: "resource_path_untrusted" });
    } finally {
      await rm(outsideRoot, { force: true, recursive: true });
    }
  });
});
