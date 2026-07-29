import { readFile, realpath } from "node:fs/promises";
import {
  extname,
  isAbsolute,
  posix,
  relative,
  resolve,
  sep
} from "node:path";
import {
  DESKTOP_APP_HOST,
  DESKTOP_APP_ORIGIN,
  DESKTOP_APP_SCHEME
} from "../contracts.js";

export const APP_CONTENT_SECURITY_POLICY =
  "default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; " +
  "img-src 'self' data: blob:; connect-src http://127.0.0.1:*; object-src 'none'; " +
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

type AssetReader = (path: string) => Promise<Uint8Array>;

class InvalidAppPathError extends Error {}

function isContained(root: string, candidate: string): boolean {
  const pathFromRoot = relative(root, candidate);
  return (
    pathFromRoot === "" ||
    (!isAbsolute(pathFromRoot) &&
      pathFromRoot !== ".." &&
      !pathFromRoot.startsWith(`..${sep}`))
  );
}

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

export async function resolveAppAsset(
  rawPath: string,
  rendererRoot: string
): Promise<string> {
  const relativeAsset = reviewedRelativeAsset(rawPath);
  const canonicalRoot = await realpath(rendererRoot);
  const candidate = resolve(
    canonicalRoot,
    ...relativeAsset.split(posix.sep)
  );
  if (!isContained(canonicalRoot, candidate)) {
    throw new InvalidAppPathError("app asset escapes renderer root");
  }

  const canonicalCandidate = await realpath(candidate);
  if (!isContained(canonicalRoot, canonicalCandidate)) {
    throw new InvalidAppPathError("app asset escapes renderer root");
  }
  return canonicalCandidate;
}

function responseHeaders(contentType: string): HeadersInit {
  return {
    "Content-Security-Policy": APP_CONTENT_SECURITY_POLICY,
    "Content-Type": contentType,
    "X-Content-Type-Options": "nosniff"
  };
}

function textResponse(body: string, status: number): Response {
  return new Response(body, {
    status,
    headers: responseHeaders("text/plain; charset=utf-8")
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

function isMissingFileError(error: unknown): boolean {
  return (
    error instanceof Error &&
    "code" in error &&
    (error as NodeJS.ErrnoException).code === "ENOENT"
  );
}

export async function appProtocolResponse(
  request: ProtocolRequest,
  rendererRoot: string,
  readAsset: AssetReader = readFile
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
    const assetPath = await resolveAppAsset(rawPath, rendererRoot);
    const body = await readAsset(assetPath);
    const contentType =
      contentTypes.get(extname(assetPath).toLowerCase()) ??
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
    if (isMissingFileError(error)) {
      return textResponse("Not found", 404);
    }
    return textResponse("Not found", 404);
  }
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
  rendererRoot: string
): void {
  registrar.handle(DESKTOP_APP_SCHEME, (request) =>
    appProtocolResponse(request, rendererRoot)
  );
}
