import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

import ts from "typescript";


async function loadReaderContext() {
  const frontendDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
  const sourcePath = path.join(frontendDir, "lib", "readerContext.ts");
  const outputDir = fs.mkdtempSync(path.join(os.tmpdir(), "pet-reader-context-"));
  const outputPath = path.join(outputDir, "readerContext.mjs");
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
  assert.deepEqual(
    errors.map((item) => ts.flattenDiagnosticMessageText(item.messageText, "\n")),
    [],
  );
  fs.writeFileSync(outputPath, transpiled.outputText, "utf8");
  try {
    return await import(`${pathToFileURL(outputPath).href}?v=${Date.now()}`);
  } finally {
    fs.rmSync(outputDir, { recursive: true, force: true });
  }
}

const reader = await loadReaderContext();
const blocks = [
  { index: 3, type: "heading", original: "Method", translation: "方法", status: "done" },
  { index: 4, type: "paragraph", original: "Source paragraph", translation: "译文段落", status: "done" },
  { index: 5, type: "paragraph", original: "Next paragraph", translation: null, status: "pending" },
];

test("builds the selection reader context without legacy pane state", () => {
  const context = reader.buildReaderAgentContext(
    blocks,
    4,
    { block_index: 4, side: "translation", text: "译文" },
    {
      page: 7,
      region_id: "region-4",
      layout_confidence: 0.96,
      render_policy: "replace",
    },
  );

  assert.equal(context.reader_mode, "selection_translation");
  assert.equal(context.active_block.index, 4);
  assert.equal(context.previous_block.index, 3);
  assert.equal(context.next_block.index, 5);
  assert.deepEqual(context.selected_text, {
    block_index: 4,
    side: "translation",
    text: "译文",
  });
  assert.deepEqual(
    {
      page: context.page,
      region_id: context.region_id,
      layout_confidence: context.layout_confidence,
      render_policy: context.render_policy,
    },
    {
      page: 7,
      region_id: "region-4",
      layout_confidence: 0.96,
      render_policy: "replace",
    },
  );
  assert.equal("right_pane_side" in context, false);
});

test("keeps an unmapped original selection without inventing a block anchor", () => {
  const context = reader.buildReaderAgentContext(
    blocks,
    null,
    { block_index: null, side: "original", text: "Exact PDF selection" },
    {
      page: 2,
      region_id: null,
      layout_confidence: null,
      render_policy: "preserve",
    },
  );

  assert.equal(context.reader_mode, "selection_translation");
  assert.equal(context.active_block, null);
  assert.deepEqual(context.selected_text, {
    block_index: null,
    side: "original",
    text: "Exact PDF selection",
  });
  assert.equal(context.page, 2);
  assert.equal(context.region_id, null);
});

test("normalizes unavailable layout metadata and keeps compact block limits", () => {
  const context = reader.buildReaderAgentContext(
    [{ ...blocks[1], original: "o".repeat(1600), translation: "t".repeat(1600) }],
    4,
    null,
    {
      page: 0,
      region_id: "   ",
      layout_confidence: 2,
      render_policy: "unsafe",
    },
  );

  assert.equal(context.active_block.original.length, 1200);
  assert.equal(context.active_block.translation.length, 1200);
  assert.equal(context.page, null);
  assert.equal(context.region_id, null);
  assert.equal(context.layout_confidence, null);
  assert.equal(context.render_policy, null);
});
