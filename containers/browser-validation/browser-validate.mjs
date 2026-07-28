#!/usr/bin/env node

import { createHash } from "node:crypto";
import { spawn } from "node:child_process";
import { createRequire } from "node:module";
import process from "node:process";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");
const axeSource = require("axe-core").source;

const MAX_SPEC_BYTES = 128 * 1024;
const MAX_ITEMS = 128;
const MAX_TEXT = 2_000;
const MAX_SCREENSHOT_BYTES = 240 * 1024;
const VIEWPORT = { width: 960, height: 720 };

if (process.argv[2] === "--self-test") {
  await selfTest();
  process.exit(0);
}

const spec = parseSpec(process.argv);
const report = {
  schema: "kestrel.browser_validation.v1",
  rendered: false,
  target_url: spec.target_url,
  assertions: [],
  interactions: [],
  console_errors: [],
  network_errors: [],
  accessibility: { violations: [] },
  dom_summary: {},
  screenshot: null
};

let server = null;
let browser = null;
try {
  server = startServer(spec);
  browser = await chromium.launch({
    headless: true,
    args: ["--disable-dev-shm-usage", "--no-sandbox"]
  });
  const context = await browser.newContext({
    viewport: VIEWPORT,
    serviceWorkers: "block"
  });
  const page = await context.newPage();
  installEvidenceListeners(page, report);
  await installNetworkPolicy(context, spec, report);

  const response = await openWhenReady(page, spec.target_url, spec.limits.timeout_seconds, server);
  report.rendered = response !== null && response.status() < 500;
  report.interactions = await runInteractions(page, spec.interactions);
  report.assertions = await runAssertions(page, spec.assertions);
  report.accessibility = await accessibilityEvidence(page);
  report.dom_summary = await domSummary(page);
  report.screenshot = await screenshotEvidence(page, context, report);
} catch (error) {
  pushBounded(report.network_errors, {
    kind: "validation_error",
    message: boundedMessage(error)
  });
} finally {
  if (browser) await browser.close().catch(() => undefined);
  await stopServer(server);
}

process.stdout.write(JSON.stringify(report));

function parseSpec(argv) {
  if (argv.length !== 4 || argv[2] !== "--spec-base64url") {
    throw new Error("usage: browser-validate --spec-base64url <payload>");
  }
  const encoded = String(argv[3]);
  const bytes = Buffer.from(encoded, "base64url");
  if (bytes.length === 0 || bytes.length > MAX_SPEC_BYTES) {
    throw new Error("browser validation spec exceeds its bounded size");
  }
  const value = JSON.parse(bytes.toString("utf8"));
  if (
    value?.schema !== "kestrel.browser_validation_request.v1"
    || !Array.isArray(value.start_command)
    || value.start_command.length === 0
    || typeof value.target_url !== "string"
    || !Array.isArray(value.assertions)
    || !Array.isArray(value.interactions)
    || typeof value.network_policy !== "object"
    || typeof value.network_fixtures !== "object"
  ) {
    throw new Error("browser validation spec is invalid");
  }
  const target = new URL(value.target_url);
  if (
    target.protocol !== "http:"
    || !["127.0.0.1", "localhost"].includes(target.hostname)
    || !target.port
  ) {
    throw new Error("browser target must be container-local HTTP");
  }
  return value;
}

function startServer(spec) {
  const [command, ...args] = spec.start_command.map(String);
  const target = new URL(spec.target_url);
  const environment = {
    PATH: process.env.PATH ?? "/usr/local/bin:/usr/bin:/bin",
    HOME: "/tmp",
    TMPDIR: "/tmp",
    CI: "1",
    BROWSER: "none",
    NODE_ENV: "production",
    PORT: target.port
  };
  return spawn(command, args, {
    cwd: "/extension",
    env: environment,
    detached: true,
    stdio: "ignore"
  });
}

async function openWhenReady(page, targetUrl, timeoutSeconds, server) {
  const deadline = Date.now() + Math.max(5_000, Number(timeoutSeconds) * 1_000);
  let lastError = null;
  while (Date.now() < deadline) {
    if (server.exitCode !== null) {
      throw new Error(`project server exited before validation with ${server.exitCode}`);
    }
    try {
      return await page.goto(targetUrl, {
        waitUntil: "domcontentloaded",
        timeout: Math.min(5_000, Math.max(500, deadline - Date.now()))
      });
    } catch (error) {
      lastError = error;
      await delay(200);
    }
  }
  throw lastError ?? new Error("project route did not become ready");
}

function installEvidenceListeners(page, report) {
  page.on("console", (message) => {
    if (message.type() === "error") {
      pushBounded(report.console_errors, {
        kind: "console",
        message: boundedText(message.text())
      });
    }
  });
  page.on("pageerror", (error) => {
    pushBounded(report.console_errors, {
      kind: "pageerror",
      message: boundedMessage(error)
    });
  });
  page.on("requestfailed", (request) => {
    pushBounded(report.network_errors, {
      kind: "request_failed",
      method: request.method(),
      url: boundedText(request.url()),
      message: boundedText(request.failure()?.errorText ?? "request failed")
    });
  });
}

async function installNetworkPolicy(context, spec, report) {
  const targetOrigin = new URL(spec.target_url).origin;
  const fixtures = spec.network_fixtures ?? {};
  await context.route("**/*", async (route) => {
    const request = route.request();
    const url = request.url();
    if (
      url.startsWith("data:")
      || url.startsWith("blob:")
      || url.startsWith("about:")
      || new URL(url).origin === targetOrigin
    ) {
      await route.continue();
      return;
    }
    const fixture = fixtures[url];
    if (fixture) {
      await route.fulfill({
        status: Number(fixture.status),
        contentType: String(fixture.content_type),
        body: String(fixture.body),
        headers: { "access-control-allow-origin": "*" }
      });
      return;
    }
    pushBounded(report.network_errors, {
      kind: "blocked_by_policy",
      method: request.method(),
      url: boundedText(url),
      message: "No exact deterministic fixture was supplied."
    });
    await route.abort("blockedbyclient");
  });
}

async function runInteractions(page, interactions) {
  const results = [];
  for (const item of interactions.slice(0, 32)) {
    const result = {
      action: String(item.action),
      selector: String(item.selector),
      passed: false,
      detail: ""
    };
    try {
      const locator = page.locator(result.selector).first();
      const value = item.value == null ? null : String(item.value);
      if (result.action === "click") await locator.click();
      else if (result.action === "fill") await locator.fill(value ?? "");
      else if (result.action === "check") await locator.check();
      else if (result.action === "select") await locator.selectOption(value ?? "");
      else if (result.action === "press") await locator.press(value ?? "Enter");
      else throw new Error("unsupported interaction");
      result.passed = true;
      result.detail = "completed";
    } catch (error) {
      result.detail = boundedMessage(error);
    }
    results.push(result);
  }
  return results;
}

async function runAssertions(page, assertions) {
  const results = [];
  for (const item of assertions.slice(0, 32)) {
    const result = {
      selector: String(item.selector),
      expectation: String(item.expectation),
      passed: false,
      detail: ""
    };
    try {
      const locator = page.locator(result.selector);
      const first = locator.first();
      const value = item.value == null ? null : String(item.value);
      if (result.expectation === "visible") {
        result.passed = await first.isVisible();
      } else if (result.expectation === "hidden") {
        result.passed = await locator.count() === 0 || !(await first.isVisible());
      } else if (result.expectation === "text") {
        result.passed = boundedText(await first.innerText()).includes(value ?? "");
      } else if (result.expectation === "count") {
        result.passed = await locator.count() === Number.parseInt(value ?? "", 10);
      } else if (result.expectation === "attribute") {
        const separator = (value ?? "").indexOf("=");
        if (separator < 1) throw new Error("attribute expects name=value");
        const name = value.slice(0, separator);
        const expected = value.slice(separator + 1);
        result.passed = await first.getAttribute(name) === expected;
      } else {
        throw new Error("unsupported assertion");
      }
      result.detail = result.passed ? "matched" : "did not match";
    } catch (error) {
      result.detail = boundedMessage(error);
    }
    results.push(result);
  }
  return results;
}

async function accessibilityEvidence(page) {
  await page.addScriptTag({ content: axeSource });
  const raw = await page.evaluate(async () => globalThis.axe.run(document, {
    resultTypes: ["violations"]
  }));
  return {
    violations: raw.violations.slice(0, MAX_ITEMS).map((item) => ({
      id: boundedText(item.id),
      impact: boundedText(item.impact ?? "unknown"),
      help: boundedText(item.help),
      help_url: boundedText(item.helpUrl),
      nodes: item.nodes.length
    }))
  };
}

async function domSummary(page) {
  return page.evaluate(({ maxText }) => {
    const text = (document.body?.innerText ?? "").replace(/\s+/g, " ").trim();
    return {
      title: document.title.slice(0, 500),
      url: location.href.slice(0, 2_000),
      landmarks: Array.from(document.querySelectorAll(
        "main,nav,header,footer,aside,[role=main],[role=navigation]"
      )).slice(0, 32).map((item) => item.getAttribute("role") || item.tagName.toLowerCase()),
      headings: Array.from(document.querySelectorAll("h1,h2,h3")).slice(0, 32)
        .map((item) => (item.textContent ?? "").trim().slice(0, 500)),
      text_excerpt: text.slice(0, maxText)
    };
  }, { maxText: 8_000 });
}

async function screenshotEvidence(page, context, report) {
  const session = await context.newCDPSession(page);
  for (const quality of [70, 50, 30]) {
    const capture = await session.send("Page.captureScreenshot", {
      format: "webp",
      quality,
      fromSurface: true
    });
    const bytes = Buffer.from(capture.data, "base64");
    if (bytes.length <= MAX_SCREENSHOT_BYTES) {
      return {
        media_type: "image/webp",
        data_base64: capture.data,
        sha256: createHash("sha256").update(bytes).digest("hex"),
        width: VIEWPORT.width,
        height: VIEWPORT.height
      };
    }
  }
  pushBounded(report.network_errors, {
    kind: "artifact_limit",
    message: "Screenshot exceeded the bounded report size."
  });
  return null;
}

async function stopServer(server) {
  if (!server || server.exitCode !== null) return;
  try {
    process.kill(-server.pid, "SIGTERM");
  } catch {
    return;
  }
  await Promise.race([new Promise((resolve) => server.once("exit", resolve)), delay(500)]);
  if (server.exitCode === null) {
    try {
      process.kill(-server.pid, "SIGKILL");
    } catch {
      return;
    }
  }
}

async function selfTest() {
  const browser = await chromium.launch({
    headless: true,
    args: ["--disable-dev-shm-usage", "--no-sandbox"]
  });
  try {
    const page = await browser.newPage();
    await page.setContent("<main><h1>Kestrel browser validation</h1></main>");
    await page.addScriptTag({ content: axeSource });
    const heading = await page.locator("h1").innerText();
    const axeReady = await page.evaluate(() => typeof globalThis.axe?.run === "function");
    process.stdout.write(JSON.stringify({
      schema: "kestrel.browser_validation_image.v1",
      browser: "chromium",
      heading,
      axe_ready: axeReady
    }));
  } finally {
    await browser.close();
  }
}

function pushBounded(items, value) {
  if (items.length < MAX_ITEMS) items.push(value);
}

function boundedMessage(error) {
  return boundedText(error instanceof Error ? error.message : String(error));
}

function boundedText(value) {
  return String(value ?? "").replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/g, "")
    .slice(0, MAX_TEXT);
}

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}
