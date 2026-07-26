import type { NormalizedPdfBox } from "./api";

export type PdfBackgroundEvidence = "uniform" | "complex" | "unknown";

export type PdfBackgroundFailureReason =
  | "invalid_pixel_buffer"
  | "insufficient_samples"
  | "transparent_pixels"
  | "dominant_color_low"
  | "dominant_color_spread"
  | "high_contrast_graphic"
  | "spatial_nonuniform"
  | "protected_geometry_overlap"
  | "invalid_region"
  | "canvas_context_unavailable"
  | "canvas_read_failed";

export type RgbColor = Readonly<{
  r: number;
  g: number;
  b: number;
}>;

export type PdfBackgroundClassification = Readonly<{
  evidence: PdfBackgroundEvidence;
  reason: PdfBackgroundFailureReason | null;
  background: RgbColor | null;
  backgroundColor: string | null;
  foregroundColor: string | null;
  contrastRatio: number | null;
  sampleCount: number;
  dominantRatio: number;
  cornerDominantRatio: number;
  maxChannelDelta: number;
}>;

export type PdfBackgroundClassifierOptions = Readonly<{
  alphaThreshold?: number;
  quantizationStep?: number;
  minimumSampleCount?: number;
  minimumDominantRatio?: number;
  minimumTextDominantRatio?: number;
  minimumCornerDominantRatio?: number;
  maximumDominantChannelDelta?: number;
  tileCount?: number;
  minimumTileDominantRatio?: number;
  minimumTextTileDominantRatio?: number;
  maximumLowContrastVariationRatio?: number;
  minimumContrastRatio?: number;
  maximumSampleDimension?: number;
  trustedSingleLineText?: boolean;
}>;

export type PdfBackgroundSamplingOptions = PdfBackgroundClassifierOptions &
  Readonly<{
    /** Page-normalized line boxes from a trusted textual layout region. */
    trustedTextLineBoxes?: readonly NormalizedPdfBox[];
    /** Page-normalized word boxes from the same trusted text extraction. */
    trustedTextWordBoxes?: readonly NormalizedPdfBox[];
    /** Page-normalized protected geometry from the region and page. */
    protectedBoxes?: readonly NormalizedPdfBox[];
  }>;

type PixelBuffer = Readonly<{
  data: ArrayLike<number>;
  width: number;
  height: number;
}>;

type ResolvedOptions = Required<PdfBackgroundClassifierOptions>;

const DEFAULT_OPTIONS: ResolvedOptions = {
  alphaThreshold: 255,
  quantizationStep: 16,
  minimumSampleCount: 16,
  minimumDominantRatio: 0.72,
  minimumTextDominantRatio: 0.45,
  minimumCornerDominantRatio: 0.6,
  maximumDominantChannelDelta: 18,
  tileCount: 3,
  minimumTileDominantRatio: 0.35,
  minimumTextTileDominantRatio: 0.2,
  maximumLowContrastVariationRatio: 0.08,
  minimumContrastRatio: 4.5,
  maximumSampleDimension: 72,
  trustedSingleLineText: false,
};

const SOFT_DARK: RgbColor = { r: 24, g: 25, b: 29 };
const SOFT_LIGHT: RgbColor = { r: 248, g: 249, b: 251 };
const BLACK: RgbColor = { r: 0, g: 0, b: 0 };
const WHITE: RgbColor = { r: 255, g: 255, b: 255 };
const TRUSTED_TEXT_CONFIRMATION_SAMPLE_DIMENSION = 120;
const TRUSTED_TEXT_RING_SAMPLE_DIMENSION = 360;
const MINIMUM_TRUSTED_TEXT_DOMINANT_RATIO = 0.28;
const MINIMUM_TRUSTED_TEXT_CORNER_DOMINANT_RATIO = 0.45;
const MAXIMUM_TRUSTED_WORD_OUTSIDE_VARIATION_RATIO = 0.01;

type TrustedTextGeometry = Readonly<{
  lineBoxes: readonly NormalizedPdfBox[];
  wordBoxes: readonly NormalizedPdfBox[];
  allowContainedWordGlyphs: boolean;
}>;

export function classifyPdfBackgroundPixels(
  pixels: PixelBuffer,
  options: PdfBackgroundClassifierOptions = {},
): PdfBackgroundClassification {
  return classifyPdfBackgroundPixelsInternal(pixels, resolveOptions(options));
}

function classifyPdfBackgroundPixelsInternal(
  pixels: PixelBuffer,
  resolved: ResolvedOptions,
  trustedTextGeometry: TrustedTextGeometry | null = null,
  confirmedBackground: RgbColor | null = null,
): PdfBackgroundClassification {
  const { data, width, height } = pixels;
  const expectedLength = width * height * 4;
  if (
    !Number.isInteger(width) ||
    !Number.isInteger(height) ||
    width <= 0 ||
    height <= 0 ||
    data.length !== expectedLength
  ) {
    return failedClassification("unknown", "invalid_pixel_buffer");
  }

  const sampleCount = width * height;
  if (sampleCount < resolved.minimumSampleCount) {
    return failedClassification("unknown", "insufficient_samples", sampleCount);
  }

  const histogram = new Map<string, number>();
  const keys = new Array<string>(sampleCount);
  for (let pixel = 0; pixel < sampleCount; pixel += 1) {
    const offset = pixel * 4;
    const red = Number(data[offset]);
    const green = Number(data[offset + 1]);
    const blue = Number(data[offset + 2]);
    const alpha = Number(data[offset + 3]);
    if (![red, green, blue, alpha].every((channel) => Number.isFinite(channel))) {
      return failedClassification("unknown", "invalid_pixel_buffer", sampleCount);
    }
    if (alpha < resolved.alphaThreshold) {
      return failedClassification("complex", "transparent_pixels", sampleCount);
    }
    const key = quantizedColorKey(red, green, blue, resolved.quantizationStep);
    keys[pixel] = key;
    histogram.set(key, (histogram.get(key) ?? 0) + 1);
  }

  let globalDominantKey = "";
  let globalDominantCount = 0;
  for (const [key, count] of histogram) {
    if (count > globalDominantCount) {
      globalDominantKey = key;
      globalDominantCount = count;
    }
  }
  const cornerCandidate = trustedTextGeometry
    ? dominantColorInCorners(keys, width, height)
    : null;
  const dominantKey = confirmedBackground
    ? quantizedColorKey(
        confirmedBackground.r,
        confirmedBackground.g,
        confirmedBackground.b,
        resolved.quantizationStep,
      )
    : cornerCandidate?.key ?? globalDominantKey;
  const dominantCount = histogram.get(dominantKey) ?? 0;
  const dominantRatio = dominantCount / sampleCount;
  const cornerDominantRatio = dominantColorRatioInCorners(
    keys,
    width,
    height,
    dominantKey,
  );
  const hasTextRegionBackgroundEvidence =
    dominantRatio >= resolved.minimumTextDominantRatio &&
    cornerDominantRatio >= resolved.minimumCornerDominantRatio;
  const hasTrustedTextBackgroundEvidence = Boolean(
    trustedTextGeometry &&
      dominantRatio >= MINIMUM_TRUSTED_TEXT_DOMINANT_RATIO &&
      cornerDominantRatio >= MINIMUM_TRUSTED_TEXT_CORNER_DOMINANT_RATIO,
  );
  const trustedWordPixelBoxes =
    confirmedBackground && trustedTextGeometry?.allowContainedWordGlyphs
      ? trustedTextGeometry.wordBoxes.map((box) =>
          normalizedBoxToPixelBounds(box, width, height),
        )
      : [];
  if (
    dominantRatio < resolved.minimumDominantRatio &&
    !hasTextRegionBackgroundEvidence &&
    !hasTrustedTextBackgroundEvidence
  ) {
    return failedClassification(
      "complex",
      "dominant_color_low",
      sampleCount,
      dominantRatio,
      0,
      cornerDominantRatio,
    );
  }

  let redSum = 0;
  let greenSum = 0;
  let blueSum = 0;
  for (let pixel = 0; pixel < sampleCount; pixel += 1) {
    if (keys[pixel] !== dominantKey) continue;
    const offset = pixel * 4;
    redSum += Number(data[offset]);
    greenSum += Number(data[offset + 1]);
    blueSum += Number(data[offset + 2]);
  }
  const sampledBackground = {
    r: Math.round(redSum / dominantCount),
    g: Math.round(greenSum / dominantCount),
    b: Math.round(blueSum / dominantCount),
  };
  if (
    confirmedBackground &&
    rgbDistance(sampledBackground, confirmedBackground) >
      resolved.maximumDominantChannelDelta
  ) {
    return failedClassification(
      "complex",
      "dominant_color_spread",
      sampleCount,
      dominantRatio,
      rgbDistance(sampledBackground, confirmedBackground),
      cornerDominantRatio,
    );
  }
  const background = confirmedBackground ?? sampledBackground;

  let maxChannelDelta = 0;
  for (let pixel = 0; pixel < sampleCount; pixel += 1) {
    if (keys[pixel] !== dominantKey) continue;
    const offset = pixel * 4;
    maxChannelDelta = Math.max(
      maxChannelDelta,
      Math.abs(Number(data[offset]) - background.r),
      Math.abs(Number(data[offset + 1]) - background.g),
      Math.abs(Number(data[offset + 2]) - background.b),
    );
  }
  if (maxChannelDelta > resolved.maximumDominantChannelDelta) {
    return failedClassification(
      "complex",
      "dominant_color_spread",
      sampleCount,
      dominantRatio,
      maxChannelDelta,
      cornerDominantRatio,
    );
  }

  if (
    hasExcessLowContrastVariation(
      data,
      keys,
      width,
      height,
      dominantKey,
      background,
      trustedWordPixelBoxes.length > 0
        ? Math.min(
            resolved.maximumLowContrastVariationRatio,
            MAXIMUM_TRUSTED_WORD_OUTSIDE_VARIATION_RATIO,
          )
        : resolved.maximumLowContrastVariationRatio,
      trustedWordPixelBoxes,
    )
  ) {
    return failedClassification(
      "complex",
      "spatial_nonuniform",
      sampleCount,
      dominantRatio,
      maxChannelDelta,
      cornerDominantRatio,
    );
  }

  if (
    hasHighContrastGraphicStructure(
      data,
      width,
      height,
      background,
      trustedTextGeometry,
    )
  ) {
    return failedClassification(
      "complex",
      "high_contrast_graphic",
      sampleCount,
      dominantRatio,
      maxChannelDelta,
      cornerDominantRatio,
    );
  }

  if (
    !hasSpatiallyUniformDominantColor(
      keys,
      width,
      height,
      dominantKey,
      resolved.tileCount,
      hasTextRegionBackgroundEvidence || hasTrustedTextBackgroundEvidence
        ? resolved.minimumTextTileDominantRatio
        : resolved.minimumTileDominantRatio,
      trustedWordPixelBoxes,
    )
  ) {
    return failedClassification(
      "complex",
      "spatial_nonuniform",
      sampleCount,
      dominantRatio,
      maxChannelDelta,
      cornerDominantRatio,
    );
  }

  const foreground = chooseForeground(background, resolved.minimumContrastRatio);
  return {
    evidence: "uniform",
    reason: null,
    background,
    backgroundColor: cssRgb(background),
    foregroundColor: cssRgb(foreground.color),
    contrastRatio: foreground.ratio,
    sampleCount,
    dominantRatio,
    cornerDominantRatio,
    maxChannelDelta,
  };
}

/**
 * Samples one normalized PDF region from the already-rendered evidence canvas.
 * Any invalid geometry, unavailable/tainted canvas, alpha, or non-uniform sample
 * returns no paint colors, so callers cannot accidentally create an unsafe
 * opaque overlay.
 */
export function sampleCanvasRegionBackground(
  canvas: HTMLCanvasElement,
  bbox: NormalizedPdfBox,
  options: PdfBackgroundSamplingOptions = {},
): PdfBackgroundClassification {
  if (!validNormalizedBox(bbox) || canvas.width <= 0 || canvas.height <= 0) {
    return failedClassification("unknown", "invalid_region");
  }

  const protectedBoxes = options.protectedBoxes ?? [];
  if (!protectedBoxes.every(validNormalizedBox)) {
    return failedClassification("unknown", "invalid_region");
  }
  if (protectedBoxes.some((box) => boxesOverlap(bbox, box))) {
    return failedClassification("complex", "protected_geometry_overlap");
  }

  const trustedTextLineBoxes = options.trustedTextLineBoxes;
  const trustedTextWordBoxes = options.trustedTextWordBoxes ?? [];
  let trustedTextGeometry: TrustedTextGeometry | null = null;
  if (trustedTextLineBoxes) {
    if (
      trustedTextLineBoxes.length === 0 ||
      !trustedTextLineBoxes.every(
        (box) => validNormalizedBox(box) && boxContainsWithTolerance(bbox, box),
      )
    ) {
      return failedClassification("unknown", "invalid_region");
    }
    trustedTextGeometry = {
      lineBoxes: trustedTextLineBoxes.map((box) => boxRelativeToRegion(bbox, box)),
      wordBoxes: trustedTextWordBoxes.map((box) => boxRelativeToRegion(bbox, box)),
      allowContainedWordGlyphs: false,
    };
  }
  if (
    trustedTextWordBoxes.some(
      (wordBox) =>
        !validNormalizedBox(wordBox) ||
        !boxContainsWithTolerance(bbox, wordBox) ||
        !trustedTextLineBoxes?.some((lineBox) =>
          boxContainsWithTolerance(lineBox, wordBox),
        ),
    )
  ) {
    return failedClassification("unknown", "invalid_region");
  }

  const sourceX = Math.floor(bbox.x0 * canvas.width);
  const sourceY = Math.floor(bbox.y0 * canvas.height);
  const sourceRight = Math.ceil(bbox.x1 * canvas.width);
  const sourceBottom = Math.ceil(bbox.y1 * canvas.height);
  const sourceWidth = sourceRight - sourceX;
  const sourceHeight = sourceBottom - sourceY;
  if (sourceWidth <= 0 || sourceHeight <= 0) {
    return failedClassification("unknown", "invalid_region");
  }

  const resolved = resolveOptions(options);
  const classifyAtMaximumDimension = (
    maximumDimension: number,
    geometry: TrustedTextGeometry | null = trustedTextGeometry,
    confirmedBackground: RgbColor | null = null,
  ) => {
    const scale = Math.min(
      1,
      maximumDimension / Math.max(sourceWidth, sourceHeight),
    );
    const sampleWidth = Math.max(1, Math.round(sourceWidth * scale));
    const sampleHeight = Math.max(1, Math.round(sourceHeight * scale));
    const sampleCanvas = canvas.ownerDocument.createElement("canvas");
    sampleCanvas.width = sampleWidth;
    sampleCanvas.height = sampleHeight;
    const context = sampleCanvas.getContext("2d", { willReadFrequently: true });
    if (!context) {
      return failedClassification("unknown", "canvas_context_unavailable");
    }
    context.imageSmoothingEnabled = false;
    context.clearRect(0, 0, sampleWidth, sampleHeight);
    context.drawImage(
      canvas,
      sourceX,
      sourceY,
      sourceWidth,
      sourceHeight,
      0,
      0,
      sampleWidth,
      sampleHeight,
    );
    const image = context.getImageData(0, 0, sampleWidth, sampleHeight);
    return classifyPdfBackgroundPixelsInternal(
      image,
      resolved,
      geometry,
      confirmedBackground,
    );
  };
  try {
    let classification = classifyAtMaximumDimension(resolved.maximumSampleDimension);
    if (
      (resolved.trustedSingleLineText || trustedTextGeometry) &&
      (classification.reason === "high_contrast_graphic" ||
        classification.reason === "dominant_color_low" ||
        classification.reason === "spatial_nonuniform") &&
      resolved.maximumSampleDimension < TRUSTED_TEXT_CONFIRMATION_SAMPLE_DIMENSION &&
      Math.max(sourceWidth, sourceHeight) > resolved.maximumSampleDimension
    ) {
      // A 72px nearest-neighbour reduction can join adjacent bold glyphs into
      // one apparent two-axis component. Re-run the identical safety checks at
      // a bounded higher resolution; rules and real graphics still fail.
      classification = classifyAtMaximumDimension(
        Math.min(
          TRUSTED_TEXT_CONFIRMATION_SAMPLE_DIMENSION,
          Math.max(sourceWidth, sourceHeight),
        ),
      );
    }
    if (
      trustedTextGeometry &&
      (classification.reason === "high_contrast_graphic" ||
        classification.reason === "dominant_color_low")
    ) {
      const ringBackground = sampleTrustedTextBackgroundRing(
        canvas,
        bbox,
        trustedTextLineBoxes!,
        protectedBoxes,
        resolved,
      );
      if (ringBackground) {
        const confirmed = classifyAtMaximumDimension(
          Math.min(
            TRUSTED_TEXT_RING_SAMPLE_DIMENSION,
            Math.max(sourceWidth, sourceHeight),
          ),
          {
            ...trustedTextGeometry,
            allowContainedWordGlyphs: trustedTextWordBoxes.length > 0,
          },
          ringBackground,
        );
        return confirmed;
      }
    }
    return classification;
  } catch {
    return failedClassification("unknown", "canvas_read_failed");
  }
}

function sampleTrustedTextBackgroundRing(
  canvas: HTMLCanvasElement,
  bbox: NormalizedPdfBox,
  lineBoxes: readonly NormalizedPdfBox[],
  protectedBoxes: readonly NormalizedPdfBox[],
  options: ResolvedOptions,
): RgbColor | null {
  const lineHeights = lineBoxes
    .map((box) => (box.y1 - box.y0) * canvas.height)
    .filter((height) => Number.isFinite(height) && height > 0)
    .sort((left, right) => left - right);
  if (lineHeights.length === 0) return null;
  const medianLineHeight = lineHeights[Math.floor(lineHeights.length / 2)];
  const padding = Math.max(4, Math.min(10, Math.round(medianLineHeight * 0.35)));
  const sourceX = Math.floor(bbox.x0 * canvas.width);
  const sourceY = Math.floor(bbox.y0 * canvas.height);
  const sourceRight = Math.ceil(bbox.x1 * canvas.width);
  const sourceBottom = Math.ceil(bbox.y1 * canvas.height);
  const expandedX = Math.max(0, sourceX - padding);
  const expandedY = Math.max(0, sourceY - padding);
  const expandedRight = Math.min(canvas.width, sourceRight + padding);
  const expandedBottom = Math.min(canvas.height, sourceBottom + padding);
  const expandedWidth = expandedRight - expandedX;
  const expandedHeight = expandedBottom - expandedY;
  if (
    expandedWidth <= sourceRight - sourceX ||
    expandedHeight <= sourceBottom - sourceY
  ) {
    return null;
  }
  const expandedBox = {
    x0: expandedX / canvas.width,
    y0: expandedY / canvas.height,
    x1: expandedRight / canvas.width,
    y1: expandedBottom / canvas.height,
  };
  if (protectedBoxes.some((box) => boxesOverlap(expandedBox, box))) return null;

  // Use integer canvas boundaries for the inner edge of each strip. Reusing
  // the fractional PDF bbox would make floor/ceil sampling pull one glyph row
  // or column into the supposedly external ring.
  const sampledRegionBox = {
    x0: sourceX / canvas.width,
    y0: sourceY / canvas.height,
    x1: sourceRight / canvas.width,
    y1: sourceBottom / canvas.height,
  };
  const strips = [
    {
      x0: expandedBox.x0,
      y0: expandedBox.y0,
      x1: expandedBox.x1,
      y1: sampledRegionBox.y0,
    },
    {
      x0: expandedBox.x0,
      y0: sampledRegionBox.y1,
      x1: expandedBox.x1,
      y1: expandedBox.y1,
    },
    {
      x0: expandedBox.x0,
      y0: sampledRegionBox.y0,
      x1: sampledRegionBox.x0,
      y1: sampledRegionBox.y1,
    },
    {
      x0: sampledRegionBox.x1,
      y0: sampledRegionBox.y0,
      x1: expandedBox.x1,
      y1: sampledRegionBox.y1,
    },
  ].filter(validNormalizedBox);
  if (strips.length < 2) return null;
  const samples = strips.map((strip) =>
    sampleStrictCanvasBox(canvas, strip, options),
  );
  if (samples.some((sample) => sample?.evidence !== "uniform" || !sample.background)) {
    return null;
  }
  const backgrounds = samples.map((sample) => sample!.background!);
  const first = backgrounds[0];
  if (
    backgrounds.some(
      (background) =>
        rgbDistance(first, background) > options.maximumDominantChannelDelta,
    )
  ) {
    return null;
  }
  return {
    r: Math.round(backgrounds.reduce((sum, color) => sum + color.r, 0) / backgrounds.length),
    g: Math.round(backgrounds.reduce((sum, color) => sum + color.g, 0) / backgrounds.length),
    b: Math.round(backgrounds.reduce((sum, color) => sum + color.b, 0) / backgrounds.length),
  };
}

function sampleStrictCanvasBox(
  canvas: HTMLCanvasElement,
  bbox: NormalizedPdfBox,
  options: ResolvedOptions,
): PdfBackgroundClassification | null {
  const sourceX = Math.floor(bbox.x0 * canvas.width);
  const sourceY = Math.floor(bbox.y0 * canvas.height);
  const sourceRight = Math.ceil(bbox.x1 * canvas.width);
  const sourceBottom = Math.ceil(bbox.y1 * canvas.height);
  const sourceWidth = sourceRight - sourceX;
  const sourceHeight = sourceBottom - sourceY;
  if (sourceWidth <= 0 || sourceHeight <= 0) return null;
  const scale = Math.min(
    1,
    TRUSTED_TEXT_RING_SAMPLE_DIMENSION / Math.max(sourceWidth, sourceHeight),
  );
  const sampleWidth = Math.max(1, Math.round(sourceWidth * scale));
  const sampleHeight = Math.max(1, Math.round(sourceHeight * scale));
  const sampleCanvas = canvas.ownerDocument.createElement("canvas");
  sampleCanvas.width = sampleWidth;
  sampleCanvas.height = sampleHeight;
  const context = sampleCanvas.getContext("2d", { willReadFrequently: true });
  if (!context) return null;
  context.imageSmoothingEnabled = false;
  context.drawImage(
    canvas,
    sourceX,
    sourceY,
    sourceWidth,
    sourceHeight,
    0,
    0,
    sampleWidth,
    sampleHeight,
  );
  return classifyPdfBackgroundPixelsInternal(
    context.getImageData(0, 0, sampleWidth, sampleHeight),
    options,
  );
}

export function wcagContrastRatio(foreground: RgbColor, background: RgbColor): number {
  const light = Math.max(relativeLuminance(foreground), relativeLuminance(background));
  const dark = Math.min(relativeLuminance(foreground), relativeLuminance(background));
  return (light + 0.05) / (dark + 0.05);
}

function rgbDistance(left: RgbColor, right: RgbColor): number {
  return Math.hypot(left.r - right.r, left.g - right.g, left.b - right.b);
}

function resolveOptions(options: PdfBackgroundClassifierOptions): ResolvedOptions {
  return {
    ...DEFAULT_OPTIONS,
    ...options,
  };
}

function quantizedColorKey(red: number, green: number, blue: number, step: number): string {
  return `${Math.floor(red / step)}:${Math.floor(green / step)}:${Math.floor(blue / step)}`;
}

function hasSpatiallyUniformDominantColor(
  keys: readonly string[],
  width: number,
  height: number,
  dominantKey: string,
  tileCount: number,
  minimumRatio: number,
  ignoredBoxes: readonly PixelBounds[] = [],
): boolean {
  const columns = Math.max(1, Math.min(Math.floor(tileCount), width));
  const rows = Math.max(1, Math.min(Math.floor(tileCount), height));
  for (let row = 0; row < rows; row += 1) {
    const y0 = Math.floor((row * height) / rows);
    const y1 = Math.floor(((row + 1) * height) / rows);
    for (let column = 0; column < columns; column += 1) {
      const x0 = Math.floor((column * width) / columns);
      const x1 = Math.floor(((column + 1) * width) / columns);
      let count = 0;
      let total = 0;
      for (let y = y0; y < y1; y += 1) {
        for (let x = x0; x < x1; x += 1) {
          if (pixelInsideAnyBox(x, y, ignoredBoxes)) continue;
          total += 1;
          if (keys[y * width + x] === dominantKey) count += 1;
        }
      }
      if (total > 0 && count / total < minimumRatio) return false;
    }
  }
  return true;
}

function dominantColorRatioInCorners(
  keys: readonly string[],
  width: number,
  height: number,
  dominantKey: string,
): number {
  const patchWidth = Math.max(1, Math.floor(width * 0.2));
  const patchHeight = Math.max(1, Math.floor(height * 0.2));
  let matched = 0;
  let total = 0;
  for (const [x0, y0] of [
    [0, 0],
    [width - patchWidth, 0],
    [0, height - patchHeight],
    [width - patchWidth, height - patchHeight],
  ]) {
    for (let y = y0; y < y0 + patchHeight; y += 1) {
      for (let x = x0; x < x0 + patchWidth; x += 1) {
        total += 1;
        if (keys[y * width + x] === dominantKey) matched += 1;
      }
    }
  }
  return total === 0 ? 0 : matched / total;
}

function dominantColorInCorners(
  keys: readonly string[],
  width: number,
  height: number,
): { key: string; ratio: number } | null {
  const patchWidth = Math.max(1, Math.floor(width * 0.2));
  const patchHeight = Math.max(1, Math.floor(height * 0.2));
  const histogram = new Map<string, number>();
  let total = 0;
  for (const [x0, y0] of [
    [0, 0],
    [width - patchWidth, 0],
    [0, height - patchHeight],
    [width - patchWidth, height - patchHeight],
  ]) {
    for (let y = y0; y < y0 + patchHeight; y += 1) {
      for (let x = x0; x < x0 + patchWidth; x += 1) {
        const key = keys[y * width + x];
        histogram.set(key, (histogram.get(key) ?? 0) + 1);
        total += 1;
      }
    }
  }
  let dominantKey = "";
  let dominantCount = 0;
  for (const [key, count] of histogram) {
    if (count > dominantCount) {
      dominantKey = key;
      dominantCount = count;
    }
  }
  return total > 0 && dominantKey
    ? { key: dominantKey, ratio: dominantCount / total }
    : null;
}

function hasExcessLowContrastVariation(
  data: ArrayLike<number>,
  keys: readonly string[],
  width: number,
  height: number,
  dominantKey: string,
  background: RgbColor,
  maximumRatio: number,
  ignoredBoxes: readonly PixelBounds[] = [],
): boolean {
  let variations = 0;
  let eligiblePixels = 0;
  for (let pixel = 0; pixel < keys.length; pixel += 1) {
    const x = pixel % width;
    const y = Math.floor(pixel / width);
    if (pixelInsideAnyBox(x, y, ignoredBoxes)) continue;
    eligiblePixels += 1;
    if (keys[pixel] === dominantKey) continue;
    const offset = pixel * 4;
    const color = {
      r: Number(data[offset]),
      g: Number(data[offset + 1]),
      b: Number(data[offset + 2]),
    };
    const distance = Math.hypot(
      color.r - background.r,
      color.g - background.g,
      color.b - background.b,
    );
    if (
      distance > 12 &&
      wcagContrastRatio(color, background) < 1.8 &&
      !hasNearbyHighContrastPixel(data, width, height, pixel, background)
    ) {
      variations += 1;
    }
  }
  return eligiblePixels > 0 && variations / eligiblePixels > maximumRatio;
}

function pixelInsideAnyBox(
  x: number,
  y: number,
  boxes: readonly PixelBounds[],
): boolean {
  return boxes.some(
    (box) => x >= box.x0 && x < box.x1 && y >= box.y0 && y < box.y1,
  );
}

function hasNearbyHighContrastPixel(
  data: ArrayLike<number>,
  width: number,
  height: number,
  pixel: number,
  background: RgbColor,
): boolean {
  const centerX = pixel % width;
  const centerY = Math.floor(pixel / width);
  for (let y = Math.max(0, centerY - 2); y <= Math.min(height - 1, centerY + 2); y += 1) {
    for (let x = Math.max(0, centerX - 2); x <= Math.min(width - 1, centerX + 2); x += 1) {
      const offset = (y * width + x) * 4;
      const color = {
        r: Number(data[offset]),
        g: Number(data[offset + 1]),
        b: Number(data[offset + 2]),
      };
      if (wcagContrastRatio(color, background) >= 1.8) return true;
    }
  }
  return false;
}

function hasHighContrastGraphicStructure(
  data: ArrayLike<number>,
  width: number,
  height: number,
  background: RgbColor,
  trustedTextGeometry: TrustedTextGeometry | null,
): boolean {
  const trustedTextLineBoxes = trustedTextGeometry?.lineBoxes ?? [];
  const trustedTextWordBoxes = trustedTextGeometry?.wordBoxes ?? [];
  const trustedLinePixelBoxes = trustedTextLineBoxes.map((box) =>
    normalizedBoxToPixelBounds(box, width, height),
  );
  const trustedWordPixelBoxes = trustedTextWordBoxes.map((box) =>
    normalizedBoxToPixelBounds(box, width, height),
  );
  const strokeInsideTrustedWord = (bounds: PixelBounds) =>
    Boolean(
      trustedTextGeometry?.allowContainedWordGlyphs &&
      trustedWordPixelBoxes.some((box) => pixelBoundsContain(box, bounds, 1)),
    );
  const highContrast = new Uint8Array(width * height);
  for (let pixel = 0; pixel < highContrast.length; pixel += 1) {
    const offset = pixel * 4;
    const color = {
      r: Number(data[offset]),
      g: Number(data[offset + 1]),
      b: Number(data[offset + 2]),
    };
    highContrast[pixel] = wcagContrastRatio(color, background) >= 1.8 ? 1 : 0;
  }

  const horizontalLimit = Math.max(8, Math.ceil(width * 0.35));
  for (let y = 0; y < height; y += 1) {
    let runStart = -1;
    for (let x = 0; x <= width; x += 1) {
      const active = x < width && highContrast[y * width + x];
      if (active && runStart < 0) runStart = x;
      if (!active && runStart >= 0) {
        if (
          x - runStart >= horizontalLimit &&
          !strokeInsideTrustedWord({ x0: runStart, y0: y, x1: x, y1: y + 1 })
        ) {
          return true;
        }
        runStart = -1;
      }
    }
  }
  const verticalLimit = Math.max(8, Math.ceil(height * 0.35));
  for (let x = 0; x < width; x += 1) {
    let runStart = -1;
    for (let y = 0; y <= height; y += 1) {
      const active = y < height && highContrast[y * width + x];
      if (active && runStart < 0) runStart = y;
      if (!active && runStart >= 0) {
        if (
          y - runStart >= verticalLimit &&
          !strokeInsideTrustedWord({ x0: x, y0: runStart, x1: x + 1, y1: y })
        ) {
          return true;
        }
        runStart = -1;
      }
    }
  }

  const visited = new Uint8Array(highContrast.length);
  const minimumComponentSize = Math.max(8, Math.ceil(highContrast.length * 0.0125));
  const minimumOutsideComponentSize = Math.max(
    4,
    Math.ceil(highContrast.length * 0.0025),
  );
  for (let start = 0; start < highContrast.length; start += 1) {
    if (!highContrast[start] || visited[start]) continue;
    const queue = [start];
    visited[start] = 1;
    let cursor = 0;
    let count = 0;
    let minX = width;
    let maxX = -1;
    let minY = height;
    let maxY = -1;
    while (cursor < queue.length) {
      const pixel = queue[cursor++];
      const x = pixel % width;
      const y = Math.floor(pixel / width);
      count += 1;
      minX = Math.min(minX, x);
      maxX = Math.max(maxX, x);
      minY = Math.min(minY, y);
      maxY = Math.max(maxY, y);
      for (let nextY = Math.max(0, y - 1); nextY <= Math.min(height - 1, y + 1); nextY += 1) {
        for (let nextX = Math.max(0, x - 1); nextX <= Math.min(width - 1, x + 1); nextX += 1) {
          const next = nextY * width + nextX;
          if (!highContrast[next] || visited[next]) continue;
          visited[next] = 1;
          queue.push(next);
        }
      }
    }
    const componentBounds = { x0: minX, y0: minY, x1: maxX + 1, y1: maxY + 1 };
    const componentLine = trustedLinePixelBoxes.find((box) =>
      pixelBoundsContain(box, componentBounds, 1),
    );
    const componentWord = trustedWordPixelBoxes.find((box) =>
      pixelBoundsContain(box, componentBounds, 1),
    );
    if (
      trustedLinePixelBoxes.length > 0 &&
      !componentLine &&
      count >= minimumOutsideComponentSize
    ) {
      return true;
    }
    if (
      trustedTextGeometry?.allowContainedWordGlyphs &&
      !componentWord &&
      count >= minimumOutsideComponentSize
    ) {
      return true;
    }
    const comparisonWidth = componentLine
      ? Math.max(1, componentLine.x1 - componentLine.x0)
      : width;
    const comparisonHeight = componentLine
      ? Math.max(1, componentLine.y1 - componentLine.y0)
      : height;
    const spansWidth = (maxX - minX + 1) / comparisonWidth >= 0.3;
    const spansHeight = (maxY - minY + 1) / comparisonHeight >= 0.3;
    // Ring evidence can only exempt a glyph fully contained by authoritative
    // word geometry. A line box alone is insufficient because it can also
    // contain an axis, formula stroke, or icon.
    if (
      count >= minimumComponentSize &&
      spansWidth &&
      spansHeight &&
      !(trustedTextGeometry?.allowContainedWordGlyphs && componentWord)
    ) {
      return true;
    }
  }
  return false;
}

function chooseForeground(
  background: RgbColor,
  minimumContrastRatio: number,
): { color: RgbColor; ratio: number } {
  const softCandidates = [SOFT_DARK, SOFT_LIGHT]
    .map((color) => ({ color, ratio: wcagContrastRatio(color, background) }))
    .sort((left, right) => right.ratio - left.ratio);
  if (softCandidates[0].ratio >= minimumContrastRatio) return softCandidates[0];

  return [BLACK, WHITE]
    .map((color) => ({ color, ratio: wcagContrastRatio(color, background) }))
    .sort((left, right) => right.ratio - left.ratio)[0];
}

function relativeLuminance(color: RgbColor): number {
  const linear = [color.r, color.g, color.b].map((channel) => {
    const value = channel / 255;
    return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2];
}

function cssRgb(color: RgbColor): string {
  return `rgb(${color.r}, ${color.g}, ${color.b})`;
}

function validNormalizedBox(box: NormalizedPdfBox): boolean {
  return (
    [box.x0, box.y0, box.x1, box.y1].every(Number.isFinite) &&
    box.x0 >= 0 &&
    box.y0 >= 0 &&
    box.x1 <= 1 &&
    box.y1 <= 1 &&
    box.x1 > box.x0 &&
    box.y1 > box.y0
  );
}

function boxContainsWithTolerance(
  outer: NormalizedPdfBox,
  inner: NormalizedPdfBox,
): boolean {
  const epsilon = 0.003;
  return (
    inner.x0 >= outer.x0 - epsilon &&
    inner.y0 >= outer.y0 - epsilon &&
    inner.x1 <= outer.x1 + epsilon &&
    inner.y1 <= outer.y1 + epsilon
  );
}

function boxesOverlap(left: NormalizedPdfBox, right: NormalizedPdfBox): boolean {
  return (
    Math.min(left.x1, right.x1) > Math.max(left.x0, right.x0) &&
    Math.min(left.y1, right.y1) > Math.max(left.y0, right.y0)
  );
}

function boxRelativeToRegion(
  region: NormalizedPdfBox,
  box: NormalizedPdfBox,
): NormalizedPdfBox {
  const width = region.x1 - region.x0;
  const height = region.y1 - region.y0;
  return {
    x0: Math.max(0, Math.min(1, (box.x0 - region.x0) / width)),
    y0: Math.max(0, Math.min(1, (box.y0 - region.y0) / height)),
    x1: Math.max(0, Math.min(1, (box.x1 - region.x0) / width)),
    y1: Math.max(0, Math.min(1, (box.y1 - region.y0) / height)),
  };
}

type PixelBounds = Readonly<{
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}>;

function normalizedBoxToPixelBounds(
  box: NormalizedPdfBox,
  width: number,
  height: number,
): PixelBounds {
  return {
    x0: Math.max(0, Math.floor(box.x0 * width)),
    y0: Math.max(0, Math.floor(box.y0 * height)),
    x1: Math.min(width, Math.ceil(box.x1 * width)),
    y1: Math.min(height, Math.ceil(box.y1 * height)),
  };
}

function pixelBoundsContain(
  outer: PixelBounds,
  inner: PixelBounds,
  tolerance: number,
): boolean {
  return (
    inner.x0 >= outer.x0 - tolerance &&
    inner.y0 >= outer.y0 - tolerance &&
    inner.x1 <= outer.x1 + tolerance &&
    inner.y1 <= outer.y1 + tolerance
  );
}

function failedClassification(
  evidence: Exclude<PdfBackgroundEvidence, "uniform">,
  reason: PdfBackgroundFailureReason,
  sampleCount = 0,
  dominantRatio = 0,
  maxChannelDelta = 0,
  cornerDominantRatio = 0,
): PdfBackgroundClassification {
  return {
    evidence,
    reason,
    background: null,
    backgroundColor: null,
    foregroundColor: null,
    contrastRatio: null,
    sampleCount,
    dominantRatio,
    cornerDominantRatio,
    maxChannelDelta,
  };
}
