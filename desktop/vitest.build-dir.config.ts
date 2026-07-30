import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "node",
    include: ["scripts/build-dir.test.mjs", "scripts/audit-reviewed.test.mjs"]
  }
});
