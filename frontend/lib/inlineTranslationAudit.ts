import type { Block, NormalizedPdfBox, TranslationLayout } from "./api";
import {
  containsProtectedMath,
  deriveTranslationRegionCoverBox,
  getInlineTranslationText,
  type FittedTranslationBlock,
  type TranslationPageCssSize,
} from "./translationFit";

export type InlineTranslationAuditInput = Readonly<{
  blocks: readonly Block[];
  layout: TranslationLayout;
  fittedByBlock: Readonly<Record<number, FittedTranslationBlock | undefined>>;
  backgroundByRegion: Readonly<Record<string, unknown>>;
  pageCssSizeAt100: Readonly<Record<number, TranslationPageCssSize | undefined>>;
  overflowBlockIndexes?: ReadonlySet<number> | readonly number[];
}>;

export type InlineTranslationAuditReport = Readonly<{
  translatable_block_count: number;
  completed_translation_count: number;
  pending_translation_count: number;
  error_translation_count: number;
  incomplete_translation_count: number;
  eligible_text_count: number;
  protected_excluded_count: number;
  inline_count: number;
  panel_count: number;
  unmapped_count: number;
  accessibility_ratio: number;
  safe_inline_count: number;
  safe_inline_coverage: number;
  replace_region_count: number;
  replace_average_confidence: number;
  fit_pending_count: number;
  translation_mismatch_count: number;
  protected_overlap_count: number;
  overflow_count: number;
  silent_missing_count: number;
  failure_counts: Readonly<Record<string, number>>;
  coverage_failure_counts: Readonly<Record<string, number>>;
}>;

export type InlineTranslationGateOptions = Readonly<{
  minimumCoverage: number;
  minimumReplaceConfidence?: number;
}>;

export type InlineTranslationGateReason =
  | "invalid_threshold"
  | "accessibility_incomplete"
  | "fit_pending"
  | "overflow"
  | "protected_overlap"
  | "translation_mismatch"
  | "silent_missing"
  | "coverage_below_minimum"
  | "replace_confidence_below_minimum";

export type InlineTranslationGateResult = Readonly<{
  passed: boolean;
  reasons: readonly InlineTranslationGateReason[];
}>;

const ELIGIBLE_BLOCK_TYPES = new Set<Block["type"]>(["heading", "paragraph"]);

export function buildInlineTranslationAuditReport(
  input: InlineTranslationAuditInput,
): InlineTranslationAuditReport {
  const regionsByBlock = new Map<number, TranslationLayout["regions"]>();
  const regionsById = new Map(input.layout.regions.map((region) => [region.region_id, region]));
  const pageRotationByNumber = new Map(
    input.layout.pages.map((page) => [page.page, page.rotation]),
  );
  const protectedByPage = new Map<number, NormalizedPdfBox[]>();
  for (const page of input.layout.pages) {
    protectedByPage.set(page.page, [...(page.protected_boxes ?? [])]);
  }
  for (const region of input.layout.regions) {
    const blockRegions = regionsByBlock.get(region.block_index) ?? [];
    regionsByBlock.set(region.block_index, [...blockRegions, region]);
    if ((region.protected_boxes ?? []).length > 0) {
      const pageBoxes = protectedByPage.get(region.page) ?? [];
      protectedByPage.set(region.page, [
        ...pageBoxes,
        ...(region.protected_boxes ?? []),
      ]);
    }
  }

  const translatableBlocks = input.blocks.filter(
    (block) => block.status !== "skip" && Boolean(block.original.trim()),
  );
  const completed = new Map(
    translatableBlocks
      .filter(hasCompletedTranslation)
      .map((block) => [block.index, block] as const),
  );
  const inline = new Set<number>();
  const panel = new Set<number>();
  const unmapped = new Set<number>();
  const fitPending = new Set<number>();
  const mismatched = new Set<number>();
  const protectedExcluded = new Set<number>();
  const overflow = new Set(normalizeIndexes(input.overflowBlockIndexes));
  const silentMissing = new Set<number>();
  const failureCounts = new Map<string, number>();
  const failureReasonByBlock = new Map<number, string>();
  const replaceConfidences: number[] = [];
  let protectedOverlapCount = 0;

  for (const [blockIndex, block] of completed) {
    const layoutRegions = regionsByBlock.get(blockIndex) ?? [];
    const fitted = input.fittedByBlock[blockIndex];
    const staticallyReplaceable =
      layoutRegions.length > 0 &&
      layoutRegions.every(
        (region) => region.render_policy === "replace" && region.failure_reason === null,
      );
    const backgroundComplete = layoutRegions.every((region) =>
      Object.prototype.hasOwnProperty.call(input.backgroundByRegion, region.region_id) &&
      input.backgroundByRegion[region.region_id] !== null &&
      input.backgroundByRegion[region.region_id] !== undefined,
    );
    if (staticallyReplaceable && (!backgroundComplete || !fitted)) {
      fitPending.add(blockIndex);
      increment(failureCounts, "fit_pending");
    }

    if (ELIGIBLE_BLOCK_TYPES.has(block.type) && containsProtectedMath(block.original)) {
      protectedExcluded.add(blockIndex);
    }
    if (fitted?.reason === "overflow") overflow.add(blockIndex);
    const fittedRegionsBound =
      fitted?.policy === "replace" &&
      fitted.regions.length > 0 &&
      new Set(fitted.regions.map((region) => region.regionId)).size === fitted.regions.length &&
      fitted.regions.every((fittedRegion, fittedIndex) => {
        const layoutRegion = regionsById.get(fittedRegion.regionId);
        const pageCssSize = layoutRegion
          ? input.pageCssSizeAt100[layoutRegion.page]
          : undefined;
        const expectedCover = layoutRegion && pageCssSize
          ? deriveTranslationRegionCoverBox(
              layoutRegion,
              pageCssSize,
              input.layout.regions.filter((region) => region.page === layoutRegion.page),
            )
          : null;
        return (
          fittedRegion.flowOrder === fittedIndex &&
          fittedRegion.blockIndex === blockIndex &&
          layoutRegion?.block_index === blockIndex &&
          layoutRegion.page === fittedRegion.page &&
          layoutRegion.flow_order === fittedRegion.flowOrder &&
          layoutRegion.rotation === 0 &&
          fittedRegion.rotation === 0 &&
          pageRotationByNumber.get(layoutRegion.page) === 0 &&
          expectedCover !== null &&
          sameBox(expectedCover, fittedRegion.bbox) &&
          layoutRegion.render_policy === "replace" &&
          layoutRegion.failure_reason === null
        );
      });
    const fittedRegionsDisplayReady =
      fittedRegionsBound &&
      fitted!.regions.every((region) =>
        renderableBackground(input.backgroundByRegion[region.regionId]),
      );

    if (layoutRegions.length === 0) {
      unmapped.add(blockIndex);
      increment(failureCounts, "unmapped");
      failureReasonByBlock.set(blockIndex, "unmapped");
    } else if (fitted?.policy === "replace") {
      if (!fittedRegionsBound) {
        silentMissing.add(blockIndex);
        panel.add(blockIndex);
        increment(failureCounts, "silent_missing");
        failureReasonByBlock.set(blockIndex, "silent_missing");
      } else if (!fittedRegionsDisplayReady) {
        panel.add(blockIndex);
        increment(failureCounts, "background_unrenderable");
        failureReasonByBlock.set(blockIndex, "background_unrenderable");
      } else {
        inline.add(blockIndex);
      }
    } else {
      panel.add(blockIndex);
      const reason = fitted?.reason ?? primaryLayoutFailureReason(layoutRegions) ?? "panel_only";
      if (reason !== "overflow") increment(failureCounts, reason);
      failureReasonByBlock.set(blockIndex, reason);
    }

    if (fitted?.policy !== "replace") continue;
    const reconstructed = [...fitted.regions]
      .sort(
        (left, right) =>
          left.flowOrder - right.flowOrder ||
          left.page - right.page ||
          left.regionId.localeCompare(right.regionId),
      )
      .map((region) => region.text)
      .join("");
    if (normalizeText(reconstructed) !== normalizeText(getInlineTranslationText(block))) {
      mismatched.add(blockIndex);
      increment(failureCounts, "translation_mismatch");
    }

    for (const fittedRegion of fitted.regions) {
      const layoutRegion = regionsById.get(fittedRegion.regionId);
      const confidence = layoutRegion?.confidence;
      if (fittedRegionsDisplayReady) {
        replaceConfidences.push(
          typeof confidence === "number" && Number.isFinite(confidence) ? confidence : 0,
        );
      }
      if (!layoutRegion) {
        if (!silentMissing.has(blockIndex)) {
          silentMissing.add(blockIndex);
          increment(failureCounts, "silent_missing");
        }
        continue;
      }
      const protectedBoxes = protectedByPage.get(fittedRegion.page) ?? [];
      if (protectedBoxes.some((box) => hasPositiveAreaIntersection(fittedRegion.bbox, box))) {
        protectedOverlapCount += 1;
        increment(failureCounts, "protected_overlap");
      }
    }
  }

  for (const blockIndex of overflow) {
    if (completed.has(blockIndex)) increment(failureCounts, "overflow");
  }

  const completedCount = completed.size;
  const errorCount = translatableBlocks.filter((block) => block.status === "error").length;
  const incompleteCount = translatableBlocks.length - completedCount;
  const pendingCount = incompleteCount - errorCount;
  const accessibleCount = inline.size + panel.size + unmapped.size;
  const eligibleIndexes = new Set(
    [...completed.values()]
      .filter(
        (block) =>
          ELIGIBLE_BLOCK_TYPES.has(block.type) && !protectedExcluded.has(block.index),
      )
      .map((block) => block.index),
  );
  const safeInlineCount = [...inline].filter((blockIndex) => eligibleIndexes.has(blockIndex)).length;
  const coverageFailureCounts = new Map<string, number>();
  for (const blockIndex of eligibleIndexes) {
    if (inline.has(blockIndex)) continue;
    increment(coverageFailureCounts, failureReasonByBlock.get(blockIndex) ?? "silent_missing");
  }

  return {
    translatable_block_count: translatableBlocks.length,
    completed_translation_count: completedCount,
    pending_translation_count: pendingCount,
    error_translation_count: errorCount,
    incomplete_translation_count: incompleteCount,
    eligible_text_count: eligibleIndexes.size,
    protected_excluded_count: protectedExcluded.size,
    inline_count: inline.size,
    panel_count: panel.size,
    unmapped_count: unmapped.size,
    accessibility_ratio: ratio(accessibleCount, completedCount, 1),
    safe_inline_count: safeInlineCount,
    safe_inline_coverage: ratio(safeInlineCount, eligibleIndexes.size, 0),
    replace_region_count: replaceConfidences.length,
    replace_average_confidence: average(replaceConfidences),
    fit_pending_count: fitPending.size,
    translation_mismatch_count: mismatched.size,
    protected_overlap_count: protectedOverlapCount,
    overflow_count: [...overflow].filter((blockIndex) => completed.has(blockIndex)).length,
    silent_missing_count: silentMissing.size,
    failure_counts: Object.fromEntries([...failureCounts].sort(([left], [right]) => left.localeCompare(right))),
    coverage_failure_counts: Object.fromEntries(
      [...coverageFailureCounts].sort(([left], [right]) => left.localeCompare(right)),
    ),
  };
}

export function evaluateInlineTranslationGate(
  report: InlineTranslationAuditReport,
  options: InlineTranslationGateOptions,
): InlineTranslationGateResult {
  if (
    !validRatioThreshold(options.minimumCoverage) ||
    (options.minimumReplaceConfidence !== undefined &&
      !validRatioThreshold(options.minimumReplaceConfidence))
  ) {
    return { passed: false, reasons: ["invalid_threshold"] };
  }
  const minimumCoverage = options.minimumCoverage;
  const minimumReplaceConfidence = options.minimumReplaceConfidence ?? 0.92;
  const reasons: InlineTranslationGateReason[] = [];
  if (report.accessibility_ratio < 1) reasons.push("accessibility_incomplete");
  if (report.fit_pending_count > 0) reasons.push("fit_pending");
  if (report.overflow_count > 0) reasons.push("overflow");
  if (report.protected_overlap_count > 0) reasons.push("protected_overlap");
  if (report.translation_mismatch_count > 0) reasons.push("translation_mismatch");
  if (report.silent_missing_count > 0) reasons.push("silent_missing");
  if (report.safe_inline_coverage < minimumCoverage) reasons.push("coverage_below_minimum");
  if (report.replace_average_confidence < minimumReplaceConfidence) {
    reasons.push("replace_confidence_below_minimum");
  }
  return { passed: reasons.length === 0, reasons };
}

function hasCompletedTranslation(block: Block): boolean {
  return block.status === "done" && Boolean(block.translation?.trim());
}

function normalizeIndexes(indexes: InlineTranslationAuditInput["overflowBlockIndexes"]): number[] {
  if (!indexes) return [];
  return [...indexes].filter((index) => Number.isInteger(index) && index >= 0);
}

function primaryLayoutFailureReason(
  regions: TranslationLayout["regions"],
): string | null {
  return [...regions]
    .sort(
      (left, right) =>
        left.flow_order - right.flow_order ||
        left.page - right.page ||
        left.region_id.localeCompare(right.region_id),
    )
    .find((region) => region.failure_reason)?.failure_reason ?? null;
}

function normalizeText(value: string): string {
  return value.replace(/\r\n?/g, "\n").normalize("NFC");
}

function hasPositiveAreaIntersection(left: NormalizedPdfBox, right: NormalizedPdfBox): boolean {
  return (
    Math.min(left.x1, right.x1) > Math.max(left.x0, right.x0) &&
    Math.min(left.y1, right.y1) > Math.max(left.y0, right.y0)
  );
}

function sameBox(left: NormalizedPdfBox, right: NormalizedPdfBox): boolean {
  return (
    left.x0 === right.x0 &&
    left.y0 === right.y0 &&
    left.x1 === right.x1 &&
    left.y1 === right.y1
  );
}

function renderableBackground(value: unknown): boolean {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Record<string, unknown>;
  return (
    candidate.evidence === "uniform" &&
    typeof candidate.backgroundColor === "string" &&
    candidate.backgroundColor.length > 0 &&
    typeof candidate.foregroundColor === "string" &&
    candidate.foregroundColor.length > 0
  );
}

function increment(counts: Map<string, number>, reason: string): void {
  counts.set(reason, (counts.get(reason) ?? 0) + 1);
}

function ratio(numerator: number, denominator: number, emptyValue: number): number {
  return denominator === 0 ? emptyValue : numerator / denominator;
}

function average(values: readonly number[]): number {
  if (values.length === 0) return 0;
  return values.reduce((total, value) => total + value, 0) / values.length;
}

function validRatioThreshold(value: number | undefined): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 1;
}
