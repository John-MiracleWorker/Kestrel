import { extname, posix } from "node:path";
import {
  DESKTOP_APP_HOST,
  DESKTOP_APP_ORIGIN,
  DESKTOP_APP_SCHEME,
  DESKTOP_CREDENTIAL_HOST,
  DESKTOP_CREDENTIAL_ORIGIN
} from "../contracts.js";
import type {
  VerifiedCredentialAssets,
  VerifiedRendererAssets
} from "./resource-manifest.js";

export const APP_CONTENT_SECURITY_POLICY =
  "default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; " +
  "img-src 'self' data: blob:; connect-src http://127.0.0.1:*; object-src 'none'; " +
  "base-uri 'none'; form-action 'none'; frame-ancestors 'none';";

export const CREDENTIAL_CONTENT_SECURITY_POLICY =
  "default-src 'none'; script-src 'self'; style-src 'self'; " +
  "img-src 'none'; connect-src 'none'; object-src 'none'; " +
  "base-uri 'none'; form-action 'none'; frame-ancestors 'none';";

const allowedAssetTypes = new Set([
  ".css",
  ".gif",
  ".ico",
  ".jpeg",
  ".jpg",
  ".js",
  ".otf",
  ".png",
  ".svg",
  ".ttf",
  ".wasm",
  ".webp",
  ".woff",
  ".woff2"
]);

const contentTypes = new Map([
  [".css", "text/css; charset=utf-8"],
  [".gif", "image/gif"],
  [".html", "text/html; charset=utf-8"],
  [".ico", "image/x-icon"],
  [".jpeg", "image/jpeg"],
  [".jpg", "image/jpeg"],
  [".js", "text/javascript; charset=utf-8"],
  [".otf", "font/otf"],
  [".png", "image/png"],
  [".svg", "image/svg+xml"],
  [".ttf", "font/ttf"],
  [".wasm", "application/wasm"],
  [".webp", "image/webp"],
  [".woff", "font/woff"],
  [".woff2", "font/woff2"]
]);

export interface ProtocolRequest {
  method: string;
  url: string;
}

export interface SchemeRegistrar {
  registerSchemesAsPrivileged(
    schemes: Array<{
      scheme: string;
      privileges: {
        standard: boolean;
        secure: boolean;
        supportFetchAPI: boolean;
        stream: boolean;
      };
    }>
  ): void;
}

export interface ProtocolHandlerRegistrar {
  handle(
    scheme: string,
    handler: (request: ProtocolRequest) => Promise<Response>
  ): unknown;
}

class InvalidAppPathError extends Error {}

function decodedAssetPath(rawPath: string): string {
  if (rawPath.includes("\\") || /%(?:00|2f|5c)/i.test(rawPath)) {
    throw new InvalidAppPathError("ambiguous app path");
  }

  let decoded: string;
  try {
    decoded = decodeURIComponent(rawPath);
  } catch {
    throw new InvalidAppPathError("malformed app path");
  }

  if (
    decoded.includes("\0") ||
    decoded.includes("\\") ||
    /%(?:00|2e|2f|5c)/i.test(decoded)
  ) {
    throw new InvalidAppPathError("ambiguous app path");
  }

  return decoded;
}

function reviewedRelativeAsset(rawPath: string): string {
  const decoded = decodedAssetPath(rawPath);
  const withoutLeadingSlash = decoded.replace(/^\/+/, "");
  const segments = withoutLeadingSlash.split("/");
  if (segments.some((segment) => segment === "." || segment === "..")) {
    throw new InvalidAppPathError("app path traversal");
  }

  const normalized = posix.normalize(withoutLeadingSlash);
  if (
    normalized === ".." ||
    normalized.startsWith("../") ||
    posix.isAbsolute(normalized)
  ) {
    throw new InvalidAppPathError("app path traversal");
  }

  const extension = extname(normalized).toLowerCase();
  if (
    normalized === "" ||
    normalized === "." ||
    decoded.endsWith("/") ||
    extension === ""
  ) {
    return "index.html";
  }
  if (normalized === "index.html") {
    return normalized;
  }
  if (!allowedAssetTypes.has(extension)) {
    throw new InvalidAppPathError("unreviewed app asset type");
  }
  return normalized;
}

function responseHeaders(
  contentType: string,
  contentSecurityPolicy = APP_CONTENT_SECURITY_POLICY
): HeadersInit {
  return {
    "Content-Security-Policy": contentSecurityPolicy,
    "Content-Type": contentType,
    "X-Content-Type-Options": "nosniff"
  };
}

function textResponse(
  body: string,
  status: number,
  contentSecurityPolicy = APP_CONTENT_SECURITY_POLICY
): Response {
  return new Response(body, {
    status,
    headers: responseHeaders(
      "text/plain; charset=utf-8",
      contentSecurityPolicy
    )
  });
}

function rawPathFromAppUrl(value: string): string {
  if (!value.startsWith(DESKTOP_APP_ORIGIN)) {
    throw new InvalidAppPathError("invalid app origin");
  }
  const suffix = value.slice(DESKTOP_APP_ORIGIN.length);
  const boundary = suffix.charAt(0);
  if (boundary !== "" && boundary !== "/" && boundary !== "?" && boundary !== "#") {
    throw new InvalidAppPathError("invalid app origin");
  }
  const delimiter = suffix.search(/[?#]/);
  const rawPath = delimiter === -1 ? suffix : suffix.slice(0, delimiter);
  return rawPath || "/";
}

export async function appProtocolResponse(
  request: ProtocolRequest,
  rendererAssets: VerifiedRendererAssets
): Promise<Response> {
  if (request.method !== "GET") {
    return textResponse("Method not allowed", 405);
  }

  let url: URL;
  try {
    url = new URL(request.url);
  } catch {
    return textResponse("Bad request", 400);
  }
  if (
    url.protocol !== `${DESKTOP_APP_SCHEME}:` ||
    url.hostname !== DESKTOP_APP_HOST ||
    url.username !== "" ||
    url.password !== "" ||
    url.port !== ""
  ) {
    return textResponse("Bad request", 400);
  }

  try {
    const rawPath = rawPathFromAppUrl(request.url);
    const relativeAsset = reviewedRelativeAsset(rawPath);
    const body = rendererAssets.read(relativeAsset);
    if (body === undefined) {
      return textResponse("Not found", 404);
    }
    const contentType =
      contentTypes.get(extname(relativeAsset).toLowerCase()) ??
      "application/octet-stream";
    const responseBody = new Uint8Array(body.byteLength);
    responseBody.set(body);
    return new Response(responseBody.buffer, {
      status: 200,
      headers: responseHeaders(contentType)
    });
  } catch (error) {
    if (error instanceof InvalidAppPathError) {
      return textResponse("Bad request", 400);
    }
    return textResponse("Not found", 404);
  }
}

const credentialContentTypes = new Map([
  ["index.html", "text/html; charset=utf-8"],
  ["form.js", "text/javascript; charset=utf-8"],
  ["styles.css", "text/css; charset=utf-8"]
]);

export async function credentialProtocolResponse(
  request: ProtocolRequest,
  credentialAssets: VerifiedCredentialAssets
): Promise<Response> {
  if (request.method !== "GET") {
    return textResponse(
      "Method not allowed",
      405,
      CREDENTIAL_CONTENT_SECURITY_POLICY
    );
  }
  let url: URL;
  try {
    url = new URL(request.url);
  } catch {
    return textResponse(
      "Bad request",
      400,
      CREDENTIAL_CONTENT_SECURITY_POLICY
    );
  }
  if (
    url.protocol !== `${DESKTOP_APP_SCHEME}:` ||
    url.hostname !== DESKTOP_CREDENTIAL_HOST ||
    url.username !== "" ||
    url.password !== "" ||
    url.port !== "" ||
    url.search !== "" ||
    url.hash !== ""
  ) {
    return textResponse(
      "Bad request",
      400,
      CREDENTIAL_CONTENT_SECURITY_POLICY
    );
  }
  const relativePath = [...credentialContentTypes.keys()].find(
    (candidate) =>
      request.url ===
      `${DESKTOP_CREDENTIAL_ORIGIN}/${candidate}`
  );
  if (relativePath === undefined) {
    return textResponse(
      "Not found",
      404,
      CREDENTIAL_CONTENT_SECURITY_POLICY
    );
  }
  const body = credentialAssets.read(relativePath);
  if (body === undefined) {
    return textResponse(
      "Not found",
      404,
      CREDENTIAL_CONTENT_SECURITY_POLICY
    );
  }
  const responseBody = Uint8Array.from(body);
  return new Response(responseBody.buffer, {
    status: 200,
    headers: responseHeaders(
      credentialContentTypes.get(relativePath) ??
        "application/octet-stream",
      CREDENTIAL_CONTENT_SECURITY_POLICY
    )
  });
}

export function registerKestrelScheme(registrar: SchemeRegistrar): void {
  registrar.registerSchemesAsPrivileged([
    {
      scheme: DESKTOP_APP_SCHEME,
      privileges: {
        standard: true,
        secure: true,
        supportFetchAPI: true,
        stream: true
      }
    }
  ]);
}

export function registerAppProtocol(
  registrar: ProtocolHandlerRegistrar,
  rendererAssets: VerifiedRendererAssets
): void {
  registrar.handle(DESKTOP_APP_SCHEME, (request) =>
    appProtocolResponse(request, rendererAssets)
  );
}

export function registerKestrelProtocol(
  registrar: ProtocolHandlerRegistrar,
  assets: {
    rendererAssets: VerifiedRendererAssets;
    credentialAssets: VerifiedCredentialAssets;
  }
): void {
  registrar.handle(DESKTOP_APP_SCHEME, async (request) => {
    let url: URL;
    try {
      url = new URL(request.url);
    } catch {
      return textResponse("Bad request", 400);
    }
    if (url.hostname === DESKTOP_APP_HOST) {
      return appProtocolResponse(
        request,
        assets.rendererAssets
      );
    }
    if (url.hostname === DESKTOP_CREDENTIAL_HOST) {
      return credentialProtocolResponse(
        request,
        assets.credentialAssets
      );
    }
    return textResponse("Bad request", 400);
  });
}
