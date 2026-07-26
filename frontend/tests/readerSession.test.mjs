import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

import ts from "typescript";


async function loadModule() {
  const frontendDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
  const sourcePath = path.join(frontendDir, "lib", "readerSession.ts");
  const outputDir = fs.mkdtempSync(path.join(os.tmpdir(), "pet-reader-session-"));
  const outputPath = path.join(outputDir, "readerSession.mjs");
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

const session = await loadModule();
const context = {
  paperId: "paper-1",
  blockIndexes: [0, 1, 2],
  pageCount: 12,
  now: () => "2026-07-21T08:00:00.000Z",
};

test("v1 reader state migrates without retaining dual-pane fields", () => {
  const migrated = session.parseReaderSession({
    version: 1,
    paperId: "paper-1",
    rightPaneSide: "original",
    rightScrollTop: 888,
    activeIndex: 1,
    pdfScrollTop: 321.5,
    pdfZoomPercent: 150,
    updatedAt: "2026-07-20T00:00:00.000Z",
  }, context);

  assert.deepEqual(migrated, {
    version: 2,
    paperId: "paper-1",
    readerMode: "selection_translation",
    activeIndex: 1,
    pdfPage: 1,
    pdfScrollTop: 0,
    pdfZoomPercent: 150,
    inspector: null,
    updatedAt: "2026-07-20T00:00:00.000Z",
  });
  const serialized = JSON.parse(session.serializeReaderSession(migrated));
  assert.equal("rightPaneSide" in serialized, false);
  assert.equal("rightScrollTop" in serialized, false);
  assert.equal(serialized.version, 2);
});

test("v2 roundtrip validates page, zoom and active block while retiring the old inspector", () => {
  const parsed = session.parseReaderSession({
    version: 2,
    paperId: "paper-1",
    readerMode: "unexpected",
    activeIndex: 99,
    pdfPage: 99,
    pdfScrollTop: -4,
    pdfZoomPercent: 260,
    inspector: { blockIndex: 2, regionId: "region-2", content: "original" },
  }, { ...context, validRegionIds: new Set(["region-2"]) });

  assert.equal(parsed.readerMode, "selection_translation");
  assert.equal(parsed.activeIndex, null);
  assert.equal(parsed.pdfPage, 12);
  assert.equal(parsed.pdfScrollTop, 0);
  assert.equal(parsed.pdfZoomPercent, 200);
  assert.equal(parsed.inspector, null);
  assert.deepEqual(
    session.parseStoredReaderSession(session.serializeReaderSession(parsed), context),
    parsed,
  );
});

test("zoom accepts fit-width values while keeping a canonical 100 percent default", () => {
  assert.equal(session.createDefaultReaderSession("paper-1", context.now).pdfZoomPercent, 100);
  assert.equal(
    session.parseReaderSession({ version: 2, pdfZoomPercent: 12 }, context).pdfZoomPercent,
    25,
  );
  assert.equal(
    session.parseReaderSession({ version: 2, pdfZoomPercent: 112.34 }, context).pdfZoomPercent,
    112.3,
  );
});

test("trackpad pinch zoom follows gesture amplitude instead of fixed ten-percent steps", () => {
  assert.equal(session.getTrackpadPinchZoomPercent(100, -17), 104.3);
  assert.equal(session.getTrackpadPinchZoomPercent(100, 17), 95.8);
  assert.equal(session.getTrackpadPinchZoomPercent(198, -100), 200);
  assert.equal(session.getTrackpadPinchZoomPercent(26, 100), 25);
  assert.equal(session.getTrackpadPinchZoomPercent(112.3, Number.NaN), 112.3);
});

test("fractional page counts never produce page zero", () => {
  const parsed = session.parseReaderSession({ version: 2, pdfPage: 9 }, {
    ...context,
    pageCount: 0.5,
  });
  assert.equal(parsed.pdfPage, 1);
});

test("malformed and cross-paper sessions fall back deterministically", () => {
  const malformed = session.parseStoredReaderSession("{broken", context);
  const crossPaper = session.parseReaderSession({ version: 2, paperId: "other" }, context);
  assert.deepEqual(malformed, session.createDefaultReaderSession("paper-1", context.now));
  assert.deepEqual(crossPaper, session.createDefaultReaderSession("paper-1", context.now));
});

test("storage helpers overwrite legacy JSON with canonical v2 only", () => {
  const values = new Map([[session.readerSessionKey("paper-1"), JSON.stringify({
    version: 1,
    paperId: "paper-1",
    rightPaneSide: "translation",
    activeIndex: 0,
    pdfScrollTop: 12,
    pdfZoomPercent: 125,
  })]]);
  const storage = {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
  };
  const loaded = session.loadReaderSession(context, storage);
  assert.equal(session.saveReaderSession(loaded, storage), true);
  const persisted = JSON.parse(values.get(session.readerSessionKey("paper-1")));
  assert.equal(persisted.version, 2);
  assert.equal("rightPaneSide" in persisted, false);
});
