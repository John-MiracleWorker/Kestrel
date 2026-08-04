import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { DESKTOP_APP_ENTRY_URL } from "../contracts";
import {
  canonicalDesktopRouteUrl,
  isTrustedAppFrameUrl,
  selectDesktopDeepLink
} from "./app-route";

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
    new URL(
      "../../../config/workbench-route-contract.json",
      import.meta.url
    ),
    "utf8"
  )
) as RouteContract;

const stableRoutes = [
  "mission/command",
  "projects/overview",
  "memory/layers",
  "flock/overview",
  "automate/routines",
  "extend/catalog",
  "settings/general"
] as const;

describe("desktop app route boundary", () => {
  it("matches the cross-runtime route contract", () => {
    expect(routeContract.schema).toBe("kestrel.workbench.routes.v1");
    expect(
      routeContract.destinations.map((destination) => ({
        ...destination,
        trusted: isTrustedAppFrameUrl(
          `${DESKTOP_APP_ENTRY_URL}${destination.canonical_hash}`
        ),
        canonical: canonicalDesktopRouteUrl(
          `kestrel://app/${destination.id}`
        )
      }))
    ).toEqual(
      routeContract.destinations.map((destination) => ({
        ...destination,
        trusted: true,
        canonical: `${DESKTOP_APP_ENTRY_URL}${destination.canonical_hash}`
      }))
    );

    for (const legacy of routeContract.legacy_routes) {
      expect(
        canonicalDesktopRouteUrl(
          `${DESKTOP_APP_ENTRY_URL}${legacy.legacy_hash}`
        )
      ).toBe(
        `${DESKTOP_APP_ENTRY_URL}${legacy.canonical_hash}`
      );
    }
  });

  it.each(stableRoutes)(
    "trusts the stable renderer route %s on reviewed app paths",
    (route) => {
      expect(
        isTrustedAppFrameUrl(
          `kestrel://app/index.html#/${route}`
        )
      ).toBe(true);
      expect(
        isTrustedAppFrameUrl(`kestrel://app/#/${route}`)
      ).toBe(true);
    }
  );

  it("trusts bounded canonical evidence queries without allowing encoded route segments", () => {
    expect(
      isTrustedAppFrameUrl(
        "kestrel://app/index.html#/flock/qualification?run_id=run+1&task_id=proof%2F2"
      )
    ).toBe(true);

    for (const url of [
      "kestrel://app/index.html#/flock%2Frouting",
      "kestrel://app/index.html#/flock/routing%2Fhidden",
      "kestrel://app/index.html#/flock/routing?run_id=%00hidden",
      "kestrel://app/index.html#/flock/routing?run_id=%ZZ",
      "kestrel://app/index.html#/flock/routing?run_id=one&run_id=two",
      `kestrel://app/index.html#/flock/routing?run_id=${"x".repeat(200)}`
    ]) {
      expect(isTrustedAppFrameUrl(url), url).toBe(false);
    }
  });

  it.each([
    "kestrel://app/index.html#/unknown/overview",
    "kestrel://app/index.html#/Mission/command",
    "kestrel://app/index.html#/mission/a/b",
    "kestrel://app/index.html#/mission/",
    "kestrel://app/index.html#/mission/command?",
    "kestrel://app/index.html?route=mission#/mission/command",
    "kestrel://user@app/index.html#/mission/command",
    "kestrel://app:444/index.html#/mission/command",
    "kestrel://app.evil/index.html#/mission/command",
    "kestrel://app\\index.html#/mission/command",
    "kestrel://app/%2e%2e/index.html#/mission/command",
    `kestrel://app/index.html#/mission/command?${"x".repeat(300)}`
  ])("rejects an unreviewed renderer URL %s", (url) => {
    expect(isTrustedAppFrameUrl(url)).toBe(false);
  });

  it("keeps reviewed legacy frame hashes during the transition", () => {
    for (const hash of [
      "",
      "#mission",
      "#chat",
      "#outcomes",
      "#routines",
      "#routing",
      "#advanced",
      "#settings",
      "#workspace",
      "#tools"
    ]) {
      expect(
        isTrustedAppFrameUrl(
          `kestrel://app/index.html${hash}`
        ),
        hash
      ).toBe(true);
    }
  });

  it("canonicalizes internal, legacy, and operating-system deep links", () => {
    expect(
      canonicalDesktopRouteUrl(
        "kestrel://app/#/flock/qualification?run_id=run+1"
      )
    ).toBe(
      "kestrel://app/index.html#/flock/qualification?run_id=run+1"
    );
    expect(
      canonicalDesktopRouteUrl("kestrel://app/index.html#routing")
    ).toBe("kestrel://app/index.html#/flock/routing");
    expect(
      canonicalDesktopRouteUrl(
        "kestrel://app/memory/evidence?run_id=run+1"
      )
    ).toBe(
      "kestrel://app/index.html#/memory/evidence?run_id=run+1"
    );
    expect(
      canonicalDesktopRouteUrl("kestrel://app/settings")
    ).toBe("kestrel://app/index.html#/settings/general");
  });

  it("selects only the first reviewed Kestrel route from process arguments", () => {
    expect(
      selectDesktopDeepLink([
        "/Applications/Kestrel",
        "--flag",
        "https://evil.test/",
        "kestrel://app/flock/qualification?run_id=run_1",
        "kestrel://app/settings"
      ])
    ).toBe(
      "kestrel://app/index.html#/flock/qualification?run_id=run_1"
    );
    expect(
      selectDesktopDeepLink([
        "/Applications/Kestrel",
        "kestrel://app/unknown"
      ])
    ).toBeNull();
  });
});
