import katex from "katex";

import type {
  Block,
  NormalizedPdfBox,
  TranslationLayout,
  TranslationLayoutPage,
  TranslationLayoutRegion,
} from "./api";

export const TRANSLATION_FIT_VERSION = 1;
export const INLINE_REPLACE_CONFIDENCE = 0.9;
export const MIN_SOURCE_FONT_RATIO = 0.72;
export const MIN_FONT_PX_AT_100 = 9;
export const DEFAULT_FONT_STEP_PX = 0.125;
export const CANONICAL_COVER_BLEED_PX = 2;

const AGGREGATE_LINE_BOX_EDGE_TOLERANCE_PX = 1;
const AGGREGATE_SOURCE_LINE_HEIGHT_RATIO = 1.2;
const MIN_AGGREGATE_WRAPPED_LINES = 3;

export type TranslationFitReason =
  | "translation_not_done"
  | "translation_missing"
  | "layout_unmapped"
  | "layout_not_precise"
  | "non_text_content"
  | "low_confidence"
  | "invalid_geometry"
  | "invalid_flow_order"
  | "unsupported_rotation"
  | "protected_overlap"
  | "cross_block_overlap"
  | "protected_geometry_missing"
  | "background_complex"
  | "background_unverified"
  | "page_metrics_unavailable"
  | "font_metrics_unavailable"
  | "immutable_missing"
  | "immutable_duplicate"
  | "immutable_reordered"
  | "immutable_changed"
  | "katex_invalid"
  | "table_structure_unavailable"
  | "overflow";

export type FitToken = {
  kind: "text" | "space" | "immutable";
  value: string;
};

export type TextMeasureInput = {
  tokens: readonly FitToken[];
  widthPx100: number;
  heightPx100: number;
  fontPx100: number;
  lineHeightPx100: number;
};

export interface TranslationTextMeasurer {
  maxFittingPrefix(input: TextMeasureInput): number | Promise<number>;
  verify(input: TextMeasureInput): boolean | Promise<boolean>;
}

export type RegionBackgroundEvidence = "uniform" | "complex" | "unknown";

export type TranslationPageCssSize = {
  widthPx: number;
  heightPx: number;
};

export type TranslationFitOptions = {
  backgroundByRegion?: Readonly<Record<string, RegionBackgroundEvidence>>;
  pageCssSizeAt100?: Readonly<Record<number, TranslationPageCssSize>>;
  fontStepPx?: number;
  validateMath?: (latex: string) => boolean;
};

export type FittedTranslationRegion = {
  regionId: string;
  blockIndex: number;
  page: number;
  flowOrder: number;
  bbox: NormalizedPdfBox;
  rotation: 0;
  text: string;
  fontPx100: number;
  lineHeightPx100: number;
};

export type FittedTranslationBlock = {
  blockIndex: number;
  policy: "replace" | "preserve" | "panel_only";
  reason: TranslationFitReason | null;
  sourceFontPx100: number | null;
  regions: readonly FittedTranslationRegion[];
};

export type TranslationFitPlan = {
  version: typeof TRANSLATION_FIT_VERSION;
  layoutCacheKey: string;
  blocks: readonly FittedTranslationBlock[];
};

export function containsProtectedMath(text: string): boolean {
  return extractImmutableFragments(text).some((fragment) => fragment.kind === "math");
}

export function getInlineTranslationText(block: Block): string {
  const translation = block.translation?.trim() ?? "";
  if (block.type !== "heading" || !translation) return translation;
  if (extractImmutableFragments(block.original).length > 0) return translation;
  const match = translation.match(/\s*[（(]([^()（）]+)[）)]\s*$/u);
  if (!match || match.index === undefined) return translation;
  const source = normalizeHeadingComparison(block.original);
  const sourceWithoutNumber = normalizeHeadingComparison(
    block.original.replace(/^\s*\d+(?:\.\d+)*\.?\s+/u, ""),
  );
  const repeated = normalizeHeadingComparison(match[1]);
  if (repeated !== source && repeated !== sourceWithoutNumber) return translation;
  const compacted = translation.slice(0, match.index).trimEnd();
  const sectionNumber = block.original.match(/^\s*(\d+(?:\.\d+)*\.?)\s+/u)?.[1];
  if (
    sectionNumber &&
    !new RegExp(`^\\s*${escapeRegExp(sectionNumber)}(?:\\s|$)`, "u").test(compacted)
  ) {
    return translation;
  }
  return compacted || translation;
}

type ImmutableFragment = {
  kind: "math" | "citation" | "literal";
  value: string;
  start: number;
  end: number;
};

type RegionFrame = {
  region: TranslationLayoutRegion;
  coverBbox: NormalizedPdfBox;
  widthPx100: number;
  heightPx100: number;
  sourceFontPx100: number;
};

type LayoutAttempt = {
  fitted: boolean;
  regions: FittedTranslationRegion[];
};

type LayoutGeometryAudit = {
  protectedByPage: ReadonlyMap<number, readonly NormalizedPdfBox[]>;
  textLinesByPage: ReadonlyMap<number, readonly TextLineBox[]>;
  unsafePages: ReadonlySet<number>;
  layoutUnsafe: boolean;
};

type TextLineBox = {
  blockIndex: number;
  box: NormalizedPdfBox;
};

const TEXT_REGION_KINDS = new Set([
  "heading",
  "paragraph",
  "text",
  "title",
  "list",
  "page_footnote",
  "image_caption",
  "image_footnote",
  "chart_caption",
  "chart_footnote",
  "table_caption",
  "table_footnote",
  "code_caption",
  "code_footnote",
]);

const PRECISE_LAYOUT_ADAPTERS = new Set([
  "poppler_bbox_layout",
  "mineru_middle",
  "hybrid_poppler_mineru",
]);

const FRAGMENT_PATTERNS: readonly [ImmutableFragment["kind"], RegExp][] = [
  ["literal", /⟦PET_IMMUTABLE_[^⟧\n]+⟧/g],
  [
    "math",
    /\\begin\{(equation\*?|align\*?|aligned|gather\*?|multline\*?)\}[\s\S]*?\\end\{\1\}/g,
  ],
  ["math", /\$\$[\s\S]+?\$\$/g],
  ["math", /\\\[[\s\S]+?\\\]/g],
  ["math", /\\\([\s\S]+?\\\)/g],
  [
    "citation",
    /\\(?:cite|citep|citet|citealp|citeauthor|ref|eqref|autoref)\*?(?:\[[^\]\n]*\])*\{[^{}\n]+\}/g,
  ],
  ["citation", /\[(?:\d+(?:\s*[-–—,;]\s*\d+)*)\]/g],
];

export async function buildTranslationFitPlan(
  layout: TranslationLayout,
  blocks: readonly Block[],
  measurer: TranslationTextMeasurer,
  options: TranslationFitOptions = {},
): Promise<TranslationFitPlan> {
  const regionsByBlock = new Map<number, TranslationLayoutRegion[]>();
  const regionsByPage = new Map<number, TranslationLayoutRegion[]>();
  for (const region of layout.regions) {
    const group = regionsByBlock.get(region.block_index) ?? [];
    group.push(region);
    regionsByBlock.set(region.block_index, group);
    const pageGroup = regionsByPage.get(region.page) ?? [];
    pageGroup.push(region);
    regionsByPage.set(region.page, pageGroup);
  }
  const pages = new Map(layout.pages.map((page) => [page.page, page]));
  const geometry = auditLayoutGeometry(layout.regions, pages);
  const results: FittedTranslationBlock[] = [];

  for (const block of blocks) {
    const blockRegions = regionsByBlock.get(block.index) ?? [];
    results.push(
      await fitTranslationBlock(
        block,
        blockRegions,
        regionsByPage,
        pages,
        geometry,
        PRECISE_LAYOUT_ADAPTERS.has(layout.adapter),
        measurer,
        options,
      ),
    );
  }

  return {
    version: TRANSLATION_FIT_VERSION,
    layoutCacheKey: layout.cache_key,
    blocks: results,
  };
}

async function fitTranslationBlock(
  block: Block,
  rawRegions: readonly TranslationLayoutRegion[],
  regionsByPage: ReadonlyMap<number, readonly TranslationLayoutRegion[]>,
  pages: ReadonlyMap<number, TranslationLayoutPage>,
  geometry: LayoutGeometryAudit,
  preciseAdapter: boolean,
  measurer: TranslationTextMeasurer,
  options: TranslationFitOptions,
): Promise<FittedTranslationBlock> {
  if (block.type === "table") {
    return failedBlock(block.index, "preserve", "table_structure_unavailable");
  }
  if (!TEXT_REGION_KINDS.has(block.type)) {
    return failedBlock(block.index, "preserve", "non_text_content");
  }
  if (block.status !== "done") {
    return failedBlock(block.index, "panel_only", "translation_not_done");
  }
  const translation = getInlineTranslationText(block);
  if (!translation) {
    return failedBlock(block.index, "panel_only", "translation_missing");
  }
  if (rawRegions.length === 0) {
    return failedBlock(block.index, "panel_only", "layout_unmapped");
  }
  if (!preciseAdapter) {
    return failedBlock(block.index, "panel_only", "layout_not_precise");
  }
  if (geometry.layoutUnsafe) {
    return failedBlock(block.index, "panel_only", "invalid_geometry");
  }

  const regions = [...rawRegions].sort((left, right) => left.flow_order - right.flow_order);
  const frames: RegionFrame[] = [];
  for (let index = 0; index < regions.length; index += 1) {
    const region = regions[index];
    if (region.flow_order !== index || (index > 0 && region.page < regions[index - 1].page)) {
      return failedBlock(block.index, "panel_only", "invalid_flow_order");
    }
    const page = pages.get(region.page);
    if (!page || geometry.unsafePages.has(region.page) || !validRegionGeometry(region)) {
      return failedBlock(block.index, "panel_only", "invalid_geometry");
    }
    if (region.rotation !== 0 || page.rotation !== 0) {
      return failedBlock(block.index, "panel_only", "unsupported_rotation");
    }
    if (
      !Number.isFinite(region.confidence) ||
      region.confidence < INLINE_REPLACE_CONFIDENCE ||
      region.confidence > 1
    ) {
      return failedBlock(block.index, "panel_only", "low_confidence");
    }
    if (region.render_policy !== "replace" || region.failure_reason !== null) {
      return failedBlock(block.index, "panel_only", "layout_not_precise");
    }
    if (!TEXT_REGION_KINDS.has(region.kind)) {
      return failedBlock(block.index, "preserve", "non_text_content");
    }
    const pageCssSize = options.pageCssSizeAt100?.[region.page];
    if (!validPageCssSize(pageCssSize)) {
      return failedBlock(block.index, "panel_only", "page_metrics_unavailable");
    }
    const coverBbox = deriveTranslationRegionCoverBox(
      region,
      pageCssSize,
      regionsByPage.get(region.page) ?? [],
    );
    if (!coverBbox) {
      return failedBlock(block.index, "panel_only", "invalid_geometry");
    }
    const widthPx100 = (coverBbox.x1 - coverBbox.x0) * pageCssSize.widthPx;
    const heightPx100 = (coverBbox.y1 - coverBbox.y0) * pageCssSize.heightPx;
    if (!Number.isFinite(widthPx100) || !Number.isFinite(heightPx100) || widthPx100 <= 0 || heightPx100 <= 0) {
      return failedBlock(block.index, "panel_only", "invalid_geometry");
    }
    // The full source text is an unambiguous geometry signal only when this
    // block owns one region; multi-region text cannot be safely apportioned.
    const sourceFontPx100 = estimateRegionSourceFontPx100(
      region,
      regions.length === 1 ? block.original : null,
      pageCssSize,
    );
    if (sourceFontPx100 === null) {
      return failedBlock(block.index, "panel_only", "font_metrics_unavailable");
    }
    frames.push({
      region,
      coverBbox,
      widthPx100,
      heightPx100,
      sourceFontPx100,
    });
  }

  const immutableReason = validateImmutableFragments(
    block.original,
    translation,
    options.validateMath ?? validateKatexMath,
  );
  if (immutableReason) {
    return failedBlock(block.index, "panel_only", immutableReason);
  }
  if (extractImmutableFragments(block.original).some((item) => item.kind === "math")) {
    return failedBlock(block.index, "panel_only", "protected_geometry_missing");
  }

  for (const frame of frames) {
    const protectedBoxes = geometry.protectedByPage.get(frame.region.page) ?? [];
    if (protectedBoxes.some((box) => positiveAreaIntersection(frame.coverBbox, box))) {
      return failedBlock(block.index, "panel_only", "protected_overlap");
    }
    const competingLines = geometry.textLinesByPage.get(frame.region.page) ?? [];
    if (
      competingLines.some(
        (line) =>
          line.blockIndex !== block.index &&
          positiveAreaIntersection(frame.coverBbox, line.box),
      )
    ) {
      return failedBlock(block.index, "panel_only", "cross_block_overlap");
    }
    const background = options.backgroundByRegion?.[frame.region.region_id] ?? "unknown";
    if (background === "complex") {
      return failedBlock(block.index, "panel_only", "background_complex");
    }
    if (background !== "uniform") {
      return failedBlock(block.index, "panel_only", "background_unverified");
    }
  }

  // Keep the block-level maximum for API compatibility and diagnostics. Each
  // region is typeset from its own source line height below; using this maximum
  // for every region would make a large heading/caption region overflow an
  // otherwise normal paragraph region in the same block.
  const sourceFontPx100 = Math.max(...frames.map((frame) => frame.sourceFontPx100));

  const tokens = tokenizeTranslation(translation);
  const step = validFontStep(options.fontStepPx) ? options.fontStepPx! : DEFAULT_FONT_STEP_PX;
  const scales = buildFontScales(sourceFontPx100, step);

  const minimumAttempt = await attemptLayout(
    block.index,
    frames,
    tokens,
    scales[0],
    block.type === "heading" ? 1.15 : 1.25,
    measurer,
  );
  if (!minimumAttempt.fitted) {
    return failedBlock(block.index, "panel_only", "overflow", sourceFontPx100);
  }

  let lower = 0;
  let upper = scales.length - 1;
  let best = minimumAttempt;
  while (lower <= upper) {
    const middle = Math.floor((lower + upper) / 2);
    const attempt = await attemptLayout(
      block.index,
      frames,
      tokens,
      scales[middle],
      block.type === "heading" ? 1.15 : 1.25,
      measurer,
    );
    if (attempt.fitted) {
      best = attempt;
      lower = middle + 1;
    } else {
      upper = middle - 1;
    }
  }

  return {
    blockIndex: block.index,
    policy: "replace",
    reason: null,
    sourceFontPx100,
    regions: best.regions,
  };
}

async function attemptLayout(
  blockIndex: number,
  frames: readonly RegionFrame[],
  tokens: readonly FitToken[],
  fontScale: number,
  lineHeightRatio: number,
  measurer: TranslationTextMeasurer,
): Promise<LayoutAttempt> {
  const fitted: FittedTranslationRegion[] = [];
  let cursor = 0;

  for (const frame of frames) {
    const fontPx100 = Math.max(
      frame.sourceFontPx100 * fontScale,
      MIN_FONT_PX_AT_100,
    );
    const lineHeightPx100 = fontPx100 * lineHeightRatio;
    const remaining = tokens.slice(cursor);
    const input: TextMeasureInput = {
      tokens: remaining,
      widthPx100: Math.max(1, frame.widthPx100 - 2),
      heightPx100: Math.max(1, frame.heightPx100 - 1),
      fontPx100,
      lineHeightPx100,
    };
    const rawCount = remaining.length === 0 ? 0 : await measurer.maxFittingPrefix(input);
    const count = Math.max(0, Math.min(remaining.length, Math.floor(rawCount)));
    // A block may leave only a trailing suffix of regions unused. Skipping a
    // leading or middle region would show English and Chinese interleaved for
    // one logical block, which violates the atomic fallback contract.
    if (remaining.length > 0 && count === 0) {
      return { fitted: false, regions: [] };
    }
    const chunkTokens = remaining.slice(0, count);
    if (chunkTokens.length > 0) {
      const verified = await measurer.verify({ ...input, tokens: chunkTokens });
      if (!verified) return { fitted: false, regions: [] };
    }
    // Do not return an empty fitted region. The renderer treats every fitted
    // region as an opaque replacement surface, so an empty trailing region
    // would hide valid English even though no Chinese text belongs there.
    if (chunkTokens.length > 0) {
      fitted.push({
        regionId: frame.region.region_id,
        blockIndex,
        page: frame.region.page,
        flowOrder: frame.region.flow_order,
        bbox: frame.coverBbox,
        rotation: 0,
        text: joinTokens(chunkTokens),
        fontPx100,
        lineHeightPx100,
      });
    }
    cursor += count;
  }

  if (cursor !== tokens.length) return { fitted: false, regions: [] };
  return { fitted: true, regions: fitted };
}

export function extractImmutableFragments(text: string): readonly ImmutableFragment[] {
  const candidates: ImmutableFragment[] = [];
  for (const [kind, pattern] of FRAGMENT_PATTERNS) {
    pattern.lastIndex = 0;
    for (const match of text.matchAll(pattern)) {
      const start = match.index;
      if (start === undefined) continue;
      candidates.push({ kind, value: match[0], start, end: start + match[0].length });
    }
  }
  candidates.push(...extractInlineDollarMath(text));
  candidates.sort((left, right) => left.start - right.start || right.end - right.start - (left.end - left.start));
  const fragments: ImmutableFragment[] = [];
  let occupiedUntil = -1;
  for (const candidate of candidates) {
    if (candidate.start < occupiedUntil) continue;
    fragments.push(candidate);
    occupiedUntil = candidate.end;
  }
  return fragments;
}

export function validateImmutableFragments(
  original: string,
  translation: string,
  validateMath: (latex: string) => boolean = validateKatexMath,
): TranslationFitReason | null {
  const source = extractImmutableFragments(original);
  const target = extractImmutableFragments(translation);
  const sourceValues = source.map((item) => `${item.kind}:${item.value}`);
  const targetValues = target.map((item) => `${item.kind}:${item.value}`);

  if (targetValues.length < sourceValues.length) return "immutable_missing";
  if (targetValues.length > sourceValues.length) {
    const sourceCounts = countValues(sourceValues);
    const targetCounts = countValues(targetValues);
    const duplicated = [...targetCounts].some(
      ([value, count]) => {
        const sourceCount = sourceCounts.get(value) ?? 0;
        return sourceCount > 0 && count > sourceCount;
      },
    );
    return duplicated ? "immutable_duplicate" : "immutable_changed";
  }
  if (sourceValues.some((value, index) => targetValues[index] !== value)) {
    const sourceSorted = [...sourceValues].sort();
    const targetSorted = [...targetValues].sort();
    if (sourceSorted.every((value, index) => targetSorted[index] === value)) {
      return "immutable_reordered";
    }
    return "immutable_changed";
  }
  if (source.some((item) => item.kind === "math" && !validateMath(item.value))) {
    return "katex_invalid";
  }
  return null;
}

export function validateKatexMath(value: string): boolean {
  try {
    const latex = stripMathDelimiters(value).trim();
    if (!latex) return false;
    katex.renderToString(latex, {
      throwOnError: true,
      strict: "error",
      trust: false,
      output: "html",
    });
    return true;
  } catch {
    return false;
  }
}

export function tokenizeTranslation(text: string): readonly FitToken[] {
  const fragments = extractImmutableFragments(text);
  const tokens: FitToken[] = [];
  let cursor = 0;
  for (const fragment of fragments) {
    tokens.push(...tokenizePlainText(text.slice(cursor, fragment.start)));
    tokens.push({ kind: "immutable", value: fragment.value });
    cursor = fragment.end;
  }
  tokens.push(...tokenizePlainText(text.slice(cursor)));
  return tokens;
}

export function joinTokens(tokens: readonly FitToken[]): string {
  return tokens.map((token) => token.value).join("");
}

export function isUniformBackground(
  rgba: Uint8ClampedArray,
  options: { maxRange?: number; maxStandardDeviation?: number } = {},
): boolean {
  if (rgba.length === 0 || rgba.length % 4 !== 0) return false;
  let count = 0;
  let mean = 0;
  let squaredDifferenceSum = 0;
  let minimum = Number.POSITIVE_INFINITY;
  let maximum = Number.NEGATIVE_INFINITY;
  for (let index = 0; index < rgba.length; index += 4) {
    if (rgba[index + 3] < 250) return false;
    const luminance =
      rgba[index] * 0.2126 + rgba[index + 1] * 0.7152 + rgba[index + 2] * 0.0722;
    count += 1;
    const delta = luminance - mean;
    mean += delta / count;
    squaredDifferenceSum += delta * (luminance - mean);
    minimum = Math.min(minimum, luminance);
    maximum = Math.max(maximum, luminance);
  }
  const variance = squaredDifferenceSum / count;
  const range = maximum - minimum;
  return (
    range <= (options.maxRange ?? 18) &&
    Math.sqrt(variance) <= (options.maxStandardDeviation ?? 6)
  );
}

function auditLayoutGeometry(
  regions: readonly TranslationLayoutRegion[],
  pages: ReadonlyMap<number, TranslationLayoutPage>,
): LayoutGeometryAudit {
  const boxes = new Map<number, NormalizedPdfBox[]>();
  const textLines = new Map<number, TextLineBox[]>();
  const unsafePages = new Set<number>();
  let layoutUnsafe = false;
  for (const [pageNumber, page] of pages) {
    const pageBoxes: NormalizedPdfBox[] = [];
    for (const box of page.protected_boxes ?? []) {
      if (!validBox(box)) {
        unsafePages.add(pageNumber);
        continue;
      }
      pageBoxes.push(box);
    }
    boxes.set(pageNumber, pageBoxes);
  }
  for (const region of regions) {
    if (!pages.has(region.page)) {
      layoutUnsafe = true;
      continue;
    }
    const pageBoxes = boxes.get(region.page) ?? [];
    for (const box of region.protected_boxes ?? []) {
      if (!validBox(box)) {
        unsafePages.add(region.page);
        continue;
      }
      pageBoxes.push(box);
    }
    boxes.set(region.page, pageBoxes);
    if (!TEXT_REGION_KINDS.has(region.kind)) continue;
    if (!validBox(region.bbox)) {
      unsafePages.add(region.page);
    } else if (!hasValidTextLineGeometry(region)) {
      const pageLines = textLines.get(region.page) ?? [];
      pageLines.push({
        blockIndex: region.block_index,
        box: region.bbox,
      });
      textLines.set(region.page, pageLines);
    } else if (!validRegionGeometry(region)) {
      unsafePages.add(region.page);
    } else {
      const pageLines = textLines.get(region.page) ?? [];
      for (const lineBox of region.line_boxes) {
        pageLines.push({
          blockIndex: region.block_index,
          box: lineBox,
        });
      }
      textLines.set(region.page, pageLines);
    }
  }
  return {
    protectedByPage: boxes,
    textLinesByPage: textLines,
    unsafePages,
    layoutUnsafe,
  };
}

function hasValidTextLineGeometry(
  region: TranslationLayoutRegion,
): boolean {
  return (
    region.line_boxes.length > 0 &&
    region.line_boxes.every(validBox) &&
    region.line_boxes.every((box) => containsBox(region.bbox, box))
  );
}

export function deriveCanonicalCoverBox(
  bbox: NormalizedPdfBox,
  pageCssSizeAt100: TranslationPageCssSize,
): NormalizedPdfBox | null {
  if (!validBox(bbox) || !validPageCssSize(pageCssSizeAt100)) return null;
  const xBleed = CANONICAL_COVER_BLEED_PX / pageCssSizeAt100.widthPx;
  const yBleed = CANONICAL_COVER_BLEED_PX / pageCssSizeAt100.heightPx;
  const cover = {
    x0: Math.max(0, bbox.x0 - xBleed),
    y0: Math.max(0, bbox.y0 - yBleed),
    x1: Math.min(1, bbox.x1 + xBleed),
    y1: Math.min(1, bbox.y1 + yBleed),
  };
  return validBox(cover) ? cover : null;
}

/**
 * Derive the one cover rectangle shared by fit, background evidence, rendering,
 * and static audit. A neighboring block may trim only bleed that lies outside
 * the candidate's canonical bbox, on the canonical CSS-pixel grid, and only
 * while every authoritative candidate line keeps the full two-pixel cover.
 */
export function deriveTranslationRegionCoverBox(
  region: TranslationLayoutRegion,
  pageCssSizeAt100: TranslationPageCssSize,
  pageRegions: readonly TranslationLayoutRegion[],
): NormalizedPdfBox | null {
  if (!validRegionGeometry(region) || !validPageCssSize(pageCssSizeAt100)) return null;
  const fullCover = deriveCanonicalCoverBox(region.bbox, pageCssSizeAt100);
  if (!fullCover) return null;
  const lineBounds = unionBoxes(region.line_boxes);
  const requiredLineCover = lineBounds
    ? deriveCanonicalCoverBox(lineBounds, pageCssSizeAt100)
    : null;
  if (!requiredLineCover) return null;

  const peers: NormalizedPdfBox[] = [];
  for (const peer of pageRegions) {
    if (
      peer.region_id === region.region_id ||
      peer.block_index === region.block_index ||
      peer.page !== region.page ||
      !TEXT_REGION_KINDS.has(peer.kind)
    ) {
      continue;
    }
    if (!validBox(peer.bbox)) return null;
    if (!hasValidTextLineGeometry(peer)) {
      peers.push(peer.bbox);
      continue;
    }
    if (!validRegionGeometry(peer)) return null;
    peers.push(...peer.line_boxes);
  }

  let cover = { ...fullCover };
  for (const peer of peers) {
    if (!positiveAreaIntersection(cover, peer)) continue;
    if (positiveAreaIntersection(region.bbox, peer)) return null;

    if (peer.y1 <= region.bbox.y0) {
      cover.y0 = Math.max(
        cover.y0,
        Math.ceil(peer.y1 * pageCssSizeAt100.heightPx) / pageCssSizeAt100.heightPx,
      );
    } else if (peer.y0 >= region.bbox.y1) {
      cover.y1 = Math.min(
        cover.y1,
        Math.floor(peer.y0 * pageCssSizeAt100.heightPx) / pageCssSizeAt100.heightPx,
      );
    }
    if (peer.x1 <= region.bbox.x0) {
      cover.x0 = Math.max(
        cover.x0,
        Math.ceil(peer.x1 * pageCssSizeAt100.widthPx) / pageCssSizeAt100.widthPx,
      );
    } else if (peer.x0 >= region.bbox.x1) {
      cover.x1 = Math.min(
        cover.x1,
        Math.floor(peer.x0 * pageCssSizeAt100.widthPx) / pageCssSizeAt100.widthPx,
      );
    }
  }

  return validBox(cover) && containsBox(cover, region.bbox) && containsBox(cover, requiredLineCover)
    ? cover
    : null;
}

function unionBoxes(boxes: readonly NormalizedPdfBox[]): NormalizedPdfBox | null {
  if (boxes.length === 0 || !boxes.every(validBox)) return null;
  return boxes.reduce<NormalizedPdfBox>(
    (bounds, box) => ({
      x0: Math.min(bounds.x0, box.x0),
      y0: Math.min(bounds.y0, box.y0),
      x1: Math.max(bounds.x1, box.x1),
      y1: Math.max(bounds.y1, box.y1),
    }),
    { ...boxes[0] },
  );
}

function estimateRegionSourceFontPx100(
  region: TranslationLayoutRegion,
  sourceText: string | null,
  pageCssSizeAt100: TranslationPageCssSize,
): number | null {
  const wordHeights = region.word_boxes
    .map((box) => (box.y1 - box.y0) * pageCssSizeAt100.heightPx)
    .filter((height) => Number.isFinite(height) && height > 0)
    .sort((left, right) => left - right);
  if (wordHeights.length > 0) {
    const middle = Math.floor(wordHeights.length / 2);
    const median = wordHeights.length % 2 === 0
      ? (wordHeights[middle - 1] + wordHeights[middle]) / 2
      : wordHeights[middle];
    const estimate = Math.round(median * 1000) / 1000;
    return Number.isFinite(estimate) && estimate > 0 ? estimate : null;
  }
  const lineHeights = region.line_boxes
    .map((box) => (box.y1 - box.y0) * pageCssSizeAt100.heightPx)
    .filter((height) => Number.isFinite(height) && height > 0)
    .sort((left, right) => left - right);
  if (lineHeights.length === 0) return null;
  if (
    lineHeights.length === 1 &&
    sourceText &&
    region.geometry_source === "mineru_middle"
  ) {
    const aggregateEstimate = estimateAggregateLineBoxFontPx100(
      region,
      sourceText,
      pageCssSizeAt100,
    );
    if (aggregateEstimate !== null) return aggregateEstimate;
  }
  const middle = Math.floor(lineHeights.length / 2);
  const median = lineHeights.length % 2 === 0
    ? (lineHeights[middle - 1] + lineHeights[middle]) / 2
    : lineHeights[middle];
  // A PDF line box is a proxy for source font size, but parser group boxes can
  // occasionally appear as one oversized line. Use the median within this
  // region so another region's line metrics cannot distort its font size.
  const estimate = Math.round(median * 1000) / 1000;
  return Number.isFinite(estimate) && estimate > 0 ? estimate : null;
}

function estimateAggregateLineBoxFontPx100(
  region: TranslationLayoutRegion,
  sourceText: string,
  pageCssSizeAt100: TranslationPageCssSize,
): number | null {
  const lineBox = region.line_boxes[0];
  if (!lineBox || !sameBoxWithinOneCssPixel(lineBox, region.bbox, pageCssSizeAt100)) {
    return null;
  }
  const widthPx100 = (region.bbox.x1 - region.bbox.x0) * pageCssSizeAt100.widthPx;
  const heightPx100 = (region.bbox.y1 - region.bbox.y0) * pageCssSizeAt100.heightPx;
  const sourceAdvanceEm = estimateSourceTextAdvanceEm(sourceText);
  if (
    !Number.isFinite(widthPx100) ||
    !Number.isFinite(heightPx100) ||
    !Number.isFinite(sourceAdvanceEm) ||
    widthPx100 <= 0 ||
    heightPx100 <= 0 ||
    sourceAdvanceEm <= 0
  ) {
    return null;
  }

  // At the absolute rendering floor, the source must still require several
  // lines before a bbox-sized "line" is treated as a MinerU-style aggregate.
  // Short or ambiguous single-line boxes keep the measured height.
  const minimumWrappedLineCount = Math.ceil(
    (sourceAdvanceEm * MIN_FONT_PX_AT_100) / widthPx100,
  );
  if (
    minimumWrappedLineCount < MIN_AGGREGATE_WRAPPED_LINES ||
    heightPx100 <
      MIN_AGGREGATE_WRAPPED_LINES *
        MIN_FONT_PX_AT_100 *
        AGGREGATE_SOURCE_LINE_HEIGHT_RATIO
  ) {
    return null;
  }

  // Solve the text-area approximation
  //   advance(em) * font ~= width * lineCount
  //   height ~= lineCount * font * sourceLineHeightRatio
  // rather than interpreting the whole paragraph height as one line.
  const estimate = Math.sqrt(
    (widthPx100 * heightPx100) /
      (sourceAdvanceEm * AGGREGATE_SOURCE_LINE_HEIGHT_RATIO),
  );
  if (!Number.isFinite(estimate) || estimate < MIN_FONT_PX_AT_100) return null;
  return Math.round(estimate * 1000) / 1000;
}

function sameBoxWithinOneCssPixel(
  left: NormalizedPdfBox,
  right: NormalizedPdfBox,
  pageCssSizeAt100: TranslationPageCssSize,
): boolean {
  return (
    Math.abs(left.x0 - right.x0) * pageCssSizeAt100.widthPx <=
      AGGREGATE_LINE_BOX_EDGE_TOLERANCE_PX &&
    Math.abs(left.x1 - right.x1) * pageCssSizeAt100.widthPx <=
      AGGREGATE_LINE_BOX_EDGE_TOLERANCE_PX &&
    Math.abs(left.y0 - right.y0) * pageCssSizeAt100.heightPx <=
      AGGREGATE_LINE_BOX_EDGE_TOLERANCE_PX &&
    Math.abs(left.y1 - right.y1) * pageCssSizeAt100.heightPx <=
      AGGREGATE_LINE_BOX_EDGE_TOLERANCE_PX
  );
}

function estimateSourceTextAdvanceEm(text: string): number {
  const compact = text.trim().replace(/\s+/gu, " ");
  let advance = 0;
  for (const character of compact) {
    if (/\s/u.test(character)) {
      advance += 0.33;
    } else if (
      /\p{Script=Han}|\p{Script=Hiragana}|\p{Script=Katakana}|\p{Script=Hangul}/u.test(
        character,
      )
    ) {
      advance += 1;
    } else {
      advance += 0.5;
    }
  }
  return advance;
}

function buildFontScales(referenceFontPx100: number, fontStepPx: number): readonly number[] {
  const scales = [MIN_SOURCE_FONT_RATIO];
  const minimumReferenceFont = referenceFontPx100 * MIN_SOURCE_FONT_RATIO;
  let fontTick = Math.ceil((minimumReferenceFont + Number.EPSILON) / fontStepPx);
  for (;;) {
    const referenceFont = fontTick * fontStepPx;
    if (referenceFont >= referenceFontPx100 - Number.EPSILON) break;
    const scale = referenceFont / referenceFontPx100;
    if (scale > MIN_SOURCE_FONT_RATIO && scale < 1) scales.push(scale);
    fontTick += 1;
  }
  scales.push(1);
  return scales;
}

function failedBlock(
  blockIndex: number,
  policy: "preserve" | "panel_only",
  reason: TranslationFitReason,
  sourceFontPx100: number | null = null,
): FittedTranslationBlock {
  return { blockIndex, policy, reason, sourceFontPx100, regions: [] };
}

function validBox(box: NormalizedPdfBox): boolean {
  return (
    [box.x0, box.y0, box.x1, box.y1].every(
      (value) => Number.isFinite(value) && value >= 0 && value <= 1,
    ) &&
    box.x0 < box.x1 &&
    box.y0 < box.y1
  );
}

function validRegionGeometry(region: TranslationLayoutRegion): boolean {
  if (!validBox(region.bbox)) return false;
  if (region.line_boxes.length === 0) return false;
  if (
    ![
      ...region.line_boxes,
      ...region.word_boxes,
      ...(region.protected_boxes ?? []),
    ].every(validBox)
  ) {
    return false;
  }
  if (
    ![...region.line_boxes, ...region.word_boxes].every((box) =>
      containsBox(region.bbox, box),
    )
  ) {
    return false;
  }
  if (
    region.source_line_orders.length > 0 &&
    region.source_line_orders.length !== region.line_boxes.length
  ) {
    return false;
  }
  if (region.source_word_orders.length !== region.word_boxes.length) return false;
  return (
    validSourceOrders(region.source_line_orders) &&
    validSourceOrders(region.source_word_orders) &&
    (region.source_block_order === null || validSourceOrder(region.source_block_order))
  );
}

function containsBox(outer: NormalizedPdfBox, inner: NormalizedPdfBox): boolean {
  const epsilon = 0.003;
  return (
    inner.x0 >= outer.x0 - epsilon &&
    inner.y0 >= outer.y0 - epsilon &&
    inner.x1 <= outer.x1 + epsilon &&
    inner.y1 <= outer.y1 + epsilon
  );
}

function validSourceOrders(orders: readonly number[]): boolean {
  return orders.every(
    (order, index) => validSourceOrder(order) && (index === 0 || order > orders[index - 1]),
  );
}

function validSourceOrder(order: number): boolean {
  return Number.isInteger(order) && order >= 0;
}

function validPageCssSize(
  size: TranslationPageCssSize | undefined,
): size is TranslationPageCssSize {
  return Boolean(
    size &&
    Number.isFinite(size.widthPx) &&
    Number.isFinite(size.heightPx) &&
    size.widthPx > 0 &&
    size.heightPx > 0,
  );
}

function positiveAreaIntersection(left: NormalizedPdfBox, right: NormalizedPdfBox): boolean {
  return (
    Math.min(left.x1, right.x1) > Math.max(left.x0, right.x0) &&
    Math.min(left.y1, right.y1) > Math.max(left.y0, right.y0)
  );
}

function tokenizePlainText(text: string): FitToken[] {
  const parts = text.match(/[\p{Script=Han}]|\s+|[^\s\p{Script=Han}]+/gu) ?? [];
  return parts.map((value) => ({
    kind: /^\s+$/u.test(value) ? "space" : "text",
    value,
  }));
}

function stripMathDelimiters(value: string): string {
  if (value.startsWith("$$") && value.endsWith("$$")) return value.slice(2, -2);
  if (value.startsWith("\\[") && value.endsWith("\\]")) return value.slice(2, -2);
  if (value.startsWith("\\(") && value.endsWith("\\)")) return value.slice(2, -2);
  if (value.startsWith("$") && value.endsWith("$")) return value.slice(1, -1);
  return value;
}

function isLikelyInlineMath(value: string): boolean {
  const content = value.slice(1, -1).trim();
  if (!content) return false;
  // A pair of currency markers in prose must not turn the intervening words
  // into an immutable formula (for example "$5 million ... $10 million").
  if (/^\d/u.test(content) && !content.includes("\\") && /[A-Za-z]{2,}/u.test(content)) {
    return false;
  }
  return true;
}

function extractInlineDollarMath(text: string): ImmutableFragment[] {
  const positions: number[] = [];
  for (let index = 0; index < text.length; index += 1) {
    if (
      text[index] === "$" &&
      text[index - 1] !== "\\" &&
      text[index - 1] !== "$" &&
      text[index + 1] !== "$"
    ) {
      positions.push(index);
    }
  }
  const fragments: ImmutableFragment[] = [];
  let cursor = 0;
  while (cursor + 1 < positions.length) {
    const start = positions[cursor];
    const end = positions[cursor + 1] + 1;
    const value = text.slice(start, end);
    if (!value.includes("\n") && isLikelyInlineMath(value)) {
      fragments.push({ kind: "math", value, start, end });
      cursor += 2;
    } else {
      cursor += 1;
    }
  }
  return fragments;
}

function countValues(values: readonly string[]): Map<string, number> {
  const counts = new Map<string, number>();
  for (const value of values) counts.set(value, (counts.get(value) ?? 0) + 1);
  return counts;
}

function validFontStep(value: number | undefined): value is number {
  return typeof value === "number" && Number.isFinite(value) && value > 0;
}

function normalizeHeadingComparison(value: string): string {
  return value.normalize("NFKC").replace(/\s+/gu, " ").trim().toLocaleLowerCase("en-US");
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
