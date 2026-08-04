import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import {
  DESTINATIONS,
  formatAppLocation,
  parseAppLocation,
} from "./destinations";

type RouteContract = {
  schema: string;
  destinations: Array<{
    id: string;
    default_subroute: string;
    canonical_hash: string;
  }>;
  legacy_routes: Array<{
    legacy_hash: string;
    canonical_hash: string;
  }>;
};

const routeContract = JSON.parse(
  readFileSync(
    resolve(
      process.cwd(),
      "..",
      "config",
      "workbench-route-contract.json",
    ),
    "utf8",
  ),
) as RouteContract;

describe("Workbench destinations", () => {
  it.each([
    ["#/mission", { destination: "mission", subroute: "command" }],
    [
      "#/projects/repo_1",
      { destination: "projects", subroute: "repo_1" },
    ],
    [
      "#/flock/qualification",
      { destination: "flock", subroute: "qualification" },
    ],
    [
      "#/settings/updates",
      { destination: "settings", subroute: "updates" },
    ],
  ] as const)("parses %s", (hash, expected) => {
    expect(parseAppLocation(hash)).toMatchObject(expected);
  });

  it("defaults unknown routes to Mission without discarding evidence query", () => {
    expect(parseAppLocation("#/unknown?run_id=run_1")).toEqual({
      destination: "mission",
      subroute: "command",
      query: { run_id: "run_1" },
      recoveryReason: "unknown_route",
    });
  });

  it("keeps exactly the approved seven stable destinations", () => {
    expect(DESTINATIONS.map((destination) => destination.id)).toEqual([
      "mission",
      "projects",
      "memory",
      "flock",
      "automate",
      "extend",
      "settings",
    ]);
  });

  it("matches the cross-runtime route contract", () => {
    expect(routeContract.schema).toBe("kestrel.workbench.routes.v1");
    expect(
      DESTINATIONS.map(({ id, defaultSubroute }) => ({
        id,
        default_subroute: defaultSubroute,
        canonical_hash: formatAppLocation(
          parseAppLocation(`#/${id}`),
        ),
      })),
    ).toEqual(routeContract.destinations);

    for (const legacy of routeContract.legacy_routes) {
      expect(
        formatAppLocation(parseAppLocation(legacy.legacy_hash)),
      ).toBe(legacy.canonical_hash);
    }
  });

  it("round-trips nested routes and encoded query evidence", () => {
    const formatted = formatAppLocation({
      destination: "memory",
      subroute: "evidence",
      query: { run_id: "run 1", task_id: "proof/2" },
    });
    expect(formatted).toBe(
      "#/memory/evidence?run_id=run+1&task_id=proof%2F2",
    );
    expect(parseAppLocation(formatted)).toMatchObject({
      destination: "memory",
      subroute: "evidence",
      query: { run_id: "run 1", task_id: "proof/2" },
    });
  });

  it("maps legacy Workbench hashes without breaking saved deep links", () => {
    expect(parseAppLocation("#chat")).toMatchObject({
      destination: "mission",
      subroute: "history",
      recoveryReason: "legacy_route",
    });
    expect(parseAppLocation("#routing")).toMatchObject({
      destination: "flock",
      subroute: "routing",
      recoveryReason: "legacy_route",
    });
  });

  it.each([
    "#/mission/history/extra",
    "#/mission/history%2Fextra",
    "#/mission/",
    "#/Mission/command",
    "#/unknown/overview",
    "#/flock/routing?run_id=one&run_id=two",
    "#/flock/routing?run_id=%00hidden",
    `#/flock/routing?run_id=${"x".repeat(200)}`,
  ])("recovers the untrusted route grammar %s", (hash) => {
    expect(parseAppLocation(hash)).toMatchObject({
      destination: "mission",
      subroute: "command",
      recoveryReason: "unknown_route",
    });
  });
});
