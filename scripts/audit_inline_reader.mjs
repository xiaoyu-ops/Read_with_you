#!/usr/bin/env node

/**
 * Read-only browser audit for the inline PDF translation reader.
 *
 * The script reuses running frontend/backend services, visits every PDF page so
 * virtualized overlays are actually rendered, and fails closed on inaccessible
 * translations, pending fits, overflows, protected overlaps, text mismatches,
 * silent omissions, low safe coverage, or low replacement confidence.
 *
 * Usage:
 *   node scripts/audit_inline_reader.mjs \
 *     --paper 1706.03762 \
 *     --source-class mineru_complex
 */

import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import { createRequire } from "node:module";
import { createHash } from "node:crypto";
import { fileURLToPath } from "node:url";


const scriptPath = fileURLToPath(import.meta.url);
const rootDir = path.resolve(path.dirname(scriptPath), "..");
const requireFromFrontend = createRequire(path.join(rootDir, "frontend", "package.json"));
const { chromium } = requireFromFrontend("playwright");

const SOURCE_THRESHOLDS = Object.freeze({
  arxiv_digital: 0.90,
  local_digital: 0.85,
  mineru_complex: 0.80,
  scan_ocr: 0.70,
});
const HYBRID_LAYOUT_ADAPTER = "hybrid_poppler_mineru";
const HYBRID_GEOMETRY_SOURCES = new Set(["poppler_bbox_layout", "mineru_middle"]);
const PRECISE_LAYOUT_ADAPTERS = new Set([
  ...HYBRID_GEOMETRY_SOURCES,
  HYBRID_LAYOUT_ADAPTER,
]);
const MINIMUM_REPLACE_CONFIDENCE = 0.92;
const FIRST_READABLE_TARGET_MS = 2_500;
const INSPECTOR_TARGET_MS = 100;

function argument(name, fallback = null) {
  const index = process.argv.indexOf(name);
  return index >= 0 && process.argv[index + 1] ? process.argv[index + 1] : fallback;
}

const flags = new Set(process.argv.slice(2));
const paperId = argument("--paper", argument("--arxiv-id", "1706.03762"));
const appBase = argument("--app-base", "http://127.0.0.1:3000").replace(/\/$/, "");
const apiBase = argument("--api-base", "http://127.0.0.1:8000").replace(/\/$/, "");
const requestedSourceClass = argument("--source-class", "auto");
const timeoutMs = positiveInteger(argument("--timeout-ms", "90000"), 90_000);
const settleTimeoutMs = positiveInteger(argument("--settle-timeout-ms", "20000"), 20_000);
const headed = flags.has("--headed");
const enforcePerformance = !flags.has("--skip-performance");
const requireComplete = flags.has("--require-complete");
const includeDebugBlocks = flags.has("--debug-blocks");
const safePaperId = String(paperId).replace(/[^a-zA-Z0-9._-]+/g, "-");
const defaultOutput = path.join(rootDir, "output", "playwright", `inline-audit-${safePaperId}.json`);
const outputPath = path.resolve(argument("--output", defaultOutput));
const screenshotPath = outputPath.replace(/\.json$/i, "-reader.png");

function positiveInteger(value, fallback) {
  const parsed = Number.parseInt(String(value), 10);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : fallback;
}

function ratioBelow(value, threshold) {
  return typeof value !== "number" || !Number.isFinite(value) || value < threshold;
}

const REPORT_COUNT_FIELDS = Object.freeze([
  "translatable_block_count",
  "completed_translation_count",
  "pending_translation_count",
  "error_translation_count",
  "incomplete_translation_count",
  "eligible_text_count",
  "protected_excluded_count",
  "inline_count",
  "panel_count",
  "unmapped_count",
  "safe_inline_count",
  "replace_region_count",
  "fit_pending_count",
  "translation_mismatch_count",
  "protected_overlap_count",
  "overflow_count",
  "silent_missing_count",
]);

const REPORT_RATIO_FIELDS = Object.freeze([
  "accessibility_ratio",
  "safe_inline_coverage",
  "replace_average_confidence",
]);

function validateComponentReport(report) {
  if (!report || typeof report !== "object" || Array.isArray(report)) {
    throw new Error("Reader audit report is not an object");
  }
  for (const field of REPORT_COUNT_FIELDS) {
    if (!Number.isInteger(report[field]) || report[field] < 0) {
      throw new Error(`Reader audit report has invalid count: ${field}`);
    }
  }
  for (const field of REPORT_RATIO_FIELDS) {
    if (
      typeof report[field] !== "number" ||
      !Number.isFinite(report[field]) ||
      report[field] < 0 ||
      report[field] > 1
    ) {
      throw new Error(`Reader audit report has invalid ratio: ${field}`);
    }
  }
  for (const field of ["failure_counts", "coverage_failure_counts"]) {
    const counts = report[field];
    if (!counts || typeof counts !== "object" || Array.isArray(counts)) {
      throw new Error(`Reader audit report has invalid map: ${field}`);
    }
    for (const [reason, count] of Object.entries(counts)) {
      if (!reason || !Number.isInteger(count) || count < 0) {
        throw new Error(`Reader audit report has invalid ${field} entry`);
      }
    }
  }
  if (
    report.completed_translation_count + report.incomplete_translation_count !==
      report.translatable_block_count ||
    report.pending_translation_count + report.error_translation_count !==
      report.incomplete_translation_count
  ) {
    throw new Error("Reader audit report translation counts are inconsistent");
  }
  if (
    report.inline_count + report.panel_count + report.unmapped_count !==
    report.completed_translation_count
  ) {
    throw new Error("Reader audit report accessibility counts are inconsistent");
  }
  if (
    report.safe_inline_count > report.inline_count ||
    report.safe_inline_count > report.eligible_text_count ||
    report.protected_excluded_count > report.completed_translation_count
  ) {
    throw new Error("Reader audit report coverage counts are inconsistent");
  }
  const expectedAccessibility =
    report.completed_translation_count === 0
      ? 1
      : (report.inline_count + report.panel_count + report.unmapped_count) /
        report.completed_translation_count;
  const expectedCoverage =
    report.eligible_text_count === 0
      ? 0
      : report.safe_inline_count / report.eligible_text_count;
  if (
    Math.abs(report.accessibility_ratio - expectedAccessibility) > 1e-9 ||
    Math.abs(report.safe_inline_coverage - expectedCoverage) > 1e-9
  ) {
    throw new Error("Reader audit report ratios do not match their counts");
  }
  const coverageFailureTotal = Object.values(report.coverage_failure_counts).reduce(
    (total, count) => total + count,
    0,
  );
  if (coverageFailureTotal !== report.eligible_text_count - report.safe_inline_count) {
    throw new Error("Reader audit report coverage failures are incomplete");
  }
  return report;
}

async function fetchJsonResponse(url) {
  const response = await fetch(url, { signal: AbortSignal.timeout(timeoutMs) });
  if (!response.ok) throw new Error(`${new URL(url).pathname} returned HTTP ${response.status}`);
  const value = await response.json();
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${new URL(url).pathname} did not return a JSON object`);
  }
  return { value, response };
}

async function fetchJson(url) {
  return (await fetchJsonResponse(url)).value;
}

async function fetchSha256(url) {
  const response = await fetch(url, { signal: AbortSignal.timeout(timeoutMs) });
  if (!response.ok || !response.body) {
    throw new Error(`${new URL(url).pathname} returned HTTP ${response.status}`);
  }
  const digest = createHash("sha256");
  for await (const chunk of response.body) digest.update(chunk);
  return digest.digest("hex");
}

async function resolveSourceClass() {
  if (
    requestedSourceClass !== "auto" &&
    !Object.hasOwn(SOURCE_THRESHOLDS, requestedSourceClass)
  ) {
    throw new Error(
      `Unknown --source-class ${requestedSourceClass}; expected ${Object.keys(SOURCE_THRESHOLDS).join(", ")}`,
    );
  }

  const [paper, layoutResponse] = await Promise.all([
    fetchJson(`${apiBase}/papers/${encodeURIComponent(paperId)}`),
    fetchJsonResponse(
      `${apiBase}/papers/${encodeURIComponent(paperId)}/translation-layout?build=false`,
    ),
  ]);
  const layout = layoutResponse.value;
  const source = typeof paper.source === "string" ? paper.source : "";
  const adapter = typeof layout.adapter === "string" ? layout.adapter : "";
  if (!PRECISE_LAYOUT_ADAPTERS.has(adapter)) {
    throw new Error(`Read-only layout preflight returned non-precise adapter ${adapter || "<missing>"}`);
  }
  const inferred = layoutResponse.response.headers.get("x-pet-layout-source-class");
  if (!inferred || !Object.hasOwn(SOURCE_THRESHOLDS, inferred)) {
    throw new Error("Server did not provide trusted source provenance for the cached layout");
  }
  if (requestedSourceClass !== "auto" && requestedSourceClass !== inferred) {
    throw new Error(
      `--source-class ${requestedSourceClass} does not match inferred class ${inferred}`,
    );
  }
  const cacheKey = typeof layout.cache_key === "string" ? layout.cache_key : "";
  if (!/^[0-9a-f]{64}$/.test(cacheKey)) {
    throw new Error("Read-only layout preflight returned an invalid cache key");
  }
  const expectedPdfSha256 =
    typeof layout.source_pdf_sha256 === "string" ? layout.source_pdf_sha256 : "";
  if (!/^[0-9a-f]{64}$/.test(expectedPdfSha256)) {
    throw new Error("Read-only layout preflight returned an invalid source PDF hash");
  }
  if (
    !Array.isArray(paper.blocks) ||
    !Array.isArray(layout.pages) ||
    !Array.isArray(layout.regions)
  ) {
    throw new Error("Read-only preflight did not return paper blocks, pages, and layout regions");
  }
  for (const page of layout.pages) {
    if (page?.protected_boxes !== undefined && !Array.isArray(page.protected_boxes)) {
      throw new Error("Read-only preflight returned invalid page-level protection geometry");
    }
  }
  if (adapter === HYBRID_LAYOUT_ADAPTER) {
    const sources = Array.isArray(layout.sources) ? layout.sources : [];
    const sourceAdapters = new Set(sources.map((item) => item?.adapter));
    if (![...HYBRID_GEOMETRY_SOURCES].every((item) => sourceAdapters.has(item))) {
      throw new Error("Hybrid layout preflight is missing Poppler or MinerU provenance");
    }
    if (
      layout.regions.some(
        (region) => !HYBRID_GEOMETRY_SOURCES.has(region?.geometry_source),
      )
    ) {
      throw new Error("Hybrid layout preflight returned a region without trusted geometry provenance");
    }
  }
  const blocksByIndex = new Map();
  for (const block of paper.blocks) {
    if (
      !block ||
      !Number.isInteger(block.index) ||
      block.index < 0 ||
      typeof block.original !== "string" ||
      (block.translation !== null && typeof block.translation !== "string") ||
      blocksByIndex.has(block.index)
    ) {
      throw new Error("Read-only preflight returned invalid or duplicate paper blocks");
    }
    blocksByIndex.set(block.index, block);
  }
  const mappedBlockIndexes = new Set();
  for (const region of layout.regions) {
    if (
      !region ||
      !Number.isInteger(region.block_index) ||
      region.block_index < 0 ||
      !blocksByIndex.has(region.block_index)
    ) {
      throw new Error("Read-only preflight returned a layout region with no source block");
    }
    mappedBlockIndexes.add(region.block_index);
  }
  const expectedUnmappedBlocks = new Map(
    [...blocksByIndex]
      .filter(
        ([blockIndex, block]) =>
          block.status === "done" &&
          typeof block.translation === "string" &&
          Boolean(block.translation.trim()) &&
          !mappedBlockIndexes.has(blockIndex),
      )
      .map(([blockIndex, block]) => [blockIndex, block.translation]),
  );
  return {
    sourceClass: inferred,
    expectedCacheKey: cacheKey,
    source,
    adapter,
    expectedPdfSha256,
    blocksByIndex,
    expectedUnmappedBlocks,
  };
}

async function launchBrowser() {
  try {
    return await chromium.launch({ headless: !headed });
  } catch (error) {
    const chromePath = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
    if (!fs.existsSync(chromePath)) throw error;
    return chromium.launch({
      executablePath: chromePath,
      headless: !headed,
    });
  }
}

async function closeBrowserWithin(browserInstance, timeout = 5_000) {
  if (!browserInstance) return;
  let timer;
  await Promise.race([
    browserInstance.close(),
    new Promise((resolve) => {
      timer = setTimeout(resolve, timeout);
    }),
  ]);
  if (timer) clearTimeout(timer);
}

async function parseRootReport(reader) {
  const raw = await reader.getAttribute("data-inline-audit-report");
  if (!raw) return null;
  const value = JSON.parse(raw);
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  return value;
}

async function waitForAuditToSettle(reader) {
  const startedAt = Date.now();
  while (Date.now() - startedAt < settleTimeoutMs) {
    const report = await parseRootReport(reader);
    if (report && report.fit_pending_count === 0) return report;
    await new Promise((resolve) => setTimeout(resolve, 150));
  }
  return parseRootReport(reader);
}

async function moveToPage(page, targetPage) {
  const label = page.locator(".inline-reader-page-controls span");
  const currentText = await label.textContent();
  let currentPage = Number.parseInt(currentText ?? "", 10);
  if (!Number.isInteger(currentPage)) currentPage = 1;
  while (currentPage !== targetPage) {
    const nextPage = currentPage < targetPage ? currentPage + 1 : currentPage - 1;
    const buttonName = currentPage < targetPage ? "下一页" : "上一页";
    await page.getByRole("button", { name: buttonName, exact: true }).click();
    await page.waitForFunction(
      ({ selector, expected }) => document.querySelector(selector)?.textContent?.trim().startsWith(`${expected} /`),
      { selector: ".inline-reader-page-controls span", expected: nextPage },
      { timeout: Math.min(timeoutMs, 10_000) },
    );
    currentPage = nextPage;
  }
}

async function visitEveryPage(
  page,
  pageNumbers,
  overflowBlocks,
  renderErrors,
  failureDetails,
  domEvidence,
  passLabel,
) {
  const perPageTimeoutMs = Math.min(timeoutMs, 15_000);
  const pageCount = pageNumbers.length;
  for (const pageNumber of pageNumbers) {
    process.stderr.write(`[inline-audit] ${passLabel} page ${pageNumber}/${pageCount}\n`);
    await moveToPage(page, pageNumber);
    const shell = page.locator(`[data-page-number="${pageNumber}"]`);
    const rendered = shell.locator('[data-page-rendered="true"]');
    try {
      await rendered.waitFor({ state: "attached", timeout: perPageTimeoutMs });
      await shell.locator(".reader-pdf-loading").waitFor({ state: "detached", timeout: perPageTimeoutMs });
      const canvas = shell.locator(".reader-pdf-canvas");
      await canvas.waitFor({ state: "visible", timeout: perPageTimeoutMs });
      await shell.locator(".inline-translation-layer").waitFor({
        state: "attached",
        timeout: perPageTimeoutMs,
      });
      const canvasReady = await canvas.evaluate(
        (element) =>
          element.width > 0 &&
          element.height > 0 &&
          element.getBoundingClientRect().width > 0 &&
          element.getBoundingClientRect().height > 0,
      );
      if (!canvasReady) throw new Error("PDF canvas has no rendered area");
    } catch {
      renderErrors.add(pageNumber);
      continue;
    }
    if ((await shell.locator(".reader-pdf-page-error").count()) > 0) {
      renderErrors.add(pageNumber);
    }
    const inlineEntries = await shell.locator(".inline-translation-region").evaluateAll((regions) =>
      regions.flatMap((region) => {
        const element = region;
        const blockIndex = Number.parseInt(element.dataset.blockIndex ?? "", 10);
        const overflowed =
          element.scrollHeight > element.clientHeight + 1 ||
          element.scrollWidth > element.clientWidth + 1;
        return Number.isInteger(blockIndex)
          ? [{ blockIndex, overflowed, primary: element.getAttribute("role") === "button" }]
          : [];
      }),
    );
    for (const { blockIndex, overflowed, primary } of inlineEntries) {
      domEvidence.inlineBlocks.add(blockIndex);
      if (primary && !domEvidence.inlinePrimaryPageByBlock.has(blockIndex)) {
        domEvidence.inlinePrimaryPageByBlock.set(blockIndex, pageNumber);
      }
      if (overflowed) overflowBlocks.add(blockIndex);
    }
    const panelEntries = await shell.locator(".inline-preserved-hitarea").evaluateAll((regions) =>
      regions.flatMap((region) => {
        const blockIndex = Number.parseInt(region.dataset.blockIndex ?? "", 10);
        return Number.isInteger(blockIndex)
          ? [{ blockIndex, primary: region.getAttribute("role") === "button" }]
          : [];
      }),
    );
    for (const { blockIndex, primary } of panelEntries) {
      domEvidence.panelBlocks.add(blockIndex);
      if (primary && !domEvidence.panelPrimaryPageByBlock.has(blockIndex)) {
        domEvidence.panelPrimaryPageByBlock.set(blockIndex, pageNumber);
      }
    }
    if (includeDebugBlocks) {
      const details = await shell.locator(".inline-preserved-hitarea").evaluateAll((regions) =>
        regions.flatMap((region) => {
          const blockIndex = Number.parseInt(region.dataset.blockIndex ?? "", 10);
          if (!Number.isInteger(blockIndex)) return [];
          return [{
            block_index: blockIndex,
            page: Number.parseInt(region.closest("[data-page-number]")?.dataset.pageNumber ?? "", 10),
            fit_reason: region.dataset.fitReason ?? null,
            background_reason: region.dataset.backgroundReason ?? null,
            source_font_px: Number.parseFloat(region.dataset.sourceFontPx ?? ""),
          }];
        }),
      );
      for (const detail of details) failureDetails.set(detail.block_index, detail);
    }
  }
}

async function openInspector(
  page,
  targetPage,
  selector,
  expectedSide,
  expectedBlockIndex,
  expectedContent,
) {
  if (targetPage === null) return null;
  await moveToPage(page, targetPage);
  const shell = page.locator(`[data-page-number="${targetPage}"]`);
  const perTargetTimeoutMs = Math.min(timeoutMs, 15_000);
  await shell.locator('[data-page-rendered="true"]').waitFor({
    state: "attached",
    timeout: perTargetTimeoutMs,
  });
  await shell.locator(".reader-pdf-loading").waitFor({
    state: "detached",
    timeout: perTargetTimeoutMs,
  });
  await shell.locator(".inline-translation-layer").waitFor({
    state: "attached",
    timeout: perTargetTimeoutMs,
  });
  const target = shell
    .locator(`${selector}[data-block-index="${expectedBlockIndex}"]`)
    .first();
  try {
    await target.waitFor({ state: "visible", timeout: perTargetTimeoutMs });
  } catch {
    return null;
  }
  await target.scrollIntoViewIfNeeded();
  const blockIndex = Number.parseInt(await target.getAttribute("data-block-index") ?? "", 10);
  if (!Number.isInteger(blockIndex) || blockIndex !== expectedBlockIndex) return null;
  await target.evaluate((element) => {
    window.__petInlineAuditObserver?.disconnect();
    window.__petInlineAuditClickStartedAt = null;
    window.__petInlineAuditInspectorOpenedAt = null;
    const markInspectorVisible = () => {
      const inspector = document.querySelector(".inline-inspector");
      if (!inspector) return false;
      const style = getComputedStyle(inspector);
      const rect = inspector.getBoundingClientRect();
      if (
        style.display === "none" ||
        style.visibility === "hidden" ||
        rect.width <= 0 ||
        rect.height <= 0
      ) {
        return false;
      }
      window.__petInlineAuditInspectorOpenedAt = performance.now();
      window.__petInlineAuditObserver?.disconnect();
      window.__petInlineAuditObserver = null;
      return true;
    };
    const observer = new MutationObserver(() => markInspectorVisible());
    window.__petInlineAuditObserver = observer;
    observer.observe(document.body, { childList: true, subtree: true });
    element.addEventListener(
      "click",
      () => {
        window.__petInlineAuditClickStartedAt = performance.now();
        queueMicrotask(markInspectorVisible);
      },
      { capture: true, once: true },
    );
  });
  await target.click({ timeout: 2_000 });
  const inspector = page.locator(".inline-inspector");
  await inspector.waitFor({ state: "visible", timeout: 2_000 });
  const latency = await page.evaluate(() => {
    const startedAt = window.__petInlineAuditClickStartedAt;
    const openedAt = window.__petInlineAuditInspectorOpenedAt;
    return typeof startedAt === "number" && typeof openedAt === "number"
      ? openedAt - startedAt
      : null;
  });
  const targetMismatch =
    Number.parseInt(await inspector.getAttribute("data-block-index") ?? "", 10) !== blockIndex ||
    (await inspector.getAttribute("data-inspector-side")) !== expectedSide ||
    (await inspector.locator(".inline-inspector-content").getAttribute("data-audit-content")) !==
      expectedContent ||
    typeof latency !== "number" ||
    !Number.isFinite(latency) ||
    latency < 0;
  const close = page.getByRole("button", { name: "关闭原文核对" });
  if ((await close.count()) > 0) {
    await close.click();
    await inspector.waitFor({ state: "hidden", timeout: 2_000 });
  }
  if (targetMismatch) return null;
  return { latency, blockIndex };
}

async function verifyUnmappedDrawer(page, expectedBlocks) {
  const expectedCount = expectedBlocks.size;
  if (expectedCount === 0) return true;
  const button = page.getByRole("button", { name: `未定位译文 ${expectedCount}` });
  if ((await button.count()) !== 1) return false;
  await button.click();
  const drawer = page.locator(".inline-unmapped-drawer");
  try {
    await drawer.waitFor({ state: "visible", timeout: 2_000 });
    const entries = await drawer
      .locator(".inline-unmapped-list article[data-block-index]")
      .evaluateAll((items) => items.flatMap((item) => {
        const value = Number.parseInt(item.dataset.blockIndex ?? "", 10);
        const style = getComputedStyle(item);
        const rect = item.getBoundingClientRect();
        const visible =
          style.display !== "none" &&
          style.visibility !== "hidden" &&
          rect.width > 0 &&
          rect.height > 0;
        const content = item.getAttribute("data-audit-content");
        return Number.isInteger(value) && visible && Boolean(item.textContent?.trim()) && content !== null
          ? [{ blockIndex: value, content }]
          : [];
      }));
    return (
      entries.length === expectedCount &&
      new Set(entries.map((entry) => entry.blockIndex)).size === expectedCount &&
      entries.every(
        (entry) => expectedBlocks.get(entry.blockIndex) === entry.content,
      )
    );
  } finally {
    const close = page.getByRole("button", { name: "关闭未定位译文" });
    if ((await close.count()) > 0) await close.click();
  }
}

function evaluateGate(report, sourceClass, extras) {
  const reasons = [];
  if (ratioBelow(report.accessibility_ratio, 1)) reasons.push("accessibility_incomplete");
  if (report.fit_pending_count > 0) reasons.push("fit_pending");
  if (report.overflow_count > 0) reasons.push("overflow");
  if (report.protected_overlap_count > 0) reasons.push("protected_overlap");
  if (report.translation_mismatch_count > 0) reasons.push("translation_mismatch");
  if (report.silent_missing_count > 0) reasons.push("silent_missing");
  if (requireComplete && report.incomplete_translation_count > 0) {
    reasons.push("translation_incomplete");
  }
  if (ratioBelow(report.safe_inline_coverage, SOURCE_THRESHOLDS[sourceClass])) {
    reasons.push("coverage_below_minimum");
  }
  if (ratioBelow(report.replace_average_confidence, MINIMUM_REPLACE_CONFIDENCE)) {
    reasons.push("replace_confidence_below_minimum");
  }
  if (extras.renderErrorCount > 0) reasons.push("pdf_page_render_error");
  if (extras.pageErrorCount > 0) reasons.push("browser_page_error");
  if (extras.accessibilityInteractionFailures.length > 0) {
    reasons.push("accessibility_interaction_failed");
  }
  if (!enforcePerformance) reasons.push("performance_not_enforced");
  if (enforcePerformance && extras.firstReadableMs > FIRST_READABLE_TARGET_MS) {
    reasons.push("first_readable_slow");
  }
  if (
    enforcePerformance &&
    (extras.inspectorLatencyMs === null || extras.inspectorLatencyMs > INSPECTOR_TARGET_MS)
  ) {
    reasons.push("inspector_slow_or_unavailable");
  }
  return [...new Set(reasons)];
}

function safeNumber(value, fallback = 0) {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

let browser;
let page;
let finalReport;

try {
  const preflight = await resolveSourceClass();
  const sourceClass = preflight.sourceClass;
  browser = await launchBrowser();
  page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const pageLayoutResponsePromise = page.waitForResponse(
    (response) => {
      const url = new URL(response.url());
      const layoutPath = `/papers/${encodeURIComponent(paperId)}/translation-layout`;
      return (
        response.request().method() === "GET" &&
        url.pathname.endsWith(layoutPath)
      );
    },
    { timeout: timeoutMs },
  );
  const browserPageErrors = [];
  page.on("pageerror", (error) => browserPageErrors.push(String(error)));

  const navigationStartedAt = performance.now();
  await page.goto(`${appBase}/paper/${encodeURIComponent(paperId)}`, {
    waitUntil: "domcontentloaded",
    timeout: timeoutMs,
  });
  const pageLayoutResponse = await pageLayoutResponsePromise;
  const pageLayoutUrl = new URL(pageLayoutResponse.url());
  const pageSourceClass = await pageLayoutResponse.headerValue(
    "x-pet-layout-source-class",
  );
  if (
    pageLayoutUrl.searchParams.get("build") !== "false" ||
    pageSourceClass !== sourceClass
  ) {
    throw new Error(
      "Reader layout response did not match the read-only trusted source provenance",
    );
  }
  const reader = page.locator('[data-reader-mode="inline_translation"]');
  await reader.waitFor({ state: "visible", timeout: timeoutMs });
  await page.locator(".inline-reader-loading").waitFor({ state: "detached", timeout: timeoutMs });
  const firstPage = page.locator('[data-page-number="1"]');
  await firstPage.locator('[data-page-rendered="true"]').waitFor({
    state: "attached",
    timeout: timeoutMs,
  });
  await firstPage.locator(".reader-pdf-loading").waitFor({
    state: "detached",
    timeout: timeoutMs,
  });
  await firstPage.locator(".reader-pdf-canvas").waitFor({ state: "visible", timeout: timeoutMs });
  const firstReadableMs = performance.now() - navigationStartedAt;

  const pageCount = Number.parseInt(await reader.getAttribute("data-layout-page-count") ?? "", 10);
  if (!Number.isInteger(pageCount) || pageCount < 1) throw new Error("Reader exposed an invalid page count");
  const cacheKey = await reader.getAttribute("data-layout-cache-key");
  if (cacheKey !== preflight.expectedCacheKey) {
    throw new Error(
      `Reader layout cache ${cacheKey ?? "missing"} does not match API preflight ${preflight.expectedCacheKey}`,
    );
  }
  const resolvedPdfUrlValue = await reader.getAttribute("data-source-pdf-url");
  if (!resolvedPdfUrlValue) throw new Error("Reader did not expose its resolved PDF URL");
  const resolvedPdfUrl = new URL(resolvedPdfUrlValue, `${appBase}/`).toString();
  const resolvedPdfSha256 = await fetchSha256(resolvedPdfUrl);
  if (resolvedPdfSha256 !== preflight.expectedPdfSha256) {
    throw new Error("Browser-resolved PDF bytes do not match the layout source hash");
  }
  const overflowBlocks = new Set();
  const renderErrors = new Set();
  const failureDetails = new Map();
  const domEvidence = {
    inlineBlocks: new Set(),
    panelBlocks: new Set(),
    inlinePrimaryPageByBlock: new Map(),
    panelPrimaryPageByBlock: new Map(),
  };

  const ascendingPages = Array.from({ length: pageCount }, (_, index) => index + 1);
  const descendingPages = [...ascendingPages].reverse();
  await visitEveryPage(
    page,
    ascendingPages,
    overflowBlocks,
    renderErrors,
    failureDetails,
    domEvidence,
    "evidence",
  );
  await waitForAuditToSettle(reader);
  await visitEveryPage(
    page,
    descendingPages,
    overflowBlocks,
    renderErrors,
    failureDetails,
    domEvidence,
    "verification",
  );
  const componentReport = validateComponentReport(await parseRootReport(reader));

  const accessibilityInteractionFailures = [];
  const panelOnlyDomBlocks = new Set(
    [...domEvidence.panelBlocks].filter(
      (blockIndex) => !domEvidence.inlineBlocks.has(blockIndex),
    ),
  );
  if (domEvidence.inlineBlocks.size !== componentReport.inline_count) {
    accessibilityInteractionFailures.push("inline_dom_count_mismatch");
  }
  if (panelOnlyDomBlocks.size !== componentReport.panel_count) {
    accessibilityInteractionFailures.push("panel_dom_count_mismatch");
  }
  const inspectorLatencies = [];
  for (const blockIndex of [...domEvidence.inlineBlocks].sort((left, right) => left - right)) {
    const targetPage = domEvidence.inlinePrimaryPageByBlock.get(blockIndex) ?? null;
    const expectedContent = preflight.blocksByIndex.get(blockIndex)?.original;
    try {
      if (typeof expectedContent !== "string") {
        throw new Error("inline block is absent from the preflight paper");
      }
      const opened = await openInspector(
        page,
        targetPage,
        '.inline-translation-region[data-render-policy="replace"][role="button"]',
        "original",
        blockIndex,
        expectedContent,
      );
      if (!opened) throw new Error("inline target did not open its inspector");
      inspectorLatencies.push(opened.latency);
    } catch {
      accessibilityInteractionFailures.push(`inline_inspector_failed:${blockIndex}`);
    }
  }
  for (const blockIndex of [...panelOnlyDomBlocks].sort((left, right) => left - right)) {
    const targetPage = domEvidence.panelPrimaryPageByBlock.get(blockIndex) ?? null;
    const expectedContent = preflight.blocksByIndex.get(blockIndex)?.translation;
    try {
      if (typeof expectedContent !== "string" || !expectedContent.trim()) {
        throw new Error("panel block has no completed preflight translation");
      }
      const opened = await openInspector(
        page,
        targetPage,
        '.inline-preserved-hitarea[role="button"]',
        "translation",
        blockIndex,
        expectedContent,
      );
      if (!opened) throw new Error("panel target did not open its inspector");
      inspectorLatencies.push(opened.latency);
    } catch {
      accessibilityInteractionFailures.push(`panel_inspector_failed:${blockIndex}`);
    }
  }
  const inspectorLatencyMs = inspectorLatencies.length > 0
    ? Math.max(...inspectorLatencies)
    : null;
  let unmappedDrawerOpened = false;
  try {
    unmappedDrawerOpened =
      componentReport.unmapped_count === preflight.expectedUnmappedBlocks.size &&
      await verifyUnmappedDrawer(page, preflight.expectedUnmappedBlocks);
  } catch {
    unmappedDrawerOpened = false;
  }
  if (!unmappedDrawerOpened) {
    accessibilityInteractionFailures.push("unmapped_drawer_failed");
  }

  const failureCounts = { ...(componentReport.failure_counts ?? {}) };
  const componentOverflowCount = safeNumber(componentReport.overflow_count);
  const auditedOverflowCount = Math.max(componentOverflowCount, overflowBlocks.size);
  if (auditedOverflowCount > 0) {
    failureCounts.overflow = Math.max(
      safeNumber(failureCounts.overflow),
      auditedOverflowCount,
    );
  }
  const audited = {
    ...componentReport,
    overflow_count: auditedOverflowCount,
    failure_counts: Object.fromEntries(Object.entries(failureCounts).sort(([left], [right]) => left.localeCompare(right))),
  };
  const extras = {
    renderErrorCount: renderErrors.size,
    pageErrorCount: browserPageErrors.length,
    firstReadableMs,
    inspectorLatencyMs,
    accessibilityInteractionFailures,
  };
  const reasons = evaluateGate(audited, sourceClass, extras);
  finalReport = {
    version: 1,
    audited_at: new Date().toISOString(),
    paper_id: paperId,
    target: {
      app_origin: new URL(appBase).origin,
      api_origin: new URL(apiBase).origin,
      paper_source: preflight.source,
      layout_adapter: preflight.adapter,
      pdf_url: `${new URL(resolvedPdfUrl).origin}${new URL(resolvedPdfUrl).pathname}`,
      source_pdf_sha256: resolvedPdfSha256,
      viewport: { width: 1440, height: 900 },
      device_scale_factor: await page.evaluate(() => window.devicePixelRatio),
      browser_version: browser.version(),
    },
    source_class: sourceClass,
    coverage_threshold: SOURCE_THRESHOLDS[sourceClass],
    replace_confidence_threshold: MINIMUM_REPLACE_CONFIDENCE,
    page_count: pageCount,
    layout_cache_key: cacheKey,
    report: audited,
    require_complete: requireComplete,
    performance: {
      enforced: enforcePerformance,
      first_readable_ms: Math.round(firstReadableMs),
      first_readable_target_ms: FIRST_READABLE_TARGET_MS,
      inspector_open_ms: inspectorLatencyMs === null ? null : Math.round(safeNumber(inspectorLatencyMs) * 100) / 100,
      inspector_target_ms: INSPECTOR_TARGET_MS,
    },
    render_error_count: renderErrors.size,
    browser_page_error_count: browserPageErrors.length,
    accessibility: {
      inline_dom_block_count: domEvidence.inlineBlocks.size,
      panel_dom_block_count: panelOnlyDomBlocks.size,
      unmapped_drawer_count: componentReport.unmapped_count,
      interaction_failures: accessibilityInteractionFailures,
    },
    ...(includeDebugBlocks
      ? { debug_failure_blocks: [...failureDetails.values()].sort((left, right) => left.block_index - right.block_index) }
      : {}),
    reasons,
    status: reasons.length === 0 ? "pass" : "fail",
  };

  fs.mkdirSync(path.dirname(outputPath), { recursive: true });
  await page.screenshot({ path: screenshotPath, fullPage: false });
  fs.writeFileSync(outputPath, `${JSON.stringify(finalReport, null, 2)}\n`, "utf8");
  process.stdout.write(`${JSON.stringify(finalReport, null, 2)}\n`);
  if (reasons.length > 0) process.exitCode = 1;
} catch (error) {
  finalReport = {
    version: 1,
    paper_id: paperId,
    status: "fail",
    reasons: ["audit_runtime_error"],
    error: error instanceof Error ? error.message : String(error),
  };
  fs.mkdirSync(path.dirname(outputPath), { recursive: true });
  fs.writeFileSync(outputPath, `${JSON.stringify(finalReport, null, 2)}\n`, "utf8");
  process.stderr.write(`${JSON.stringify(finalReport, null, 2)}\n`);
  process.exitCode = 1;
} finally {
  await closeBrowserWithin(browser);
}
process.exit(process.exitCode ?? 0);
