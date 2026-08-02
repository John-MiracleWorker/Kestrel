/**
 * LAN discovery installed-renderer e2e journeys (LAN plan Task 10).
 *
 * Every byte comes from the controlled private-network fixture in
 * `./fixtures.ts` (`lanDiscoveryFixtureInitScript`): one fixture interface,
 * one confirmed scope (192.168.90.0/24), one model server at
 * 192.168.90.2:11434. The stub throws on any request outside its table, so
 * the suite can never touch the CI runner's ambient network — a regression
 * that tried fails loudly instead of silently scanning.
 *
 * Journeys (from the plan's renderer contract):
 *  - no discovery traffic exists before the owner chooses Scan network;
 *  - the owner sees the exact private scope and bounds before confirming,
 *    and every discovered server renders as a disabled draft with the
 *    prompt/code privacy warning;
 *  - public or over-wide scopes are rejected before any scan exists;
 *  - owner cancellation stops the scan.
 */
import { expect, test, type Page } from "@playwright/test";
import { existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { lanDiscoveryFixtureInitScript } from "./fixtures";

const here = dirname(fileURLToPath(import.meta.url));
const indexPath = resolve(here, "../../web/dist/index.html");

const LAN_API = "/api/routing/lan";

test.skip(
  !existsSync(indexPath),
  "web/dist is not built; run `npm --prefix web run build` first",
);

async function openLanWorkspace(
  page: Page,
  options: { holdRunning?: boolean } = {},
): Promise<void> {
  await page.addInitScript(lanDiscoveryFixtureInitScript(options));
  await page.goto("/#/flock/lan");
  await expect(
    page.getByRole("heading", { name: "LAN model discovery" }),
  ).toBeVisible();
  await page.waitForFunction(() => document.fonts.status === "loaded");
}

async function requestedLanPaths(page: Page): Promise<string[]> {
  return page.evaluate(
    (lanApi) =>
      (window as unknown as { __kestrelE2eRequestedPaths: string[] })
        .__kestrelE2eRequestedPaths.filter((path) => path.includes(lanApi)),
    LAN_API,
  );
}

async function beginPreview(page: Page): Promise<void> {
  await page.getByRole("button", { name: "Scan network" }).click();
  const fixtureInterface = page.getByRole("radio", { name: /Fixture Wi-Fi/ });
  await expect(fixtureInterface).toBeVisible();
  await fixtureInterface.click();
  await page.getByRole("button", { name: "Preview scope" }).click();
}

test.describe("LAN discovery adversarial renderer journeys", () => {
  test("no LAN discovery traffic exists before the owner acts", async ({
    page,
  }) => {
    await openLanWorkspace(page);

    await expect(
      page.getByText(/Nothing is probed until you choose Scan network/),
    ).toBeVisible();
    // Give any erroneous background poller a chance to fire.
    await page.waitForTimeout(500);

    expect(await requestedLanPaths(page)).toEqual([]);
  });

  test("owner sees the exact scope and bounds before confirming; results are disabled drafts", async ({
    page,
  }) => {
    await openLanWorkspace(page);
    await beginPreview(page);

    // The exact private scope and bounds are visible before confirmation.
    await expect(page.getByLabel("Network scope")).toHaveValue(
      "192.168.90.0/24",
    );
    await expect(
      page.getByText("Up to 254 hosts × 4 known model ports"),
    ).toBeVisible();
    await expect(page.getByText("1234, 8000, 8080, 11434")).toBeVisible();
    expect(await requestedLanPaths(page)).toEqual([
      `GET ${LAN_API}/interfaces`,
      `POST ${LAN_API}/preview`,
    ]);

    await page.getByRole("button", { name: "Confirm and scan" }).click();
    await expect(page.getByText("Scan status: completed")).toBeVisible({
      timeout: 30_000,
    });

    // The draft -> start mutation sequence happened in order, after preview.
    const lanPaths = await requestedLanPaths(page);
    const createIndex = lanPaths.indexOf(`POST ${LAN_API}/scans`);
    const startIndex = lanPaths.findIndex((path) =>
      path.startsWith(`POST ${LAN_API}/scans/lan_`) && path.endsWith("/start"),
    );
    expect(createIndex).toBeGreaterThan(1);
    expect(startIndex).toBeGreaterThan(createIndex);

    // Every discovered server is a disabled draft with the privacy warning.
    await expect(page.getByText("192.168.90.2:11434")).toBeVisible();
    await expect(page.getByText("fixture-llama")).toBeVisible();
    await expect(page.getByText("not enabled")).toBeVisible();
    expect(await page.getByText("disabled").count()).toBeGreaterThan(0);
    await expect(
      page.getByText(/prompts and code leave this computer/),
    ).toBeVisible();
  });

  test("public and over-wide scopes are rejected before any scan exists", async ({
    page,
  }) => {
    await openLanWorkspace(page);
    await page.getByRole("button", { name: "Scan network" }).click();
    const fixtureInterface = page.getByRole("radio", { name: /Fixture Wi-Fi/ });
    await expect(fixtureInterface).toBeVisible();
    await fixtureInterface.click();

    const scopeField = page.getByLabel("Network scope");
    for (const hostileScope of ["8.8.8.0/24", "192.168.0.0/16", "0.0.0.0/0"]) {
      await scopeField.fill(hostileScope);
      await page.getByRole("button", { name: "Preview scope" }).click();
      await expect(page.locator(".banner.error")).toContainText(
        "private interface scope",
      );
      // A rejected preview never reveals the confirmation gate.
      await expect(
        page.getByRole("button", { name: "Confirm and scan" }),
      ).toHaveCount(0);
    }

    // Previews were attempted (and refused server-side); no scan was created.
    const lanPaths = await requestedLanPaths(page);
    expect(lanPaths).toEqual([
      `GET ${LAN_API}/interfaces`,
      `POST ${LAN_API}/preview`,
      `POST ${LAN_API}/preview`,
      `POST ${LAN_API}/preview`,
    ]);
  });

  test("owner cancellation stops the scan", async ({ page }) => {
    await openLanWorkspace(page, { holdRunning: true });
    await beginPreview(page);
    await page.getByRole("button", { name: "Confirm and scan" }).click();

    await expect(page.getByText("Scan status: running")).toBeVisible({
      timeout: 30_000,
    });
    // The confirmed scope is locked while the scan runs.
    await expect(page.getByLabel("Network scope")).toBeDisabled();
    await expect(
      page.getByRole("radio", { name: /Fixture Wi-Fi/ }),
    ).toBeDisabled();

    await page.getByRole("button", { name: "Cancel scan" }).click();
    await expect(page.getByText("Scan status: cancelled")).toBeVisible({
      timeout: 30_000,
    });

    const lanPaths = await requestedLanPaths(page);
    const cancelIndex = lanPaths.findIndex((path) =>
      path.startsWith(`POST ${LAN_API}/scans/lan_`) && path.endsWith("/cancel"),
    );
    expect(cancelIndex).toBeGreaterThan(-1);
    // No discovery result panel appears after cancellation.
    await expect(page.getByText("192.168.90.2:11434")).toHaveCount(0);
  });
});
