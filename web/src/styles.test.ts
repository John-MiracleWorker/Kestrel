import { describe, expect, it } from "vitest";

declare module "node:fs" {
  export function readFileSync(path: string, encoding: "utf8"): string;
}

import { readFileSync } from "node:fs";

declare const process: { cwd(): string };

describe("styles.css", () => {
  it("keeps production CSS free of HTML wrappers and starts imports before rules", () => {
    const stylesheet = readFileSync(`${process.cwd()}/src/styles.css`, "utf8");

    expect(stylesheet).not.toMatch(/<\/?style\b/i);

    const firstStatement = stylesheet.trimStart();
    expect(firstStatement).toMatch(/^@import\s+url\(/);
  });
});
