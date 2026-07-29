import { DESKTOP_APP_HOST, DESKTOP_APP_SCHEME } from "../contracts.js";

export type NavigationDecision = "allow" | "deny";

export interface PreventableEvent {
  preventDefault(): void;
  url?: string;
}

export interface RestrictedWebContents {
  on(
    event: string,
    listener: (event: PreventableEvent, url?: string, ...args: unknown[]) => void
  ): unknown;
  setWindowOpenHandler(
    handler: (details: { url: string }) => { action: "deny" }
  ): unknown;
}

export interface RestrictedSession {
  setPermissionRequestHandler(
    handler: (
      webContents: unknown,
      permission: string,
      callback: (allowed: boolean) => void,
      details?: unknown
    ) => void
  ): unknown;
  setPermissionCheckHandler(
    handler: (
      webContents: unknown,
      permission: string,
      requestingOrigin?: string,
      details?: unknown
    ) => boolean
  ): unknown;
}

export function navigationDecision(value: string): NavigationDecision {
  try {
    const url = new URL(value);
    const isPrivateApp =
      url.protocol === `${DESKTOP_APP_SCHEME}:` &&
      url.hostname === DESKTOP_APP_HOST &&
      url.username === "" &&
      url.password === "" &&
      url.port === "";
    return isPrivateApp ? "allow" : "deny";
  } catch {
    return "deny";
  }
}

export function windowOpenDecision(_value: string): { action: "deny" } {
  return { action: "deny" };
}

function denyUntrustedNavigation(
  event: PreventableEvent,
  deprecatedUrl?: string
): void {
  const target = event.url ?? deprecatedUrl ?? "";
  if (navigationDecision(target) === "deny") {
    event.preventDefault();
  }
}

export function installWebContentsBoundary(
  webContents: RestrictedWebContents
): void {
  webContents.on("will-navigate", denyUntrustedNavigation);
  webContents.on("will-redirect", denyUntrustedNavigation);
  webContents.on("will-attach-webview", (event) => {
    event.preventDefault();
  });
  webContents.setWindowOpenHandler(({ url }) => windowOpenDecision(url));
}

export function installSessionBoundary(session: RestrictedSession): void {
  session.setPermissionRequestHandler(
    (_webContents, _permission, callback) => {
      callback(false);
    }
  );
  session.setPermissionCheckHandler(() => false);
}
