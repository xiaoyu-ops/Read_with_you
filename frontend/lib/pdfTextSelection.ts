import type { NormalizedPdfBox, TranslationLayoutRegion } from "./api";


export const PDF_TEXT_SELECTION_VERSION = 2 as const;
export const PDF_TEXT_SELECTION_MAX_CHARS = 4_000;
const QUOTE_CONTEXT_CHARS = 32;
const MIN_RECT_REGION_COVERAGE = 0.55;
const MIN_UNIQUE_SCORE_GAP = 0.12;

export type PdfTextItemAnchor = {
  item_index: number;
  char_offset: number;
};

export type PdfTextSelectionQuote = {
  exact: string;
  prefix: string;
  suffix: string;
};

export type PdfTextSelectionMapping = {
  block_index: number | null;
  region_id: string | null;
  layout_confidence: number | null;
};

export type PdfTextSelection = PdfTextSelectionMapping & {
  version: typeof PDF_TEXT_SELECTION_VERSION;
  source_pdf_sha256: string;
  page: number;
  raw_text: string;
  text_sha256: string;
  start: PdfTextItemAnchor;
  end: PdfTextItemAnchor;
  quote: PdfTextSelectionQuote;
  rects: NormalizedPdfBox[];
};

type RectLike = Pick<DOMRectReadOnly, "left" | "top" | "right" | "bottom" | "width" | "height">;

export type PdfTextSelectionResult =
  | { selection: PdfTextSelection; clientRect: DOMRect }
  | { error: string };

export async function serializePdfTextSelection({
  range,
  textLayer,
  page,
  sourcePdfSha256,
  regions,
}: {
  range: Range;
  textLayer: HTMLElement;
  page: number;
  sourcePdfSha256: string;
  regions: readonly TranslationLayoutRegion[];
}): Promise<PdfTextSelectionResult> {
  if (!Number.isInteger(page) || page <= 0 || !/^[a-f0-9]{64}$/i.test(sourcePdfSha256)) {
    return { error: "当前 PDF 证据无效，请重新载入论文。" };
  }
  if (range.collapsed || !textLayer.contains(range.startContainer) || !textLayer.contains(range.endContainer)) {
    return { error: "请在同一页原文中选择一段连续文字。" };
  }

  const start = textItemAnchor(textLayer, range.startContainer, range.startOffset);
  const end = textItemAnchor(textLayer, range.endContainer, range.endOffset);
  if (!start || !end || compareAnchors(start, end) > 0) {
    return { error: "当前选区无法稳定定位，请重新选择。" };
  }
  const rebuilt = rangeFromTextItemAnchors(textLayer, start, end);
  if (!rebuilt) return { error: "当前选区无法稳定重建，请重新选择。" };

  const rawText = range.toString();
  if (rawText !== rebuilt.toString()) {
    return { error: "选中文字与 PDF 文字层不一致，请重新选择。" };
  }
  if (rawText.trim().length < 2) return { error: "请至少选择两个有效字符。" };
  if (rawText.length > PDF_TEXT_SELECTION_MAX_CHARS) {
    return { error: `选区过长，请缩短到 ${PDF_TEXT_SELECTION_MAX_CHARS} 个字符以内。` };
  }

  const pageRect = textLayer.getBoundingClientRect();
  const rects = normalizePdfSelectionRects(Array.from(range.getClientRects()), pageRect);
  if (rects.length === 0) return { error: "当前选区没有可靠的页面坐标，请重新选择。" };
  const clientRect = range.getBoundingClientRect();
  if (!validRect(clientRect)) return { error: "当前选区没有可靠的页面坐标，请重新选择。" };

  const itemStrings = textItemStrings(textLayer);
  if (!anchorsMatchTextItems(itemStrings, start, end)) {
    return { error: "PDF 文字层已变化，请重新选择。" };
  }
  const quote = buildQuote(itemStrings, start, end, rawText);
  const mapping = mapSelectionToLayout(rects, regions, page);
  return {
    selection: {
      version: PDF_TEXT_SELECTION_VERSION,
      source_pdf_sha256: sourcePdfSha256.toLowerCase(),
      page,
      raw_text: rawText,
      text_sha256: await sha256Text(rawText),
      start,
      end,
      quote,
      rects,
      ...mapping,
    },
    clientRect,
  };
}

export function normalizePdfSelectionRects(
  rects: readonly RectLike[],
  pageRect: RectLike,
): NormalizedPdfBox[] {
  if (!validRect(pageRect)) return [];
  const normalized: NormalizedPdfBox[] = [];
  for (const rect of rects) {
    if (!validRect(rect)) continue;
    const x0 = clamp01((rect.left - pageRect.left) / pageRect.width);
    const y0 = clamp01((rect.top - pageRect.top) / pageRect.height);
    const x1 = clamp01((rect.right - pageRect.left) / pageRect.width);
    const y1 = clamp01((rect.bottom - pageRect.top) / pageRect.height);
    if (x1 <= x0 || y1 <= y0) continue;
    const next = { x0: roundCoordinate(x0), y0: roundCoordinate(y0), x1: roundCoordinate(x1), y1: roundCoordinate(y1) };
    const previous = normalized.at(-1);
    if (!previous || !sameBox(previous, next)) normalized.push(next);
  }
  return normalized;
}

export function mapSelectionToLayout(
  rects: readonly NormalizedPdfBox[],
  regions: readonly TranslationLayoutRegion[],
  page: number,
): PdfTextSelectionMapping {
  const pageRegions = regions.filter((region) => region.page === page && validBox(region.bbox));
  if (rects.length === 0 || pageRegions.length === 0) return emptyMapping();

  const matched: Array<{ region: TranslationLayoutRegion; score: number }> = [];
  for (const rect of rects) {
    if (!validBox(rect)) return emptyMapping();
    const candidates = pageRegions
      .map((region) => ({ region, score: intersectionArea(rect, region.bbox) / boxArea(rect) }))
      .filter((candidate) => candidate.score >= MIN_RECT_REGION_COVERAGE)
      .sort((left, right) => right.score - left.score);
    if (candidates.length === 0) return emptyMapping();
    if (
      candidates.length > 1 &&
      candidates[0].score - candidates[1].score < MIN_UNIQUE_SCORE_GAP
    ) {
      return emptyMapping();
    }
    matched.push(candidates[0]);
  }

  const blockIndexes = new Set(matched.map(({ region }) => region.block_index));
  if (blockIndexes.size !== 1) return emptyMapping();
  const blockIndex = matched[0].region.block_index;
  const regionIds = new Set(matched.map(({ region }) => region.region_id));
  return {
    block_index: blockIndex,
    region_id: regionIds.size === 1 ? matched[0].region.region_id : null,
    layout_confidence: Math.min(...matched.map(({ region, score }) => Math.min(region.confidence, score))),
  };
}

export function rangeFromTextItemAnchors(
  textLayer: HTMLElement,
  start: PdfTextItemAnchor,
  end: PdfTextItemAnchor,
): Range | null {
  const startSpan = textItemElement(textLayer, start.item_index);
  const endSpan = textItemElement(textLayer, end.item_index);
  if (!startSpan || !endSpan) return null;
  const startPoint = textPositionAtOffset(startSpan, start.char_offset);
  const endPoint = textPositionAtOffset(endSpan, end.char_offset);
  if (!startPoint || !endPoint) return null;
  const range = textLayer.ownerDocument.createRange();
  try {
    range.setStart(startPoint.node, startPoint.offset);
    range.setEnd(endPoint.node, endPoint.offset);
  } catch {
    return null;
  }
  return range.collapsed ? null : range;
}

export async function sha256Text(value: string): Promise<string> {
  const cryptoApi = globalThis.crypto;
  if (!cryptoApi?.subtle) throw new Error("Web Crypto unavailable");
  const digest = await cryptoApi.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function textItemAnchor(
  textLayer: HTMLElement,
  node: Node,
  offset: number,
): PdfTextItemAnchor | null {
  const element = node.nodeType === Node.ELEMENT_NODE
    ? node as Element
    : node.parentElement;
  const span = element?.closest<HTMLElement>("[data-text-item-index]");
  if (!span || !textLayer.contains(span)) return null;
  const itemIndex = Number(span.dataset.textItemIndex);
  if (!Number.isInteger(itemIndex) || itemIndex < 0) return null;
  const prefix = textLayer.ownerDocument.createRange();
  prefix.selectNodeContents(span);
  try {
    prefix.setEnd(node, offset);
  } catch {
    return null;
  }
  const charOffset = prefix.toString().length;
  if (charOffset < 0 || charOffset > (span.textContent ?? "").length) return null;
  return { item_index: itemIndex, char_offset: charOffset };
}

function textPositionAtOffset(
  target: HTMLElement,
  charOffset: number,
): { node: Text; offset: number } | null {
  if (!Number.isInteger(charOffset) || charOffset < 0) return null;
  const walker = target.ownerDocument.createTreeWalker(target, NodeFilter.SHOW_TEXT);
  let cursor = 0;
  let last: Text | null = null;
  while (walker.nextNode()) {
    const node = walker.currentNode as Text;
    last = node;
    const next = cursor + node.data.length;
    if (charOffset <= next) return { node, offset: charOffset - cursor };
    cursor = next;
  }
  if (last && charOffset === cursor) return { node: last, offset: last.data.length };
  return null;
}

function textItemStrings(textLayer: HTMLElement): string[] {
  return Array.from(textLayer.querySelectorAll<HTMLElement>("[data-text-item-index]"))
    .sort((left, right) => Number(left.dataset.textItemIndex) - Number(right.dataset.textItemIndex))
    .map((element) => element.textContent ?? "");
}

function anchorsMatchTextItems(
  items: readonly string[],
  start: PdfTextItemAnchor,
  end: PdfTextItemAnchor,
): boolean {
  return Boolean(
    items[start.item_index] !== undefined &&
    items[end.item_index] !== undefined &&
    start.char_offset <= items[start.item_index].length &&
    end.char_offset <= items[end.item_index].length,
  );
}

function buildQuote(
  items: readonly string[],
  start: PdfTextItemAnchor,
  end: PdfTextItemAnchor,
  exact: string,
): PdfTextSelectionQuote {
  const before = `${items.slice(0, start.item_index).join("")}${items[start.item_index].slice(0, start.char_offset)}`;
  const after = `${items[end.item_index].slice(end.char_offset)}${items.slice(end.item_index + 1).join("")}`;
  return {
    exact,
    prefix: before.slice(-QUOTE_CONTEXT_CHARS),
    suffix: after.slice(0, QUOTE_CONTEXT_CHARS),
  };
}

function textItemElement(textLayer: HTMLElement, itemIndex: number): HTMLElement | null {
  if (!Number.isInteger(itemIndex) || itemIndex < 0) return null;
  return textLayer.querySelector<HTMLElement>(`[data-text-item-index="${itemIndex}"]`);
}

function compareAnchors(left: PdfTextItemAnchor, right: PdfTextItemAnchor): number {
  return left.item_index - right.item_index || left.char_offset - right.char_offset;
}

function emptyMapping(): PdfTextSelectionMapping {
  return { block_index: null, region_id: null, layout_confidence: null };
}

function validRect(rect: RectLike): boolean {
  return [rect.left, rect.top, rect.right, rect.bottom, rect.width, rect.height].every(Number.isFinite) &&
    rect.width > 0 && rect.height > 0 && rect.right > rect.left && rect.bottom > rect.top;
}

function validBox(box: NormalizedPdfBox): boolean {
  return [box.x0, box.y0, box.x1, box.y1].every(Number.isFinite) &&
    box.x0 >= 0 && box.y0 >= 0 && box.x1 <= 1 && box.y1 <= 1 && box.x1 > box.x0 && box.y1 > box.y0;
}

function boxArea(box: NormalizedPdfBox): number {
  return (box.x1 - box.x0) * (box.y1 - box.y0);
}

function intersectionArea(left: NormalizedPdfBox, right: NormalizedPdfBox): number {
  return Math.max(0, Math.min(left.x1, right.x1) - Math.max(left.x0, right.x0)) *
    Math.max(0, Math.min(left.y1, right.y1) - Math.max(left.y0, right.y0));
}

function clamp01(value: number): number {
  return Math.min(1, Math.max(0, value));
}

function roundCoordinate(value: number): number {
  return Math.round(value * 1_000_000) / 1_000_000;
}

function sameBox(left: NormalizedPdfBox, right: NormalizedPdfBox): boolean {
  return left.x0 === right.x0 && left.y0 === right.y0 && left.x1 === right.x1 && left.y1 === right.y1;
}
