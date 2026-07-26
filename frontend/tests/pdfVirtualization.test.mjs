import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

import ts from "typescript";


async function loadPdfVirtualization() {
  const frontendDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
  const sourcePath = path.join(frontendDir, "lib", "pdfVirtualization.ts");
  const outputDir = fs.mkdtempSync(path.join(os.tmpdir(), "pet-pdf-virtualization-"));
  const outputPath = path.join(outputDir, "pdfVirtualization.mjs");
  const transpiled = ts.transpileModule(fs.readFileSync(sourcePath, "utf8"), {
    compilerOptions: {
      target: ts.ScriptTarget.ES2022,
      module: ts.ModuleKind.ES2022,
      moduleResolution: ts.ModuleResolutionKind.Bundler,
      esModuleInterop: true,
    },
    fileName: sourcePath,
    reportDiagnostics: true,
  });
  const errors = (transpiled.diagnostics ?? []).filter(
    (diagnostic) => diagnostic.category === ts.DiagnosticCategory.Error,
  );
  if (errors.length > 0) {
    throw new Error(
      errors.map((diagnostic) => ts.flattenDiagnosticMessageText(diagnostic.messageText, "\n")).join("\n"),
    );
  }
  fs.writeFileSync(outputPath, transpiled.outputText, "utf8");
  try {
    return await import(`${pathToFileURL(outputPath).href}?v=${Date.now()}`);
  } finally {
    fs.rmSync(outputDir, { recursive: true, force: true });
  }
}

const pdf = await loadPdfVirtualization();

const page = (pageNumber, width = 600, height = 800) => ({
  page: pageNumber,
  width,
  height,
  rotation: 0,
});

test("expands the current page by two and clips document edges", () => {
  assert.deepEqual(
    pdf.getPdfPageRenderWindow({ visiblePages: [], currentPage: 1, pageCount: 30 }),
    [1, 2, 3],
  );
  assert.deepEqual(
    pdf.getPdfPageRenderWindow({ visiblePages: [], currentPage: 15, pageCount: 30 }),
    [13, 14, 15, 16, 17],
  );
  assert.deepEqual(
    pdf.getPdfPageRenderWindow({ visiblePages: [], currentPage: 30, pageCount: 30 }),
    [28, 29, 30],
  );
});

test("unions windows for every visible page and a pinned current page", () => {
  assert.deepEqual(
    pdf.getPdfPageRenderWindow({ visiblePages: [10, 11], currentPage: 20, pageCount: 30 }),
    [8, 9, 10, 11, 12, 13, 18, 19, 20, 21, 22],
  );
});

test("safely clamps page-like values and ignores non-finite inputs", () => {
  assert.deepEqual(
    pdf.getPdfPageRenderWindow({
      visiblePages: [-9, 0, 99, Number.NaN, Number.POSITIVE_INFINITY],
      currentPage: 2.9,
      pageCount: 30.8,
    }),
    [1, 2, 3, 4, 28, 29, 30],
  );
  assert.deepEqual(
    pdf.getPdfPageRenderWindow({ visiblePages: [], currentPage: Number.NaN, pageCount: 4 }),
    [1, 2, 3],
  );
  assert.deepEqual(
    pdf.getPdfPageRenderWindow({ visiblePages: [1], currentPage: 1, pageCount: Number.NaN }),
    [],
  );
});

test("supports an explicitly smaller bounded radius", () => {
  assert.deepEqual(
    pdf.getPdfPageRenderWindow({ visiblePages: [4], currentPage: null, pageCount: 8, radius: 1 }),
    [3, 4, 5],
  );
  assert.deepEqual(
    pdf.getPdfPageRenderWindow({ visiblePages: [4], currentPage: null, pageCount: 8, radius: -3 }),
    [4],
  );
});

test("fits the widest page to container and finite max width with one shared scale", () => {
  assert.deepEqual(
    pdf.getPdfPageCssSizes([page(1, 600, 800), page(2, 300, 600)], 720, 640),
    {
      1: { widthPx: 640, heightPx: 853.333 },
      2: { widthPx: 320, heightPx: 640 },
    },
  );
  assert.deepEqual(
    pdf.getPdfPageCssSizes([page(1, 600, 800), page(2, 300, 600)], 500, 960),
    {
      1: { widthPx: 500, heightPx: 666.667 },
      2: { widthPx: 250, heightPx: 500 },
    },
  );
});

test("uses a finite default cap even when the caller passes an invalid maximum", () => {
  const sizes = pdf.getPdfPageCssSizes([page(1, 600, 800)], Number.MAX_VALUE, Number.POSITIVE_INFINITY);
  assert.deepEqual(sizes, { 1: { widthPx: 960, heightPx: 1280 } });
  assert.ok(Number.isFinite(sizes[1].widthPx));
  assert.ok(Number.isFinite(sizes[1].heightPx));
});

test("rejects invalid container geometry and skips malformed or duplicate pages", () => {
  assert.deepEqual(pdf.getPdfPageCssSizes([page(1)], 0), {});
  assert.deepEqual(pdf.getPdfPageCssSizes([page(1)], Number.NaN), {});
  assert.deepEqual(
    pdf.getPdfPageCssSizes([
      page(0),
      page(1, 600, 800),
      page(1, 300, 300),
      page(2, -1, 800),
      page(3, 600, Number.POSITIVE_INFINITY),
      page(4, 300, 450),
    ], 600),
    {
      1: { widthPx: 600, heightPx: 800 },
      4: { widthPx: 300, heightPx: 450 },
    },
  );
});

test("caps high zoom and DPR canvas memory without changing CSS geometry", () => {
  const widthPx = 1920;
  const heightPx = 2485;
  const scale = pdf.getPdfCanvasOutputScale({ widthPx, heightPx, devicePixelRatio: 3 });
  const backingWidth = Math.round(widthPx * scale);
  const backingHeight = Math.round(heightPx * scale);

  assert.ok(scale < 3);
  assert.ok(backingWidth * backingHeight <= pdf.DEFAULT_PDF_CANVAS_MAX_PIXELS + 1);
  assert.ok(backingWidth <= pdf.DEFAULT_PDF_CANVAS_MAX_DIMENSION);
  assert.ok(backingHeight <= pdf.DEFAULT_PDF_CANVAS_MAX_DIMENSION);
  assert.equal(pdf.getPdfCanvasOutputScale({ widthPx: 960, heightPx: 1242, devicePixelRatio: 2 }), 2);
  assert.equal(pdf.getPdfCanvasOutputScale({ widthPx: Number.NaN, heightPx: 100 }), 1);

  const longPageScale = pdf.getPdfCanvasOutputScale({
    widthPx: 960,
    heightPx: 1_000_000,
    devicePixelRatio: 3,
  });
  assert.ok(longPageScale < 0.01);
  assert.ok(Math.round(960 * longPageScale) <= pdf.DEFAULT_PDF_CANVAS_MAX_DIMENSION);
  assert.ok(Math.round(1_000_000 * longPageScale) <= pdf.DEFAULT_PDF_CANVAS_MAX_DIMENSION);
  assert.ok(
    Math.round(960 * longPageScale) * Math.round(1_000_000 * longPageScale) <=
      pdf.DEFAULT_PDF_CANVAS_MAX_PIXELS,
  );
});
