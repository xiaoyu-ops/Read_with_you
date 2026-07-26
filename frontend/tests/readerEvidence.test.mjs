import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

import ts from "typescript";


async function loadReaderEvidence() {
  const frontendDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
  const sourcePath = path.join(frontendDir, "lib", "readerEvidence.ts");
  const outputDir = fs.mkdtempSync(path.join(os.tmpdir(), "pet-reader-evidence-"));
  const outputPath = path.join(outputDir, "readerEvidence.mjs");
  const transpiled = ts.transpileModule(fs.readFileSync(sourcePath, "utf8"), {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 },
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

const evidence = await loadReaderEvidence();

test("prefers explicit region, block and page evidence fields", () => {
  assert.deepEqual(
    evidence.getReaderEvidenceHint({
      claim: "训练细节",
      location: {
        block_index: 12,
        page: 4,
        region_id: "region-12",
      },
    }),
    {
      arxivId: null,
      blockIndex: 12,
      page: 4,
      regionId: "region-12",
      noteHeading: null,
      label: "训练细节",
    },
  );
});

test("parses compatible block and page references but not section numbers", () => {
  assert.deepEqual(
    evidence.getReaderEvidenceHint({ claim: "代码可用", source: "block #9, page 6" }),
    { arxivId: null, blockIndex: 9, page: 6, regionId: null, noteHeading: null, label: "代码可用" },
  );
  assert.equal(evidence.hasReaderEvidenceLocation({ citation: "Section 4.1" }), false);
  assert.equal(evidence.hasReaderEvidenceLocation({ source: "段落 #3" }), true);
});

test("does not turn bare numbers, issue references or page-only text into navigation", () => {
  for (const input of [
    { source: "#3" },
    { source: "GitHub issue #3" },
    { citation: "page 6" },
    { citation: "第 6 页" },
  ]) {
    assert.deepEqual(evidence.getReaderEvidenceHint(input), {
      arxivId: null,
      blockIndex: null,
      page: null,
      regionId: null,
      noteHeading: null,
      label: input.source ?? input.citation,
    });
    assert.equal(evidence.hasReaderEvidenceLocation(input), false);
  }
});

test("uses page only as context for a trusted block or structured region", () => {
  assert.deepEqual(
    evidence.getReaderEvidenceHint({ source: "block 9, page 6" }),
    { arxivId: null, blockIndex: 9, page: 6, regionId: null, noteHeading: null, label: "block 9, page 6" },
  );
  assert.deepEqual(
    evidence.getReaderEvidenceHint({
      claim: "region evidence",
      location: { region_id: "region-9", page: 6 },
    }),
    { arxivId: null, blockIndex: null, page: 6, regionId: "region-9", noteHeading: null, label: "region evidence" },
  );
  assert.equal(
    evidence.hasReaderEvidenceLocation({ location: { page: 6 } }),
    false,
  );
  assert.equal(
    evidence.hasReaderEvidenceLocation({ region_id: "untrusted-top-level", page: 6 }),
    false,
  );
});

test("rejects invalid coordinates and keeps a bounded natural label", () => {
  const hint = evidence.getReaderEvidenceHint({
    detail: "x".repeat(300),
    block_index: -1,
    page: 0,
    region_id: "   ",
  });
  assert.equal(hint.blockIndex, null);
  assert.equal(hint.arxivId, null);
  assert.equal(hint.page, null);
  assert.equal(hint.regionId, null);
  assert.equal(hint.noteHeading, null);
  assert.equal(hint.label.length, 180);
});

test("treats a structured paper-note heading as a navigable note citation", () => {
  assert.deepEqual(
    evidence.getReaderEvidenceHint({
      claim: "我的方法疑问",
      source: "你的笔记",
      note_heading: "方法与证据",
    }),
    {
      arxivId: null,
      blockIndex: null,
      page: null,
      regionId: null,
      noteHeading: "方法与证据",
      label: "我的方法疑问",
    },
  );
  assert.equal(
    evidence.hasReaderEvidenceLocation({ note_heading: "方法与证据" }),
    true,
  );
});

test("keeps a cross-paper id separate from the local block locator", () => {
  assert.deepEqual(
    evidence.getReaderEvidenceHint({
      arxiv_id: "2303.09540",
      claim: "另一篇论文里的方法笔记",
      location: { block_index: 7, page: 3, region_id: "other-region" },
    }),
    {
      arxivId: "2303.09540",
      blockIndex: 7,
      page: 3,
      regionId: "other-region",
      noteHeading: null,
      label: "另一篇论文里的方法笔记",
    },
  );
});
