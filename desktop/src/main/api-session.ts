import type { DesktopRuntimeMarker } from "../contracts.js";

export interface ApiSessionWebContents {
  readonly id: number;
  isDestroyed(): boolean;
  once(event: "destroyed", listener: () => void): unknown;
}

export interface ApiSessionRequestDetails {
  id: number;
  url: string;
  method: string;
  webContentsId?: number;
  webContents?: ApiSessionWebContents;
  frame?: { readonly url: string } | null;
  referrer?: string;
  requestHeaders: Record<string, string>;
}

export interface ApiSessionWebRequest {
  onBeforeSendHeaders(
    filter: { urls: string[] },
    listener: (
      details: ApiSessionRequestDetails,
      callback: (response: {
        requestHeaders: Record<string, string>;
      }) => void
    ) => void
  ): void;
}

export interface DesktopApiSessionActivation {
  baseUrl: string;
  apiToken: string;
  generation: number;
}

export interface DesktopApiSessionAuthority {
  activate(activation: DesktopApiSessionActivation): void;
  deactivate(generation?: number): void;
  bindRenderer(webContents: ApiSessionWebContents): () => void;
  runtimeMarker(): DesktopRuntimeMarker | null;
}

interface ActiveAuthority {
  baseUrl: string;
  origin: string;
  apiToken: string;
  generation: number;
}

interface RendererBinding {
  webContents: ApiSessionWebContents;
  generation: number;
}

const AUTHORITY_HEADER_NAMES = new Set([
  "authorization",
  "x-kestrel-api-key"
]);

function invalidActivation(): Error {
  return new Error("desktop_api_session_activation_invalid");
}

function parseActivation(
  activation: DesktopApiSessionActivation
): ActiveAuthority {
  if (
    typeof activation !== "object" ||
    activation === null ||
    typeof activation.baseUrl !== "string" ||
    typeof activation.apiToken !== "string" ||
    !Number.isSafeInteger(activation.generation) ||
    activation.generation <= 0 ||
    activation.apiToken.length === 0 ||
    activation.apiToken.length > 4_096 ||
    activation.apiToken.trim() !== activation.apiToken ||
    /[\r\n]/.test(activation.apiToken)
  ) {
    throw invalidActivation();
  }
  let parsed: URL;
  try {
    parsed = new URL(activation.baseUrl);
  } catch {
    throw invalidActivation();
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
    parsed.href !== activation.baseUrl
  ) {
    throw invalidActivation();
  }
  return {
    baseUrl: activation.baseUrl,
    origin: parsed.origin,
    apiToken: activation.apiToken,
    generation: activation.generation
  };
}

function stripAuthorityHeaders(
  headers: Readonly<Record<string, string>>
): Record<string, string> {
  const stripped: Record<string, string> = {};
  for (const [name, value] of Object.entries(headers)) {
    if (!AUTHORITY_HEADER_NAMES.has(name.toLowerCase())) {
      stripped[name] = value;
    }
  }
  return stripped;
}

function exactAppFrame(frame: ApiSessionRequestDetails["frame"]): boolean {
  if (frame === null || frame === undefined) {
    return false;
  }
  let parsed: URL;
  try {
    parsed = new URL(frame.url);
  } catch {
    return false;
  }
  return (
    parsed.protocol === "kestrel:" &&
    parsed.hostname === "app" &&
    parsed.username === "" &&
    parsed.password === "" &&
    parsed.port === ""
  );
}

function exactApiTarget(url: string, active: ActiveAuthority): boolean {
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return false;
  }
  return (
    parsed.protocol === "http:" &&
    parsed.hostname === "127.0.0.1" &&
    parsed.username === "" &&
    parsed.password === "" &&
    parsed.hash === "" &&
    parsed.origin === active.origin &&
    (parsed.pathname === "/api" || parsed.pathname.startsWith("/api/"))
  );
}

export function installDesktopApiSession(
  webRequest: ApiSessionWebRequest
): DesktopApiSessionAuthority {
  let active: ActiveAuthority | null = null;
  let highestGeneration = 0;
  const bindings = new Map<number, RendererBinding>();

  webRequest.onBeforeSendHeaders(
    { urls: ["<all_urls>"] },
    (details, callback) => {
      const bindingById =
        details.webContentsId === undefined
          ? undefined
          : bindings.get(details.webContentsId);
      const bindingByObject =
        details.webContents === undefined
          ? undefined
          : bindings.get(details.webContents.id);
      const binding = bindingById ?? bindingByObject;
      const requestHeaders = stripAuthorityHeaders(
        details.requestHeaders
      );
      if (
        active !== null &&
        binding !== undefined &&
        details.webContentsId === binding.webContents.id &&
        details.webContents === binding.webContents &&
        !binding.webContents.isDestroyed() &&
        binding.generation === active.generation &&
        details.method.toUpperCase() !== "OPTIONS" &&
        exactAppFrame(details.frame) &&
        exactApiTarget(details.url, active)
      ) {
        requestHeaders.Authorization = `Bearer ${active.apiToken}`;
      }
      callback({ requestHeaders });
    }
  );

  const authority: DesktopApiSessionAuthority = {
    activate(activation): void {
      active = null;
      const next = parseActivation(activation);
      if (next.generation <= highestGeneration) {
        throw invalidActivation();
      }
      highestGeneration = next.generation;
      active = next;
    },
    deactivate(generation): void {
      if (
        active !== null &&
        (generation === undefined || generation === active.generation)
      ) {
        active = null;
      }
    },
    bindRenderer(webContents): () => void {
      const boundAuthority = active;
      if (
        boundAuthority === null ||
        !Number.isSafeInteger(webContents.id) ||
        webContents.id <= 0 ||
        webContents.isDestroyed()
      ) {
        return () => undefined;
      }
      const binding: RendererBinding = {
        webContents,
        generation: boundAuthority.generation
      };
      bindings.set(webContents.id, binding);
      let unbound = false;
      const unbind = (): void => {
        if (unbound) {
          return;
        }
        unbound = true;
        if (bindings.get(webContents.id) === binding) {
          bindings.delete(webContents.id);
        }
      };
      webContents.once("destroyed", unbind);
      return unbind;
    },
    runtimeMarker(): DesktopRuntimeMarker | null {
      if (active === null) {
        return null;
      }
      return Object.freeze({
        schema: "kestrel.desktop.runtime.v1",
        baseUrl: active.baseUrl,
        generation: active.generation
      });
    }
  };
  return Object.freeze(authority);
}
