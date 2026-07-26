import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

import ts from "typescript";


async function loadModule() {
  const frontendDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
  const sourcePath = path.join(frontendDir, "lib", "readerReducer.ts");
  const outputDir = fs.mkdtempSync(path.join(os.tmpdir(), "pet-reader-reducer-"));
  const outputPath = path.join(outputDir, "readerReducer.mjs");
  const transpiled = ts.transpileModule(fs.readFileSync(sourcePath, "utf8"), {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 },
    fileName: sourcePath,
    reportDiagnostics: true,
  });
  const errors = (transpiled.diagnostics ?? []).filter(
    (diagnostic) => diagnostic.category === ts.DiagnosticCategory.Error,
  );
  assert.deepEqual(errors.map((item) => ts.flattenDiagnosticMessageText(item.messageText, "\n")), []);
  fs.writeFileSync(outputPath, transpiled.outputText, "utf8");
  try {
    return await import(`${pathToFileURL(outputPath).href}?v=${Date.now()}`);
  } finally {
    fs.rmSync(outputDir, { recursive: true, force: true });
  }
}

const reducer = await loadModule();

function blocks() {
  return [
    { index: 0, type: "paragraph", original: "A", translation: null, status: "pending" },
    { index: 1, type: "paragraph", original: "B", translation: "旧译文", status: "done" },
    { index: 2, type: "formula", original: "x=y", translation: null, status: "skip" },
  ];
}

test("block_done atomically updates one block and invalidates its old overlay", () => {
  const initial = blocks();
  initial[0] = { ...initial[0], translation: "旧版本", status: "done" };
  let state = reducer.createReaderTranslationState(initial);
  state = reducer.readerTranslationReducer(state, {
    type: "fit_ready",
    blockIndex: 0,
    revision: 0,
    overlay: { text: "stale" },
  });
  assert.deepEqual(state.overlayByBlock[0], { text: "stale" });

  state = reducer.readerTranslationReducer(state, {
    type: "stream_event",
    generation: 0,
    event: { event: "block_done", data: { index: 0, translation: "新译文" } },
  });
  assert.equal(state.blocks[0].translation, "新译文");
  assert.equal(state.blocks[0].status, "done");
  assert.equal(state.blocks[1].translation, "旧译文");
  assert.equal(state.overlayByBlock[0], undefined);
  assert.equal(reducer.selectFitRevision(state, 0), 1);

  state = reducer.readerTranslationReducer(state, {
    type: "fit_ready",
    blockIndex: 0,
    revision: 1,
    overlay: { text: "new" },
  });
  assert.deepEqual(state.overlayByBlock[0], { text: "new" });
});

test("block_error removes overlay and rejects a stale asynchronous fit", () => {
  let state = reducer.createReaderTranslationState(blocks());
  state = reducer.readerTranslationReducer(state, {
    type: "fit_ready",
    blockIndex: 1,
    revision: 0,
    overlay: { text: "旧译文" },
  });
  state = reducer.reduceReaderTranslationEvent(state, {
    event: "block_error",
    data: { index: 1, status: "error" },
  }, 0);
  assert.equal(state.blocks[1].status, "error");
  assert.equal(state.blocks[1].translation, "旧译文");
  assert.equal(state.overlayByBlock[1], undefined);
  assert.equal(reducer.selectFitRevision(state, 1), 1);

  const unchanged = reducer.readerTranslationReducer(state, {
    type: "fit_ready",
    blockIndex: 1,
    revision: 0,
    overlay: { text: "late" },
  });
  assert.strictEqual(unchanged, state);
});

test("complete and done are both accepted terminal events", () => {
  for (const terminal of ["complete", "done"]) {
    let state = reducer.createReaderTranslationState(blocks());
    state = reducer.readerTranslationReducer(state, { type: "stream_started", generation: 1 });
    assert.equal(state.streamStatus, "streaming");
    state = reducer.reduceReaderTranslationEvent(state, { event: terminal, data: {} }, 1);
    assert.equal(state.streamStatus, "complete");
    assert.equal(state.terminalEvent, terminal);
  }
});

test("late events and aborts from an older stream generation are ignored", () => {
  let state = reducer.createReaderTranslationState(blocks());
  state = reducer.readerTranslationReducer(state, { type: "stream_started", generation: 1 });
  state = reducer.readerTranslationReducer(state, { type: "stream_started", generation: 2 });
  const current = state;

  state = reducer.readerTranslationReducer(state, {
    type: "stream_event",
    generation: 1,
    event: { event: "block_done", data: { index: 0, translation: "旧流译文" } },
  });
  assert.strictEqual(state, current);
  state = reducer.readerTranslationReducer(state, {
    type: "stream_aborted",
    generation: 1,
  });
  assert.strictEqual(state, current);
  state = reducer.readerTranslationReducer(state, {
    type: "stream_failed",
    generation: 1,
    error: "旧流错误",
  });
  assert.strictEqual(state, current);
  assert.equal(state.streamStatus, "streaming");
  assert.equal(state.streamGeneration, 2);
});

test("malformed events do not mutate reader state", () => {
  const state = reducer.createReaderTranslationState(blocks());
  assert.strictEqual(
    reducer.reduceReaderTranslationEvent(state, {
      event: "block_done",
      data: { index: "0", translation: "bad" },
    }, 0),
    state,
  );
  assert.strictEqual(
    reducer.reduceReaderTranslationEvent(state, { event: "unknown", data: {} }, 0),
    state,
  );
});

test("progress denominator remains stable across active and failed states", () => {
  const values = [
    { index: 0, type: "paragraph", original: "A", translation: "甲", status: "done" },
    { index: 1, type: "paragraph", original: "B", translation: null, status: "pending" },
    { index: 2, type: "paragraph", original: "C", translation: null, status: "translating" },
    { index: 3, type: "paragraph", original: "D", translation: null, status: "error" },
    { index: 4, type: "formula", original: "x", translation: null, status: "skip" },
  ];
  assert.deepEqual(reducer.selectTranslationProgress(values), {
    done: 1,
    failed: 1,
    pending: 1,
    translating: 1,
    total: 4,
  });
});

test("retry invalidates previous fit and only current revision may attach", () => {
  let state = reducer.createReaderTranslationState(blocks());
  state = reducer.readerTranslationReducer(state, {
    type: "fit_ready",
    blockIndex: 1,
    revision: 0,
    overlay: { text: "old" },
  });
  state = reducer.readerTranslationReducer(state, { type: "retry_started", blockIndex: 1 });
  assert.equal(state.blocks[1].status, "translating");
  assert.equal(state.overlayByBlock[1], undefined);
  assert.equal(reducer.selectFitRevision(state, 1), 1);
});
