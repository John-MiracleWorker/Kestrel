// @vitest-environment node

/**
 * Programmatic WCAG contrast gate for Workbench token pairs.
 *
 * axe's color-contrast rule cannot run under jsdom (no layout/paint), so
 * contrast is enforced here against the design tokens directly. Both themes
 * must pass independently — the dark `--action` differs from the light one
 * deliberately. A failing pair is fixed by adjusting the TOKEN in
 * tokens.css, never by exempting the pair.
 *
 * Helpers mirror the established theme.test.ts implementations (that file
 * gates foregrounds on canvas/surface/on-action; this file extends coverage
 * to control and soft-surface pairs). The `/* theme:light|dark * /` markers
 * in tokens.css are load-bearing — keep them when editing tokens.
 */
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const AA_TEXT = 4.5;
const AA_NON_TEXT = 3.0;

const SOFT_SURFACES = [
  "action-soft",
  "success-soft",
  "attention-soft",
  "caution-soft",
  "info-soft",
  "danger-soft",
] as const;

const STATUS_SOFT_PAIRS = [
  ["action", "action-soft"],
  ["success", "success-soft"],
  ["attention", "attention-soft"],
  ["caution", "caution-soft"],
  ["info", "info-soft"],
  ["danger", "danger-soft"],
] as const;

const CONTROL_BACKGROUNDS = [
  "selected",
  "surface-raised",
  "surface-sunken",
] as const;

describe("Workbench contrast tokens", () => {
  const css = readFileSync(
    `${process.cwd()}/src/design/tokens.css`,
    "utf8",
  );

  it("keeps ink and muted-ink AA on every soft status surface", () => {
    for (const theme of ["light", "dark"] as const) {
      const block = themeBlock(css, theme);
      for (const surface of SOFT_SURFACES) {
        for (const foreground of ["ink", "muted-ink"] as const) {
          expect(
            contrastRatio(tokenHex(block, foreground), tokenHex(block, surface)),
            `${theme} --${foreground} on --${surface}`,
          ).toBeGreaterThanOrEqual(AA_TEXT);
        }
      }
    }
  });

  it("keeps each status hue AA on its own soft pair", () => {
    for (const theme of ["light", "dark"] as const) {
      const block = themeBlock(css, theme);
      for (const [foreground, surface] of STATUS_SOFT_PAIRS) {
        expect(
          contrastRatio(tokenHex(block, foreground), tokenHex(block, surface)),
          `${theme} --${foreground} on --${surface}`,
        ).toBeGreaterThanOrEqual(AA_TEXT);
      }
    }
  });

  it("keeps ink AA on selected, raised, and sunken control surfaces", () => {
    for (const theme of ["light", "dark"] as const) {
      const block = themeBlock(css, theme);
      for (const surface of CONTROL_BACKGROUNDS) {
        for (const foreground of ["ink", "muted-ink"] as const) {
          expect(
            contrastRatio(tokenHex(block, foreground), tokenHex(block, surface)),
            `${theme} --${foreground} on --${surface}`,
          ).toBeGreaterThanOrEqual(AA_TEXT);
        }
      }
    }
  });

  it("keeps strong borders and focus indicators at non-text AA against canvas and surface", () => {
    for (const theme of ["light", "dark"] as const) {
      const block = themeBlock(css, theme);
      for (const background of ["canvas", "surface"] as const) {
        for (const indicator of ["border-strong", "focus"] as const) {
          expect(
            contrastRatio(
              tokenHex(block, indicator),
              tokenHex(block, background),
            ),
            `${theme} --${indicator} on --${background} (non-text)`,
          ).toBeGreaterThanOrEqual(AA_NON_TEXT);
        }
      }
    }
  });
});

function themeBlock(css: string, theme: "light" | "dark"): string {
  const marker = `/* theme:${theme} */`;
  const start = css.indexOf(marker);
  const next = css.indexOf("/* theme:", start + marker.length);
  expect(start).toBeGreaterThanOrEqual(0);
  return css.slice(start, next === -1 ? undefined : next);
}

function tokenHex(block: string, token: string): string {
  const match = block.match(new RegExp(`--${token}:\\s*(#[0-9a-f]{6})`, "i"));
  expect(match, `missing hex token --${token}`).not.toBeNull();
  return match?.[1] ?? "#000000";
}

function contrastRatio(left: string, right: string): number {
  const [lighter, darker] = [luminance(left), luminance(right)].sort(
    (first, second) => second - first,
  );
  return (lighter + 0.05) / (darker + 0.05);
}

function luminance(hex: string): number {
  const channels =
    hex
      .slice(1)
      .match(/../g)
      ?.map((value) => Number.parseInt(value, 16) / 255)
      .map((value) =>
        value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4,
      ) ?? [0, 0, 0];
  return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
}
