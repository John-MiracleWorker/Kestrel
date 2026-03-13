#!/usr/bin/env node

const fs = require("fs");
const path = require("path");
const { spawnSync } = require("child_process");

const repoRoot = path.resolve(__dirname, "..");
const cwd = process.cwd();
const [tool, ...rawArgs] = process.argv.slice(2);

if (!tool) {
  console.error("Usage: node scripts/run-python-tool.cjs <module> [args...]");
  process.exit(1);
}

function existingPath(candidate) {
  return fs.existsSync(candidate) ? candidate : null;
}

function unique(values) {
  return [...new Set(values.filter(Boolean))];
}

function resolvePythonCommand() {
  const envPython = process.env.KESTREL_PYTHON;
  const candidates = unique([
    envPython,
    path.join(repoRoot, "venv", "Scripts", "python.exe"),
    path.join(repoRoot, ".venv", "Scripts", "python.exe"),
    path.join(repoRoot, "venv", "bin", "python"),
    path.join(repoRoot, ".venv", "bin", "python"),
  ]);

  for (const candidate of candidates) {
    const pythonPath = existingPath(candidate);
    if (pythonPath) {
      return { command: pythonPath, prefixArgs: [] };
    }
  }

  if (process.platform === "win32") {
    return { command: "py", prefixArgs: ["-3"] };
  }

  return { command: "python3", prefixArgs: [] };
}

function hasFlag(args, flag) {
  return args.includes(flag) || args.some((value) => value.startsWith(`${flag}=`));
}

function buildArgs() {
  const args = [...rawArgs];

  if (tool === "pytest" && !hasFlag(args, "--basetemp")) {
    args.push("--basetemp", path.join(cwd, ".pytest_cache", "tmp"));
  }

  if (tool === "mypy" && !hasFlag(args, "--config-file")) {
    args.unshift(path.join(repoRoot, "mypy.ini"));
    args.unshift("--config-file");
  }

  if (tool === "ruff" && !hasFlag(args, "--config")) {
    args.unshift(path.join(repoRoot, "ruff.toml"));
    args.unshift("--config");
  }

  return args;
}

const args = [...buildArgs()];
const candidates = [];
const resolved = resolvePythonCommand();
candidates.push(resolved);

if (resolved.command === "python3") {
  candidates.push({ command: "python", prefixArgs: [] });
}

let result = null;
for (const candidate of candidates) {
  result = spawnSync(
    candidate.command,
    [...candidate.prefixArgs, "-m", tool, ...args],
    {
      cwd,
      stdio: "inherit",
      env: process.env,
      shell: false,
    },
  );
  if (!result.error) {
    break;
  }
}

if (!result || result.error) {
  console.error(result?.error?.message ?? "Unable to locate a Python interpreter.");
  process.exit(1);
}

process.exit(result.status ?? 1);
