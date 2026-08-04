import {
  Bird,
  Blocks,
  Brain,
  FolderKanban,
  SlidersHorizontal,
  Target,
  Workflow,
  type LucideIcon,
} from "lucide-react";

export type AppDestination =
  | "mission"
  | "projects"
  | "memory"
  | "flock"
  | "automate"
  | "extend"
  | "settings";

export type AppLocation = {
  destination: AppDestination;
  subroute: string;
  query: Record<string, string>;
  recoveryReason?: "legacy_route" | "unknown_route";
};

export type DestinationDefinition = Readonly<{
  id: AppDestination;
  label: string;
  icon: LucideIcon;
  defaultSubroute: string;
}>;

export const DESTINATIONS = Object.freeze([
  {
    id: "mission",
    label: "Mission",
    icon: Target,
    defaultSubroute: "command",
  },
  {
    id: "projects",
    label: "Projects",
    icon: FolderKanban,
    defaultSubroute: "overview",
  },
  {
    id: "memory",
    label: "Memory",
    icon: Brain,
    defaultSubroute: "layers",
  },
  {
    id: "flock",
    label: "Flock",
    icon: Bird,
    defaultSubroute: "overview",
  },
  {
    id: "automate",
    label: "Automate",
    icon: Workflow,
    defaultSubroute: "routines",
  },
  {
    id: "extend",
    label: "Extend",
    icon: Blocks,
    defaultSubroute: "catalog",
  },
  {
    id: "settings",
    label: "Settings",
    icon: SlidersHorizontal,
    defaultSubroute: "general",
  },
] satisfies readonly DestinationDefinition[]);

const destinationById = new Map(
  DESTINATIONS.map((destination) => [destination.id, destination]),
);

const legacyLocations: Record<string, AppLocation> = {
  mission: {
    destination: "mission",
    subroute: "command",
    query: {},
    recoveryReason: "legacy_route",
  },
  chat: {
    destination: "mission",
    subroute: "history",
    query: {},
    recoveryReason: "legacy_route",
  },
  outcomes: {
    destination: "mission",
    subroute: "outcomes",
    query: {},
    recoveryReason: "legacy_route",
  },
  routines: {
    destination: "automate",
    subroute: "routines",
    query: {},
    recoveryReason: "legacy_route",
  },
  routing: {
    destination: "flock",
    subroute: "routing",
    query: {},
    recoveryReason: "legacy_route",
  },
  advanced: {
    destination: "extend",
    subroute: "capabilities",
    query: {},
    recoveryReason: "legacy_route",
  },
  settings: {
    destination: "settings",
    subroute: "general",
    query: {},
    recoveryReason: "legacy_route",
  },
  workspace: {
    destination: "mission",
    subroute: "command",
    query: {},
    recoveryReason: "legacy_route",
  },
  tools: {
    destination: "extend",
    subroute: "capabilities",
    query: {},
    recoveryReason: "legacy_route",
  },
};

export function parseAppLocation(hash: string): AppLocation {
  const withoutHash = hash.trim().replace(/^#/, "");
  const queryIndex = withoutHash.indexOf("?");
  const rawPath =
    queryIndex >= 0 ? withoutHash.slice(0, queryIndex) : withoutHash;
  const rawQuery =
    queryIndex >= 0 ? withoutHash.slice(queryIndex + 1) : "";
  const parsedQuery = queryRecord(rawQuery, queryIndex >= 0);
  if (!parsedQuery.valid) {
    return recoveredLocation({});
  }
  const query = parsedQuery.query;
  const legacy = legacyLocations[rawPath.toLowerCase()];
  if (legacy && rawPath === rawPath.toLowerCase()) {
    return { ...legacy, query };
  }

  if (!rawPath) {
    return defaultLocation("mission", query);
  }
  const routeMatch =
    /^\/([a-z]+)(?:\/([A-Za-z0-9][A-Za-z0-9._~-]{0,95}))?$/.exec(
      rawPath,
    );
  if (!routeMatch) {
    return recoveredLocation(query);
  }
  const destination = routeMatch[1];
  if (!isDestination(destination)) {
    return recoveredLocation(query);
  }
  return {
    destination,
    subroute:
      routeMatch[2] ||
      destinationById.get(destination)?.defaultSubroute ||
      "overview",
    query,
  };
}

export function formatAppLocation(location: AppLocation): string {
  const search = new URLSearchParams();
  Object.entries(location.query).forEach(([key, value]) => {
    search.set(key, value);
  });
  const query = search.toString();
  return `#/${location.destination}/${encodeURIComponent(location.subroute)}${
    query ? `?${query}` : ""
  }`;
}

export function destinationLocation(
  destination: AppDestination,
  query: Record<string, string> = {},
): AppLocation {
  return defaultLocation(destination, query);
}

function defaultLocation(
  destination: AppDestination,
  query: Record<string, string>,
): AppLocation {
  return {
    destination,
    subroute:
      destinationById.get(destination)?.defaultSubroute ?? "overview",
    query,
  };
}

function recoveredLocation(
  query: Record<string, string>,
): AppLocation {
  return {
    ...defaultLocation("mission", query),
    recoveryReason: "unknown_route",
  };
}

function isDestination(value: string): value is AppDestination {
  return destinationById.has(value as AppDestination);
}

function queryRecord(
  value: string,
  wasSupplied: boolean,
): Readonly<{
  query: Record<string, string>;
  valid: boolean;
}> {
  if (!wasSupplied) {
    return { query: {}, valid: value === "" };
  }
  if (
    value.length === 0 ||
    value.length > 256 ||
    /%(?![0-9A-Fa-f]{2})/.test(value)
  ) {
    return { query: {}, valid: false };
  }
  const entries = [...new URLSearchParams(value).entries()];
  if (entries.length === 0 || entries.length > 8) {
    return { query: {}, valid: false };
  }
  const query: Record<string, string> = {};
  for (const [key, entryValue] of entries) {
    if (
      !/^[A-Za-z][A-Za-z0-9_.-]{0,63}$/.test(key) ||
      Object.prototype.hasOwnProperty.call(query, key) ||
      entryValue.length > 128 ||
      entryValue.includes("\\") ||
      /[\u0000-\u001f\u007f]/.test(entryValue)
    ) {
      return { query: {}, valid: false };
    }
    query[key] = entryValue;
  }
  return { query, valid: true };
}
