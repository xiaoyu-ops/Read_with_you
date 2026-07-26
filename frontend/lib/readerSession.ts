export const READER_SESSION_VERSION = 2 as const;
export const READER_SESSION_PREFIX = "peinidu.readerSession";
export const PDF_ZOOM_MIN = 25;
export const PDF_ZOOM_DEFAULT = 100;
export const PDF_ZOOM_MAX = 200;
const TRACKPAD_PINCH_SENSITIVITY = 0.0025;

export type ReaderInspectorState = {
  blockIndex: number;
  regionId: string | null;
  content: "original" | "translation";
};

export type ReaderSessionV2 = {
  version: typeof READER_SESSION_VERSION;
  paperId: string;
  readerMode: "selection_translation";
  activeIndex: number | null;
  pdfPage: number;
  pdfScrollTop: number;
  pdfZoomPercent: number;
  inspector: ReaderInspectorState | null;
  updatedAt: string;
};

export type ReaderSessionContext = {
  paperId: string;
  blockIndexes: readonly number[];
  pageCount?: number;
  validRegionIds?: ReadonlySet<string>;
  now?: () => string;
};

export type ReaderSessionStorage = Pick<Storage, "getItem" | "setItem">;

export function readerSessionKey(paperId: string): string {
  return `${READER_SESSION_PREFIX}.${paperId}`;
}

export function clampPdfZoomPercent(value: unknown): number {
  const zoom = typeof value === "number" && Number.isFinite(value)
    ? Math.round(value * 10) / 10
    : PDF_ZOOM_DEFAULT;
  return Math.min(PDF_ZOOM_MAX, Math.max(PDF_ZOOM_MIN, zoom));
}

export function getTrackpadPinchZoomPercent(
  currentZoomPercent: number,
  deltaY: number,
): number {
  if (!Number.isFinite(deltaY)) return clampPdfZoomPercent(currentZoomPercent);
  const boundedDelta = Math.min(100, Math.max(-100, deltaY));
  return clampPdfZoomPercent(
    currentZoomPercent * Math.exp(-boundedDelta * TRACKPAD_PINCH_SENSITIVITY),
  );
}

export function createDefaultReaderSession(
  paperId: string,
  now: () => string = defaultNow,
): ReaderSessionV2 {
  return {
    version: READER_SESSION_VERSION,
    paperId,
    readerMode: "selection_translation",
    activeIndex: null,
    pdfPage: 1,
    pdfScrollTop: 0,
    pdfZoomPercent: PDF_ZOOM_DEFAULT,
    inspector: null,
    updatedAt: now(),
  };
}

/** Parse v2 or migrate the previous dual-pane v1 state into a canonical v2 object. */
export function parseReaderSession(
  value: unknown,
  context: ReaderSessionContext,
): ReaderSessionV2 {
  const now = context.now ?? defaultNow;
  const fallback = createDefaultReaderSession(context.paperId, now);
  if (!isRecord(value)) return fallback;
  if (typeof value.paperId === "string" && value.paperId !== context.paperId) return fallback;
  if (value.version !== 2 && value.version !== 1 && value.version !== undefined) return fallback;

  const blockIndexes = new Set(context.blockIndexes);
  const activeIndex = validBlockIndex(value.activeIndex, blockIndexes) ? value.activeIndex : null;
  const session: ReaderSessionV2 = {
    version: READER_SESSION_VERSION,
    paperId: context.paperId,
    readerMode: "selection_translation",
    activeIndex,
    pdfPage: clampPage(value.pdfPage, context.pageCount),
    pdfScrollTop: value.version === 2 ? nonNegativeNumber(value.pdfScrollTop, 0) : 0,
    pdfZoomPercent: clampPdfZoomPercent(value.pdfZoomPercent),
    inspector: null,
    updatedAt: typeof value.updatedAt === "string" && value.updatedAt
      ? value.updatedAt
      : now(),
  };
  return session;
}

export function parseStoredReaderSession(
  raw: string | null,
  context: ReaderSessionContext,
): ReaderSessionV2 {
  if (!raw) return createDefaultReaderSession(context.paperId, context.now ?? defaultNow);
  try {
    return parseReaderSession(JSON.parse(raw) as unknown, context);
  } catch {
    return createDefaultReaderSession(context.paperId, context.now ?? defaultNow);
  }
}

/** Serialize only canonical v2 fields; legacy right-pane fields can never leak back. */
export function serializeReaderSession(session: ReaderSessionV2): string {
  const canonical: ReaderSessionV2 = {
    version: READER_SESSION_VERSION,
    paperId: session.paperId,
    readerMode: "selection_translation",
    activeIndex: session.activeIndex,
    pdfPage: session.pdfPage,
    pdfScrollTop: session.pdfScrollTop,
    pdfZoomPercent: session.pdfZoomPercent,
    inspector: session.inspector
      ? {
          blockIndex: session.inspector.blockIndex,
          regionId: session.inspector.regionId,
          content: session.inspector.content,
        }
      : null,
    updatedAt: session.updatedAt,
  };
  return JSON.stringify(canonical);
}

export function loadReaderSession(
  context: ReaderSessionContext,
  storage: ReaderSessionStorage | null = browserStorage(),
): ReaderSessionV2 {
  if (!storage) return createDefaultReaderSession(context.paperId, context.now ?? defaultNow);
  try {
    return parseStoredReaderSession(storage.getItem(readerSessionKey(context.paperId)), context);
  } catch {
    return createDefaultReaderSession(context.paperId, context.now ?? defaultNow);
  }
}

export function saveReaderSession(
  session: ReaderSessionV2,
  storage: ReaderSessionStorage | null = browserStorage(),
): boolean {
  if (!storage) return false;
  try {
    storage.setItem(readerSessionKey(session.paperId), serializeReaderSession(session));
    return true;
  } catch {
    return false;
  }
}

export function updateReaderSession(
  session: ReaderSessionV2,
  patch: Partial<Omit<ReaderSessionV2, "version" | "paperId" | "readerMode">>,
  context: ReaderSessionContext,
): ReaderSessionV2 {
  const now = context.now ?? defaultNow;
  return parseReaderSession(
    {
      ...session,
      ...patch,
      version: READER_SESSION_VERSION,
      paperId: context.paperId,
      readerMode: "selection_translation",
      updatedAt: now(),
    },
    context,
  );
}

function parseInspector(
  value: unknown,
  blockIndexes: ReadonlySet<number>,
  validRegionIds?: ReadonlySet<string>,
): ReaderInspectorState | null {
  if (!isRecord(value) || !validBlockIndex(value.blockIndex, blockIndexes)) return null;
  const regionId = value.regionId === null
    ? null
    : typeof value.regionId === "string" && value.regionId
      ? value.regionId
      : null;
  if (regionId !== null && validRegionIds && !validRegionIds.has(regionId)) return null;
  if (value.content !== "original" && value.content !== "translation") return null;
  return { blockIndex: value.blockIndex, regionId, content: value.content };
}

function clampPage(value: unknown, pageCount?: number): number {
  const page = typeof value === "number" && Number.isFinite(value)
    ? Math.max(1, Math.floor(value))
    : 1;
  if (typeof pageCount !== "number" || !Number.isFinite(pageCount) || pageCount <= 0) {
    return page;
  }
  return Math.min(page, Math.max(1, Math.floor(pageCount)));
}

function nonNegativeNumber(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value)
    ? Math.max(0, value)
    : fallback;
}

function validBlockIndex(value: unknown, blockIndexes: ReadonlySet<number>): value is number {
  return typeof value === "number" && Number.isInteger(value) && blockIndexes.has(value);
}

function browserStorage(): ReaderSessionStorage | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

function defaultNow(): string {
  return new Date().toISOString();
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
