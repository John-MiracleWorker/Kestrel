export const THEME_STORAGE_KEY = "kestrel.theme.preference.v1";
export const THEME_CHANGE_EVENT = "kestrel-theme-change";
export const MOTION_STORAGE_KEY = "kestrel.motion.preference.v1";
export const MOTION_CHANGE_EVENT = "kestrel-motion-change";

export type ThemePreference = "light" | "dark" | "system";
export type ResolvedTheme = Exclude<ThemePreference, "system">;
export type MotionPreference = "system" | "reduce";

type ThemeRoot = {
  dataset: Record<string, string> | DOMStringMap;
  style: {
    colorScheme: string;
  };
  setAttribute(name: string, value: string): void;
};
type AppearanceRoot = ThemeRoot & {
  removeAttribute(name: string): void;
};

type ThemeStorage = Pick<Storage, "getItem">;
type AppearanceHost = Pick<Window, "localStorage" | "dispatchEvent">;

export function isThemePreference(value: unknown): value is ThemePreference {
  return value === "light" || value === "dark" || value === "system";
}

export function isMotionPreference(value: unknown): value is MotionPreference {
  return value === "system" || value === "reduce";
}

export function resolveTheme(
  preference: ThemePreference,
  systemDark: boolean,
): ResolvedTheme {
  if (preference === "system") {
    return systemDark ? "dark" : "light";
  }
  return preference;
}

export function readThemePreference(storage: ThemeStorage): ThemePreference {
  try {
    const stored = storage.getItem(THEME_STORAGE_KEY);
    return isThemePreference(stored) ? stored : "system";
  } catch {
    return "system";
  }
}

export function readMotionPreference(storage: ThemeStorage): MotionPreference {
  try {
    const stored = storage.getItem(MOTION_STORAGE_KEY);
    return isMotionPreference(stored) ? stored : "system";
  } catch {
    return "system";
  }
}

export function applyTheme(
  root: ThemeRoot,
  preference: ThemePreference,
  systemDark: boolean,
): ResolvedTheme {
  const resolved = resolveTheme(preference, systemDark);
  root.dataset.theme = resolved;
  root.dataset.themePreference = preference;
  root.style.colorScheme = resolved;
  root.setAttribute("data-theme", resolved);
  return resolved;
}

export function applyMotionPreference(
  root: AppearanceRoot,
  preference: MotionPreference,
): void {
  if (preference === "reduce") {
    root.dataset.reducedMotion = "reduce";
    root.setAttribute("data-reduced-motion", "reduce");
    return;
  }
  delete root.dataset.reducedMotion;
  root.removeAttribute("data-reduced-motion");
}

export function setThemePreference(
  preference: ThemePreference,
  host: AppearanceHost = window,
): void {
  persistAndPublish(
    THEME_STORAGE_KEY,
    THEME_CHANGE_EVENT,
    preference,
    host,
  );
}

export function setMotionPreference(
  preference: MotionPreference,
  host: AppearanceHost = window,
): void {
  persistAndPublish(
    MOTION_STORAGE_KEY,
    MOTION_CHANGE_EVENT,
    preference,
    host,
  );
}

export function installTheme(
  root: AppearanceRoot = document.documentElement,
  host: Window = window,
): () => void {
  const colorScheme = host.matchMedia("(prefers-color-scheme: dark)");
  const initialPreferences = readInitialPreferences(host);
  let preference = initialPreferences.theme;
  let motionPreference = initialPreferences.motion;

  const render = () => {
    applyTheme(root, preference, colorScheme.matches);
  };
  const renderMotion = () => {
    applyMotionPreference(root, motionPreference);
  };
  const handleSystemChange = () => {
    if (preference === "system") {
      render();
    }
  };
  const handleStorage = (event: StorageEvent) => {
    if (event.key === THEME_STORAGE_KEY) {
      preference = isThemePreference(event.newValue) ? event.newValue : "system";
      render();
      return;
    }
    if (event.key === MOTION_STORAGE_KEY) {
      motionPreference = isMotionPreference(event.newValue)
        ? event.newValue
        : "system";
      renderMotion();
    }
  };
  const handlePreferenceChange = (event: Event) => {
    const next = (event as CustomEvent<ThemePreference>).detail;
    if (isThemePreference(next)) {
      preference = next;
      render();
    }
  };
  const handleMotionChange = (event: Event) => {
    const next = (event as CustomEvent<MotionPreference>).detail;
    if (isMotionPreference(next)) {
      motionPreference = next;
      renderMotion();
    }
  };

  render();
  renderMotion();
  colorScheme.addEventListener("change", handleSystemChange);
  host.addEventListener("storage", handleStorage);
  host.addEventListener(THEME_CHANGE_EVENT, handlePreferenceChange);
  host.addEventListener(MOTION_CHANGE_EVENT, handleMotionChange);

  return () => {
    colorScheme.removeEventListener("change", handleSystemChange);
    host.removeEventListener("storage", handleStorage);
    host.removeEventListener(THEME_CHANGE_EVENT, handlePreferenceChange);
    host.removeEventListener(MOTION_CHANGE_EVENT, handleMotionChange);
  };
}

function readInitialPreferences(
  host: Pick<Window, "localStorage">,
): {
  theme: ThemePreference;
  motion: MotionPreference;
} {
  try {
    const storage = host.localStorage;
    return {
      theme: readThemePreference(storage),
      motion: readMotionPreference(storage),
    };
  } catch {
    return {
      theme: "system",
      motion: "system",
    };
  }
}

function persistAndPublish<T extends string>(
  storageKey: string,
  eventName: string,
  preference: T,
  host: AppearanceHost,
): void {
  try {
    host.localStorage.setItem(storageKey, preference);
  } catch {
    // The current session can still honor the setting when persistence is
    // unavailable (for example, locked-down browser storage).
  }
  host.dispatchEvent(
    new CustomEvent<T>(eventName, {
      detail: preference,
    }),
  );
}
