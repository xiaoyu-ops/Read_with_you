"use client";

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type {
  KeyboardEvent as ReactKeyboardEvent,
  PointerEvent as ReactPointerEvent,
} from "react";
import type {
  PDFDocumentLoadingTask,
  PDFDocumentProxy,
  PDFPageProxy,
  RenderTask,
} from "pdfjs-dist";

import { CollectionPicker } from "./CollectionPicker";
import { PdfExportControl } from "./PdfExportControl";
import {
  API_BASE,
  createAnnotation,
  deleteAnnotation,
  getPaperNote,
  getTranslationLayout,
  listAnnotations,
  PaperNoteRevisionConflictError,
  savePaperNote,
  SelectionTranslationRequestError,
  translateSelection,
  updateAnnotation,
  type Annotation,
  type AnnotationKind,
  type NormalizedPdfBox,
  type PaperNote,
  type PaperDetail,
  type TranslationLayout,
  type TranslationLayoutRegion,
  type SelectionTranslationResponse,
} from "@/lib/api";
import {
  buildReaderAgentContext,
  type PetQuestionRequest,
  type ReaderAgentContext,
  type ReaderLocationContext,
} from "@/lib/readerContext";
import {
  getReaderEvidenceHint,
  type ReaderNavigationRequest,
} from "@/lib/readerEvidence";
import {
  DEFAULT_PDF_PAGE_MAX_WIDTH_PX,
  getPdfCanvasOutputScale,
  getPdfPageCssSizes,
  getPdfPageRenderWindow,
  type PdfPageCssSize,
} from "@/lib/pdfVirtualization";
import { resolvePdfAssetUrl } from "@/lib/pdfAssetUrl";
import {
  rangeFromTextItemAnchors,
  serializePdfTextSelection,
  sha256Text,
  type PdfTextSelection,
} from "@/lib/pdfTextSelection";
import {
  clampPdfZoomPercent,
  getTrackpadPinchZoomPercent,
  loadReaderSession,
  PDF_ZOOM_MAX,
  PDF_ZOOM_MIN,
  saveReaderSession,
  updateReaderSession,
  type ReaderSessionContext,
  type ReaderSessionV2,
} from "@/lib/readerSession";

type PdfJsModule = typeof import("pdfjs-dist");
type PdfDocumentPreloadResult = {
  document: PDFDocumentProxy;
  loadingTask: PDFDocumentLoadingTask;
  pdfjs: PdfJsModule;
};
type PdfDocumentPreload = {
  paperId: string;
  url: string;
  promise: Promise<PdfDocumentPreloadResult>;
  loadingTask?: PDFDocumentLoadingTask;
  consumers?: number;
  cleanupTimer?: number;
};
type WindowWithPdfDocumentPreload = Window & {
  __petPdfDocumentPreload?: PdfDocumentPreload;
  __petPdfDocumentLoadCounts?: Record<string, number>;
  __petPdfDocumentLoadTrace?: Array<{ url: string; source: string }>;
  __petPdfDocumentPreloadTrace?: Array<Record<string, unknown>>;
};

const PDF_ZOOM_STEP = 10;
const PDF_PAGE_GAP_PX = 20;
const READER_SPLIT_STORAGE_KEY = "peinidu.readerSplitRatio.v1";
const READER_FIT_MODE_STORAGE_KEY = "peinidu.readerFitMode.v1";
const DEFAULT_READER_SPLIT_RATIO = 60;
const MIN_READER_SPLIT_RATIO = 40;
const MAX_READER_SPLIT_RATIO = 72;
// Two-thirds resolution keeps 10pt+ paper text readable for the first paint;
// the same canvas is upgraded to full device resolution immediately after it.
const INITIAL_PAGE_PREVIEW_OUTPUT_SCALE = 0.67;
const ANNOTATION_KINDS: readonly AnnotationKind[] = [
  "highlight",
  "important",
  "question",
  "method",
  "conclusion",
];
const ANNOTATION_KIND_LABELS: Record<AnnotationKind, string> = {
  highlight: "普通",
  important: "重要",
  question: "疑问",
  method: "方法",
  conclusion: "结论",
};
const INLINE_ANNOTATION_HIGHLIGHTS: Record<AnnotationKind, string> = {
  highlight: "reader-inline-annotation",
  important: "reader-inline-annotation-important",
  question: "reader-inline-annotation-question",
  method: "reader-inline-annotation-method",
  conclusion: "reader-inline-annotation-conclusion",
};

type PinchZoomAnchor = {
  page: number;
  xRatio: number;
  yRatio: number;
  clientX: number;
  clientY: number;
};
const inlineAnnotationRanges = new Map<AnnotationKind, Set<Range>>(
  ANNOTATION_KINDS.map((kind) => [kind, new Set<Range>()]),
);
const EMPTY_ANNOTATIONS: readonly Annotation[] = [];

type SelectionTranslationPanel = {
  selection: PdfTextSelection;
  originalSourceText: string;
  sourceText: string;
  status: "loading" | "done" | "error" | "cancelled";
  result: SelectionTranslationResponse | null;
  error: string | null;
  retryable: boolean;
};

function syncInlineAnnotationHighlights(): void {
  if (
    typeof CSS === "undefined" ||
    !("highlights" in CSS) ||
    typeof Highlight === "undefined"
  ) {
    return;
  }
  for (const kind of ANNOTATION_KINDS) {
    const name = INLINE_ANNOTATION_HIGHLIGHTS[kind];
    const ranges = inlineAnnotationRanges.get(kind);
    if (!ranges || ranges.size === 0) {
      CSS.highlights.delete(name);
      continue;
    }
    CSS.highlights.set(name, new Highlight(...ranges));
  }
}

let pdfJsModule: Promise<PdfJsModule> | null = null;
const PDF_JS_MODULE_URL = "/pdfjs/pdf-5.6.205.min.js";
const PDF_JS_WORKER_URL = "/pdfjs/pdf.worker-5.6.205.min.js";

function loadPdfJs(): Promise<PdfJsModule> {
  if (!pdfJsModule) {
    const pending = import(/* webpackIgnore: true */ PDF_JS_MODULE_URL).then((pdfjs) => {
      const module = pdfjs as PdfJsModule;
      module.GlobalWorkerOptions.workerSrc = PDF_JS_WORKER_URL;
      return module;
    });
    const cached = pending.catch((error) => {
      pdfJsModule = null;
      throw error;
    });
    pdfJsModule = cached;
  }
  return pdfJsModule;
}

function preloadPdfRuntime(): void {
  if (typeof document === "undefined") return;
  if (!document.querySelector(`link[href="${PDF_JS_WORKER_URL}"]`)) {
    const workerPreload = document.createElement("link");
    workerPreload.rel = "modulepreload";
    workerPreload.href = PDF_JS_WORKER_URL;
    document.head.appendChild(workerPreload);
  }
  void loadPdfJs().catch(() => undefined);
}

if (typeof window !== "undefined") preloadPdfRuntime();

function borrowPdfDocumentPreload(
  paperId: string,
  url: string,
): PdfDocumentPreload | null {
  if (typeof window === "undefined") return null;
  const target = window as WindowWithPdfDocumentPreload;
  const preload = target.__petPdfDocumentPreload;
  if (!preload || preload.paperId !== paperId || preload.url !== url) {
    if (process.env.NODE_ENV !== "production") {
      const trace = target.__petPdfDocumentPreloadTrace ?? [];
      trace.push({
        event: "borrow_miss",
        paperId,
        url,
        currentPaperId: preload?.paperId ?? null,
        currentUrl: preload?.url ?? null,
      });
      target.__petPdfDocumentPreloadTrace = trace;
    }
    return null;
  }
  if (preload.cleanupTimer !== undefined) {
    window.clearTimeout(preload.cleanupTimer);
    preload.cleanupTimer = undefined;
  }
  preload.consumers = (preload.consumers ?? 0) + 1;
  return preload;
}

function releasePdfDocumentPreload(preload: PdfDocumentPreload): void {
  if (typeof window === "undefined") return;
  const target = window as WindowWithPdfDocumentPreload;
  preload.consumers = Math.max(0, (preload.consumers ?? 1) - 1);
  if (preload.consumers > 0) return;
  // React StrictMode 会立即释放再接管同一任务；给下一次 effect 一个短交接窗口。
  preload.cleanupTimer = window.setTimeout(() => {
    if ((preload.consumers ?? 0) > 0) return;
    if (target.__petPdfDocumentPreload === preload) {
      delete target.__petPdfDocumentPreload;
    }
    safelyDestroyPdfDocumentPreload(preload);
  }, 250);
}

function safelyDestroyLoadingTask(
  loadingTask: PDFDocumentLoadingTask | null | undefined,
): void {
  if (!loadingTask) return;
  void loadingTask.destroy().catch(() => undefined);
}

function safelyDestroyPdfDocumentPreload(preload: PdfDocumentPreload): void {
  void preload.promise
    .then(({ document }) => document.destroy())
    .catch(async () => {
      try {
        await preload.loadingTask?.destroy();
      } catch {
        // 预加载已释放；销毁失败不应产生未处理的 Promise。
      }
    });
}

function recordPdfDocumentLoad(url: string, source: string): void {
  if (typeof window === "undefined" || process.env.NODE_ENV === "production") return;
  const target = window as WindowWithPdfDocumentPreload;
  const normalizedUrl = new URL(url, window.location.href).toString();
  const counts = target.__petPdfDocumentLoadCounts ?? {};
  counts[normalizedUrl] = (counts[normalizedUrl] ?? 0) + 1;
  target.__petPdfDocumentLoadCounts = counts;
  const trace = target.__petPdfDocumentLoadTrace ?? [];
  trace.push({ url: normalizedUrl, source });
  target.__petPdfDocumentLoadTrace = trace;
}

export function ensurePdfDocumentPreload(
  paperId: string,
  url: string,
): PdfDocumentPreload | null {
  if (typeof window === "undefined") return null;
  const target = window as WindowWithPdfDocumentPreload;
  const existing = target.__petPdfDocumentPreload;
  if (existing?.paperId === paperId && existing.url === url) return existing;
  if (existing && (existing.consumers ?? 0) === 0) {
    releasePdfDocumentPreload(existing);
  }
  const preload: PdfDocumentPreload = {
    paperId,
    url,
    consumers: 0,
    promise: Promise.resolve(null as unknown as PdfDocumentPreloadResult),
  };
  preload.promise = loadPdfJs().then(async (pdfjs) => {
    recordPdfDocumentLoad(url, "route_bridge");
    const loadingTask = pdfjs.getDocument({ url });
    preload.loadingTask = loadingTask;
    const document = await loadingTask.promise;
    return { document, loadingTask, pdfjs };
  });
  target.__petPdfDocumentPreload = preload;
  preload.cleanupTimer = window.setTimeout(() => {
    if (
      target.__petPdfDocumentPreload !== preload ||
      (preload.consumers ?? 0) > 0
    ) return;
    delete target.__petPdfDocumentPreload;
    safelyDestroyPdfDocumentPreload(preload);
  }, 120_000);
  void preload.promise.catch((error: unknown) => {
    if (process.env.NODE_ENV !== "production") {
      const trace = target.__petPdfDocumentPreloadTrace ?? [];
      trace.push({
        event: "route_bridge_rejected",
        url,
        message: error instanceof Error ? error.message : String(error),
      });
      target.__petPdfDocumentPreloadTrace = trace;
    }
    if (target.__petPdfDocumentPreload === preload) {
      delete target.__petPdfDocumentPreload;
    }
    safelyDestroyLoadingTask(preload.loadingTask);
  });
  return preload;
}

function clampReaderSplitRatio(value: number): number {
  if (!Number.isFinite(value)) return DEFAULT_READER_SPLIT_RATIO;
  return Math.min(MAX_READER_SPLIT_RATIO, Math.max(MIN_READER_SPLIT_RATIO, Math.round(value)));
}

function readerFitModeStorageKey(paperId: string): string {
  return `${READER_FIT_MODE_STORAGE_KEY}:${paperId}`;
}

export function InlinePdfReader({
  paper,
  onAgentContextChange,
  onAskPet,
  navigationRequest,
  onFirstPageReady,
}: {
  paper: PaperDetail;
  onAgentContextChange?: (context: ReaderAgentContext) => void;
  onAskPet?: (request: PetQuestionRequest) => void;
  navigationRequest?: ReaderNavigationRequest | null;
  onFirstPageReady?: () => void;
}) {
  const blockIndexes = useMemo(() => paper.blocks.map((block) => block.index), [paper.blocks]);
  const speculativePdfUrl = useMemo(
    () =>
      resolvePdfAssetUrl(
        `/assets/${encodeURIComponent(paper.arxiv_id)}/original.pdf`,
        API_BASE,
      ),
    [paper.arxiv_id],
  );
  const initialSessionRef = useRef<ReaderSessionV2 | null>(null);
  if (initialSessionRef.current === null) {
    initialSessionRef.current = loadReaderSession({ paperId: paper.arxiv_id, blockIndexes });
  }

  const initialSession = initialSessionRef.current;
  const sessionRef = useRef<ReaderSessionV2>(initialSession);
  const [activeIndex, setActiveIndex] = useState<number | null>(initialSession.activeIndex);
  const [activeRegionId, setActiveRegionId] = useState<string | null>(null);
  const [currentPage, setCurrentPage] = useState(initialSession.pdfPage);
  const [zoomPercent, setZoomPercent] = useState(initialSession.pdfZoomPercent);
  const [zoomInput, setZoomInput] = useState(String(initialSession.pdfZoomPercent));
  const [isFitMode, setIsFitMode] = useState(false);
  const [splitRatio, setSplitRatio] = useState(DEFAULT_READER_SPLIT_RATIO);
  const [layout, setLayout] = useState<TranslationLayout | null>(null);
  const [pdfDoc, setPdfDoc] = useState<PDFDocumentProxy | null>(null);
  const [pdfJs, setPdfJs] = useState<PdfJsModule | null>(null);
  const [initialPdfPageReady, setInitialPdfPageReady] = useState(false);
  const [layoutLoading, setLayoutLoading] = useState(true);
  const [readerError, setReaderError] = useState<string | null>(null);
  const [annotations, setAnnotations] = useState<Annotation[]>([]);
  const [annotationError, setAnnotationError] = useState<string | null>(null);
  const [annotationSaving, setAnnotationSaving] = useState(false);
  const [selectionNoteDraft, setSelectionNoteDraft] = useState("");
  const [selectionKind, setSelectionKind] = useState<AnnotationKind>("highlight");
  const [annotationFilter, setAnnotationFilter] = useState<"all" | AnnotationKind>("all");
  const [editingAnnotationId, setEditingAnnotationId] = useState<string | null>(null);
  const [editingAnnotationNote, setEditingAnnotationNote] = useState("");
  const [editingAnnotationKind, setEditingAnnotationKind] = useState<AnnotationKind>("highlight");
  const [paperNote, setPaperNote] = useState<PaperNote | null>(null);
  const [paperNoteDraft, setPaperNoteDraft] = useState("");
  const [paperNoteStatus, setPaperNoteStatus] = useState<
    "loading" | "saved" | "dirty" | "saving" | "conflict" | "error"
  >("loading");
  const [paperNoteError, setPaperNoteError] = useState<string | null>(null);
  const [selectionTranslation, setSelectionTranslation] = useState<SelectionTranslationPanel | null>(null);
  const [unselectableTextPages, setUnselectableTextPages] = useState<Set<number>>(
    () => new Set(),
  );
  const [pendingEvidenceFocus, setPendingEvidenceFocus] = useState<{
    requestId: number;
    page: number;
    regionId: string;
  } | null>(null);
  const [availableWidth, setAvailableWidth] = useState(880);

  const mainRef = useRef<HTMLDivElement | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const pageRefs = useRef(new Map<number, HTMLDivElement>());
  const scrollSaveTimerRef = useRef<number | null>(null);
  const paperNoteSaveTimerRef = useRef<number | null>(null);
  const paperNoteSavingRef = useRef(false);
  const paperNoteDraftRef = useRef("");
  const paperNoteEditorRef = useRef<HTMLTextAreaElement | null>(null);
  const restoredScrollRef = useRef(false);
  const zoomPercentRef = useRef(initialSession.pdfZoomPercent);
  const isFitModeRef = useRef(false);
  const pinchFrameRef = useRef<number | null>(null);
  const pinchDeltaRef = useRef(0);
  const pinchAnchorRef = useRef<PinchZoomAnchor | null>(null);
  const selectionTranslationAbortRef = useRef<AbortController | null>(null);
  const handledNavigationIdRef = useRef<number | null>(null);
  const firstPageReadyReportedRef = useRef(false);
  const pdfTextSelectionRef = useRef<PdfTextSelection | null>(null);

  const sessionContext = useCallback(
    (candidateLayout: TranslationLayout | null): ReaderSessionContext => ({
      paperId: paper.arxiv_id,
      blockIndexes,
      pageCount: candidateLayout?.page_count,
      validRegionIds: candidateLayout
        ? new Set(candidateLayout.regions.map((region) => region.region_id))
        : undefined,
    }),
    [blockIndexes, paper.arxiv_id],
  );

  const persistSession = useCallback(
    (patch: Partial<Omit<ReaderSessionV2, "version" | "paperId" | "readerMode">>) => {
      const next = updateReaderSession(sessionRef.current, patch, sessionContext(layout));
      sessionRef.current = next;
      saveReaderSession(next);
    },
    [layout, sessionContext],
  );

  const updateReaderFitMode = useCallback((value: boolean) => {
    isFitModeRef.current = value;
    setIsFitMode(value);
    window.localStorage.setItem(readerFitModeStorageKey(paper.arxiv_id), String(value));
  }, [paper.arxiv_id]);

  useEffect(() => {
    return () => {
      selectionTranslationAbortRef.current?.abort();
      if (pinchFrameRef.current !== null) window.cancelAnimationFrame(pinchFrameRef.current);
      if (scrollSaveTimerRef.current) window.clearTimeout(scrollSaveTimerRef.current);
      if (paperNoteSaveTimerRef.current) window.clearTimeout(paperNoteSaveTimerRef.current);
    };
  }, []);

  useEffect(() => {
    const restored = loadReaderSession({ paperId: paper.arxiv_id, blockIndexes });
    initialSessionRef.current = restored;
    sessionRef.current = restored;
    setActiveIndex(restored.activeIndex);
    setActiveRegionId(null);
    setCurrentPage(restored.pdfPage);
    setZoomPercent(restored.pdfZoomPercent);
    setZoomInput(String(restored.pdfZoomPercent));
    const storedFitMode = window.localStorage.getItem(readerFitModeStorageKey(paper.arxiv_id));
    const restoredFitMode = storedFitMode === null ? true : storedFitMode === "true";
    isFitModeRef.current = restoredFitMode;
    setIsFitMode(restoredFitMode);
    setPendingEvidenceFocus(null);
    pdfTextSelectionRef.current = null;
    selectionTranslationAbortRef.current?.abort();
    selectionTranslationAbortRef.current = null;
    setSelectionTranslation(null);
    setSelectionNoteDraft("");
    setSelectionKind("highlight");
    setEditingAnnotationId(null);
    setPaperNote(null);
    setPaperNoteDraft("");
    paperNoteDraftRef.current = "";
    setPaperNoteStatus("loading");
    setPaperNoteError(null);
    setUnselectableTextPages(new Set());
    handledNavigationIdRef.current = null;
    setInitialPdfPageReady(false);
    firstPageReadyReportedRef.current = false;
    restoredScrollRef.current = false;
  }, [blockIndexes, paper.arxiv_id]);

  useEffect(() => {
    const raw = window.localStorage.getItem(READER_SPLIT_STORAGE_KEY);
    if (raw === null) return;
    const stored = Number(raw);
    if (Number.isFinite(stored)) setSplitRatio(clampReaderSplitRatio(stored));
  }, []);

  const updateReaderSplitRatio = useCallback((value: number) => {
    const next = clampReaderSplitRatio(value);
    setSplitRatio(next);
    window.localStorage.setItem(READER_SPLIT_STORAGE_KEY, String(next));
  }, []);

  useEffect(() => {
    let cancelled = false;
    let pdfAbandoned = false;
    let loadingTask: PDFDocumentLoadingTask | null = null;
    let loadedDocument: PDFDocumentProxy | null = null;
    let borrowedPreload: PdfDocumentPreload | null = null;
    let preloadLeaseActive = false;

    async function loadLayoutAndPdf() {
      setLayoutLoading(true);
      setReaderError(null);
      setLayout(null);
      setPdfDoc(null);
      setPdfJs(null);
      try {
        const layoutPromise = getTranslationLayout(paper.arxiv_id);
        borrowedPreload = borrowPdfDocumentPreload(
          paper.arxiv_id,
          speculativePdfUrl,
        );
        preloadLeaseActive = borrowedPreload !== null;
        const pdfPromise = borrowedPreload?.promise ?? loadPdfJs().then(async (pdfjs) => {
            if (cancelled || pdfAbandoned) {
              throw new DOMException("Reader unmounted", "AbortError");
            }
            recordPdfDocumentLoad(speculativePdfUrl, "reader_fallback");
            loadingTask = pdfjs.getDocument({ url: speculativePdfUrl });
            const document = await loadingTask.promise;
            return { document, loadingTask, pdfjs };
          });
        void pdfPromise.catch(() => undefined);
        const nextLayout = await layoutPromise;
        if (cancelled) return;
        const expectedPages = nextLayout.pages.map((page) => page.page);
        if (
          nextLayout.page_count <= 0 ||
          nextLayout.pages.length !== nextLayout.page_count ||
          expectedPages.some((page, index) => page !== index + 1)
        ) {
          throw new Error("原位译文版面页数不一致，请重建版面。");
        }
        setLayout(nextLayout);

        const restored = loadReaderSession({
          ...sessionContext(nextLayout),
        });
        sessionRef.current = restored;
        setActiveIndex(restored.activeIndex);
        setActiveRegionId(null);
        setCurrentPage(restored.pdfPage);
        setZoomPercent(restored.pdfZoomPercent);
        setZoomInput(String(restored.pdfZoomPercent));

        const speculative = await pdfPromise;
        loadingTask = speculative.loadingTask;
        loadedDocument = speculative.document;
        if (cancelled) {
          if (!borrowedPreload) await loadedDocument.destroy();
          return;
        }
        const layoutPdfUrl = resolvePdfAssetUrl(nextLayout.pdf_url, API_BASE);
        if (layoutPdfUrl !== speculativePdfUrl) {
          if (borrowedPreload && preloadLeaseActive) {
            releasePdfDocumentPreload(borrowedPreload);
            preloadLeaseActive = false;
          } else {
            await loadedDocument.destroy();
          }
          loadedDocument = null;
          recordPdfDocumentLoad(layoutPdfUrl, "layout_url");
          loadingTask = speculative.pdfjs.getDocument({ url: layoutPdfUrl });
          loadedDocument = await loadingTask.promise;
          if (cancelled) {
            await loadedDocument.destroy();
            return;
          }
        }
        if (loadedDocument.numPages !== nextLayout.page_count) {
          throw new Error("原始 PDF 与版面缓存页数不一致，请重建版面。");
        }
        setPdfJs(speculative.pdfjs);
        setPdfDoc(loadedDocument);
      } catch (error) {
        pdfAbandoned = true;
        if (borrowedPreload && preloadLeaseActive) {
          releasePdfDocumentPreload(borrowedPreload);
          preloadLeaseActive = false;
        } else {
          safelyDestroyLoadingTask(loadingTask);
        }
        if (!cancelled) setReaderError((error as Error).message);
      } finally {
        if (!cancelled) setLayoutLoading(false);
      }
    }

    void loadLayoutAndPdf();
    return () => {
      cancelled = true;
      pdfAbandoned = true;
      if (borrowedPreload && preloadLeaseActive) {
        releasePdfDocumentPreload(borrowedPreload);
        preloadLeaseActive = false;
      } else {
        safelyDestroyLoadingTask(loadingTask);
      }
    };
  }, [paper.arxiv_id, sessionContext, speculativePdfUrl]);

  useEffect(() => {
    let cancelled = false;
    listAnnotations(paper.arxiv_id)
      .then((items) => {
        if (!cancelled) setAnnotations(items);
      })
      .catch((error) => {
        if (!cancelled) setAnnotationError((error as Error).message);
      });
    return () => {
      cancelled = true;
    };
  }, [paper.arxiv_id]);

  useEffect(() => {
    let cancelled = false;
    getPaperNote(paper.arxiv_id)
      .then((note) => {
        if (cancelled) return;
        setPaperNote(note);
        setPaperNoteDraft(note.markdown);
        paperNoteDraftRef.current = note.markdown;
        setPaperNoteStatus("saved");
        setPaperNoteError(null);
      })
      .catch((error) => {
        if (cancelled) return;
        setPaperNoteStatus("error");
        setPaperNoteError((error as Error).message);
      });
    return () => {
      cancelled = true;
    };
  }, [paper.arxiv_id]);

  const savePaperNoteNow = useCallback(async () => {
    if (
      !paperNote ||
      paperNoteSavingRef.current ||
      paperNoteStatus === "conflict" ||
      paperNoteDraftRef.current === paperNote.markdown
    ) {
      return;
    }
    const markdown = paperNoteDraftRef.current;
    paperNoteSavingRef.current = true;
    setPaperNoteStatus("saving");
    setPaperNoteError(null);
    try {
      const saved = await savePaperNote(
        paper.arxiv_id,
        markdown,
        paperNote.revision,
      );
      setPaperNote(saved);
      setPaperNoteStatus(
        paperNoteDraftRef.current === saved.markdown ? "saved" : "dirty",
      );
    } catch (error) {
      if (error instanceof PaperNoteRevisionConflictError) {
        setPaperNoteStatus("conflict");
      } else {
        setPaperNoteStatus("error");
      }
      setPaperNoteError((error as Error).message);
    } finally {
      paperNoteSavingRef.current = false;
    }
  }, [paper.arxiv_id, paperNote, paperNoteStatus]);

  useEffect(() => {
    if (
      !paperNote ||
      paperNoteStatus === "loading" ||
      paperNoteStatus === "saving" ||
      paperNoteStatus === "conflict" ||
      paperNoteDraft === paperNote.markdown
    ) {
      return;
    }
    if (paperNoteSaveTimerRef.current) {
      window.clearTimeout(paperNoteSaveTimerRef.current);
    }
    paperNoteSaveTimerRef.current = window.setTimeout(() => {
      paperNoteSaveTimerRef.current = null;
      void savePaperNoteNow();
    }, 800);
    return () => {
      if (paperNoteSaveTimerRef.current) {
        window.clearTimeout(paperNoteSaveTimerRef.current);
        paperNoteSaveTimerRef.current = null;
      }
    };
  }, [paperNote, paperNoteDraft, paperNoteStatus, savePaperNoteNow]);

  useEffect(() => {
    const node = scrollRef.current;
    if (!node) return;
    const update = () => setAvailableWidth(Math.max(240, node.clientWidth - 32));
    update();
    const observer = new ResizeObserver(update);
    observer.observe(node);
    return () => observer.disconnect();
  }, [layout]);

  useEffect(() => {
    if (window.location.hash !== "#paper-notes") return;
    const focusNotes = () => {
      document.getElementById("paper-notes")?.scrollIntoView({
        block: "start",
        behavior: "auto",
      });
    };
    focusNotes();
    const timer = window.setTimeout(focusNotes, 250);
    return () => window.clearTimeout(timer);
  }, []);

  const pageCssSizeAt100 = useMemo(
    () =>
      layout
        ? getPdfPageCssSizes(layout.pages, DEFAULT_PDF_PAGE_MAX_WIDTH_PX)
        : {},
    [layout],
  );
  const fitZoomPercent = useMemo(() => {
    const pageWidths = Object.values(pageCssSizeAt100).map((size) => size.widthPx);
    const widestPage = pageWidths.length > 0 ? Math.max(...pageWidths) : 0;
    if (!Number.isFinite(widestPage) || widestPage <= 0) return 100;
    return clampPdfZoomPercent((availableWidth / widestPage) * 100);
  }, [availableWidth, pageCssSizeAt100]);

  useEffect(() => {
    if (!isFitMode) return;
    setZoomPercent((current) => current === fitZoomPercent ? current : fitZoomPercent);
  }, [fitZoomPercent, isFitMode]);

  useEffect(() => {
    const root = scrollRef.current;
    if (!root) return;

    const handleTrackpadPinch = (event: WheelEvent) => {
      if (!event.ctrlKey) return;
      event.preventDefault();
      if (isFitModeRef.current) updateReaderFitMode(false);

      const pageNode = event.target instanceof Element
        ? event.target.closest<HTMLElement>(".inline-reader-page-shell")
        : null;
      if (pageNode && root.contains(pageNode)) {
        const page = Number(pageNode.dataset.pageNumber);
        const rect = pageNode.getBoundingClientRect();
        if (Number.isInteger(page) && rect.width > 0 && rect.height > 0) {
          pinchAnchorRef.current = {
            page,
            xRatio: Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width)),
            yRatio: Math.min(1, Math.max(0, (event.clientY - rect.top) / rect.height)),
            clientX: event.clientX,
            clientY: event.clientY,
          };
        }
      }

      const deltaScale = event.deltaMode === WheelEvent.DOM_DELTA_LINE
        ? 16
        : event.deltaMode === WheelEvent.DOM_DELTA_PAGE
          ? root.clientHeight
          : 1;
      pinchDeltaRef.current += event.deltaY * deltaScale;
      if (pinchFrameRef.current !== null) return;
      pinchFrameRef.current = window.requestAnimationFrame(() => {
        pinchFrameRef.current = null;
        const deltaY = pinchDeltaRef.current;
        pinchDeltaRef.current = 0;
        const nextZoom = getTrackpadPinchZoomPercent(zoomPercentRef.current, deltaY);
        if (nextZoom === zoomPercentRef.current) {
          pinchAnchorRef.current = null;
          return;
        }
        zoomPercentRef.current = nextZoom;
        setZoomPercent(nextZoom);
      });
    };

    root.addEventListener("wheel", handleTrackpadPinch, { passive: false });
    return () => {
      root.removeEventListener("wheel", handleTrackpadPinch);
      if (pinchFrameRef.current !== null) {
        window.cancelAnimationFrame(pinchFrameRef.current);
        pinchFrameRef.current = null;
      }
      pinchDeltaRef.current = 0;
      pinchAnchorRef.current = null;
    };
  }, [updateReaderFitMode]);

  useLayoutEffect(() => {
    zoomPercentRef.current = zoomPercent;
    const anchor = pinchAnchorRef.current;
    const root = scrollRef.current;
    const pageNode = anchor ? pageRefs.current.get(anchor.page) : null;
    if (!anchor || !root || !pageNode) return;
    const rect = pageNode.getBoundingClientRect();
    root.scrollLeft += rect.left + rect.width * anchor.xRatio - anchor.clientX;
    root.scrollTop += rect.top + rect.height * anchor.yRatio - anchor.clientY;
    pinchAnchorRef.current = null;
  }, [zoomPercent]);

  const zoom = zoomPercent / 100;
  const renderPages = useMemo(
    () =>
      getPdfPageRenderWindow({
        currentPage,
        pageCount: layout?.page_count ?? 0,
        radius: initialPdfPageReady ? undefined : 0,
      }),
    [currentPage, initialPdfPageReady, layout?.page_count],
  );
  const renderPageSet = useMemo(() => new Set(renderPages), [renderPages]);

  useEffect(() => {
    const root = scrollRef.current;
    if (!root || !layout || Object.keys(pageCssSizeAt100).length === 0) return;
    const visible = new Set<number>();
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          const page = Number((entry.target as HTMLElement).dataset.pageNumber);
          if (!Number.isInteger(page)) continue;
          if (entry.isIntersecting) visible.add(page);
          else visible.delete(page);
        }
        if (visible.size === 0) return;
        const rootRect = root.getBoundingClientRect();
        const center = rootRect.top + rootRect.height / 2;
        let nearest = [...visible][0] ?? 1;
        let distance = Number.POSITIVE_INFINITY;
        for (const page of visible) {
          const rect = pageRefs.current.get(page)?.getBoundingClientRect();
          if (!rect) continue;
          const nextDistance = Math.abs(rect.top + rect.height / 2 - center);
          if (nextDistance < distance) {
            nearest = page;
            distance = nextDistance;
          }
        }
        setCurrentPage(nearest);
      },
      { root, threshold: [0, 0.01, 0.25, 0.6] },
    );
    for (const node of pageRefs.current.values()) observer.observe(node);
    return () => observer.disconnect();
  }, [layout, pageCssSizeAt100]);

  useEffect(() => {
    const root = scrollRef.current;
    if (!root || !layout || !pdfDoc || restoredScrollRef.current) return;
    restoredScrollRef.current = true;
    const saved = sessionRef.current;
    const restore = () => {
      if (saved.pdfScrollTop > 0) root.scrollTop = saved.pdfScrollTop;
      else pageRefs.current.get(saved.pdfPage)?.scrollIntoView({ block: "start" });
    };
    window.requestAnimationFrame(() => {
      restore();
      window.setTimeout(restore, 240);
    });
  }, [layout, pageCssSizeAt100, pdfDoc]);

  useEffect(() => {
    setZoomInput(String(zoomPercent));
    persistSession({ pdfZoomPercent: zoomPercent });
  }, [persistSession, zoomPercent]);

  useEffect(() => {
    persistSession({ activeIndex, pdfPage: currentPage, inspector: null });
  }, [activeIndex, currentPage, persistSession]);

  const regionsByPage = useMemo(() => {
    const result = new Map<number, TranslationLayoutRegion[]>();
    for (const region of layout?.regions ?? []) {
      const pageRegions = result.get(region.page) ?? [];
      pageRegions.push(region);
      result.set(region.page, pageRegions);
    }
    return result;
  }, [layout]);

  const regionsByBlock = useMemo(() => {
    const result = new Map<number, TranslationLayoutRegion[]>();
    for (const region of layout?.regions ?? []) {
      const blockRegions = result.get(region.block_index) ?? [];
      blockRegions.push(region);
      result.set(region.block_index, blockRegions);
    }
    return result;
  }, [layout]);

  const regionById = useMemo(
    () => new Map((layout?.regions ?? []).map((region) => [region.region_id, region])),
    [layout],
  );

  const originalTextLayerAnnotationsByPage = useMemo(() => {
    const result = new Map<number, Annotation[]>();
    for (const annotation of annotations) {
      if (annotation.side !== "original" || annotation.selector?.version !== 2) continue;
      const items = result.get(annotation.selector.page) ?? [];
      items.push(annotation);
      result.set(annotation.selector.page, items);
    }
    return result;
  }, [annotations]);

  const primaryRegionIdByBlock = useMemo(() => {
    const result = new Map<number, string>();
    for (const [blockIndex, regions] of regionsByBlock) {
      const primary = [...regions].sort(
        (left, right) =>
          left.flow_order - right.flow_order ||
          left.page - right.page ||
          left.region_id.localeCompare(right.region_id),
      )[0];
      if (primary) result.set(blockIndex, primary.region_id);
    }
    return result;
  }, [regionsByBlock]);

  const blockByIndex = useMemo(
    () => new Map(paper.blocks.map((block) => [block.index, block])),
    [paper.blocks],
  );

  const currentPagePrimaryRegion = useMemo(
    () =>
      layout?.regions
        .filter((region) => region.page === currentPage)
        .sort((left, right) =>
          left.flow_order - right.flow_order || left.region_id.localeCompare(right.region_id)
        )[0] ?? null,
    [currentPage, layout],
  );

  const activePdfSelection = selectionTranslation?.selection ?? null;
  const activePdfSelectionText = selectionTranslation?.sourceText ?? activePdfSelection?.raw_text ?? null;

  const contextActiveIndex = useMemo(() => {
    if (activePdfSelection?.block_index !== null && activePdfSelection?.block_index !== undefined) {
      return activePdfSelection.block_index;
    }
    if (
      activeIndex !== null &&
      (regionsByBlock.get(activeIndex) ?? []).some((region) => region.page === currentPage)
    ) {
      return activeIndex;
    }
    return currentPagePrimaryRegion?.block_index ?? null;
  }, [
    activeIndex,
    activePdfSelection?.block_index,
    currentPage,
    currentPagePrimaryRegion?.block_index,
    regionsByBlock,
  ]);

  const contextLocation = useMemo<ReaderLocationContext>(() => {
    if (activePdfSelection) {
      return {
        page: activePdfSelection.page,
        region_id: activePdfSelection.region_id,
        layout_confidence: activePdfSelection.layout_confidence,
        render_policy: "preserve",
      };
    }
    const currentPageRegion = contextActiveIndex === null
      ? null
      : (regionsByBlock.get(contextActiveIndex) ?? [])
          .filter((region) => region.page === currentPage)
          .sort((left, right) =>
            left.flow_order - right.flow_order || left.region_id.localeCompare(right.region_id)
          )[0] ?? null;
    const activeRegion = activeRegionId ? regionById.get(activeRegionId) ?? null : null;
    const eligibleActiveRegionId = activeRegion?.page === currentPage
      ? activeRegion.region_id
      : null;
    const regionId = eligibleActiveRegionId ?? currentPageRegion?.region_id ??
      currentPagePrimaryRegion?.region_id ?? null;
    const region = regionId ? regionById.get(regionId) : null;
    return region
      ? {
          page: region.page,
          region_id: region.region_id,
          layout_confidence: region.confidence,
          render_policy: "preserve",
        }
      : {
          page: currentPage,
          region_id: null,
          layout_confidence: null,
          render_policy: null,
        };
  }, [
    activeRegionId,
    activePdfSelection,
    contextActiveIndex,
    currentPage,
    currentPagePrimaryRegion?.region_id,
    regionById,
    regionsByBlock,
  ]);

  useEffect(() => {
    onAgentContextChange?.(
      buildReaderAgentContext(
        paper.blocks,
        contextActiveIndex,
        activePdfSelection && activePdfSelectionText
            ? {
                block_index: activePdfSelection.block_index,
                side: "original",
                text: activePdfSelectionText,
              }
            : null,
        contextLocation,
      ),
    );
  }, [activePdfSelection, activePdfSelectionText, contextActiveIndex, contextLocation, onAgentContextChange, paper.blocks]);

  const runPdfSelectionTranslation = useCallback(async (
    selection: PdfTextSelection,
    sourceText: string,
  ) => {
    selectionTranslationAbortRef.current?.abort();
    const controller = new AbortController();
    selectionTranslationAbortRef.current = controller;
    const sourceEdited = sourceText !== selection.raw_text;
    setSelectionTranslation({
      selection,
      originalSourceText: selection.raw_text,
      sourceText,
      status: "loading",
      result: null,
      error: null,
      retryable: false,
    });
    try {
      const textHash = sourceEdited ? await sha256Text(sourceText) : selection.text_sha256;
      if (selectionTranslationAbortRef.current !== controller) return;
      const result = await translateSelection(
        paper.arxiv_id,
        {
          ...selection,
          raw_text: sourceText,
          text_sha256: textHash,
          quote: sourceEdited ? { ...selection.quote, exact: sourceText } : selection.quote,
          block_index: sourceEdited ? null : selection.block_index,
          region_id: sourceEdited ? null : selection.region_id,
          layout_confidence: sourceEdited ? null : selection.layout_confidence,
          source_edited: sourceEdited,
        },
        controller.signal,
      );
      if (selectionTranslationAbortRef.current !== controller) return;
      setSelectionTranslation((current) => current?.selection === selection
        ? { ...current, status: "done", result, error: null, retryable: false }
        : current);
    } catch (error) {
      if (selectionTranslationAbortRef.current !== controller) return;
      if (error instanceof DOMException && error.name === "AbortError") {
        setSelectionTranslation((current) => current?.selection === selection
          ? { ...current, status: "cancelled", error: "已取消本次翻译。", retryable: true }
          : current);
      } else {
        setSelectionTranslation((current) => current?.selection === selection
          ? {
              ...current,
              status: "error",
              result: null,
              error: error instanceof Error ? error.message : "翻译当前选区失败。",
              retryable: error instanceof SelectionTranslationRequestError
                ? error.retryable
                : true,
            }
          : current);
      }
    } finally {
      if (selectionTranslationAbortRef.current === controller) {
        selectionTranslationAbortRef.current = null;
      }
    }
  }, [paper.arxiv_id]);

  const handlePdfTextSelection = useCallback((selection: PdfTextSelection, _rect: DOMRect) => {
    pdfTextSelectionRef.current = selection;
    if (selection.block_index !== null) setActiveIndex(selection.block_index);
    setActiveRegionId(selection.region_id);
    setAnnotationError(null);
    setSelectionNoteDraft("");
    setSelectionKind("highlight");
    void runPdfSelectionTranslation(selection, selection.raw_text);
  }, [runPdfSelectionTranslation]);

  const cancelPdfSelectionTranslation = useCallback(() => {
    selectionTranslationAbortRef.current?.abort();
  }, []);

  const closePdfSelectionTranslation = useCallback(() => {
    selectionTranslationAbortRef.current?.abort();
    selectionTranslationAbortRef.current = null;
    setSelectionTranslation(null);
    window.getSelection()?.removeAllRanges();
  }, []);

  const retryPdfSelectionTranslation = useCallback(() => {
    if (!selectionTranslation) return;
    void runPdfSelectionTranslation(
      selectionTranslation.selection,
      selectionTranslation.sourceText,
    );
  }, [runPdfSelectionTranslation, selectionTranslation]);

  const askPetAboutPdfSelection = useCallback(() => {
    if (!selectionTranslation || !onAskPet) return;
    const selection = selectionTranslation.selection;
    const sourceEdited = selectionTranslation.sourceText !== selection.raw_text;
    const selectedBlockIndex = sourceEdited ? null : selection.block_index;
    onAskPet({
      message: "请解释我选中的这段原文。",
      context: buildReaderAgentContext(
        paper.blocks,
        selectedBlockIndex,
        {
          block_index: selectedBlockIndex,
          side: "original",
          text: selectionTranslation.sourceText,
        },
        {
          page: selection.page,
          region_id: sourceEdited ? null : selection.region_id,
          layout_confidence: sourceEdited ? null : selection.layout_confidence,
          render_policy: "preserve",
        },
      ),
    });
  }, [onAskPet, paper.blocks, selectionTranslation]);

  const savePdfSelectionHighlight = useCallback(async ({
    note,
    kind,
  }: {
    note: string;
    kind: AnnotationKind;
  }) => {
    if (!selectionTranslation || annotationSaving) return;
    const selection = selectionTranslation.selection;
    if (selectionTranslation.sourceText !== selection.raw_text) {
      setAnnotationError("原文已被手动修改。恢复 PDF 原文后，才能可靠保存高亮。");
      return;
    }
    if (selection.block_index === null) {
      setAnnotationError("这段文字暂未匹配到唯一段落，可以翻译或问 Pet，但不能可靠保存高亮。");
      return;
    }
    setAnnotationSaving(true);
    setAnnotationError(null);
    try {
      const annotation = await createAnnotation(paper.arxiv_id, {
        block_index: selection.block_index,
        side: "original",
        text: selection.raw_text,
        note,
        kind,
        selector: {
          version: 2,
          source_pdf_sha256: selection.source_pdf_sha256,
          page: selection.page,
          start: selection.start,
          end: selection.end,
          quote: selection.quote,
          rects: selection.rects,
          region_id: selection.region_id,
          layout_confidence: selection.layout_confidence,
        },
      });
      setAnnotations((items) => [...items, annotation]);
      setSelectionNoteDraft("");
    } catch (error) {
      setAnnotationError((error as Error).message);
    } finally {
      setAnnotationSaving(false);
    }
  }, [annotationSaving, paper.arxiv_id, selectionTranslation]);

  const beginAnnotationEdit = useCallback((annotation: Annotation) => {
    setEditingAnnotationId(annotation.id);
    setEditingAnnotationNote(annotation.note);
    setEditingAnnotationKind(annotation.kind);
    setAnnotationError(null);
  }, []);

  const saveAnnotationEdit = useCallback(async () => {
    if (!editingAnnotationId || annotationSaving) return;
    setAnnotationSaving(true);
    setAnnotationError(null);
    try {
      const updated = await updateAnnotation(paper.arxiv_id, editingAnnotationId, {
        note: editingAnnotationNote,
        kind: editingAnnotationKind,
      });
      setAnnotations((items) =>
        items.map((item) => item.id === updated.id ? updated : item),
      );
      setEditingAnnotationId(null);
    } catch (error) {
      setAnnotationError((error as Error).message);
    } finally {
      setAnnotationSaving(false);
    }
  }, [
    annotationSaving,
    editingAnnotationId,
    editingAnnotationKind,
    editingAnnotationNote,
    paper.arxiv_id,
  ]);

  const removeAnnotation = useCallback(
    async (annotationId: string) => {
      setAnnotationError(null);
      try {
        await deleteAnnotation(paper.arxiv_id, annotationId);
        setAnnotations((items) => items.filter((item) => item.id !== annotationId));
      } catch (error) {
        setAnnotationError((error as Error).message);
      }
    },
    [paper.arxiv_id],
  );

  const focusAnnotation = useCallback((annotation: Annotation) => {
    setActiveIndex(annotation.block_index);
    const selector = annotation.selector;
    if (selector?.version !== 2) return;
    setCurrentPage(selector.page);
    setActiveRegionId(selector.region_id);
    window.requestAnimationFrame(() => {
      pageRefs.current.get(selector.page)?.scrollIntoView({
        block: "start",
        behavior: "smooth",
      });
    });
  }, []);

  const visibleAnnotations = useMemo(
    () =>
      [...annotations]
        .reverse()
        .filter((annotation) =>
          annotationFilter === "all" || annotation.kind === annotationFilter
        ),
    [annotationFilter, annotations],
  );

  useEffect(() => {
    if (!selectionTranslation) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        closePdfSelectionTranslation();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [closePdfSelectionTranslation, selectionTranslation]);

  const commitZoomInput = () => {
    const parsed = Number.parseInt(zoomInput.replace("%", ""), 10);
    if (!Number.isFinite(parsed)) {
      setZoomInput(String(zoomPercent));
      return;
    }
    updateReaderFitMode(false);
    setZoomPercent(clampPdfZoomPercent(parsed));
  };

  const resizeReaderSplitFromClientX = useCallback((clientX: number) => {
    const main = mainRef.current;
    if (!main) return;
    const bounds = main.getBoundingClientRect();
    if (bounds.width <= 0) return;
    updateReaderSplitRatio(((clientX - bounds.left) / bounds.width) * 100);
  }, [updateReaderSplitRatio]);

  const beginReaderSplitResize = useCallback((event: ReactPointerEvent<HTMLDivElement>) => {
    if (window.matchMedia("(max-width: 900px)").matches) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    resizeReaderSplitFromClientX(event.clientX);
  }, [resizeReaderSplitFromClientX]);

  const continueReaderSplitResize = useCallback((event: ReactPointerEvent<HTMLDivElement>) => {
    if (!event.currentTarget.hasPointerCapture(event.pointerId)) return;
    resizeReaderSplitFromClientX(event.clientX);
  }, [resizeReaderSplitFromClientX]);

  const handleReaderSplitKeyDown = useCallback((event: ReactKeyboardEvent<HTMLDivElement>) => {
    let next: number | null = null;
    if (event.key === "ArrowLeft") next = splitRatio - 2;
    if (event.key === "ArrowRight") next = splitRatio + 2;
    if (event.key === "Home") next = MIN_READER_SPLIT_RATIO;
    if (event.key === "End") next = MAX_READER_SPLIT_RATIO;
    if (next === null) return;
    event.preventDefault();
    updateReaderSplitRatio(next);
  }, [splitRatio, updateReaderSplitRatio]);

  const scrollToPage = (page: number) => {
    const nextPage = Math.min(layout?.page_count ?? 1, Math.max(1, page));
    setCurrentPage(nextPage);
    pageRefs.current.get(nextPage)?.scrollIntoView({ block: "start", behavior: "smooth" });
  };

  const registerPage = useCallback((page: number, node: HTMLDivElement | null) => {
    if (node) pageRefs.current.set(page, node);
    else pageRefs.current.delete(page);
  }, []);

  useEffect(() => {
    if (
      !navigationRequest ||
      handledNavigationIdRef.current === navigationRequest.id
    ) {
      return;
    }

    const hint = getReaderEvidenceHint(navigationRequest.evidence);
    if (hint.arxivId && hint.arxivId !== paper.arxiv_id) {
      handledNavigationIdRef.current = navigationRequest.id;
      window.sessionStorage.setItem(
        "pet:pending-reader-evidence",
        JSON.stringify({
          arxivId: hint.arxivId,
          evidence: navigationRequest.evidence,
        }),
      );
      window.location.assign(`/paper/${encodeURIComponent(hint.arxivId)}`);
      return;
    }
    if (hint.noteHeading) {
      handledNavigationIdRef.current = navigationRequest.id;
      const editor = paperNoteEditorRef.current;
      if (!editor) return;
      const escapedHeading = hint.noteHeading.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      const match = new RegExp(`^#{1,6}[ \\t]+${escapedHeading}[ \\t]*$`, "m").exec(
        paperNoteDraftRef.current,
      );
      editor.scrollIntoView({ block: "center", behavior: "smooth" });
      editor.focus();
      if (match?.index !== undefined) {
        editor.setSelectionRange(match.index, match.index + match[0].length);
      }
      return;
    }
    if (!layout || !pdfDoc) return;
    const validBlockIndex =
      hint.blockIndex !== null && blockByIndex.has(hint.blockIndex)
        ? hint.blockIndex
        : null;
    let targetRegion = hint.regionId ? regionById.get(hint.regionId) ?? null : null;
    if (
      targetRegion &&
      hint.blockIndex !== null &&
      targetRegion.block_index !== hint.blockIndex
    ) {
      targetRegion = null;
    }
    if (!targetRegion && validBlockIndex !== null) {
      const primaryRegionId = primaryRegionIdByBlock.get(validBlockIndex);
      targetRegion = primaryRegionId ? regionById.get(primaryRegionId) ?? null : null;
    }

    const targetBlockIndex = targetRegion?.block_index ?? validBlockIndex;
    // A page number is never an independent evidence anchor. Once the block
    // or region has been validated against the current layout, navigate using
    // that current region's page only; otherwise keep the document in place.
    const targetPage = targetRegion?.page ?? null;
    if (targetBlockIndex === null && targetPage === null) {
      handledNavigationIdRef.current = navigationRequest.id;
      return;
    }

    handledNavigationIdRef.current = navigationRequest.id;
    setPendingEvidenceFocus(null);
    window.getSelection()?.removeAllRanges();
    setActiveIndex(targetBlockIndex);
    setActiveRegionId(targetRegion?.region_id ?? null);

    if (targetPage === null) return;

    setCurrentPage(targetPage);
    pageRefs.current.get(targetPage)?.scrollIntoView({ block: "start", behavior: "auto" });

    setPendingEvidenceFocus(
      targetRegion
        ? {
            requestId: navigationRequest.id,
            page: targetPage,
            regionId: targetRegion.region_id,
          }
        : null,
    );
  }, [
    blockByIndex,
    layout,
    navigationRequest,
    pdfDoc,
    primaryRegionIdByBlock,
    regionById,
  ]);

  useEffect(() => {
    if (!pendingEvidenceFocus) return;
    let cancelled = false;
    let attempts = 0;
    let timer: number | null = null;
    const focusRegion = () => {
      if (cancelled) return;
      const pageNode = pageRefs.current.get(pendingEvidenceFocus.page);
      const regionNode = [...(pageNode?.querySelectorAll<HTMLElement>("[data-region-id]") ?? [])]
        .find((node) => node.dataset.regionId === pendingEvidenceFocus.regionId);
      if (regionNode) {
        regionNode.focus({ preventScroll: true });
        regionNode.scrollIntoView({ block: "center", inline: "center", behavior: "auto" });
        setPendingEvidenceFocus((current) =>
          current?.requestId === pendingEvidenceFocus.requestId ? null : current
        );
        return;
      }
      attempts += 1;
      if (attempts < 40) timer = window.setTimeout(focusRegion, 75);
    };
    window.requestAnimationFrame(focusRegion);
    return () => {
      cancelled = true;
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [pendingEvidenceFocus]);

  const handleInitialPdfPageRendered = useCallback(() => {
    setInitialPdfPageReady(true);
    if (!firstPageReadyReportedRef.current) {
      firstPageReadyReportedRef.current = true;
      onFirstPageReady?.();
    }
  }, [onFirstPageReady]);
  const handleTextLayerAvailability = useCallback((page: number, available: boolean) => {
    setUnselectableTextPages((current) => {
      const hasPage = current.has(page);
      if (hasPage === !available) return current;
      const next = new Set(current);
      if (available) next.delete(page);
      else next.add(page);
      return next;
    });
  }, []);

  const visibleError = readerError;
  const selectionTranslationDirty = Boolean(
    selectionTranslation?.result &&
    selectionTranslation.sourceText !== selectionTranslation.result.source_text,
  );
  const paperNoteStatusLabel = {
    loading: "读取中",
    saved: "已保存",
    dirty: "未保存",
    saving: "保存中",
    conflict: "存在冲突",
    error: "保存失败",
  }[paperNoteStatus];

  return (
    <section
      className="inline-reader"
      data-reader-mode="selection_translation"
      data-layout-cache-key={layout?.cache_key}
      data-layout-page-count={layout?.page_count}
      data-source-pdf-url={layout ? resolvePdfAssetUrl(layout.pdf_url, API_BASE) : undefined}
      role="region"
      aria-label="PDF 原文划选翻译阅读器"
    >
      <header className="reader-titlebar">
        <div className="min-w-0">
          <p className="reader-eyebrow">
            PDF 原文阅读
            <span aria-hidden="true">/</span>
            {paper.arxiv_id}
          </p>
          <h1 className="reader-title">{paper.title}</h1>
          <p className="reader-byline">
            {paper.authors.slice(0, 4).join(" · ")}
            {paper.authors.length > 4 ? ` 等 ${paper.authors.length} 位作者` : ""}
          </p>
        </div>
        <div className="reader-title-actions">
          <PdfExportControl
            paperId={paper.arxiv_id}
            readerReady={initialPdfPageReady}
          />
          <CollectionPicker arxivId={paper.arxiv_id} />
          <span className="reader-stat">标注 {annotations.length}</span>
        </div>
      </header>

      {visibleError && <p className="reader-inline-error">{visibleError}</p>}
      {annotationError && <p className="reader-inline-error">{annotationError}</p>}

      <div
        ref={mainRef}
        className="inline-reader-main"
        data-reader-layout="split"
        style={{
          gridTemplateColumns: `minmax(0, ${splitRatio}fr) 0.75rem minmax(0, ${100 - splitRatio}fr)`,
        }}
      >
      <div className="inline-reader-workbench">
        <div className="inline-reader-toolbar">
          <div className="inline-reader-status">
            <strong>原始 PDF 阅读</strong>
            <span>
              {layout
                ? `划选原文自动翻译，也可问 Pet 或高亮 · ${layout.adapter}`
                : "正在准备版面"}
            </span>
          </div>
          <div className="inline-reader-page-controls" aria-label="PDF 页码">
            <button type="button" disabled={currentPage <= 1} onClick={() => scrollToPage(currentPage - 1)}>
              上一页
            </button>
            <span>
              {currentPage} / {layout?.page_count ?? "—"}
            </span>
            <button
              type="button"
              disabled={!layout || currentPage >= layout.page_count}
              onClick={() => scrollToPage(currentPage + 1)}
            >
              下一页
            </button>
          </div>
          <div className="reader-pdf-zoom-controls" aria-label="PDF 缩放">
            <button
              type="button"
              className="reader-pdf-zoom-button"
              aria-label="缩小 PDF"
              disabled={zoomPercent <= PDF_ZOOM_MIN}
              onClick={() => {
                updateReaderFitMode(false);
                setZoomPercent((value) => clampPdfZoomPercent(value - PDF_ZOOM_STEP));
              }}
            >
              −
            </button>
            <label className="reader-pdf-zoom-field">
              <input
                className="reader-pdf-zoom-input"
                value={zoomInput}
                inputMode="decimal"
                aria-label="PDF 缩放百分比"
                onChange={(event) => setZoomInput(event.target.value)}
                onBlur={commitZoomInput}
                onKeyDown={(event) => {
                  if (event.key === "Enter") {
                    commitZoomInput();
                    event.currentTarget.blur();
                  }
                  if (event.key === "Escape") {
                    setZoomInput(String(zoomPercent));
                    event.currentTarget.blur();
                  }
                }}
              />
              <span>%</span>
            </label>
            <button
              type="button"
              className="reader-pdf-zoom-button"
              aria-label="放大 PDF"
              disabled={zoomPercent >= PDF_ZOOM_MAX}
              onClick={() => {
                updateReaderFitMode(false);
                setZoomPercent((value) => clampPdfZoomPercent(value + PDF_ZOOM_STEP));
              }}
            >
              +
            </button>
            <button
              type="button"
              className="reader-pdf-fit-button"
              disabled={isFitMode && zoomPercent === fitZoomPercent}
              onClick={() => {
                updateReaderFitMode(true);
                setZoomPercent(fitZoomPercent);
              }}
            >
              适宽
            </button>
          </div>
        </div>
        {unselectableTextPages.has(currentPage) && (
          <p className="reader-text-layer-unavailable" role="status">
            这一页没有可选择的文字层，暂不能划选翻译。扫描件需先生成可靠 OCR 文字层。
          </p>
        )}

        <div
          ref={scrollRef}
          className="inline-reader-scroll"
          data-testid="pdf-scroll-viewport"
          onScroll={(event) => {
            const scrollTop = event.currentTarget.scrollTop;
            if (scrollSaveTimerRef.current) window.clearTimeout(scrollSaveTimerRef.current);
            scrollSaveTimerRef.current = window.setTimeout(() => {
              persistSession({ pdfScrollTop: scrollTop, pdfPage: currentPage });
            }, 180);
          }}
        >
          {layoutLoading && <div className="inline-reader-loading">正在读取原始 PDF 与版面…</div>}
          {!layoutLoading && layout && pdfDoc && pdfJs && (
            <div className="inline-reader-pages" style={{ gap: `${PDF_PAGE_GAP_PX}px` }}>
              {layout.pages.map((page) => {
                const size = pageCssSizeAt100[page.page];
                if (!size) return null;
                return (
                  <div
                    key={page.page}
                    ref={(node) => registerPage(page.page, node)}
                    className="inline-reader-page-shell"
                    data-page-number={page.page}
                    style={{
                      width: `${size.widthPx * zoom}px`,
                      height: `${size.heightPx * zoom}px`,
                    }}
                  >
                    {renderPageSet.has(page.page) && (
                      <InlinePdfPage
                        pdfDoc={pdfDoc}
                        pdfJs={pdfJs}
                        page={page.page}
                        sizeAt100={size}
                        zoom={zoom}
                        regions={regionsByPage.get(page.page) ?? []}
                        originalAnnotations={originalTextLayerAnnotationsByPage.get(page.page) ?? EMPTY_ANNOTATIONS}
                        activeIndex={activeIndex}
                        activeRegionId={activeRegionId}
                        sourcePdfSha256={layout.source_pdf_sha256}
                        onDisplayRendered={handleInitialPdfPageRendered}
                        onTextLayerAvailability={handleTextLayerAvailability}
                        onPdfTextSelection={handlePdfTextSelection}
                        onPdfTextSelectionError={setAnnotationError}
                      />
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>

      <div
        className="reader-split-resizer"
        role="separator"
        aria-label="调整阅读与翻译宽度"
        aria-orientation="vertical"
        aria-valuemin={MIN_READER_SPLIT_RATIO}
        aria-valuemax={MAX_READER_SPLIT_RATIO}
        aria-valuenow={splitRatio}
        tabIndex={0}
        onPointerDown={beginReaderSplitResize}
        onPointerMove={continueReaderSplitResize}
        onKeyDown={handleReaderSplitKeyDown}
      >
        <span aria-hidden="true" />
      </div>

        <aside
          id="paper-notes"
          className="selection-translation-panel"
          role="region"
          aria-labelledby="selection-translation-title"
          data-selection-translation-status={selectionTranslation?.status ?? "idle"}
        >
          <div className="inline-inspector-heading">
            <div>
              <p className="inline-inspector-kicker">
                {selectionTranslation
                  ? `当前选区 · 第 ${selectionTranslation.selection.page} 页`
                  : "随论文长期保存"}
              </p>
              <h2 id="selection-translation-title">论文阅读笔记</h2>
            </div>
          </div>

          <section className="reader-note-section reader-selection-section">
            <div className="reader-note-section-heading">
              <h3>当前选区</h3>
              <span>{selectionTranslation ? "可翻译、标记并写下判断" : "等待划选"}</span>
            </div>

            {!selectionTranslation ? (
              <div className="selection-translation-empty">
                <p>在左侧 PDF 中划选原文，译文和选区笔记会出现在这里。</p>
                <span>整篇论文笔记始终可以在下方编辑。</span>
              </div>
            ) : (
              <div className="reader-selection-note-flow">
                <label className="selection-translation-source">
                  <span>识别到的英文原文</span>
                  <textarea
                    value={selectionTranslation.sourceText}
                    rows={4}
                    disabled={selectionTranslation.status === "loading"}
                    onChange={(event) => setSelectionTranslation((current) => current
                      ? { ...current, sourceText: event.target.value }
                      : current)}
                  />
                </label>
                {selectionTranslation.sourceText !== selectionTranslation.originalSourceText && (
                  <div className="selection-translation-edit-note">
                    <p>原文已手动修正；重新翻译后将不再声称精确对应 block 或 region。</p>
                    <button
                      type="button"
                      disabled={selectionTranslation.status === "loading"}
                      onClick={() => setSelectionTranslation((current) => current
                        ? { ...current, sourceText: current.originalSourceText }
                        : current)}
                    >
                      恢复 PDF 原文
                    </button>
                  </div>
                )}

                <div className="selection-translation-result" aria-live="polite">
                  {selectionTranslation.status === "loading" && (
                    <p className="selection-translation-status">正在翻译你选中的内容…</p>
                  )}
                  {selectionTranslation.status === "done" && selectionTranslation.result && !selectionTranslationDirty && (
                    <>
                      <p className="inline-inspector-kicker">中文译文</p>
                      <p data-testid="selection-translation-text">{selectionTranslation.result.translation}</p>
                    </>
                  )}
                  {selectionTranslationDirty && (
                    <p className="selection-translation-status">原文已修改，请重新翻译以更新译文。</p>
                  )}
                  {(selectionTranslation.status === "error" || selectionTranslation.status === "cancelled") && (
                    <p className="selection-translation-error" role="alert">{selectionTranslation.error}</p>
                  )}
                </div>

                <fieldset className="reader-annotation-kind-picker">
                  <legend>语义标记</legend>
                  <div>
                    {ANNOTATION_KINDS.map((kind) => (
                      <label key={kind} data-annotation-kind={kind}>
                        <input
                          type="radio"
                          name="selection-annotation-kind"
                          value={kind}
                          checked={selectionKind === kind}
                          onChange={() => setSelectionKind(kind)}
                        />
                        <span>{ANNOTATION_KIND_LABELS[kind]}</span>
                      </label>
                    ))}
                  </div>
                </fieldset>

                <label className="reader-selection-note-editor">
                  <span>选区笔记 · Markdown</span>
                  <textarea
                    value={selectionNoteDraft}
                    rows={4}
                    maxLength={8000}
                    placeholder="这段为什么重要？哪里还有疑问？"
                    onChange={(event) => setSelectionNoteDraft(event.target.value)}
                  />
                </label>

                <div className="selection-translation-footer">
                  {selectionTranslation.status === "loading" ? (
                    <button type="button" onClick={cancelPdfSelectionTranslation}>取消翻译</button>
                  ) : (
                    <button
                      type="button"
                      disabled={
                        selectionTranslation.sourceText.trim().length < 2 ||
                        (selectionTranslation.status === "error" && !selectionTranslation.retryable)
                      }
                      onClick={retryPdfSelectionTranslation}
                    >
                      {selectionTranslation.status === "done" ? "重新翻译" : "重试翻译"}
                    </button>
                  )}
                  <button
                    type="button"
                    className="reader-primary-action"
                    disabled={
                      annotationSaving ||
                      selectionNoteDraft.trim().length === 0 ||
                      selectionTranslation.selection.block_index === null ||
                      selectionTranslation.sourceText !== selectionTranslation.originalSourceText
                    }
                    onClick={() => void savePdfSelectionHighlight({
                      note: selectionNoteDraft,
                      kind: selectionKind,
                    })}
                  >
                    {annotationSaving ? "保存中…" : "保存高亮和笔记"}
                  </button>
                  <button
                    type="button"
                    disabled={
                      annotationSaving ||
                      selectionTranslation.selection.block_index === null ||
                      selectionTranslation.sourceText !== selectionTranslation.originalSourceText
                    }
                    onClick={() => void savePdfSelectionHighlight({
                      note: "",
                      kind: selectionKind,
                    })}
                  >
                    仅高亮
                  </button>
                  {onAskPet && (
                    <button type="button" onClick={askPetAboutPdfSelection}>问 Pet</button>
                  )}
                </div>
              </div>
            )}
          </section>

          <details className="reader-paper-note" open>
            <summary>
              <span>整篇论文笔记</span>
              <span data-paper-note-status={paperNoteStatus}>{paperNoteStatusLabel}</span>
            </summary>
            <div className="reader-paper-note-body">
              <textarea
                ref={paperNoteEditorRef}
                data-testid="paper-note-editor"
                aria-label="整篇论文 Markdown 笔记"
                value={paperNoteDraft}
                rows={12}
                maxLength={200000}
                disabled={paperNoteStatus === "loading"}
                placeholder={"# 阅读笔记\n\n## 核心问题\n\n## 方法与证据\n"}
                onChange={(event) => {
                  const markdown = event.target.value;
                  paperNoteDraftRef.current = markdown;
                  setPaperNoteDraft(markdown);
                  if (paperNoteStatus !== "conflict") setPaperNoteStatus("dirty");
                  setPaperNoteError(null);
                }}
                onKeyDown={(event) => {
                  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "s") {
                    event.preventDefault();
                    if (paperNoteSaveTimerRef.current) {
                      window.clearTimeout(paperNoteSaveTimerRef.current);
                      paperNoteSaveTimerRef.current = null;
                    }
                    void savePaperNoteNow();
                  }
                }}
              />
              <div className="reader-paper-note-meta" aria-live="polite">
                <span>{paperNoteDraft.length.toLocaleString()} / 200,000</span>
                {paperNoteError && <span role="alert">{paperNoteError} 草稿仍保留在编辑器中。</span>}
              </div>
            </div>
          </details>

          <section className="reader-note-section reader-saved-notes">
            <div className="reader-note-section-heading">
              <div>
                <h3>已保存选区笔记</h3>
                <span>{annotations.length} 条高亮与笔记</span>
              </div>
              <label>
                <span className="sr-only">按语义筛选选区笔记</span>
                <select
                  value={annotationFilter}
                  onChange={(event) =>
                    setAnnotationFilter(event.target.value as "all" | AnnotationKind)
                  }
                >
                  <option value="all">全部</option>
                  {ANNOTATION_KINDS.map((kind) => (
                    <option key={kind} value={kind}>{ANNOTATION_KIND_LABELS[kind]}</option>
                  ))}
                </select>
              </label>
            </div>

            {visibleAnnotations.length === 0 ? (
              <p className="reader-saved-notes-empty">还没有符合当前筛选的选区笔记。</p>
            ) : (
              <div className="reader-annotations-list">
                {visibleAnnotations.map((annotation) => (
                  <article
                    key={annotation.id}
                    className="reader-annotation-item"
                    data-annotation-id={annotation.id}
                    data-annotation-kind={annotation.kind}
                  >
                    {editingAnnotationId === annotation.id ? (
                      <div className="reader-annotation-edit">
                        <select
                          aria-label="修改语义类型"
                          value={editingAnnotationKind}
                          onChange={(event) =>
                            setEditingAnnotationKind(event.target.value as AnnotationKind)
                          }
                        >
                          {ANNOTATION_KINDS.map((kind) => (
                            <option key={kind} value={kind}>{ANNOTATION_KIND_LABELS[kind]}</option>
                          ))}
                        </select>
                        <textarea
                          aria-label="修改选区笔记"
                          value={editingAnnotationNote}
                          rows={4}
                          maxLength={8000}
                          onChange={(event) => setEditingAnnotationNote(event.target.value)}
                        />
                        <div>
                          <button type="button" onClick={() => void saveAnnotationEdit()}>
                            保存修改
                          </button>
                          <button type="button" onClick={() => setEditingAnnotationId(null)}>
                            取消
                          </button>
                        </div>
                      </div>
                    ) : (
                      <>
                        <button
                          type="button"
                          className="reader-annotation-anchor"
                          onClick={() => focusAnnotation(annotation)}
                        >
                          <span>{ANNOTATION_KIND_LABELS[annotation.kind]}</span>
                          <q>{annotation.text}</q>
                          {annotation.selector?.version === 2 && (
                            <small>第 {annotation.selector.page} 页</small>
                          )}
                        </button>
                        {annotation.note && (
                          <p className="reader-annotation-note">{annotation.note}</p>
                        )}
                        <div className="reader-annotation-actions">
                          <button type="button" onClick={() => beginAnnotationEdit(annotation)}>
                            编辑
                          </button>
                          <button type="button" onClick={() => void removeAnnotation(annotation.id)}>
                            删除
                          </button>
                        </div>
                      </>
                    )}
                  </article>
                ))}
              </div>
            )}
          </section>
        </aside>
      </div>
    </section>
  );
}

function InlinePdfPage({
  pdfDoc,
  pdfJs,
  page,
  sizeAt100,
  zoom,
  regions,
  originalAnnotations,
  activeIndex,
  activeRegionId,
  sourcePdfSha256,
  onDisplayRendered,
  onTextLayerAvailability,
  onPdfTextSelection,
  onPdfTextSelectionError,
}: {
  pdfDoc: PDFDocumentProxy;
  pdfJs: PdfJsModule;
  page: number;
  sizeAt100: PdfPageCssSize;
  zoom: number;
  regions: readonly TranslationLayoutRegion[];
  originalAnnotations: readonly Annotation[];
  activeIndex: number | null;
  activeRegionId: string | null;
  sourcePdfSha256: string;
  onDisplayRendered: (page: number) => void;
  onTextLayerAvailability: (page: number, available: boolean) => void;
  onPdfTextSelection: (selection: PdfTextSelection, rect: DOMRect) => void;
  onPdfTextSelectionError: (message: string | null) => void;
}) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const textLayerRef = useRef<HTMLDivElement | null>(null);
  const hasPresentedDisplayRef = useRef(false);
  const [rendering, setRendering] = useState(true);
  const [renderError, setRenderError] = useState<string | null>(null);
  const [renderAttempt, setRenderAttempt] = useState(0);
  const interactionZIndexByRegionId = useMemo(() => {
    const ordered = [...regions].sort(compareRegionStackingPriority);
    return new Map(ordered.map((region, index) => [region.region_id, index + 1]));
  }, [regions]);
  const expectedRenderSignature = `${page}:${sizeAt100.widthPx}:${sizeAt100.heightPx}:${zoom}`;
  const [renderedSignature, setRenderedSignature] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    let renderTask: RenderTask | null = null;
    let pdfPage: PDFPageProxy | null = null;

    async function renderDisplay() {
      if (!hasPresentedDisplayRef.current) setRendering(true);
      setRenderedSignature(null);
      setRenderError(null);
      const canvas = canvasRef.current;
      if (!canvas) return;
      try {
        pdfPage = await pdfDoc.getPage(page);
        if (cancelled) return;
        const baseViewport = pdfPage.getViewport({ scale: 1 });
        const cssWidth = sizeAt100.widthPx * zoom;
        const cssHeight = sizeAt100.heightPx * zoom;
        const viewport = pdfPage.getViewport({ scale: cssWidth / baseViewport.width });
        const outputScale = getPdfCanvasOutputScale({
          widthPx: viewport.width,
          heightPx: viewport.height,
          devicePixelRatio: window.devicePixelRatio || 1,
        });
        const previewOutputScale =
          page === 1 && !hasPresentedDisplayRef.current
            ? Math.min(outputScale, INITIAL_PAGE_PREVIEW_OUTPUT_SCALE)
            : outputScale;
        // Keep the last readable bitmap visible and scale it with the page shell.
        // Mutating the visible canvas backing store before PDF.js finishes would
        // clear it and create a black/blank flash during trackpad zoom.
        canvas.style.width = `${cssWidth}px`;
        canvas.style.height = `${cssHeight}px`;
        const renderedCanvas = canvas.ownerDocument.createElement("canvas");
        renderedCanvas.width = Math.max(1, Math.round(viewport.width * previewOutputScale));
        renderedCanvas.height = Math.max(1, Math.round(viewport.height * previewOutputScale));
        const context = renderedCanvas.getContext("2d", { alpha: false });
        if (!context) throw new Error("PDF canvas unavailable");
        renderTask = pdfPage.render({
          canvas: renderedCanvas,
          canvasContext: context,
          viewport,
          transform:
            previewOutputScale === 1
              ? undefined
              : [previewOutputScale, 0, 0, previewOutputScale, 0, 0],
        });
        await renderTask.promise;
        if (!cancelled) {
          canvas.width = renderedCanvas.width;
          canvas.height = renderedCanvas.height;
          const visibleContext = canvas.getContext("2d", { alpha: false });
          if (!visibleContext) throw new Error("PDF canvas unavailable");
          visibleContext.drawImage(renderedCanvas, 0, 0);
          hasPresentedDisplayRef.current = true;
          canvas.dataset.displayQuality =
            previewOutputScale < outputScale ? "preview" : "full";
          setRenderedSignature(expectedRenderSignature);
          setRendering(false);
        }
        if (!cancelled && previewOutputScale < outputScale) {
          // Let React remove the loading mask and let the browser paint the
          // readable preview before full-resolution work and secondary panels.
          await new Promise<void>((resolve) => {
            window.requestAnimationFrame(() => window.setTimeout(resolve, 0));
          });
          if (cancelled) return;
          const fullCanvas = canvas.ownerDocument.createElement("canvas");
          fullCanvas.width = Math.max(1, Math.round(viewport.width * outputScale));
          fullCanvas.height = Math.max(1, Math.round(viewport.height * outputScale));
          const fullContext = fullCanvas.getContext("2d", { alpha: false });
          if (fullContext) {
            try {
              renderTask = pdfPage.render({
                canvas: fullCanvas,
                canvasContext: fullContext,
                viewport,
                transform:
                  outputScale === 1
                    ? undefined
                    : [outputScale, 0, 0, outputScale, 0, 0],
              });
              await renderTask.promise;
              if (!cancelled) {
                canvas.width = fullCanvas.width;
                canvas.height = fullCanvas.height;
                const upgradedContext = canvas.getContext("2d", { alpha: false });
                if (!upgradedContext) throw new Error("PDF canvas unavailable");
                upgradedContext.drawImage(fullCanvas, 0, 0);
                canvas.dataset.displayQuality = "full";
              }
            } catch (error) {
              if (cancelled || isRenderCancelled(error)) return;
              // 预览已可读；后台清晰度升级失败不应重新遮挡或清空原页。
            }
          }
        }
        if (!cancelled) onDisplayRendered(page);
      } catch (error) {
        if (!cancelled && !isRenderCancelled(error)) {
          setRenderError(`第 ${page} 页渲染失败`);
        }
      } finally {
        if (!cancelled) setRendering(false);
      }
    }

    void renderDisplay();
    return () => {
      cancelled = true;
      renderTask?.cancel();
      pdfPage?.cleanup();
    };
  }, [expectedRenderSignature, onDisplayRendered, page, pdfDoc, renderAttempt, sizeAt100.heightPx, sizeAt100.widthPx, zoom]);

  useEffect(() => {
    if (renderedSignature !== expectedRenderSignature) return;
    const container = textLayerRef.current;
    if (!container) return;
    const textLayerContainer = container;
    let cancelled = false;
    let textLayer: InstanceType<PdfJsModule["TextLayer"]> | null = null;
    let pdfPage: PDFPageProxy | null = null;
    const annotationRanges: Array<{ kind: AnnotationKind; range: Range }> = [];

    async function renderTextLayer() {
      textLayerContainer.replaceChildren();
      textLayerContainer.dataset.textLayerReady = "false";
      try {
        pdfPage = await pdfDoc.getPage(page);
        if (cancelled) return;
        const baseViewport = pdfPage.getViewport({ scale: 1 });
        const viewport = pdfPage.getViewport({
          scale: (sizeAt100.widthPx * zoom) / baseViewport.width,
        });
        textLayerContainer.style.setProperty("--total-scale-factor", String(viewport.scale));
        textLayerContainer.style.setProperty("--scale-round-x", "1px");
        textLayerContainer.style.setProperty("--scale-round-y", "1px");
        textLayer = new pdfJs.TextLayer({
          textContentSource: pdfPage.streamTextContent({ includeMarkedContent: true }),
          container: textLayerContainer,
          viewport,
        });
        await textLayer.render();
        if (cancelled) return;
        textLayer.textDivs.forEach((element, index) => {
          element.dataset.textItemIndex = String(index);
        });
        const selectableText = textLayer.textDivs
          .map((element) => element.textContent ?? "")
          .join("")
          .trim();
        const textLayerAvailable = selectableText.length >= 2;
        textLayerContainer.dataset.textLayerReady = textLayerAvailable ? "true" : "unavailable";
        onTextLayerAvailability(page, textLayerAvailable);
        for (const annotation of originalAnnotations) {
          const selector = annotation.selector;
          if (
            selector?.version !== 2 ||
            selector.page !== page ||
            selector.source_pdf_sha256 !== sourcePdfSha256 ||
            selector.quote.exact !== annotation.text
          ) {
            continue;
          }
          const range = rangeFromTextItemAnchors(
            textLayerContainer,
            selector.start,
            selector.end,
          );
          if (!range || range.toString() !== annotation.text) continue;
          annotationRanges.push({ kind: annotation.kind, range });
          inlineAnnotationRanges.get(annotation.kind)?.add(range);
        }
        syncInlineAnnotationHighlights();
      } catch {
        if (!cancelled) {
          textLayerContainer.replaceChildren();
          textLayerContainer.dataset.textLayerReady = "error";
          onTextLayerAvailability(page, false);
        }
      }
    }

    void renderTextLayer();
    return () => {
      cancelled = true;
      textLayer?.cancel();
      pdfPage?.cleanup();
      for (const { kind, range } of annotationRanges) {
        inlineAnnotationRanges.get(kind)?.delete(range);
      }
      syncInlineAnnotationHighlights();
      textLayerContainer.replaceChildren();
    };
  }, [
    expectedRenderSignature,
    originalAnnotations,
    onTextLayerAvailability,
    page,
    pdfDoc,
    pdfJs,
    renderedSignature,
    sizeAt100.widthPx,
    sourcePdfSha256,
    zoom,
  ]);

  const capturePdfSelection = useCallback(async () => {
    const selection = window.getSelection();
    const textLayer = textLayerRef.current;
    if (!selection || selection.rangeCount !== 1 || !textLayer || textLayer.dataset.textLayerReady !== "true") {
      return;
    }
    const range = selection.getRangeAt(0);
    if (range.collapsed) return;
    const result = await serializePdfTextSelection({
      range,
      textLayer,
      page,
      sourcePdfSha256,
      regions,
    });
    if ("error" in result) {
      onPdfTextSelectionError(result.error);
      return;
    }
    textLayer.dataset.selectionHash = result.selection.text_sha256;
    onPdfTextSelection(result.selection, result.clientRect);
  }, [onPdfTextSelection, onPdfTextSelectionError, page, regions, sourcePdfSha256]);

  return (
    <div className="inline-reader-page" data-page-rendered="true">
      <canvas ref={canvasRef} className="reader-pdf-canvas" />
      {rendering && !hasPresentedDisplayRef.current && (
        <div className="reader-pdf-loading">渲染 P{page}…</div>
      )}
      {renderError && (
        <div className="reader-pdf-page-error" role="alert">
          <p>{renderError}</p>
          <button type="button" onClick={() => setRenderAttempt((value) => value + 1)}>
            重试本页
          </button>
        </div>
      )}
      <div className="reader-pdf-page-number">P{page}</div>
      {!rendering && renderedSignature === expectedRenderSignature && (
        <div
          ref={textLayerRef}
          className="textLayer reader-pdf-text-layer"
          data-layer="text"
          data-pdf-text-page={page}
          data-source-pdf-sha256={sourcePdfSha256}
          onMouseUp={() => void capturePdfSelection()}
          onKeyUp={() => void capturePdfSelection()}
        />
      )}
      {!rendering && renderedSignature === expectedRenderSignature && (
        <div className="reader-evidence-layer" data-layer="evidence" aria-hidden="true">
          {regions.map((region) => {
            const active = activeRegionId
              ? activeRegionId === region.region_id
              : activeIndex === region.block_index;
            return (
              <div
                key={region.region_id}
                className={active ? "reader-evidence-region reader-evidence-region-active" : "reader-evidence-region"}
                data-region-id={region.region_id}
                data-block-index={region.block_index}
                tabIndex={-1}
                style={normalizedBoxStyle(
                  region.bbox,
                  interactionZIndexByRegionId.get(region.region_id) ?? 1,
                )}
              />
            );
          })}
        </div>
      )}
    </div>
  );
}

function normalizedBoxStyle(
  bbox: TranslationLayoutRegion["bbox"],
  zIndex?: number,
) {
  return {
    left: `${bbox.x0 * 100}%`,
    top: `${bbox.y0 * 100}%`,
    width: `${(bbox.x1 - bbox.x0) * 100}%`,
    height: `${(bbox.y1 - bbox.y0) * 100}%`,
    zIndex,
  };
}

function compareRegionStackingPriority(
  left: TranslationLayoutRegion,
  right: TranslationLayoutRegion,
): number {
  const areaDifference = normalizedBoxArea(right.bbox) - normalizedBoxArea(left.bbox);
  if (Math.abs(areaDifference) > Number.EPSILON) return areaDifference;
  return (
    left.bbox.y0 - right.bbox.y0 ||
    left.bbox.x0 - right.bbox.x0 ||
    left.bbox.y1 - right.bbox.y1 ||
    left.bbox.x1 - right.bbox.x1 ||
    left.region_id.localeCompare(right.region_id)
  );
}

function normalizedBoxArea(bbox: TranslationLayoutRegion["bbox"]): number {
  return (bbox.x1 - bbox.x0) * (bbox.y1 - bbox.y0);
}

function isRenderCancelled(error: unknown): boolean {
  return error instanceof Error && error.name === "RenderingCancelledException";
}
