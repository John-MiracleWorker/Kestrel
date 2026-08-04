// @vitest-environment node

/**
 * Reduced-motion contract gate.
 *
 * Pure CSS contract: no render, no browser. `tokens.css` is the design
 * system's motion authority; feature stylesheets must consume the
 * `--motion-*` tokens instead of hardcoding animation/transition durations,
 * so the OS-level media query and the `data-reduced-motion` override both
 * apply everywhere.
 */
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const SRC = `${process.cwd()}/src`;

function listCssFiles(directory: string): string[] {
  const entries = readdirSync(directory);
  const files: string[] = [];
  for (const entry of entries) {
    const path = join(directory, entry);
    if (statSync(path).isDirectory()) {
      files.push(...listCssFiles(path));
    } else if (entry.endsWith(".css")) {
      files.push(path);
    }
  }
  return files;
}

describe("reduced-motion contract", () => {
  const tokens = readFileSync(`${SRC}/design/tokens.css`, "utf8");

  it("honors the OS prefers-reduced-motion media query", () => {
    expect(tokens).toContain("@media (prefers-reduced-motion: reduce)");
    const media = tokens.slice(
      tokens.indexOf("@media (prefers-reduced-motion: reduce)"),
    );
    expect(media).toMatch(/animation-duration:\s*1ms\s*!important/);
    expect(media).toMatch(/animation-iteration-count:\s*1\s*!important/);
    expect(media).toMatch(/transition-duration:\s*1ms\s*!important/);
    expect(media).toMatch(/scroll-behavior:\s*auto\s*!important/);
  });

  it("supports the explicit data-reduced-motion override", () => {
    expect(tokens).toContain(':root[data-reduced-motion="reduce"]');
    expect(tokens).toMatch(
      /:root\[data-reduced-motion="reduce"\]\s*\{[^}]*--motion-fast:\s*1ms/,
    );
    expect(tokens).toMatch(
      /:root\[data-reduced-motion="reduce"\]\s*\{[^}]*--motion-standard:\s*1ms/,
    );
    expect(tokens).toMatch(
      /:root\[data-reduced-motion="reduce"\][\s\S]*?animation-duration:\s*1ms\s*!important/,
    );
    expect(tokens).toMatch(
      /:root\[data-reduced-motion="reduce"\][\s\S]*?transition-duration:\s*1ms\s*!important/,
    );
    expect(tokens).toMatch(
      /:root\[data-reduced-motion="reduce"\][\s\S]*?scroll-behavior:\s*auto\s*!important/,
    );
    // The override only ever reduces motion; it never re-enables it.
    expect(tokens).not.toMatch(/data-reduced-motion=["'](?:allow|full|motion)/);
  });

  it("keeps feature stylesheets on the --motion-* tokens (no hardcoded durations)", () => {
    const offenders: string[] = [];
    for (const path of listCssFiles(SRC)) {
      const css = readFileSync(path, "utf8");
      const relative = path.slice(SRC.length + 1);
      // Find hardcoded animation/transition durations outside reduce blocks.
      // Any `transition: ... <N>ms|s` or `animation: ... <N>ms|s` or
      // `transition-duration|animation-duration: <N>ms|s` that is not `1ms`
      // inside a reduce override must go through var(--motion-*).
      const durationPattern =
        new RegExp("(?:transition|animation)(?:-duration)?:[^;}]*?\\b(\\d+(?:\\.\\d+)?)(m?s)\\b", "g");
      let match: RegExpExecArray | null;
      while ((match = durationPattern.exec(css)) !== null) {
        const value = Number.parseFloat(match[1]);
        const unit = match[2];
        const ms = unit === "s" ? value * 1000 : value;
        if (ms <= 1) continue; // reduce-override 1ms/0ms durations are the contract
        offenders.push(`${relative}: ${match[0].trim()}`);
      }

      // Keyframe-loop allowlist: indefinite spinner/skeleton/typing/pulse loops.
      // These are status indicators that must keep moving; reduced-motion is
      // enforced by the global 1ms !important override in tokens.css (both the
      // OS media query and the data-reduced-motion attribute), not by the
      // --motion-* tokens, which only cover finite UI transitions.
      const allowlistPattern =
        new RegExp("animation:\\s*(?:wf-spin|wf-skeleton-shift|mission-spin|pulse|typing)\\s+[^;}]*?\\b\\d+(?:\\.\\d+)?m?s\\b", "g");
      while ((match = allowlistPattern.exec(css)) !== null) {
        const needle = match[0].trim();
        const index = offenders.indexOf(`${relative}: ${needle}`);
        if (index !== -1) offenders.splice(index, 1);
      }
    }
    expect(
      offenders,
      `hardcoded motion durations bypassing var(--motion-*):\n${offenders.join("\n")}`,
    ).toEqual([]);
  });
});
