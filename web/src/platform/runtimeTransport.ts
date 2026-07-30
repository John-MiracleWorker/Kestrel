import type { DesktopRuntimeMarker } from "../types";

export type { DesktopRuntimeMarker } from "../types";

export const DESKTOP_RUNTIME_MARKER_KEY =
  "kestrelDesktopRuntime" as const;

export interface RuntimeTransport {
  readonly mode: "browser" | "desktop";
  fetch(path: string, init?: RequestInit): Promise<Response>;
  eventSourceUrl(path: string): string | null;
}

type BrowserAuthHeaders = () => Record<string, string>;

const AUTHORITY_HEADER_NAMES = new Set([
  "authorization",
  "x-kestrel-api-key"
]);

function fixedError(code: string): Error {
  return new Error(code);
}

function hasOwnDesktopMarker(): boolean {
  return Object.prototype.hasOwnProperty.call(
    globalThis,
    DESKTOP_RUNTIME_MARKER_KEY
  );
}

export function isDesktopRuntime(): boolean {
  return hasOwnDesktopMarker();
}

function exactDesktopBaseUrl(value: unknown): string | null {
  if (typeof value !== "string") {
    return null;
  }
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    return null;
  }
  if (
    parsed.protocol !== "http:" ||
    parsed.hostname !== "127.0.0.1" ||
    parsed.port === "" ||
    parsed.username !== "" ||
    parsed.password !== "" ||
    parsed.pathname !== "/" ||
    parsed.search !== "" ||
    parsed.hash !== "" ||
    parsed.href !== value
  ) {
    return null;
  }
  return value;
}

const DESKTOP_MARKER_KEYS = new Set([
  "schema",
  "baseUrl",
  "generation"
]);

function validatedDesktopMarker(value: unknown): DesktopRuntimeMarker {
  try {
    if (
      typeof value !== "object" ||
      value === null ||
      !Object.isFrozen(value)
    ) {
      throw fixedError("desktop_runtime_marker_invalid");
    }
    const keys = Reflect.ownKeys(value);
    if (
      keys.length !== DESKTOP_MARKER_KEYS.size ||
      keys.some(
        (key) =>
          typeof key !== "string" ||
          !DESKTOP_MARKER_KEYS.has(key)
      )
    ) {
      throw fixedError("desktop_runtime_marker_invalid");
    }
    const schema = Reflect.get(value, "schema");
    const baseUrlValue = Reflect.get(value, "baseUrl");
    const generation = Reflect.get(value, "generation");
    const baseUrl = exactDesktopBaseUrl(baseUrlValue);
    if (
      schema !== "kestrel.desktop.runtime.v1" ||
      baseUrl === null ||
      !Number.isSafeInteger(generation) ||
      generation <= 0
    ) {
      throw fixedError("desktop_runtime_marker_invalid");
    }
    return Object.freeze({
      schema: "kestrel.desktop.runtime.v1",
      baseUrl,
      generation
    });
  } catch {
    throw fixedError("desktop_runtime_marker_invalid");
  }
}

function readDesktopMarkerValue(): unknown {
  try {
    return Reflect.get(globalThis, DESKTOP_RUNTIME_MARKER_KEY);
  } catch {
    throw fixedError("desktop_runtime_marker_invalid");
  }
}

function headerRecord(headers?: HeadersInit): Record<string, string> {
  if (headers === undefined) {
    return {};
  }
  if (headers instanceof Headers) {
    return Object.fromEntries(headers.entries());
  }
  if (Array.isArray(headers)) {
    return Object.fromEntries(headers);
  }
  return { ...headers };
}

function stripAuthorityHeaders(headers?: HeadersInit): Record<string, string> {
  const safe: Record<string, string> = {};
  for (const [name, value] of Object.entries(headerRecord(headers))) {
    if (!AUTHORITY_HEADER_NAMES.has(name.toLowerCase())) {
      safe[name] = value;
    }
  }
  return safe;
}

export class BrowserRuntimeTransport implements RuntimeTransport {
  readonly mode = "browser" as const;

  constructor(private readonly readAuthHeaders: BrowserAuthHeaders) {}

  fetch(path: string, init: RequestInit = {}): Promise<Response> {
    return globalThis.fetch(path, {
      ...init,
      headers: {
        ...headerRecord(init.headers),
        ...this.readAuthHeaders()
      }
    });
  }

  eventSourceUrl(path: string): string {
    return path;
  }
}

export class DesktopRuntimeTransport implements RuntimeTransport {
  readonly mode = "desktop" as const;
  private readonly baseUrl: string;
  private readonly origin: string;

  constructor(marker: unknown) {
    const snapshot = validatedDesktopMarker(marker);
    this.baseUrl = snapshot.baseUrl;
    this.origin = new URL(snapshot.baseUrl).origin;
  }

  private requestUrl(path: string): string {
    if (
      typeof path !== "string" ||
      path.length === 0 ||
      path.trim() !== path
    ) {
      throw fixedError("desktop_runtime_request_invalid");
    }
    let absoluteInput = false;
    try {
      new URL(path);
      absoluteInput = true;
    } catch {
      if (!path.startsWith("/")) {
        throw fixedError("desktop_runtime_request_invalid");
      }
    }
    let parsed: URL;
    try {
      parsed = new URL(path, this.baseUrl);
    } catch {
      throw fixedError("desktop_runtime_request_invalid");
    }
    if (
      parsed.protocol !== "http:" ||
      parsed.hostname !== "127.0.0.1" ||
      parsed.username !== "" ||
      parsed.password !== "" ||
      parsed.hash !== "" ||
      parsed.origin !== this.origin ||
      (parsed.pathname !== "/api" &&
        !parsed.pathname.startsWith("/api/")) ||
      (absoluteInput && !path.startsWith(this.origin))
    ) {
      throw fixedError("desktop_runtime_request_invalid");
    }
    return parsed.href;
  }

  fetch(path: string, init: RequestInit = {}): Promise<Response> {
    if (
      init.credentials !== undefined &&
      init.credentials !== "omit"
    ) {
      return Promise.reject(
        fixedError("desktop_runtime_request_invalid")
      );
    }
    let url: string;
    try {
      url = this.requestUrl(path);
    } catch (error) {
      return Promise.reject(error);
    }
    return globalThis.fetch(url, {
      ...init,
      credentials: "omit",
      headers: stripAuthorityHeaders(init.headers)
    });
  }

  eventSourceUrl(_path: string): null {
    return null;
  }
}

export function runtimeTransport(
  readBrowserAuthHeaders: BrowserAuthHeaders = () => ({})
): RuntimeTransport {
  if (!hasOwnDesktopMarker()) {
    return new BrowserRuntimeTransport(readBrowserAuthHeaders);
  }
  return new DesktopRuntimeTransport(readDesktopMarkerValue());
}
