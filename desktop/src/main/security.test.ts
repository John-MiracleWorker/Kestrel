import { describe, expect, it, vi } from "vitest";
import {
  credentialNavigationDecision,
  installCredentialSessionBoundary,
  installCredentialWebContentsBoundary,
  installSessionBoundary,
  installWebContentsBoundary,
  navigationDecision,
  windowOpenDecision
} from "./security";

describe("desktop renderer security", () => {
  it("blocks navigation and new windows", () => {
    expect(navigationDecision("kestrel://app/mission")).toBe("allow");
    expect(navigationDecision("kestrel://app.evil/mission")).toBe("deny");
    expect(navigationDecision("kestrel://user@app/mission")).toBe("deny");
    expect(navigationDecision("file:///tmp/index.html")).toBe("deny");
    expect(navigationDecision("https://example.com")).toBe("deny");
    expect(windowOpenDecision("kestrel://app/mission")).toEqual({
      action: "deny"
    });
    expect(windowOpenDecision("https://example.com")).toEqual({
      action: "deny"
    });
  });

  it("prevents denied navigations, redirects, webviews, and every new window", () => {
    const listeners = new Map<string, (...args: unknown[]) => void>();
    let openHandler: ((details: { url: string }) => { action: "deny" }) | undefined;
    const webContents = {
      on: vi.fn((event: string, listener: (...args: unknown[]) => void) => {
        listeners.set(event, listener);
      }),
      setWindowOpenHandler: vi.fn(
        (handler: (details: { url: string }) => { action: "deny" }) => {
          openHandler = handler;
        }
      )
    };
    const deniedNavigation = { preventDefault: vi.fn() };
    const allowedNavigation = { preventDefault: vi.fn() };
    const deniedRedirect = { preventDefault: vi.fn() };
    const webviewAttach = { preventDefault: vi.fn() };

    installWebContentsBoundary(webContents);
    listeners.get("will-navigate")?.(
      deniedNavigation,
      "https://example.com"
    );
    listeners.get("will-navigate")?.(
      allowedNavigation,
      "kestrel://app/mission"
    );
    listeners.get("will-redirect")?.(deniedRedirect, "file:///tmp/index.html");
    listeners.get("will-attach-webview")?.(webviewAttach);

    expect(deniedNavigation.preventDefault).toHaveBeenCalledOnce();
    expect(allowedNavigation.preventDefault).not.toHaveBeenCalled();
    expect(deniedRedirect.preventDefault).toHaveBeenCalledOnce();
    expect(webviewAttach.preventDefault).toHaveBeenCalledOnce();
    expect(openHandler?.({ url: "kestrel://app/mission" })).toEqual({
      action: "deny"
    });
  });

  it("prevents untrusted Electron 43 subframe navigations", () => {
    const listeners = new Map<string, (...args: unknown[]) => void>();
    const webContents = {
      on: vi.fn((event: string, listener: (...args: unknown[]) => void) => {
        listeners.set(event, listener);
      }),
      setWindowOpenHandler: vi.fn()
    };
    const remoteSubframe = {
      preventDefault: vi.fn(),
      url: "https://example.com/embed",
      isMainFrame: false
    };
    const fileSubframe = {
      preventDefault: vi.fn(),
      url: "file:///tmp/embed.html",
      isMainFrame: false
    };
    const malformedSubframe = {
      preventDefault: vi.fn(),
      url: "not a URL",
      isMainFrame: false
    };
    const privateSubframe = {
      preventDefault: vi.fn(),
      url: "kestrel://app/mission",
      isMainFrame: false
    };

    installWebContentsBoundary(webContents);
    listeners.get("will-frame-navigate")?.(remoteSubframe);
    listeners.get("will-frame-navigate")?.(fileSubframe);
    listeners.get("will-frame-navigate")?.(malformedSubframe);
    listeners.get("will-frame-navigate")?.(privateSubframe);

    expect(remoteSubframe.preventDefault).toHaveBeenCalledOnce();
    expect(fileSubframe.preventDefault).toHaveBeenCalledOnce();
    expect(malformedSubframe.preventDefault).toHaveBeenCalledOnce();
    expect(privateSubframe.preventDefault).not.toHaveBeenCalled();
  });

  it("denies every permission request and permission check", () => {
    let requestHandler:
      | ((
          webContents: unknown,
          permission: string,
          callback: (allowed: boolean) => void
        ) => void)
      | undefined;
    let checkHandler:
      | ((webContents: unknown, permission: string) => boolean)
      | undefined;
    const session = {
      setPermissionRequestHandler: vi.fn(
        (
          handler: (
            webContents: unknown,
            permission: string,
            callback: (allowed: boolean) => void
          ) => void
        ) => {
          requestHandler = handler;
        }
      ),
      setPermissionCheckHandler: vi.fn(
        (handler: (webContents: unknown, permission: string) => boolean) => {
          checkHandler = handler;
        }
      )
    };
    const callback = vi.fn();

    installSessionBoundary(session);
    requestHandler?.({}, "notifications", callback);

    expect(callback).toHaveBeenCalledWith(false);
    expect(checkHandler?.({}, "media")).toBe(false);
  });
});

describe("credential renderer security", () => {
  it("allows only the exact credential entry and isolates both private hosts", () => {
    expect(
      credentialNavigationDecision(
        "kestrel://credential/index.html"
      )
    ).toBe("allow");
    for (const url of [
      "kestrel://credential/",
      "kestrel://credential/form.js",
      "kestrel://credential/index.html?raw=value",
      "kestrel://credential/index.html#fragment",
      "kestrel://credential.evil/index.html",
      "kestrel://user@credential/index.html",
      "kestrel://credential:43123/index.html",
      "kestrel://app/index.html",
      "file:///tmp/credential.html",
      "https://example.com/",
      "not a URL"
    ]) {
      expect(credentialNavigationDecision(url)).toBe("deny");
    }
    expect(
      navigationDecision("kestrel://credential/index.html")
    ).toBe("deny");
  });

  it("denies credential redirects, subframes, webviews, popups, and all permissions", () => {
    const listeners = new Map<
      string,
      (...args: unknown[]) => void
    >();
    let openHandler:
      | ((details: { url: string }) => { action: "deny" })
      | undefined;
    const webContents = {
      on: vi.fn(
        (
          event: string,
          listener: (...args: unknown[]) => void
        ) => {
          listeners.set(event, listener);
        }
      ),
      setWindowOpenHandler: vi.fn(
        (
          handler: (details: {
            url: string;
          }) => { action: "deny" }
        ) => {
          openHandler = handler;
        }
      )
    };
    let permissionRequest:
      | ((
          webContents: unknown,
          permission: string,
          callback: (allowed: boolean) => void
        ) => void)
      | undefined;
    let permissionCheck:
      | ((webContents: unknown, permission: string) => boolean)
      | undefined;
    const session = {
      setPermissionRequestHandler: vi.fn(
        (
          handler: (
            webContents: unknown,
            permission: string,
            callback: (allowed: boolean) => void
          ) => void
        ) => {
          permissionRequest = handler;
        }
      ),
      setPermissionCheckHandler: vi.fn(
        (
          handler: (
            webContents: unknown,
            permission: string
          ) => boolean
        ) => {
          permissionCheck = handler;
        }
      )
    };
    const exactEntry = { preventDefault: vi.fn() };
    const appRedirect = { preventDefault: vi.fn() };
    const credentialMainFrame = {
      preventDefault: vi.fn(),
      url: "kestrel://credential/index.html",
      isMainFrame: true
    };
    const credentialSubframe = {
      preventDefault: vi.fn(),
      url: "kestrel://credential/index.html",
      isMainFrame: false
    };
    const webview = { preventDefault: vi.fn() };

    installCredentialWebContentsBoundary(webContents);
    installCredentialSessionBoundary(session);
    listeners.get("will-navigate")?.(
      exactEntry,
      "kestrel://credential/index.html"
    );
    listeners.get("will-redirect")?.(
      appRedirect,
      "kestrel://app/index.html"
    );
    listeners.get("will-frame-navigate")?.(
      credentialMainFrame
    );
    listeners.get("will-frame-navigate")?.(
      credentialSubframe
    );
    listeners.get("will-attach-webview")?.(webview);

    expect(exactEntry.preventDefault).not.toHaveBeenCalled();
    expect(appRedirect.preventDefault).toHaveBeenCalledOnce();
    expect(
      credentialMainFrame.preventDefault
    ).not.toHaveBeenCalled();
    expect(
      credentialSubframe.preventDefault
    ).toHaveBeenCalledOnce();
    expect(webview.preventDefault).toHaveBeenCalledOnce();
    expect(
      openHandler?.({
        url: "kestrel://credential/index.html"
      })
    ).toEqual({ action: "deny" });
    const permissionCallback = vi.fn();
    permissionRequest?.({}, "clipboard-read", permissionCallback);
    expect(permissionCallback).toHaveBeenCalledWith(false);
    expect(permissionCheck?.({}, "notifications")).toBe(false);
  });
});
