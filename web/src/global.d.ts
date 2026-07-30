import type {
  DesktopBridge,
  DesktopRuntimeMarker
} from "./platform/desktopBridge";

declare global {
  var kestrelDesktop: DesktopBridge | undefined;
  var kestrelDesktopRuntime: DesktopRuntimeMarker | undefined;

  interface Window {
    kestrelDesktop?: DesktopBridge;
    kestrelDesktopRuntime?: DesktopRuntimeMarker;
  }
}

export {};
