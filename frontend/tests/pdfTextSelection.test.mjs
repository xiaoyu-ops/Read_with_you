import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

import ts from "typescript";


async function loadSelectionModule() {
  const frontendDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
  const sourcePath = path.join(frontendDir, "lib", "pdfTextSelection.ts");
  const outputDir = fs.mkdtempSync(path.join(os.tmpdir(), "pet-pdf-selection-"));
  const outputPath = path.join(outputDir, "pdfTextSelection.mjs");
  const transpiled = ts.transpileModule(fs.readFileSync(sourcePath, "utf8"), {
    compilerOptions: {
      target: ts.ScriptTarget.ES2022,
      module: ts.ModuleKind.ES2022,
      moduleResolution: ts.ModuleResolutionKind.Bundler,
    },
    fileName: sourcePath,
    reportDiagnostics: true,
  });
  const errors = (transpiled.diagnostics ?? []).filter(
    (diagnostic) => diagnostic.category === ts.DiagnosticCategory.Error,
  );
  if (errors.length > 0) {
    throw new Error(errors.map((diagnostic) => ts.flattenDiagnosticMessageText(diagnostic.messageText, "\n")).join("\n"));
  }
  fs.writeFileSync(outputPath, transpiled.outputText, "utf8");
  try {
    return await import(`${pathToFileURL(outputPath).href}?v=${Date.now()}`);
  } finally {
    fs.rmSync(outputDir, { recursive: true, force: true });
  }
}

const selection = await loadSelectionModule();

const box = (x0, y0, x1, y1) => ({ x0, y0, x1, y1 });
const region = (regionId, blockIndex, bbox, confidence = 0.97) => ({
  region_id: regionId,
  block_index: blockIndex,
  page: 1,
  bbox,
  confidence,
});

test("normalizes selection rectangles against the visible PDF page", () => {
  const rects = selection.normalizePdfSelectionRects(
    [
      { left: 120, top: 220, right: 220, bottom: 240, width: 100, height: 20 },
      { left: 120, top: 220, right: 220, bottom: 240, width: 100, height: 20 },
      { left: 80, top: 90, right: 160, bottom: 120, width: 80, height: 30 },
    ],
    { left: 100, top: 200, right: 500, bottom: 800, width: 400, height: 600 },
  );
  assert.deepEqual(rects, [box(0.05, 0.033333, 0.3, 0.066667)]);
});

test("maps only geometrically unique selections to a block and region", () => {
  const regions = [
    region("r-1", 4, box(0.1, 0.1, 0.8, 0.25)),
    region("r-2", 5, box(0.1, 0.3, 0.8, 0.45)),
  ];
  assert.deepEqual(
    selection.mapSelectionToLayout([box(0.2, 0.12, 0.7, 0.16)], regions, 1),
    { block_index: 4, region_id: "r-1", layout_confidence: 0.97 },
  );
});

test("fails closed for selections spanning blocks or ambiguous overlap", () => {
  const spanning = selection.mapSelectionToLayout(
    [box(0.2, 0.12, 0.7, 0.16), box(0.2, 0.32, 0.7, 0.36)],
    [
      region("r-1", 4, box(0.1, 0.1, 0.8, 0.25)),
      region("r-2", 5, box(0.1, 0.3, 0.8, 0.45)),
    ],
    1,
  );
  assert.deepEqual(spanning, { block_index: null, region_id: null, layout_confidence: null });

  const ambiguous = selection.mapSelectionToLayout(
    [box(0.2, 0.12, 0.7, 0.16)],
    [
      region("r-1", 4, box(0.1, 0.1, 0.8, 0.25)),
      region("r-2", 4, box(0.1, 0.1, 0.8, 0.25)),
    ],
    1,
  );
  assert.deepEqual(ambiguous, { block_index: null, region_id: null, layout_confidence: null });
});

test("hashes the exact UTF-8 selection payload deterministically", async () => {
  assert.equal(
    await selection.sha256Text("hello"),
    "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
  );
  assert.notEqual(await selection.sha256Text("论文"), await selection.sha256Text("论文 "));
});
