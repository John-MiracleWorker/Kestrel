#!/usr/bin/env node

const fs = require("fs");
const path = require("path");

const repoRoot = path.resolve(__dirname, "..");

function remove(targetPath) {
  fs.rmSync(targetPath, { recursive: true, force: true });
}

remove(path.join(repoRoot, "node_modules"));

for (const entry of fs.readdirSync(path.join(repoRoot, "packages"), { withFileTypes: true })) {
  if (!entry.isDirectory()) {
    continue;
  }

  remove(path.join(repoRoot, "packages", entry.name, "node_modules"));
  remove(path.join(repoRoot, "packages", entry.name, "dist"));
}
