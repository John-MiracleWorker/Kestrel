import { DESKTOP_APP_ENTRY_URL } from "../contracts.js";

const APP_FRAME_PATHS = new Set(["/", "/index.html"]);
const MAX_APP_FRAME_URL_CHARACTERS = 640;
const MAX_ROUTE_QUERY_CHARACTERS = 256;
const MAX_ROUTE_QUERY_ENTRIES = 8;
const MAX_ROUTE_QUERY_VALUE_CHARACTERS = 128;

const destinationDefaults = Object.freeze({
  mission: "command",
  projects: "overview",
  memory: "layers",
  flock: "overview",
  automate: "routines",
  extend: "catalog",
  settings: "general"
});

type DesktopDestination = keyof typeof destinationDefaults;

type ParsedDesktopRoute = Readonly<{
  destination: DesktopDestination;
  subroute: string;
  query: string;
}>;

const legacyRoutes = new Map<string, ParsedDesktopRoute>([
  ["#mission", route("mission", "command")],
  ["#chat", route("mission", "history")],
  ["#outcomes", route("mission", "outcomes")],
  ["#routines", route("automate", "routines")],
  ["#routing", route("flock", "routing")],
  ["#advanced", route("extend", "capabilities")],
  ["#settings", route("settings", "general")],
  ["#workspace", route("mission", "command")],
  ["#tools", route("extend", "capabilities")]
]);

export function isTrustedAppFrameUrl(value: string): boolean {
  return parseInternalFrame(value) !== null;
}

export function canonicalDesktopRouteUrl(
  value: string
): string | null {
  const internal = parseInternalFrame(value);
  if (internal !== null) {
    return internal.route === null
      ? DESKTOP_APP_ENTRY_URL
      : formatInternalRoute(internal.route);
  }

  const parsed = parseBaseUrl(value);
  if (
    parsed === null ||
    parsed.hash !== "" ||
    value.includes("#") ||
    parsed.pathname === "/" ||
    APP_FRAME_PATHS.has(parsed.pathname)
  ) {
    return null;
  }
  const routePath = parseRoutePath(parsed.pathname);
  const query = parsed.search.startsWith("?")
    ? parsed.search.slice(1)
    : "";
  if (
    routePath === null ||
    !validCanonicalQuery(query, parsed.search !== "")
  ) {
    return null;
  }
  return formatInternalRoute({
    ...routePath,
    query
  });
}

export function selectDesktopDeepLink(
  commandLine: readonly string[]
): string | null {
  for (const argument of commandLine) {
    const routeUrl = canonicalDesktopRouteUrl(argument);
    if (routeUrl !== null) {
      return routeUrl;
    }
  }
  return null;
}

function parseInternalFrame(
  value: string
): Readonly<{ route: ParsedDesktopRoute | null }> | null {
  const parsed = parseBaseUrl(value);
  if (
    parsed === null ||
    parsed.search !== "" ||
    !APP_FRAME_PATHS.has(parsed.pathname) ||
    (value.includes("#") && parsed.hash === "")
  ) {
    return null;
  }
  if (parsed.hash === "") {
    return { route: null };
  }
  const legacy = legacyRoutes.get(parsed.hash);
  if (legacy !== undefined) {
    return { route: legacy };
  }
  const routeValue = parseHashRoute(parsed.hash);
  return routeValue === null ? null : { route: routeValue };
}

function parseBaseUrl(value: string): URL | null {
  if (
    value.length === 0 ||
    value.length > MAX_APP_FRAME_URL_CHARACTERS ||
    value.includes("\\") ||
    /[\u0000-\u001f\u007f]/.test(value)
  ) {
    return null;
  }
  try {
    const parsed = new URL(value);
    return parsed.protocol === "kestrel:" &&
      parsed.hostname === "app" &&
      parsed.username === "" &&
      parsed.password === "" &&
      parsed.port === "" &&
      parsed.href === value
      ? parsed
      : null;
  } catch {
    return null;
  }
}

function parseHashRoute(hash: string): ParsedDesktopRoute | null {
  if (!hash.startsWith("#/")) {
    return null;
  }
  const queryIndex = hash.indexOf("?");
  const rawPath =
    queryIndex < 0 ? hash.slice(1) : hash.slice(1, queryIndex);
  const query =
    queryIndex < 0 ? "" : hash.slice(queryIndex + 1);
  if (
    !validCanonicalQuery(query, queryIndex >= 0) ||
    rawPath.includes("%")
  ) {
    return null;
  }
  const routePath = parseRoutePath(rawPath);
  return routePath === null ? null : { ...routePath, query };
}

function parseRoutePath(
  pathname: string
): Omit<ParsedDesktopRoute, "query"> | null {
  const match =
    /^\/([a-z]+)(?:\/([A-Za-z0-9][A-Za-z0-9._~-]{0,95}))?$/.exec(
      pathname
    );
  if (match === null || !isDestination(match[1])) {
    return null;
  }
  return {
    destination: match[1],
    subroute: match[2] ?? destinationDefaults[match[1]]
  };
}

function validCanonicalQuery(
  query: string,
  queryWasSupplied: boolean
): boolean {
  if (!queryWasSupplied) {
    return query === "";
  }
  if (
    query.length === 0 ||
    query.length > MAX_ROUTE_QUERY_CHARACTERS ||
    /%(?![0-9A-F]{2})/.test(query)
  ) {
    return false;
  }
  const params = new URLSearchParams(query);
  if (params.toString() !== query) {
    return false;
  }
  const entries = [...params.entries()];
  if (
    entries.length === 0 ||
    entries.length > MAX_ROUTE_QUERY_ENTRIES
  ) {
    return false;
  }
  const keys = new Set<string>();
  for (const [key, value] of entries) {
    if (
      !/^[A-Za-z][A-Za-z0-9_.-]{0,63}$/.test(key) ||
      keys.has(key) ||
      value.length > MAX_ROUTE_QUERY_VALUE_CHARACTERS ||
      value.includes("\\") ||
      /[\u0000-\u001f\u007f]/.test(value)
    ) {
      return false;
    }
    keys.add(key);
  }
  return true;
}

function isDestination(
  value: string | undefined
): value is DesktopDestination {
  return (
    value !== undefined &&
    Object.prototype.hasOwnProperty.call(destinationDefaults, value)
  );
}

function formatInternalRoute(routeValue: ParsedDesktopRoute): string {
  return `${DESKTOP_APP_ENTRY_URL}#/${routeValue.destination}/${
    routeValue.subroute
  }${routeValue.query ? `?${routeValue.query}` : ""}`;
}

function route(
  destination: DesktopDestination,
  subroute: string
): ParsedDesktopRoute {
  return { destination, subroute, query: "" };
}
