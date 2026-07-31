// @vitest-environment node

import { describe, expect, it } from "vitest";

declare module "node:fs" {
  export function readFileSync(path: string, encoding: "utf8"): string;
}

import { readFileSync } from "node:fs";

declare const process: { cwd(): string };

describe("styles.css", () => {
  const styleFiles = [
    `${process.cwd()}/src/design/tokens.css`,
    `${process.cwd()}/src/design/typography.css`,
    `${process.cwd()}/src/styles.css`,
    `${process.cwd()}/src/design/design-system.css`,
  ];

  it("keeps production CSS free of HTML wrappers and remote assets", () => {
    const stylesheets = styleFiles.map((path) =>
      readFileSync(path, "utf8"),
    );

    for (const stylesheet of stylesheets) {
      expect(stylesheet).not.toMatch(/<\/?style\b/i);
      expect(stylesheet).not.toMatch(/https?:\/\//i);
    }
  });

  it("bundles both Wildflower font families locally", () => {
    const typography = readFileSync(
      `${process.cwd()}/src/design/typography.css`,
      "utf8",
    );

    expect(typography).toContain(
      'url("/fonts/fraunces-latin-variable.woff2")',
    );
    expect(typography).toContain(
      'url("/fonts/atkinson-hyperlegible-next-latin-variable.woff2")',
    );
    expect(typography).toMatch(/font-family:\s*"Fraunces"/);
    expect(typography).toMatch(
      /font-family:\s*"Atkinson Hyperlegible Next"/,
    );
  });

  it("honors OS motion reduction and only permits a reduction override", () => {
    const tokens = readFileSync(
      `${process.cwd()}/src/design/tokens.css`,
      "utf8",
    );

    expect(tokens).toContain("@media (prefers-reduced-motion: reduce)");
    expect(tokens).toContain(':root[data-reduced-motion="reduce"]');
    expect(tokens).toMatch(
      /:root\[data-reduced-motion="reduce"\]\s+\*,[\s\S]*?animation-duration:\s*1ms\s*!important/,
    );
    expect(tokens).not.toMatch(/data-reduced-motion=["'](?:allow|full|motion)/);
  });

  it("does not force the retired electric or light-only palettes", () => {
    const stylesheet = readFileSync(
      `${process.cwd()}/src/styles.css`,
      "utf8",
    );
    const simpleChat = stylesheet.slice(
      stylesheet.indexOf("/* ---------- simple chat mode ---------- */"),
      stylesheet.indexOf(".advanced-overview"),
    );

    expect(stylesheet).not.toContain("background: #f3f0e8");
    expect(stylesheet).not.toMatch(/rgba?\(\s*0\s*,\s*229\s*,\s*255/i);
    expect(simpleChat).not.toMatch(
      /#(?:f7f8f5|edf2ee|1f2726|66736f|45544f|ffffff|d5ded8|2f8a70)\b/i,
    );
    expect(simpleChat).toContain("var(--canvas)");
    expect(simpleChat).toContain("var(--surface)");
    expect(simpleChat).toContain("var(--ink)");
  });

  it("keeps primitive focus visible and removes press motion when requested", () => {
    const designSystem = readFileSync(
      `${process.cwd()}/src/design/design-system.css`,
      "utf8",
    );
    const main = readFileSync(`${process.cwd()}/src/main.tsx`, "utf8");

    expect(designSystem).toMatch(/\.wf-button:focus-visible/);
    expect(designSystem).toMatch(/outline:\s*2px solid var\(--surface\)/);
    expect(designSystem).not.toMatch(/outline:\s*none/);
    expect(designSystem).toMatch(
      /:is\(\.page-shell,\s*\.panel\)\s+:is\(input,\s*select,\s*textarea\)\.wf-field-control:focus-visible/,
    );
    expect(main.indexOf('"./design/design-system.css"')).toBeGreaterThan(
      main.indexOf('"./styles.css"'),
    );
    expect(designSystem).toMatch(
      /:root\[data-reduced-motion="reduce"\]\s+\.wf-button:active/,
    );
    expect(designSystem).toMatch(/transform:\s*none/);
  });
});
