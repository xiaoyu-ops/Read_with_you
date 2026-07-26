import type { TranslationLayoutPage } from "./api";


export const DEFAULT_PDF_PAGE_WINDOW_RADIUS = 2;
export const DEFAULT_PDF_PAGE_MAX_WIDTH_PX = 960;
export const DEFAULT_PDF_CANVAS_MAX_PIXELS = 8_000_000;
export const DEFAULT_PDF_CANVAS_MAX_DIMENSION = 8_192;

export type PdfPageCssSize = {
  widthPx: number;
  heightPx: number;
};

export type PdfPageWindowInput = {
  visiblePages?: Iterable<number> | null;
  currentPage?: number | null;
  pageCount: number;
  radius?: number;
};

export type PdfCanvasOutputScaleInput = {
  widthPx: number;
  heightPx: number;
  devicePixelRatio?: number;
  maxPixels?: number;
  maxDimension?: number;
};

/**
 * Keep PDF canvas backing stores bounded without changing CSS or overlay geometry.
 * High-DPR/high-zoom pages are rendered at the best scale that fits both caps.
 */
export function getPdfCanvasOutputScale({
  widthPx,
  heightPx,
  devicePixelRatio = 1,
  maxPixels = DEFAULT_PDF_CANVAS_MAX_PIXELS,
  maxDimension = DEFAULT_PDF_CANVAS_MAX_DIMENSION,
}: PdfCanvasOutputScaleInput): number {
  if (!validSourceSize(widthPx, heightPx)) return 1;
  const dpr = Number.isFinite(devicePixelRatio) && devicePixelRatio > 0
    ? devicePixelRatio
    : 1;
  const pixelCap = Number.isFinite(maxPixels) && maxPixels > 0
    ? maxPixels
    : DEFAULT_PDF_CANVAS_MAX_PIXELS;
  const dimensionCap = Number.isFinite(maxDimension) && maxDimension > 0
    ? maxDimension
    : DEFAULT_PDF_CANVAS_MAX_DIMENSION;
  const areaScale = Math.exp(
    (Math.log(pixelCap) - Math.log(widthPx) - Math.log(heightPx)) / 2,
  );
  const widthScale = dimensionCap / widthPx;
  const heightScale = dimensionCap / heightPx;
  return Math.min(dpr, areaScale, widthScale, heightScale);
}

/**
 * Expand every visible/current page into a bounded render window.
 *
 * Page-like finite values are truncated and clamped into the document. When
 * no page is available yet (for example before IntersectionObserver fires),
 * page 1 seeds the initial window so the reader never renders an empty view.
 */
export function getPdfPageRenderWindow({
  visiblePages,
  currentPage,
  pageCount,
  radius = DEFAULT_PDF_PAGE_WINDOW_RADIUS,
}: PdfPageWindowInput): number[] {
  const count = normalizePageCount(pageCount);
  if (count === 0) return [];

  const roots = new Set<number>();
  if (visiblePages) {
    for (const page of visiblePages) {
      const normalized = normalizePage(page, count);
      if (normalized !== null) roots.add(normalized);
    }
  }
  const normalizedCurrent = normalizePage(currentPage, count);
  if (normalizedCurrent !== null) roots.add(normalizedCurrent);
  if (roots.size === 0) roots.add(1);

  const safeRadius = normalizeRadius(radius, count);
  const rendered = new Set<number>();
  for (const root of roots) {
    const start = Math.max(1, root - safeRadius);
    const end = Math.min(count, root + safeRadius);
    for (let page = start; page <= end; page += 1) rendered.add(page);
  }
  return [...rendered].sort((left, right) => left - right);
}

/**
 * Calculate stable 100% page-shell sizes using one shared document scale.
 *
 * The widest valid source page is fitted into both the available container
 * width and a finite maximum width. Applying the same scale to every page
 * preserves each page's aspect ratio and relative physical width.
 */
export function getPdfPageCssSizes(
  pages: readonly TranslationLayoutPage[],
  availableWidthPx: number,
  maxWidthPx: number = DEFAULT_PDF_PAGE_MAX_WIDTH_PX,
): Record<number, PdfPageCssSize> {
  if (!Number.isFinite(availableWidthPx) || availableWidthPx <= 0) return {};

  const finiteMaxWidth = Number.isFinite(maxWidthPx) && maxWidthPx > 0
    ? maxWidthPx
    : DEFAULT_PDF_PAGE_MAX_WIDTH_PX;
  const targetWidth = Math.min(availableWidthPx, finiteMaxWidth);
  if (!Number.isFinite(targetWidth) || targetWidth <= 0) return {};

  const validPages: TranslationLayoutPage[] = [];
  const seenPages = new Set<number>();
  let widestSourcePage = 0;
  for (const page of pages) {
    if (
      !Number.isInteger(page.page) ||
      page.page <= 0 ||
      seenPages.has(page.page) ||
      !validSourceSize(page.width, page.height)
    ) {
      continue;
    }
    const aspectRatio = page.height / page.width;
    if (!Number.isFinite(aspectRatio) || aspectRatio <= 0) continue;
    seenPages.add(page.page);
    validPages.push(page);
    widestSourcePage = Math.max(widestSourcePage, page.width);
  }
  if (!Number.isFinite(widestSourcePage) || widestSourcePage <= 0) return {};

  const scale = targetWidth / widestSourcePage;
  if (!Number.isFinite(scale) || scale <= 0) return {};

  const sizes: Record<number, PdfPageCssSize> = {};
  for (const page of validPages) {
    const widthPx = page.width * scale;
    const heightPx = page.height * scale;
    if (!validSourceSize(widthPx, heightPx)) continue;
    sizes[page.page] = {
      widthPx: roundCssPixel(widthPx),
      heightPx: roundCssPixel(heightPx),
    };
  }
  return sizes;
}

function normalizePageCount(value: number): number {
  if (!Number.isFinite(value) || value <= 0) return 0;
  return Math.max(0, Math.floor(value));
}

function normalizePage(value: number | null | undefined, pageCount: number): number | null {
  if (typeof value !== "number" || !Number.isFinite(value)) return null;
  const integer = Math.trunc(value);
  return Math.min(pageCount, Math.max(1, integer));
}

function normalizeRadius(value: number, pageCount: number): number {
  if (!Number.isFinite(value)) return DEFAULT_PDF_PAGE_WINDOW_RADIUS;
  return Math.min(pageCount - 1, Math.max(0, Math.floor(value)));
}

function validSourceSize(width: number, height: number): boolean {
  return Number.isFinite(width) && Number.isFinite(height) && width > 0 && height > 0;
}

function roundCssPixel(value: number): number {
  return Math.round(value * 1000) / 1000;
}
