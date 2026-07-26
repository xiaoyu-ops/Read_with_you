import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

import ts from "typescript";


const audit = await loadAudit();

const box = (x0, y0, x1, y1) => ({ x0, y0, x1, y1 });
const PAGE_CSS_SIZE_AT_100 = {
  1: { widthPx: 480, heightPx: 640 },
};

function canonicalCover(bbox, page = 1) {
  const size = PAGE_CSS_SIZE_AT_100[page];
  return box(
    Math.max(0, bbox.x0 - 2 / size.widthPx),
    Math.max(0, bbox.y0 - 2 / size.heightPx),
    Math.min(1, bbox.x1 + 2 / size.widthPx),
    Math.min(1, bbox.y1 + 2 / size.heightPx),
  );
}

function buildReport(input) {
  return audit.buildInlineTranslationAuditReport({
    pageCssSizeAt100: PAGE_CSS_SIZE_AT_100,
    ...input,
  });
}

function block(index, overrides = {}) {
  return {
    index,
    type: "paragraph",
    original: `source-${index}`,
    translation: `译文-${index}`,
    status: "done",
    ...overrides,
  };
}

function region(blockIndex, overrides = {}) {
  const bounds = overrides.bbox ?? box(0.1, 0.1 + blockIndex * 0.1, 0.8, 0.18 + blockIndex * 0.1);
  return {
    region_id: `region-${blockIndex}`,
    block_index: blockIndex,
    page: 1,
    flow_order: 0,
    kind: "paragraph",
    bbox: bounds,
    line_boxes: [bounds],
    word_boxes: [],
    protected_boxes: [],
    source_block_order: blockIndex,
    source_line_orders: [blockIndex],
    source_word_orders: [],
    rotation: 0,
    confidence: 0.95,
    render_policy: "replace",
    failure_reason: null,
    ...overrides,
  };
}

function fitted(blockIndex, text = `译文-${blockIndex}`, overrides = {}) {
  const sourceBox = box(0.1, 0.1 + blockIndex * 0.1, 0.8, 0.18 + blockIndex * 0.1);
  return {
    blockIndex,
    policy: "replace",
    reason: null,
    sourceFontPx100: 12,
    regions: [
      {
        regionId: `region-${blockIndex}`,
        blockIndex,
        page: 1,
        flowOrder: 0,
        bbox: canonicalCover(sourceBox),
        rotation: 0,
        text,
        fontPx100: 12,
        lineHeightPx100: 15,
      },
    ],
    ...overrides,
  };
}

function layout(regions) {
  return {
    version: 1,
    cache_key: "a".repeat(64),
    source_pdf_sha256: "b".repeat(64),
    block_source_sha256: "c".repeat(64),
    adapter: "poppler_bbox_layout",
    adapter_version: "2",
    pdf_url: "/papers/test/pdf",
    page_count: 1,
    pages: [{ page: 1, width: 600, height: 800, rotation: 0 }],
    regions,
    quality: {
      mappable_count: regions.length,
      mapped_count: regions.length,
      replaceable_count: regions.filter((item) => item.render_policy === "replace").length,
      panel_only_count: regions.filter((item) => item.render_policy === "panel_only").length,
      unmapped_count: 0,
      mapped_ratio: 1,
      average_confidence: 0.95,
      protected_overlap_count: 0,
      protected_count: 0,
      unmapped_block_indexes: [],
      failure_counts: {},
    },
    warnings: [],
  };
}

function background(regions) {
  return Object.fromEntries(regions.map((item) => [item.region_id, {
    evidence: "uniform",
    backgroundColor: "rgb(255 255 255)",
    foregroundColor: "rgb(17 24 39)",
  }]));
}

test("classifies every completed translation as inline, panel, or unmapped", () => {
  const regions = [
    region(0),
    region(1, {
      confidence: 0.72,
      render_policy: "panel_only",
      failure_reason: "low_confidence",
    }),
  ];
  const report = buildReport({
    blocks: [block(0), block(1), block(2, { type: "formula" })],
    layout: layout(regions),
    fittedByBlock: {
      0: fitted(0),
      1: fitted(1, "译文-1", {
        policy: "panel_only",
        reason: "low_confidence",
        regions: [],
      }),
    },
    backgroundByRegion: background(regions),
  });

  assert.deepEqual(report, {
    translatable_block_count: 3,
    completed_translation_count: 3,
    pending_translation_count: 0,
    error_translation_count: 0,
    incomplete_translation_count: 0,
    eligible_text_count: 2,
    protected_excluded_count: 0,
    inline_count: 1,
    panel_count: 1,
    unmapped_count: 1,
    accessibility_ratio: 1,
    safe_inline_count: 1,
    safe_inline_coverage: 0.5,
    replace_region_count: 1,
    replace_average_confidence: 0.95,
    fit_pending_count: 0,
    translation_mismatch_count: 0,
    protected_overlap_count: 0,
    overflow_count: 0,
    silent_missing_count: 0,
    failure_counts: { low_confidence: 1, unmapped: 1 },
    coverage_failure_counts: { low_confidence: 1 },
  });
  assert.equal(JSON.stringify(report).includes("译文-0"), false);
  assert.equal(
    audit.evaluateInlineTranslationGate(report, {
      minimumCoverage: 0.5,
      minimumReplaceConfidence: 0.92,
    }).passed,
    true,
  );
});

test("reports pending and failed translations outside the completed-only coverage", () => {
  const regions = [region(0)];
  const report = buildReport({
    blocks: [
      block(0),
      block(1, { status: "pending", translation: null }),
      block(2, { status: "error", translation: null }),
      block(3, { status: "skip", translation: null }),
    ],
    layout: layout(regions),
    fittedByBlock: { 0: fitted(0) },
    backgroundByRegion: background(regions),
  });

  assert.equal(report.translatable_block_count, 3);
  assert.equal(report.completed_translation_count, 1);
  assert.equal(report.pending_translation_count, 1);
  assert.equal(report.error_translation_count, 1);
  assert.equal(report.incomplete_translation_count, 2);
});

test("reports a pending fit separately from a silent replacement leak", () => {
  const regions = [region(0), region(1)];
  const report = buildReport({
    blocks: [block(0), block(1)],
    layout: layout(regions),
    fittedByBlock: {
      1: fitted(1, "译文-1", {
        regions: [
          {
            ...fitted(1).regions[0],
            regionId: "missing-region",
          },
        ],
      }),
    },
    backgroundByRegion: {
      "region-0": undefined,
      "region-1": {
        evidence: "uniform",
        backgroundColor: "rgb(255 255 255)",
        foregroundColor: "rgb(17 24 39)",
      },
    },
  });

  assert.equal(report.fit_pending_count, 1);
  assert.equal(report.silent_missing_count, 1);
  assert.equal(report.panel_count, 2);
  assert.equal(report.inline_count, 0);
  assert.equal(report.unmapped_count, 0);
  assert.equal(report.accessibility_ratio, 1);
  assert.equal(report.failure_counts.fit_pending, 1);
  const gate = audit.evaluateInlineTranslationGate(report, { minimumCoverage: 0.8 });
  assert.equal(gate.passed, false);
  assert.deepEqual(gate.reasons, [
    "fit_pending",
    "silent_missing",
    "coverage_below_minimum",
    "replace_confidence_below_minimum",
  ]);
});

test("downgrades a fitted region whose background cannot be rendered", () => {
  const regions = [region(0)];
  const report = buildReport({
    blocks: [block(0)],
    layout: layout(regions),
    fittedByBlock: { 0: fitted(0) },
    backgroundByRegion: { "region-0": { evidence: "uniform" } },
  });

  assert.equal(report.inline_count, 0);
  assert.equal(report.panel_count, 1);
  assert.equal(report.accessibility_ratio, 1);
  assert.equal(report.safe_inline_coverage, 0);
  assert.equal(report.replace_region_count, 0);
  assert.equal(report.failure_counts.background_unrenderable, 1);
});

test("rejects duplicate fitted ids and invalid thresholds", () => {
  const regions = [region(0), region(0, { region_id: "region-0-extra", flow_order: 1 })];
  const first = fitted(0).regions[0];
  const report = buildReport({
    blocks: [block(0)],
    layout: layout(regions),
    fittedByBlock: { 0: fitted(0, "译文-0", { regions: [first, { ...first }] }) },
    backgroundByRegion: background(regions),
  });

  assert.equal(report.silent_missing_count, 1);
  assert.equal(
    audit.evaluateInlineTranslationGate(report, { minimumCoverage: Number.NaN }).reasons[0],
    "invalid_threshold",
  );
});

test("detects fitted text mismatch, cross-layout protected overlap, and overflow", () => {
  const candidate = region(0);
  const protection = region(99, {
    region_id: "protected-image",
    bbox: box(0.5, 0.5, 0.9, 0.8),
    kind: "figure",
    render_policy: "preserve",
    failure_reason: "non_text_content",
    protected_boxes: [box(0.2, 0.12, 0.3, 0.16)],
  });
  const regions = [candidate, protection];
  const report = buildReport({
    blocks: [block(0)],
    layout: layout(regions),
    fittedByBlock: { 0: fitted(0, "不完整译文") },
    backgroundByRegion: background(regions),
    overflowBlockIndexes: new Set([0]),
  });

  assert.equal(report.translation_mismatch_count, 1);
  assert.equal(report.protected_overlap_count, 1);
  assert.equal(report.overflow_count, 1);
  assert.deepEqual(report.failure_counts, {
    overflow: 1,
    protected_overlap: 1,
    translation_mismatch: 1,
  });
  assert.deepEqual(
    audit.evaluateInlineTranslationGate(report, { minimumCoverage: 1 }).reasons,
    ["overflow", "protected_overlap", "translation_mismatch"],
  );
});

test("counts page-level hybrid protection in the overlap audit", () => {
  const candidate = region(0, { geometry_source: "poppler_bbox_layout" });
  const hybrid = layout([candidate]);
  hybrid.adapter = "hybrid_poppler_mineru";
  hybrid.adapter_version = "1";
  hybrid.sources = [
    { adapter: "poppler_bbox_layout", adapter_version: "3" },
    {
      adapter: "mineru_middle",
      adapter_version: "2",
      generation: "d".repeat(32),
      is_ocr: false,
    },
  ];
  hybrid.pages[0].protected_boxes = [box(0.2, 0.12, 0.3, 0.16)];

  const report = buildReport({
    blocks: [block(0)],
    layout: hybrid,
    fittedByBlock: { 0: fitted(0) },
    backgroundByRegion: background([candidate]),
  });

  assert.equal(report.protected_overlap_count, 1);
  assert.deepEqual(report.failure_counts, { protected_overlap: 1 });
  assert.deepEqual(
    audit.evaluateInlineTranslationGate(report, { minimumCoverage: 1 }).reasons,
    ["protected_overlap"],
  );
});

test("excludes formula-protected paragraphs from safe coverage but keeps accessibility", () => {
  const regions = [region(0), region(1)];
  const report = buildReport({
    blocks: [
      block(0, {
        original: "The protected equation is $E = mc^2$.",
        translation: "受保护的公式是 $E = mc^2$。",
      }),
      block(1),
    ],
    layout: layout(regions),
    fittedByBlock: {
      0: fitted(0, "译文-0", {
        policy: "panel_only",
        reason: "protected_geometry_missing",
        regions: [],
      }),
      1: fitted(1),
    },
    backgroundByRegion: background(regions),
  });

  assert.equal(report.completed_translation_count, 2);
  assert.equal(report.protected_excluded_count, 1);
  assert.equal(report.eligible_text_count, 1);
  assert.equal(report.safe_inline_count, 1);
  assert.equal(report.safe_inline_coverage, 1);
  assert.equal(report.accessibility_ratio, 1);
  assert.equal(
    audit.evaluateInlineTranslationGate(report, { minimumCoverage: 1 }).passed,
    true,
  );
});

test("enforces the 0.920 replace-confidence boundary", () => {
  const make = (confidence) => {
    const regions = [region(0, { confidence })];
    return buildReport({
      blocks: [block(0)],
      layout: layout(regions),
      fittedByBlock: { 0: fitted(0) },
      backgroundByRegion: background(regions),
    });
  };

  const below = audit.evaluateInlineTranslationGate(make(0.919), { minimumCoverage: 1 });
  assert.equal(below.passed, false);
  assert.deepEqual(below.reasons, ["replace_confidence_below_minimum"]);

  const exact = audit.evaluateInlineTranslationGate(make(0.92), { minimumCoverage: 1 });
  assert.deepEqual(exact, { passed: true, reasons: [] });
});

test("accepts only the exact canonical cover bbox for a fitted region", () => {
  const regions = [region(0)];
  const exact = buildReport({
    blocks: [block(0)],
    layout: layout(regions),
    fittedByBlock: { 0: fitted(0) },
    backgroundByRegion: background(regions),
  });
  assert.equal(exact.inline_count, 1);
  assert.equal(exact.silent_missing_count, 0);

  const arbitrary = fitted(0);
  arbitrary.regions[0].bbox = {
    ...arbitrary.regions[0].bbox,
    x0: arbitrary.regions[0].bbox.x0 - 1 / PAGE_CSS_SIZE_AT_100[1].widthPx,
  };
  const rejected = buildReport({
    blocks: [block(0)],
    layout: layout(regions),
    fittedByBlock: { 0: arbitrary },
    backgroundByRegion: background(regions),
  });
  assert.equal(rejected.inline_count, 0);
  assert.equal(rejected.silent_missing_count, 1);
});

test("binds only the exact peer-clipped canonical cover", () => {
  const candidate = region(0, {
    bbox: box(0.1, 0.2, 0.8, 0.4),
    line_boxes: [box(0.1, 0.205, 0.8, 0.38)],
    source_line_orders: [1],
  });
  const peer = region(1, {
    bbox: box(0.1, 0.18, 0.8, 0.1984),
    line_boxes: [box(0.1, 0.18, 0.8, 0.1984)],
    source_line_orders: [0],
    confidence: 0.5,
    render_policy: "preserve",
    failure_reason: "low_confidence",
  });
  const expected = {
    x0: 0.1 - 2 / PAGE_CSS_SIZE_AT_100[1].widthPx,
    y0: Math.ceil(0.1984 * PAGE_CSS_SIZE_AT_100[1].heightPx) /
      PAGE_CSS_SIZE_AT_100[1].heightPx,
    x1: 0.8 + 2 / PAGE_CSS_SIZE_AT_100[1].widthPx,
    y1: 0.4 + 2 / PAGE_CSS_SIZE_AT_100[1].heightPx,
  };
  const clipped = fitted(0);
  clipped.regions[0].bbox = expected;
  const accepted = buildReport({
    blocks: [block(0)],
    layout: layout([candidate, peer]),
    fittedByBlock: { 0: clipped },
    backgroundByRegion: background([candidate]),
  });
  assert.equal(accepted.inline_count, 1);
  assert.equal(accepted.silent_missing_count, 0);

  const unclipped = fitted(0);
  unclipped.regions[0].bbox = canonicalCover(candidate.bbox);
  const rejected = buildReport({
    blocks: [block(0)],
    layout: layout([candidate, peer]),
    fittedByBlock: { 0: unclipped },
    backgroundByRegion: background([candidate]),
  });
  assert.equal(rejected.inline_count, 0);
  assert.equal(rejected.silent_missing_count, 1);
});

async function loadAudit() {
  const frontendDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
  const sourcePath = path.join(frontendDir, "lib", "inlineTranslationAudit.ts");
  const fitSourcePath = path.join(frontendDir, "lib", "translationFit.ts");
  const outputDir = path.join(frontendDir, ".inline-translation-audit-test");
  const outputPath = path.join(outputDir, "inlineTranslationAudit.mjs");
  const fitOutputPath = path.join(outputDir, "translationFit.mjs");
  const compile = (filePath) => ts.transpileModule(fs.readFileSync(filePath, "utf8"), {
    compilerOptions: {
      target: ts.ScriptTarget.ES2022,
      module: ts.ModuleKind.ES2022,
      moduleResolution: ts.ModuleResolutionKind.Bundler,
      esModuleInterop: true,
    },
    fileName: filePath,
    reportDiagnostics: true,
  });
  const fitTranspiled = compile(fitSourcePath);
  const auditTranspiled = compile(sourcePath);
  const errors = [
    ...(fitTranspiled.diagnostics ?? []),
    ...(auditTranspiled.diagnostics ?? []),
  ].filter((diagnostic) => diagnostic.category === ts.DiagnosticCategory.Error);
  if (errors.length > 0) {
    throw new Error(
      errors.map((diagnostic) => ts.flattenDiagnosticMessageText(diagnostic.messageText, "\n")).join("\n"),
    );
  }
  fs.mkdirSync(outputDir, { recursive: true });
  fs.writeFileSync(fitOutputPath, fitTranspiled.outputText, "utf8");
  fs.writeFileSync(
    outputPath,
    auditTranspiled.outputText.replace('from "./translationFit"', 'from "./translationFit.mjs"'),
    "utf8",
  );
  try {
    return await import(`${pathToFileURL(outputPath).href}?v=${Date.now()}`);
  } finally {
    fs.rmSync(outputDir, { recursive: true, force: true });
  }
}
