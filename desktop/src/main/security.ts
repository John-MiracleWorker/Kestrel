import type {
  Event as ElectronEvent,
  WebContentsWillFrameNavigateEventParams
} from "electron";
import {
  DESKTOP_APP_HOST,
  DESKTOP_APP_SCHEME,
  DESKTOP_CREDENTIAL_ENTRY_URL
} from "../contracts.js";

export type NavigationDecision = "allow" | "deny";

export interface PreventableEvent {
  preventDefault(): void;
  url?: string;
  isMainFrame?: boolean;
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

export function credentialNavigationDecision(
  value: string
): NavigationDecision {
  if (value !== DESKTOP_CREDENTIAL_ENTRY_URL) {
    return "deny";
  }
  try {
    const url = new URL(value);
    return url.href === DESKTOP_CREDENTIAL_ENTRY_URL
      ? "allow"
      : "deny";
  } catch {
    return "deny";
  }
}

type NavigationListener = {
  (
    event: ElectronEvent<WebContentsWillFrameNavigateEventParams>
  ): void;
  (event: PreventableEvent, deprecatedUrl?: string): void;
};

const denyUntrustedNavigation: NavigationListener = (
  event: PreventableEvent,
  deprecatedUrl?: string
): void => {
  const target = event.url ?? deprecatedUrl ?? "";
  if (navigationDecision(target) === "deny") {
    event.preventDefault();
  }
};

export function installWebContentsBoundary(
  webContents: RestrictedWebContents
): void {
  webContents.on("will-frame-navigate", denyUntrustedNavigation);
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

export function installCredentialWebContentsBoundary(
  webContents: RestrictedWebContents
): void {
  webContents.on(
    "will-navigate",
    (event, deprecatedUrl) => {
      const target = event.url ?? deprecatedUrl ?? "";
      if (credentialNavigationDecision(target) === "deny") {
        event.preventDefault();
      }
    }
  );
  webContents.on("will-redirect", (event) => {
    event.preventDefault();
  });
  webContents.on(
    "will-frame-navigate",
    (event, deprecatedUrl) => {
      const target = event.url ?? deprecatedUrl ?? "";
      if (
        event.isMainFrame !== true ||
        credentialNavigationDecision(target) === "deny"
      ) {
        event.preventDefault();
      }
    }
  );
  webContents.on("will-attach-webview", (event) => {
    event.preventDefault();
  });
  webContents.setWindowOpenHandler(({ url }) =>
    windowOpenDecision(url)
  );
}

export function installCredentialSessionBoundary(
  session: RestrictedSession
): void {
  installSessionBoundary(session);
}
