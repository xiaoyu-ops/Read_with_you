import assert from "node:assert/strict";
import test from "node:test";

import { loadTranslationFit } from "./loadTranslationFit.mjs";


const fit = await loadTranslationFit();

const box = (x0, y0, x1, y1) => ({ x0, y0, x1, y1 });

function region(overrides = {}) {
  const bounds = overrides.bbox ?? box(0.1, 0.1, 0.9, 0.3);
  const lineHeight = Math.min((bounds.y1 - bounds.y0) / 4, 0.025);
  const lineGap = Math.min((bounds.y1 - bounds.y0) / 3, 0.04);
  return {
    region_id: "b0-p1-r0-test",
    block_index: 0,
    page: 1,
    flow_order: 0,
    kind: "paragraph",
    bbox: bounds,
    line_boxes: overrides.line_boxes ?? [
      box(bounds.x0, bounds.y0, bounds.x1, bounds.y0 + lineHeight),
      box(bounds.x0, bounds.y0 + lineGap, bounds.x1, bounds.y0 + lineGap + lineHeight),
    ],
    word_boxes: [],
    protected_boxes: [],
    source_block_order: 0,
    source_line_orders: [0, 1],
    source_word_orders: [],
    rotation: 0,
    confidence: 0.96,
    render_policy: "replace",
    failure_reason: null,
    ...overrides,
    bbox: bounds,
    line_boxes: overrides.line_boxes ?? [
      box(bounds.x0, bounds.y0, bounds.x1, bounds.y0 + lineHeight),
      box(bounds.x0, bounds.y0 + lineGap, bounds.x1, bounds.y0 + lineGap + lineHeight),
    ],
  };
}

function layout(regions = [region()], pages = [{ page: 1, width: 600, height: 800, rotation: 0 }]) {
  return {
    version: 1,
    cache_key: "a".repeat(64),
    source_pdf_sha256: "b".repeat(64),
    block_source_sha256: "c".repeat(64),
    adapter: "poppler_bbox_layout",
    adapter_version: "2",
    pdf_url: "/papers/test/pdf",
    page_count: pages.length,
    pages,
    regions,
    quality: {
      mappable_count: 1,
      mapped_count: 1,
      replaceable_count: 1,
      panel_only_count: 0,
      unmapped_count: 0,
      mapped_ratio: 1,
      average_confidence: 0.96,
      protected_overlap_count: 0,
      protected_count: 0,
      unmapped_block_indexes: [],
      failure_counts: {},
    },
    warnings: [],
  };
}

function block(overrides = {}) {
  return {
    index: 0,
    type: "paragraph",
    original: "A layout-aware translation cites [3].",
    translation: "一种可靠的原位译文引用 [3]。",
    status: "done",
    ...overrides,
  };
}

function evidence(regions) {
  return Object.fromEntries(regions.map((item) => [item.region_id, "uniform"]));
}

function pageMetrics(regions, size = { widthPx: 480, heightPx: 640 }) {
  return Object.fromEntries([...new Set(regions.map((item) => item.page))].map((page) => [page, size]));
}

function fitOptions(regions, overrides = {}) {
  return {
    backgroundByRegion: evidence(regions),
    pageCssSizeAt100: pageMetrics(regions),
    ...overrides,
  };
}

function capacityMeasurer(limit = Number.POSITIVE_INFINITY) {
  const capacity = (input) => {
    if (input.fontPx100 > limit) return 0;
    const columns = Math.max(1, Math.floor(input.widthPx100 / input.fontPx100));
    const rows = Math.max(1, Math.floor(input.heightPx100 / input.lineHeightPx100));
    return Math.min(input.tokens.length, columns * rows);
  };
  return {
    maxFittingPrefix: capacity,
    verify(input) {
      return capacity(input) >= input.tokens.length;
    },
  };
}

test("accepts 0.90 but rejects 0.899 confidence", async () => {
  const acceptedRegion = region({ confidence: 0.9 });
  const accepted = await fit.buildTranslationFitPlan(
    layout([acceptedRegion]),
    [block()],
    capacityMeasurer(),
    fitOptions([acceptedRegion]),
  );
  assert.equal(accepted.blocks[0].policy, "replace");

  const rejectedRegion = region({ confidence: 0.899 });
  const rejected = await fit.buildTranslationFitPlan(
    layout([rejectedRegion]),
    [block()],
    capacityMeasurer(),
    fitOptions([rejectedRegion]),
  );
  assert.equal(rejected.blocks[0].policy, "panel_only");
  assert.equal(rejected.blocks[0].reason, "low_confidence");
});

test("treats hybrid layouts as precise and enforces page-level protection", async () => {
  const candidate = region({ geometry_source: "poppler_bbox_layout" });
  const hybrid = layout(
    [candidate],
    [{ page: 1, width: 600, height: 800, rotation: 0, protected_boxes: [] }],
  );
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

  const accepted = await fit.buildTranslationFitPlan(
    hybrid,
    [block()],
    capacityMeasurer(),
    fitOptions([candidate]),
  );
  assert.equal(accepted.blocks[0].policy, "replace");

  hybrid.pages[0].protected_boxes = [box(0.2, 0.15, 0.3, 0.2)];
  const protectedPlan = await fit.buildTranslationFitPlan(
    hybrid,
    [block()],
    capacityMeasurer(),
    fitOptions([candidate]),
  );
  assert.equal(protectedPlan.blocks[0].policy, "panel_only");
  assert.equal(protectedPlan.blocks[0].reason, "protected_overlap");
});

test("requires done translation and explicit uniform background evidence", async () => {
  const pending = await fit.buildTranslationFitPlan(
    layout(),
    [block({ status: "pending", translation: null })],
    capacityMeasurer(),
  );
  assert.equal(pending.blocks[0].reason, "translation_not_done");

  const unknown = await fit.buildTranslationFitPlan(
    layout(),
    [block()],
    capacityMeasurer(),
    { pageCssSizeAt100: pageMetrics([region()]) },
  );
  assert.equal(unknown.blocks[0].reason, "background_unverified");

  const complex = await fit.buildTranslationFitPlan(
    layout(),
    [block()],
    capacityMeasurer(),
    fitOptions([region()], { backgroundByRegion: { "b0-p1-r0-test": "complex" } }),
  );
  assert.equal(complex.blocks[0].reason, "background_complex");

  const missingPageMetrics = await fit.buildTranslationFitPlan(
    layout(),
    [block()],
    capacityMeasurer(),
    { backgroundByRegion: evidence([region()]) },
  );
  assert.equal(missingPageMetrics.blocks[0].reason, "page_metrics_unavailable");
});

test("validates continuous flow order and preserves cross-page token order", async () => {
  const gap = [region(), region({ region_id: "gap", flow_order: 2, bbox: box(0.1, 0.35, 0.9, 0.5) })];
  const invalid = await fit.buildTranslationFitPlan(
    layout(gap),
    [block()],
    capacityMeasurer(),
    fitOptions(gap),
  );
  assert.equal(invalid.blocks[0].reason, "invalid_flow_order");

  const crossPage = [
    region({
      bbox: box(0.1, 0.1, 0.35, 0.2),
      line_boxes: [box(0.1, 0.1, 0.35, 0.125)],
      source_line_orders: [0],
    }),
    region({
      region_id: "b0-p2-r1-test",
      page: 2,
      flow_order: 1,
      bbox: box(0.1, 0.1, 0.35, 0.2),
      line_boxes: [box(0.1, 0.1, 0.35, 0.125)],
      source_line_orders: [2],
    }),
  ];
  const limited = {
    maxFittingPrefix(input) {
      return Math.min(5, input.tokens.length);
    },
    verify(input) {
      return input.tokens.length <= 5;
    },
  };
  const translated = "甲乙丙丁戊己庚辛壬癸";
  const result = await fit.buildTranslationFitPlan(
    layout(crossPage, [
      { page: 1, width: 600, height: 800, rotation: 0 },
      { page: 2, width: 600, height: 800, rotation: 0 },
    ]),
    [block({ original: "abcdefghij", translation: translated })],
    limited,
    fitOptions(crossPage),
  );
  assert.equal(result.blocks[0].policy, "replace");
  assert.deepEqual(result.blocks[0].regions.map((item) => item.page), [1, 2]);
  assert.equal(result.blocks[0].regions.map((item) => item.text).join(""), translated);
});

test("rejects legacy geometry, invalid boxes and unsupported rotation", async () => {
  const candidate = region();
  const legacyLayout = layout([candidate]);
  legacyLayout.adapter = "legacy_pdf_map";
  const legacy = await fit.buildTranslationFitPlan(
    legacyLayout,
    [block()],
    capacityMeasurer(),
    fitOptions([candidate]),
  );
  assert.equal(legacy.blocks[0].reason, "layout_not_precise");

  const unknownAdapterLayout = layout([candidate]);
  unknownAdapterLayout.adapter = "future_unreviewed_adapter";
  const unknownAdapter = await fit.buildTranslationFitPlan(
    unknownAdapterLayout,
    [block()],
    capacityMeasurer(),
    fitOptions([candidate]),
  );
  assert.equal(unknownAdapter.blocks[0].reason, "layout_not_precise");

  const invalid = region({ bbox: box(0.1, 0.1, 1.1, 0.3) });
  const invalidPlan = await fit.buildTranslationFitPlan(
    layout([invalid]),
    [block()],
    capacityMeasurer(),
    fitOptions([invalid]),
  );
  assert.equal(invalidPlan.blocks[0].reason, "invalid_geometry");

  const rotated = region({ rotation: 90 });
  const rotatedPlan = await fit.buildTranslationFitPlan(
    layout([rotated]),
    [block()],
    capacityMeasurer(),
    fitOptions([rotated]),
  );
  assert.equal(rotatedPlan.blocks[0].reason, "unsupported_rotation");
});

test("recomputes protected overlap from every same-page region", async () => {
  const candidate = region();
  const protectedRegion = region({
    region_id: "protected-other-block",
    block_index: 9,
    kind: "image",
    bbox: box(0.92, 0.1, 0.99, 0.2),
    line_boxes: [box(0.92, 0.1, 0.99, 0.2)],
    source_line_orders: [0],
    protected_boxes: [box(0.2, 0.15, 0.3, 0.2)],
    render_policy: "preserve",
    failure_reason: "protected_content",
  });
  const result = await fit.buildTranslationFitPlan(
    layout([candidate, protectedRegion]),
    [block()],
    capacityMeasurer(),
    fitOptions([candidate]),
  );
  assert.equal(result.blocks[0].reason, "protected_overlap");

  const candidateCover = fit.deriveCanonicalCoverBox(candidate.bbox, {
    widthPx: 480,
    heightPx: 640,
  });
  protectedRegion.protected_boxes = [box(0, 0.1, candidateCover.x0, 0.2)];
  const touching = await fit.buildTranslationFitPlan(
    layout([candidate, protectedRegion]),
    [block()],
    capacityMeasurer(),
    fitOptions([candidate]),
  );
  assert.equal(touching.blocks[0].policy, "replace");

  protectedRegion.page = 2;
  protectedRegion.protected_boxes = [box(0.2, 0.15, 0.3, 0.2)];
  const otherPage = await fit.buildTranslationFitPlan(
    layout(
      [candidate, protectedRegion],
      [
        { page: 1, width: 600, height: 800, rotation: 0 },
        { page: 2, width: 600, height: 800, rotation: 0 },
      ],
    ),
    [block()],
    capacityMeasurer(),
    fitOptions([candidate]),
  );
  assert.equal(otherPage.blocks[0].policy, "replace");
});

test("downgrades an opaque block bbox that covers another replaceable block line", async () => {
  const heading = region({
    region_id: "heading-small",
    block_index: 0,
    kind: "heading",
    bbox: box(0.1, 0.1, 0.45, 0.15),
    line_boxes: [box(0.1, 0.105, 0.45, 0.13)],
    source_line_orders: [0],
  });
  const oversizedBody = region({
    region_id: "body-oversized",
    block_index: 1,
    flow_order: 0,
    bbox: box(0.08, 0.08, 0.92, 0.34),
    line_boxes: [box(0.08, 0.2, 0.92, 0.225)],
    source_block_order: 1,
    source_line_orders: [1],
  });
  const regions = [heading, oversizedBody];
  const result = await fit.buildTranslationFitPlan(
    layout(regions),
    [
      block({ index: 0, type: "heading", original: "Overview", translation: "概述" }),
      block({ index: 1, original: "Body", translation: "正文" }),
    ],
    capacityMeasurer(),
    fitOptions(regions),
  );

  assert.equal(result.blocks[0].policy, "replace");
  assert.equal(result.blocks[1].policy, "panel_only");
  assert.equal(result.blocks[1].reason, "invalid_geometry");
  assert.deepEqual(result.blocks[1].regions, []);
});

test("ignores lines touching the canonical cover and lines on other pages", async () => {
  const candidate = region({
    bbox: box(0.1, 0.1, 0.9, 0.3),
    line_boxes: [box(0.1, 0.2, 0.9, 0.225)],
    source_line_orders: [0],
  });
  const candidateCover = fit.deriveCanonicalCoverBox(candidate.bbox, {
    widthPx: 480,
    heightPx: 640,
  });
  const other = region({
    region_id: "other-block",
    block_index: 1,
    flow_order: 0,
    bbox: box(0.1, candidateCover.y1, 0.9, 0.4),
    line_boxes: [box(0.1, candidateCover.y1, 0.9, 0.325)],
    source_block_order: 1,
    source_line_orders: [1],
  });

  for (const override of [{}, { page: 2 }]) {
    const peer = { ...other, ...override };
    const pages = peer.page === 2
      ? [
          { page: 1, width: 600, height: 800, rotation: 0 },
          { page: 2, width: 600, height: 800, rotation: 0 },
        ]
      : undefined;
    const result = await fit.buildTranslationFitPlan(
      layout([candidate, peer], pages),
      [block()],
      capacityMeasurer(),
      fitOptions([candidate]),
    );
    assert.equal(result.blocks[0].policy, "replace");
  }
});

test("a replace candidate cannot cover low-confidence or preserved English text", async () => {
  const candidate = region({
    bbox: box(0.1, 0.1, 0.9, 0.3),
    line_boxes: [box(0.1, 0.2, 0.9, 0.225)],
    source_line_orders: [0],
  });
  const overlappingPeer = region({
    region_id: "preserved-peer",
    block_index: 1,
    flow_order: 0,
    bbox: box(0.2, 0.18, 0.8, 0.4),
    line_boxes: [box(0.2, 0.22, 0.8, 0.245)],
    source_block_order: 1,
    source_line_orders: [1],
  });

  for (const override of [
    { confidence: 0.89 },
    { render_policy: "preserve", failure_reason: "protected_content" },
  ]) {
    const peer = { ...overlappingPeer, ...override };
    const result = await fit.buildTranslationFitPlan(
      layout([candidate, peer]),
      [block()],
      capacityMeasurer(),
      fitOptions([candidate]),
    );
    assert.equal(result.blocks[0].policy, "panel_only");
    assert.equal(result.blocks[0].reason, "invalid_geometry");
  }
});

test("uses a valid peer region bbox when its line geometry is invalid", async () => {
  const candidate = region({
    bbox: box(0.1, 0.1, 0.9, 0.3),
    line_boxes: [box(0.1, 0.2, 0.9, 0.225)],
    source_line_orders: [0],
  });
  const invalidPeer = region({
    region_id: "invalid-preserved-peer",
    block_index: 1,
    bbox: box(0.2, 0.2, 0.8, 0.4),
    line_boxes: [box(0.2, 0.45, 0.8, 0.475)],
    source_block_order: 1,
    source_line_orders: [1],
    confidence: 0.5,
    render_policy: "preserve",
    failure_reason: "low_confidence",
  });
  const overlapping = await fit.buildTranslationFitPlan(
    layout([candidate, invalidPeer]),
    [block()],
    capacityMeasurer(),
    fitOptions([candidate]),
  );

  assert.equal(overlapping.blocks[0].policy, "panel_only");
  assert.equal(overlapping.blocks[0].reason, "invalid_geometry");

  const separateCandidate = region({
    bbox: box(0.1, 0.05, 0.9, 0.15),
    line_boxes: [box(0.1, 0.08, 0.9, 0.105)],
    source_line_orders: [0],
  });
  const separate = await fit.buildTranslationFitPlan(
    layout([separateCandidate, invalidPeer]),
    [block()],
    capacityMeasurer(),
    fitOptions([separateCandidate]),
  );
  assert.equal(separate.blocks[0].policy, "replace");
});

test("invalid peer line and region geometry still makes the page fail closed", async () => {
  const candidate = region({
    bbox: box(0.1, 0.1, 0.9, 0.3),
    line_boxes: [box(0.1, 0.2, 0.9, 0.225)],
    source_line_orders: [0],
  });
  const invalidPeer = region({
    region_id: "invalid-peer-bbox",
    block_index: 1,
    bbox: box(-0.1, 0.2, 0.8, 0.4),
    line_boxes: [box(0.2, 0.45, 0.8, 0.475)],
    source_block_order: 1,
    source_line_orders: [1],
    confidence: 0.5,
    render_policy: "preserve",
    failure_reason: "low_confidence",
  });
  const result = await fit.buildTranslationFitPlan(
    layout([candidate, invalidPeer]),
    [block()],
    capacityMeasurer(),
    fitOptions([candidate]),
  );

  assert.equal(result.blocks[0].policy, "panel_only");
  assert.equal(result.blocks[0].reason, "invalid_geometry");
});

test("cannot replace when a peer makes the shared canonical cover unavailable", async () => {
  const candidate = region({
    bbox: box(0.1, 0.1, 0.9, 0.3),
    line_boxes: [box(0.1, 0.2, 0.9, 0.225)],
    source_line_orders: [0],
  });
  const overlappingPeer = region({
    region_id: "overlapping-peer",
    block_index: 1,
    bbox: box(0.2, 0.2, 0.8, 0.4),
    line_boxes: [box(0.2, 0.22, 0.8, 0.245)],
    source_block_order: 1,
    source_line_orders: [1],
  });
  assert.equal(
    fit.deriveTranslationRegionCoverBox(
      candidate,
      { widthPx: 480, heightPx: 640 },
      [candidate, overlappingPeer],
    ),
    null,
  );

  const result = await fit.buildTranslationFitPlan(
    layout([candidate, overlappingPeer]),
    [block()],
    capacityMeasurer(),
    fitOptions([candidate], {
      backgroundByRegion: {
        [candidate.region_id]: "uniform",
        [overlappingPeer.region_id]: "uniform",
      },
    }),
  );

  assert.equal(result.blocks[0].policy, "panel_only");
  assert.equal(result.blocks[0].reason, "invalid_geometry");
});

test("canonical two-pixel cover bbox is rendered and rechecked against page protection", async () => {
  const candidate = region({
    bbox: box(0.1, 0.1, 0.9, 0.3),
    line_boxes: [box(0.1, 0.2, 0.9, 0.225)],
    source_line_orders: [0],
  });
  const pageSize = { widthPx: 480, heightPx: 640 };
  const accepted = await fit.buildTranslationFitPlan(
    layout([candidate]),
    [block()],
    capacityMeasurer(),
    fitOptions([candidate], { pageCssSizeAt100: { 1: pageSize } }),
  );
  assert.deepEqual(accepted.blocks[0].regions[0].bbox, {
    x0: 0.1 - 2 / pageSize.widthPx,
    y0: 0.1 - 2 / pageSize.heightPx,
    x1: 0.9 + 2 / pageSize.widthPx,
    y1: 0.3 + 2 / pageSize.heightPx,
  });

  const protectedPage = layout(
    [candidate],
    [{
      page: 1,
      width: 600,
      height: 800,
      rotation: 0,
      protected_boxes: [box(0.9005, 0.15, 0.905, 0.2)],
    }],
  );
  const rejected = await fit.buildTranslationFitPlan(
    protectedPage,
    [block()],
    capacityMeasurer(),
    fitOptions([candidate], { pageCssSizeAt100: { 1: pageSize } }),
  );
  assert.equal(rejected.blocks[0].policy, "panel_only");
  assert.equal(rejected.blocks[0].reason, "protected_overlap");
});

test("canonical cover bbox is rechecked against neighboring English lines", async () => {
  const candidate = region({
    bbox: box(0.1, 0.1, 0.9, 0.3),
    line_boxes: [box(0.1, 0.2, 0.9, 0.225)],
    source_line_orders: [0],
  });
  const peer = region({
    region_id: "cover-edge-peer",
    block_index: 1,
    bbox: box(0.9005, 0.15, 0.94, 0.25),
    line_boxes: [box(0.9005, 0.18, 0.93, 0.205)],
    source_block_order: 1,
    source_line_orders: [1],
    confidence: 0.5,
    render_policy: "preserve",
    failure_reason: "low_confidence",
  });
  const result = await fit.buildTranslationFitPlan(
    layout([candidate, peer]),
    [block()],
    capacityMeasurer(),
    fitOptions([candidate], {
      pageCssSizeAt100: { 1: { widthPx: 480, heightPx: 640 } },
    }),
  );

  assert.equal(result.blocks[0].policy, "panel_only");
  assert.equal(result.blocks[0].reason, "invalid_geometry");
});

test("clips block 15's bleed-only peer overlap at the safe canonical pixel boundary", async () => {
  const pageSize = { widthPx: 960, heightPx: 1357.714 };
  const candidate = region({
    region_id: "b15-p2-r0-0f8a87f4ea87",
    bbox: box(0.507563025210084, 0.2294887039239001, 0.8873949579831932, 0.45659928656361476),
    line_boxes: [
      box(0.5294117647058824, 0.23067776456599287, 0.8840336134453781, 0.24494649227110582),
      box(0.5126050420168067, 0.4399524375743163, 0.7025210084033613, 0.4530321046373365),
    ],
    source_line_orders: [0, 1],
  });
  const peer = region({
    region_id: "b14-p2-r1-1cb1a28e139c",
    block_index: 1,
    bbox: box(0.5136272922140318, 0.08840822435235007, 0.8839933073061909, 0.22843839456461057),
    line_boxes: [
      box(0.5142824504935526, 0.2172730404209576, 0.8421303731378387, 0.22843839456461057),
    ],
    source_block_order: 1,
    source_line_orders: [0],
    confidence: 0.5,
    render_policy: "preserve",
    failure_reason: "low_confidence",
  });
  const fullCover = fit.deriveCanonicalCoverBox(candidate.bbox, pageSize);
  assert.ok(fullCover.y0 < peer.bbox.y1);
  assert.ok(candidate.bbox.y0 > peer.bbox.y1);

  const result = await fit.buildTranslationFitPlan(
    layout([candidate, peer]),
    [block()],
    capacityMeasurer(),
    fitOptions([candidate], { pageCssSizeAt100: { 1: pageSize } }),
  );

  assert.equal(result.blocks[0].policy, "replace");
  const expectedTop = Math.ceil(peer.bbox.y1 * pageSize.heightPx) / pageSize.heightPx;
  assert.equal(result.blocks[0].regions[0].bbox.y0, expectedTop);
  assert.ok(result.blocks[0].regions[0].bbox.y0 <= candidate.line_boxes[0].y0 - 2 / pageSize.heightPx);
  assert.equal(result.blocks[0].regions[0].bbox.x0, candidate.bbox.x0 - 2 / pageSize.widthPx);
  assert.equal(result.blocks[0].regions[0].bbox.y1, candidate.bbox.y1 + 2 / pageSize.heightPx);
});

test("invalid protected, line or source-order geometry fails closed", async () => {
  const candidate = region();
  const invalidProtected = region({
    region_id: "invalid-protected",
    block_index: 9,
    kind: "image",
    bbox: box(0.92, 0.1, 0.99, 0.2),
    line_boxes: [],
    source_line_orders: [],
    protected_boxes: [box(-0.1, 0.1, 0.1, 0.2)],
    render_policy: "preserve",
    failure_reason: "protected_content",
  });
  const protectedPlan = await fit.buildTranslationFitPlan(
    layout([candidate, invalidProtected]),
    [block()],
    capacityMeasurer(),
    fitOptions([candidate]),
  );
  assert.equal(protectedPlan.blocks[0].reason, "invalid_geometry");

  const invalidOrder = region({ source_line_orders: [1, 0] });
  const orderPlan = await fit.buildTranslationFitPlan(
    layout([invalidOrder]),
    [block()],
    capacityMeasurer(),
    fitOptions([invalidOrder]),
  );
  assert.equal(orderPlan.blocks[0].reason, "invalid_geometry");
});

test("immutable sequence distinguishes missing, duplicate, reorder, change and invalid KaTeX", () => {
  const original = "Use $x$ [3] then $y$.";
  assert.equal(fit.validateImmutableFragments(original, "使用 $x$ [3] 然后 $y$。"), null);
  assert.equal(fit.validateImmutableFragments(original, "使用 $x$ [3]。"), "immutable_missing");
  assert.equal(
    fit.validateImmutableFragments(original, "使用 $x$ $x$ [3] 然后 $y$。"),
    "immutable_duplicate",
  );
  assert.equal(
    fit.validateImmutableFragments(original, "使用 $x$ [3] 然后 $y$，另见 $z$。"),
    "immutable_changed",
  );
  assert.equal(
    fit.validateImmutableFragments(original, "使用 $y$ [3] 然后 $x$。"),
    "immutable_reordered",
  );
  assert.equal(fit.validateImmutableFragments(original, "使用 $z$ [3] 然后 $y$。"), "immutable_changed");
  assert.equal(
    fit.validateImmutableFragments("Use $\\badcommand{$.", "使用 $\\badcommand{$。"),
    "katex_invalid",
  );
  assert.deepEqual(
    fit.extractImmutableFragments("Funding rose from $5 million to $10 million."),
    [],
  );
  assert.deepEqual(
    fit.extractImmutableFragments("The budget was $5 million; objective $L$ stayed fixed.")
      .map((item) => item.value),
    ["$L$"],
  );
  assert.deepEqual(
    fit.extractImmutableFragments("Budget $5million to $10million while $x$ stayed fixed.")
      .map((item) => item.value),
    ["$x$"],
  );
});

test("valid math still downgrades when its protected geometry is unavailable", async () => {
  const candidate = region();
  const result = await fit.buildTranslationFitPlan(
    layout([candidate]),
    [block({ original: "Loss $L(x)$.", translation: "损失 $L(x)$。" })],
    capacityMeasurer(),
    fitOptions([candidate]),
  );
  assert.equal(result.blocks[0].reason, "protected_geometry_missing");
  assert.deepEqual(result.blocks[0].regions, []);
});

test("uses the 9px floor and 72 percent floor with deterministic font ticks", async () => {
  const small = region({ line_boxes: [box(0.1, 0.1, 0.9, 0.11)], source_line_orders: [0] });
  const atNine = await fit.buildTranslationFitPlan(
    layout([small]),
    [block({ original: "small", translation: "小" })],
    capacityMeasurer(9),
    fitOptions([small]),
  );
  assert.equal(atNine.blocks[0].policy, "replace");
  assert.equal(atNine.blocks[0].regions[0].fontPx100, 9);

  const large = region({ line_boxes: [box(0.1, 0.1, 0.9, 0.125)], source_line_orders: [0] });
  const atTwelve = await fit.buildTranslationFitPlan(
    layout([large]),
    [block({ original: "large", translation: "大" })],
    capacityMeasurer(12),
    fitOptions([large]),
  );
  assert.equal(atTwelve.blocks[0].sourceFontPx100, 16);
  assert.equal(atTwelve.blocks[0].regions[0].fontPx100, 12);
  assert.ok(atTwelve.blocks[0].regions[0].fontPx100 >= 16 * 0.72);

  const nonIdentityScale = await fit.buildTranslationFitPlan(
    layout([large]),
    [block({ original: "scaled", translation: "缩放" })],
    capacityMeasurer(12),
    fitOptions([large], { pageCssSizeAt100: { 1: { widthPx: 375, heightPx: 500 } } }),
  );
  assert.equal(nonIdentityScale.blocks[0].sourceFontPx100, 12.5);
  assert.ok(nonIdentityScale.blocks[0].regions[0].fontPx100 >= 12.5 * 0.72);

  const mixedLineHeights = [
    large,
    region({
      region_id: "larger-second-region",
      flow_order: 1,
      bbox: box(0.1, 0.4, 0.9, 0.6),
      line_boxes: [box(0.1, 0.4, 0.9, 0.44)],
      source_line_orders: [1],
    }),
  ];
  const mixed = await fit.buildTranslationFitPlan(
    layout(mixedLineHeights),
    [block({ original: "mixed", translation: "混合字号" })],
    capacityMeasurer(30),
    fitOptions(mixedLineHeights),
  );
  assert.equal(mixed.blocks[0].sourceFontPx100, 25.6);
  assert.equal(mixed.blocks[0].regions[0].fontPx100, 16);
});

test("estimates a usable source font when one line box aggregates a long paragraph", async () => {
  const bounds = box(
    0.16993464052287582,
    0.33585858585858586,
    0.826797385620915,
    0.4621212121212121,
  );
  const aggregate = region({
    region_id: "aggregate-mineru-paragraph",
    kind: "text",
    bbox: bounds,
    line_boxes: [bounds],
    source_block_order: null,
    source_line_orders: [],
    geometry_source: "mineru_middle",
  });
  const hybrid = layout(
    [aggregate],
    [{ page: 1, width: 612, height: 792, rotation: 0 }],
  );
  hybrid.adapter = "hybrid_poppler_mineru";
  hybrid.adapter_version = "13";
  const original = (
    "Large datasets improve model performance, but redundant samples quickly reduce the marginal benefit of adding more data. "
  ).repeat(8).trim();
  const translation = (
    "大规模数据集能够改善模型性能，但重复样本会迅速降低继续增加数据带来的边际收益。"
  ).repeat(6);
  const pageSize = { widthPx: 960, heightPx: (960 * 792) / 612 };

  const result = await fit.buildTranslationFitPlan(
    hybrid,
    [block({ original, translation })],
    capacityMeasurer(),
    fitOptions([aggregate], { pageCssSizeAt100: { 1: pageSize } }),
  );

  assert.equal(result.blocks[0].policy, "replace");
  assert.ok(result.blocks[0].sourceFontPx100 > fit.MIN_FONT_PX_AT_100);
  assert.ok(result.blocks[0].sourceFontPx100 < 30);
  assert.ok(
    result.blocks[0].regions[0].fontPx100 >=
      Math.max(
        result.blocks[0].sourceFontPx100 * fit.MIN_SOURCE_FONT_RATIO,
        fit.MIN_FONT_PX_AT_100,
      ),
  );
});

test("precise Poppler word boxes override a coarse aggregate line box", async () => {
  const bounds = box(0.1, 0.1, 0.9, 0.3);
  const precise = region({
    bbox: bounds,
    line_boxes: [bounds],
    word_boxes: [
      box(0.1, 0.11, 0.2, 0.14),
      box(0.22, 0.11, 0.34, 0.14),
      box(0.36, 0.11, 0.48, 0.14),
    ],
    source_line_orders: [0],
    source_word_orders: [0, 1, 2],
    geometry_source: "poppler_bbox_layout",
  });
  const original = (
    "A long paragraph must use precise word geometry instead of treating a coarse block box as its source font. "
  ).repeat(6).trim();

  const result = await fit.buildTranslationFitPlan(
    layout([precise]),
    [block({ original, translation: "精确单词框必须优先决定原文字号。" })],
    capacityMeasurer(),
    fitOptions([precise]),
  );

  assert.equal(result.blocks[0].policy, "replace");
  assert.equal(result.blocks[0].sourceFontPx100, 19.2);
  assert.ok(
    result.blocks[0].regions[0].fontPx100 >=
      result.blocks[0].sourceFontPx100 * fit.MIN_SOURCE_FONT_RATIO,
  );
});

test("keeps the measured median for reliable multi-line boxes", async () => {
  const measured = region({
    bbox: box(0.1, 0.1, 0.9, 0.24),
    line_boxes: [
      box(0.1, 0.11, 0.9, 0.13),
      box(0.1, 0.15, 0.9, 0.175),
      box(0.1, 0.2, 0.9, 0.23),
    ],
    source_line_orders: [0, 1, 2],
  });
  const original = (
    "A long source paragraph must not change the measured font when real line boxes are available. "
  ).repeat(8).trim();

  const result = await fit.buildTranslationFitPlan(
    layout([measured]),
    [block({ original, translation: "真实多行框继续使用原有中位数字号。" })],
    capacityMeasurer(),
    fitOptions([measured]),
  );

  assert.equal(result.blocks[0].policy, "replace");
  assert.equal(result.blocks[0].sourceFontPx100, 16);
});

test("uses each region's source font with one shared scale", async () => {
  const regions = [
    region({
      bbox: box(0.1, 0.1, 0.5, 0.25),
      line_boxes: [box(0.1, 0.1, 0.5, 0.125)],
      source_line_orders: [0],
    }),
    region({
      region_id: "large-second",
      flow_order: 1,
      bbox: box(0.1, 0.4, 0.9, 0.6),
      line_boxes: [box(0.1, 0.4, 0.9, 0.44)],
      source_line_orders: [1],
    }),
  ];
  const splitCapacity = (smallLimit = Number.POSITIVE_INFINITY, largeLimit = Number.POSITIVE_INFINITY) => {
    const capacity = (input) => {
      const isSmallRegion = input.widthPx100 < 250;
      const limit = isSmallRegion ? smallLimit : largeLimit;
      if (input.fontPx100 > limit + 1e-9) return 0;
      return Math.min(input.tokens.length, isSmallRegion ? 2 : 4);
    };
    return {
      maxFittingPrefix: capacity,
      verify(input) {
        return capacity(input) >= input.tokens.length;
      },
    };
  };
  const translated = "甲乙丙丁戊己";
  const sourceSizes = [16, 25.6];

  const fullSize = await fit.buildTranslationFitPlan(
    layout(regions),
    [block({ original: "abcdef", translation: translated })],
    splitCapacity(),
    fitOptions(regions),
  );
  assert.equal(fullSize.blocks[0].policy, "replace");
  assert.deepEqual(fullSize.blocks[0].regions.map((item) => item.fontPx100), sourceSizes);

  const minimumSizes = sourceSizes.map((size) => size * fit.MIN_SOURCE_FONT_RATIO);
  const atMinimum = await fit.buildTranslationFitPlan(
    layout(regions),
    [block({ original: "abcdef", translation: translated })],
    splitCapacity(...minimumSizes),
    fitOptions(regions),
  );
  assert.equal(atMinimum.blocks[0].policy, "replace");
  atMinimum.blocks[0].regions.forEach((item, index) => {
    assert.ok(Math.abs(item.fontPx100 - minimumSizes[index]) < 1e-9);
    assert.ok(item.fontPx100 >= Math.max(sourceSizes[index] * 0.72, 9));
  });
  assert.ok(
    Math.abs(
      atMinimum.blocks[0].regions[0].fontPx100 / sourceSizes[0] -
      atMinimum.blocks[0].regions[1].fontPx100 / sourceSizes[1],
    ) < 1e-9,
  );

  const repeated = await fit.buildTranslationFitPlan(
    layout(regions),
    [block({ original: "abcdef", translation: translated })],
    splitCapacity(...minimumSizes),
    fitOptions(regions),
  );
  assert.deepEqual(repeated, atMinimum);
});

test("compacts only an exact redundant source suffix from headings", () => {
  assert.equal(
    fit.getInlineTranslationText(block({
      type: "heading",
      original: "5.1 Training Data and Batching",
      translation: "5.1 训练数据与批处理 (Training Data and Batching)",
    })),
    "5.1 训练数据与批处理",
  );
  assert.equal(
    fit.getInlineTranslationText(block({
      type: "heading",
      original: "Attention",
      translation: "注意力（核心机制）",
    })),
    "注意力（核心机制）",
  );
  assert.equal(
    fit.getInlineTranslationText(block({
      type: "paragraph",
      original: "Attention",
      translation: "注意力 (Attention)",
    })),
    "注意力 (Attention)",
  );
  assert.equal(
    fit.getInlineTranslationText(block({
      type: "heading",
      original: "5.1 Training Data",
      translation: "训练数据 (5.1 Training Data)",
    })),
    "训练数据 (5.1 Training Data)",
  );
  assert.equal(
    fit.getInlineTranslationText(block({
      type: "heading",
      original: "Attention [3]",
      translation: "注意力 (Attention [3])",
    })),
    "注意力 (Attention [3])",
  );
});

test("uses a tighter line height only for PDF heading boxes", async () => {
  const candidate = region();
  const heading = await fit.buildTranslationFitPlan(
    layout([candidate]),
    [block({ type: "heading", original: "Abstract", translation: "摘要" })],
    capacityMeasurer(),
    fitOptions([candidate]),
  );
  const paragraph = await fit.buildTranslationFitPlan(
    layout([candidate]),
    [block({ type: "paragraph", original: "Abstract", translation: "摘要" })],
    capacityMeasurer(),
    fitOptions([candidate]),
  );

  assert.equal(
    heading.blocks[0].regions[0].lineHeightPx100,
    heading.blocks[0].regions[0].fontPx100 * 1.15,
  );
  assert.equal(
    paragraph.blocks[0].regions[0].lineHeightPx100,
    paragraph.blocks[0].regions[0].fontPx100 * 1.25,
  );
});

test("overflow is atomic even after an earlier region received text", async () => {
  const regions = [
    region({ bbox: box(0.1, 0.1, 0.9, 0.3) }),
    region({ region_id: "second", flow_order: 1, bbox: box(0.1, 0.4, 0.2, 0.5) }),
  ];
  const failsOnNarrowSecondRegion = {
    maxFittingPrefix(input) {
      return input.widthPx100 > 200 ? Math.min(2, input.tokens.length) : 0;
    },
    verify(input) {
      return input.widthPx100 > 200 && input.tokens.length <= 2;
    },
  };
  const result = await fit.buildTranslationFitPlan(
    layout(regions),
    [block()],
    failsOnNarrowSecondRegion,
    fitOptions(regions),
  );
  assert.equal(result.blocks[0].reason, "overflow");
  assert.deepEqual(result.blocks[0].regions, []);
});

test("does not skip an empty leading region to place text later", async () => {
  const regions = [
    region({ bbox: box(0.1, 0.1, 0.2, 0.2) }),
    region({ region_id: "wide-second", flow_order: 1, bbox: box(0.1, 0.3, 0.9, 0.5) }),
  ];
  const skipsNarrowFirstRegion = {
    maxFittingPrefix(input) {
      return input.widthPx100 < 200 ? 0 : input.tokens.length;
    },
    verify() {
      return true;
    },
  };
  const result = await fit.buildTranslationFitPlan(
    layout(regions),
    [block()],
    skipsNarrowFirstRegion,
    fitOptions(regions),
  );
  assert.equal(result.blocks[0].reason, "overflow");
  assert.deepEqual(result.blocks[0].regions, []);
});

test("omits empty trailing regions so the renderer never blanks untouched English", async () => {
  const regions = [
    region({ bbox: box(0.1, 0.1, 0.9, 0.3) }),
    region({ region_id: "unused-second", flow_order: 1, bbox: box(0.1, 0.4, 0.9, 0.6) }),
  ];
  const result = await fit.buildTranslationFitPlan(
    layout(regions),
    [block({ original: "short", translation: "短译文" })],
    capacityMeasurer(),
    fitOptions(regions),
  );
  assert.equal(result.blocks[0].policy, "replace");
  assert.deepEqual(result.blocks[0].regions.map((item) => item.regionId), ["b0-p1-r0-test"]);
  assert.equal(result.blocks[0].regions[0].text, "短译文");
});

test("tables and non-text blocks remain preserved", async () => {
  const table = await fit.buildTranslationFitPlan(
    layout(),
    [block({ type: "table", status: "skip", translation: null })],
    capacityMeasurer(),
  );
  assert.equal(table.blocks[0].policy, "preserve");
  assert.equal(table.blocks[0].reason, "table_structure_unavailable");

  const formula = await fit.buildTranslationFitPlan(
    layout(),
    [block({ type: "formula", status: "skip", translation: null })],
    capacityMeasurer(),
  );
  assert.equal(formula.blocks[0].policy, "preserve");
  assert.equal(formula.blocks[0].reason, "non_text_content");
});

test("background sampler accepts solid pixels and rejects gradients or transparency", () => {
  const solid = new Uint8ClampedArray([255, 255, 255, 255, 252, 252, 252, 255]);
  const gradient = new Uint8ClampedArray([0, 0, 0, 255, 255, 255, 255, 255]);
  const transparent = new Uint8ClampedArray([255, 255, 255, 120]);
  assert.equal(fit.isUniformBackground(solid), true);
  assert.equal(fit.isUniformBackground(gradient), false);
  assert.equal(fit.isUniformBackground(transparent), false);
});

test("same inputs produce the same complete plan", async () => {
  const candidate = region();
  const options = fitOptions([candidate]);
  const first = await fit.buildTranslationFitPlan(layout([candidate]), [block()], capacityMeasurer(), options);
  const second = await fit.buildTranslationFitPlan(layout([candidate]), [block()], capacityMeasurer(), options);
  assert.deepEqual(first, second);
});
