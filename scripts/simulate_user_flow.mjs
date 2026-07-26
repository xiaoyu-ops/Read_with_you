#!/usr/bin/env node

import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const scriptPath = fileURLToPath(import.meta.url);
const rootDir = path.resolve(path.dirname(scriptPath), "..");
const requireFromFrontend = createRequire(path.join(rootDir, "frontend", "package.json"));

const { chromium } = requireFromFrontend("playwright");

const args = new Set(process.argv.slice(2));
const getArg = (name, fallback) => {
  const idx = process.argv.indexOf(name);
  return idx >= 0 && process.argv[idx + 1] ? process.argv[idx + 1] : fallback;
};

const options = {
  query: getArg("--query", "Attention Is All You Need"),
  apiBase: getArg("--api-base", "http://127.0.0.1:8100"),
  appBase: getArg("--app-base", "http://127.0.0.1:3100"),
  headed: args.has("--headed"),
  keepServers: args.has("--keep-servers"),
  noStart: args.has("--no-start"),
  fullLlm: args.has("--full-llm"),
};

const outDir = path.join(rootDir, "output", "playwright");
fs.mkdirSync(outDir, { recursive: true });

const reportPath = path.join(outDir, "user-flow-report.json");
const screenshotPath = path.join(outDir, "user-flow.png");
const viewportScreenshotPath = path.join(outDir, "user-flow-viewport.png");
const backendLog = path.join(outDir, "backend.log");
const frontendLog = path.join(outDir, "frontend.log");
const started = [];
const apiUrl = new URL(options.apiBase);
const appUrl = new URL(options.appBase);

function log(message) {
  console.log(`[user-flow] ${message}`);
}

function spawnLogged(label, command, commandArgs, cwd, logPath, extraEnv = {}) {
  const logStream = fs.createWriteStream(logPath, { flags: "a" });
  const child = spawn(command, commandArgs, {
    cwd,
    env: { ...process.env, ...extraEnv },
    stdio: ["ignore", "pipe", "pipe"],
  });
  child.stdout.pipe(logStream);
  child.stderr.pipe(logStream);
  child.on("exit", (code) => {
    logStream.write(`\n[${label}] exited with ${code}\n`);
    logStream.end();
  });
  started.push(child);
  log(`started ${label} pid=${child.pid}`);
  return child;
}

async function fetchOk(url) {
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 1500);
    const resp = await fetch(url, { signal: controller.signal });
    clearTimeout(timer);
    return resp.ok;
  } catch {
    return false;
  }
}

async function waitFor(url, label, timeoutMs = 90000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    if (await fetchOk(url)) return;
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  throw new Error(`${label} did not become ready: ${url}`);
}

async function waitForInlineReader(page, timeoutMs = 90000) {
  await page
    .locator('[data-reader-mode="inline_translation"]')
    .waitFor({ state: "visible", timeout: timeoutMs });
  await page.locator(".inline-reader-loading").waitFor({ state: "detached", timeout: timeoutMs });
  await page.locator(".reader-pdf-canvas").first().waitFor({ state: "visible", timeout: timeoutMs });

  const legacyReader = page.locator(
    ".reader-grid, .reader-pane, .reader-sync-rail, .reader-side-switch, [data-reader-mode='dual']",
  );
  if ((await legacyReader.count()) > 0) {
    throw new Error("Legacy dual-pane reader is still mounted");
  }
}

async function waitForTranslationAccess(page, blockIndex, translation, layout, timeoutMs = 60000) {
  const preview = translation.slice(0, 12);
  const regions = layout.regions
    .filter((region) => region.block_index === blockIndex)
    .sort((left, right) => left.page - right.page || left.flow_order - right.flow_order);

  if (regions.length === 0) {
    await page.getByRole("button", { name: /未定位译文 \d+/ }).click();
    const drawer = page.locator(".inline-unmapped-drawer");
    await drawer.waitFor({ state: "visible", timeout: timeoutMs });
    await drawer.getByText(preview).waitFor({ state: "visible", timeout: timeoutMs });
    return "unmapped_drawer";
  }

  const firstRegion = regions[0];
  const pageShell = page.locator(`[data-page-number="${firstRegion.page}"]`);
  await pageShell.scrollIntoViewIfNeeded();
  await pageShell.locator(".reader-pdf-canvas").waitFor({ state: "visible", timeout: timeoutMs });

  const access = pageShell
    .locator(
      `[data-block-index="${blockIndex}"][data-render-policy="replace"], ` +
        `[data-block-index="${blockIndex}"][data-render-policy="preserve"]`,
    )
    .first();
  await access.waitFor({ state: "visible", timeout: timeoutMs });
  const policy = await access.getAttribute("data-render-policy");
  if (policy === "replace") {
    await access.getByText(preview).waitFor({ state: "visible", timeout: timeoutMs });
    return "inline_replace";
  }

  await access.click();
  const inspector = page.locator('.inline-inspector[data-inspector-side="translation"]');
  await inspector.waitFor({ state: "visible", timeout: timeoutMs });
  await inspector.getByText(preview).waitFor({ state: "visible", timeout: timeoutMs });
  return "preserved_inspector";
}

async function apiJson(pathname, init = {}) {
  const resp = await fetch(`${options.apiBase}${pathname}`, init);
  if (!resp.ok) {
    const text = await resp.text().catch(() => "");
    throw new Error(`${pathname} failed: ${resp.status} ${text.slice(0, 200)}`);
  }
  return resp.json();
}

async function apiMaybeJson(pathname, init = {}) {
  const resp = await fetch(`${options.apiBase}${pathname}`, init);
  if (resp.status === 404) return null;
  if (!resp.ok) {
    const text = await resp.text().catch(() => "");
    throw new Error(`${pathname} failed: ${resp.status} ${text.slice(0, 200)}`);
  }
  return resp.json();
}

async function launchBrowser() {
  try {
    return await chromium.launch({ headless: !options.headed });
  } catch (error) {
    const chromePath = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
    if (fs.existsSync(chromePath)) {
      report.browserFallback = chromePath;
      return chromium.launch({
        executablePath: chromePath,
        headless: !options.headed,
      });
    }
    throw error;
  }
}

function stopStarted() {
  if (options.keepServers) return;
  for (const child of started.reverse()) {
    if (!child.killed) child.kill("SIGTERM");
  }
}

process.on("SIGINT", () => {
  stopStarted();
  process.exit(130);
});
process.on("SIGTERM", () => {
  stopStarted();
  process.exit(143);
});

const report = {
  ok: false,
  query: options.query,
  appBase: options.appBase,
  apiBase: options.apiBase,
  fullLlm: options.fullLlm,
  steps: [],
  screenshot: screenshotPath,
  viewportScreenshot: viewportScreenshotPath,
};

let browser;
let page;

try {
  if (!options.noStart) {
    if (!(await fetchOk(`${options.apiBase}/`))) {
      fs.writeFileSync(backendLog, "");
      spawnLogged(
        "backend",
        "python",
        ["-m", "uvicorn", "backend.api.main:app", "--host", apiUrl.hostname, "--port", apiUrl.port || "80"],
        rootDir,
        backendLog,
        { PEINIDU_CORS_ORIGINS: options.appBase },
      );
    }
    if (!(await fetchOk(options.appBase))) {
      fs.writeFileSync(frontendLog, "");
      spawnLogged(
        "frontend",
        "npm",
        ["run", "dev", "--", "--hostname", appUrl.hostname, "--port", appUrl.port || "80"],
        path.join(rootDir, "frontend"),
        frontendLog,
        { NEXT_PUBLIC_API_BASE: options.apiBase },
      );
    }
  }

  await waitFor(`${options.apiBase}/`, "backend");
  await waitFor(options.appBase, "frontend");
  report.steps.push("services-ready");

  browser = await launchBrowser();
  page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  page.on("pageerror", (err) => {
    report.pageError = String(err);
  });

  await page.goto(options.appBase, { waitUntil: "networkidle" });
  await page.getByPlaceholder("输入论文标题 / arXiv ID / URL").fill(options.query);
  await page.getByRole("button", { name: "检索" }).click();
  await page.getByText(/找到 \d+ 篇候选/).waitFor({ timeout: 90000 });
  report.steps.push("search-results-visible");

  const firstExtractable = page.locator("button.border-beam:not([disabled])").first();
  await firstExtractable.waitFor({ timeout: 90000 });
  report.selectedCandidate = (await firstExtractable.innerText()).split("\n").slice(0, 4).join(" | ");
  await firstExtractable.click();

  await page.waitForURL(/\/paper\//, { timeout: 180000 });
  await waitForInlineReader(page);
  report.steps.push("reader-visible");

  const paperUrl = new URL(page.url());
  const arxivId = decodeURIComponent(paperUrl.pathname.split("/paper/")[1] || "");
  if (!arxivId) throw new Error(`Could not infer arXiv id from URL: ${page.url()}`);
  report.arxivId = arxivId;

  const paper = await apiJson(`/papers/${encodeURIComponent(arxivId)}`);
  const layout = await apiJson(`/papers/${encodeURIComponent(arxivId)}/translation-layout`);
  report.blocks = paper.blocks.length;
  report.blockTypes = [...new Set(paper.blocks.map((block) => block.type))];
  report.layout = {
    adapter: layout.adapter,
    pages: layout.page_count,
    regions: layout.regions.length,
  };
  if (paper.blocks.length < 3) throw new Error(`Extraction returned too few blocks: ${paper.blocks.length}`);
  report.steps.push("paper-api-visible");

  const translatableByIndex = new Map(
    paper.blocks
      .filter((block) => ["pending", "error"].includes(block.status))
      .map((block) => [block.index, block]),
  );
  const preferredRegion = [...layout.regions]
    .sort(
      (left, right) =>
        Number(right.render_policy === "replace") - Number(left.render_policy === "replace") ||
        left.page - right.page ||
        left.flow_order - right.flow_order,
    )
    .find((region) => translatableByIndex.has(region.block_index));
  const translatable = preferredRegion
    ? translatableByIndex.get(preferredRegion.block_index)
    : translatableByIndex.values().next().value;
  if (!translatable) throw new Error("No translatable block found");
  const retry = await apiJson(`/translate/${encodeURIComponent(arxivId)}/block/${translatable.index}`, {
    method: "POST",
  });
  if (retry.status !== "done" || !retry.translation) {
    throw new Error(`Single block translation did not finish: ${JSON.stringify(retry)}`);
  }
  report.translatedBlock = retry.index;
  report.translationPreview = retry.translation.slice(0, 80);
  report.steps.push("single-block-translation-done");

  await page.reload({ waitUntil: "networkidle" });
  await waitForInlineReader(page);
  report.translationAccess = await waitForTranslationAccess(
    page,
    retry.index,
    retry.translation,
    layout,
  );
  report.steps.push("translation-visible-after-reload");

  if (options.fullLlm) {
    let analysis = await apiMaybeJson(`/analyze/${encodeURIComponent(arxivId)}`);
    if (analysis) {
      report.steps.push("agent-analysis-existing");
    } else {
      const [analysisResponse] = await Promise.all([
        page.waitForResponse(
          (resp) =>
            resp.url().includes(`/analyze/${encodeURIComponent(arxivId)}`) &&
            resp.request().method() === "POST",
          { timeout: 300000 },
        ),
        page.getByRole("button", { name: /运行四 Agent 分析/ }).click(),
      ]);
      if (!analysisResponse.ok()) {
        throw new Error(`Agent analysis failed: ${analysisResponse.status()} ${await analysisResponse.text()}`);
      }
      analysis = await analysisResponse.json();
    }
    await page.getByText("可复现性判断").waitFor({ timeout: 60000 });
    report.analysis = {
      hasSummary: Boolean(analysis.summary),
      evidenceCount: analysis.reproducibility?.evidence?.length ?? 0,
      improvements: analysis.improvements?.length ?? 0,
      highlights: analysis.highlights?.length ?? 0,
    };
    report.steps.push("agent-analysis-done");
  }

  await page.screenshot({ path: viewportScreenshotPath, fullPage: false });
  await page.screenshot({ path: screenshotPath, fullPage: true });
  report.ok = true;
  fs.writeFileSync(reportPath, JSON.stringify(report, null, 2));
  log(`ok: ${reportPath}`);
} catch (error) {
  report.error = error instanceof Error ? error.message : String(error);
  if (page) {
    await page.screenshot({ path: viewportScreenshotPath, fullPage: false }).catch(() => {});
    await page.screenshot({ path: screenshotPath, fullPage: true }).catch(() => {});
  }
  fs.writeFileSync(reportPath, JSON.stringify(report, null, 2));
  console.error(`[user-flow] failed: ${report.error}`);
  console.error(`[user-flow] report: ${reportPath}`);
  process.exitCode = 1;
} finally {
  if (browser) await browser.close().catch(() => {});
  stopStarted();
}
