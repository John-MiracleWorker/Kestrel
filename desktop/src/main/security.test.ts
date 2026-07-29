import { describe, expect, it, vi } from "vitest";
import {
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
