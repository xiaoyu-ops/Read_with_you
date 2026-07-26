import type { Page, Route } from "playwright/test";
import { expect, test } from "playwright/test";


const PAPER_ID = "browser-fixture";
const API_PREFIX = "/api-mock";
const READER_PATH = `/paper/${PAPER_ID}`;
const SESSION_KEY = `peinidu.readerSession.${PAPER_ID}`;
const SPLIT_RATIO_KEY = "peinidu.readerSplitRatio.v1";
const HIGH_REGION_ID = "region-high";
const LOW_REGION_ID = "region-low";
const ERROR_REGION_ID = "region-error";
const REMOTE_REGION_ID = "region-page-10";
const HIGH_ORIGINAL = "High confidence source text for the inline reader browser fixture.";
const HIGH_TRANSLATION = "高置信译文会原位出现；高置信译文可点击核对。";
const LOW_TRANSLATION = "低置信译文只能在核对面板中查看。";
const UNMAPPED_TRANSLATION = "这段译文没有可靠坐标，但仍可从未定位译文入口访问。";

type BrowserSseControl = {
  ready: boolean;
  generation: number;
  readonly cancelled: boolean;
  emit(event: string, data: Record<string, unknown>): number;
};

type BrowserWindow = Window & {
  __inlineReaderSse?: BrowserSseControl;
  __inlineReaderOverlayLatencyMs?: number | null;
  __petPdfDocumentLoadCounts?: Record<string, number>;
  __petPdfDocumentLoadTrace?: Array<{ url: string; source: string }>;
  __petPdfDocumentPreloadTrace?: Array<Record<string, unknown>>;
};

type AnnotationPayload = {
  block_index: number;
  side: "original" | "translation";
  text: string;
  note?: string;
  color?: string;
  kind?: "highlight" | "important" | "question" | "method" | "conclusion";
  selector?: {
    version: 1;
    region_id: string | null;
    start_offset: number;
    end_offset: number;
    occurrence: number;
  };
};

type AnnotationRecord = AnnotationPayload & {
  id: string;
  arxiv_id: string;
  note: string;
  color: string;
  kind: "highlight" | "important" | "question" | "method" | "conclusion";
  created_at: string;
  updated_at: string;
};

type PdfExportRunFixture = {
  id: string;
  arxiv_id: string;
  status: "queued" | "running" | "done" | "error" | "cancelled";
  pages_done: number;
  page_count: number;
  progress: number | null;
  error_code: string | null;
  error_message: string | null;
  retryable: boolean;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
};

function pdfExportRun(
  status: PdfExportRunFixture["status"],
  patch: Partial<PdfExportRunFixture> = {},
): PdfExportRunFixture {
  return {
    id: "pdf-export-1",
    arxiv_id: PAPER_ID,
    status,
    pages_done: status === "done" ? 30 : 0,
    page_count: 30,
    progress: status === "done" ? 1 : 0,
    error_code: null,
    error_message: null,
    retryable: status === "error",
    created_at: "2026-07-22T00:00:00Z",
    updated_at: "2026-07-22T00:00:00Z",
    completed_at: ["done", "error", "cancelled"].includes(status)
      ? "2026-07-22T00:01:00Z"
      : null,
    ...patch,
  };
}

const box = (x0: number, y0: number, x1: number, y1: number) => ({ x0, y0, x1, y1 });

function region(
  regionId: string,
  blockIndex: number,
  bbox: ReturnType<typeof box>,
  confidence: number,
  renderPolicy: "replace" | "panel_only",
  page = 1,
) {
  const lineY0 = bbox.y0 + 0.01;
  return {
    region_id: regionId,
    block_index: blockIndex,
    page,
    flow_order: blockIndex,
    kind: "paragraph",
    bbox,
    line_boxes: [box(bbox.x0, lineY0, bbox.x1, lineY0 + 0.015)],
    word_boxes: [],
    protected_boxes: [],
    source_block_order: blockIndex,
    source_line_orders: [blockIndex],
    source_word_orders: [],
    rotation: 0,
    confidence,
    render_policy: renderPolicy,
    failure_reason: renderPolicy === "replace" ? null : "low_confidence",
    geometry_source: "poppler_bbox_layout",
  };
}

const paper = {
  arxiv_id: PAPER_ID,
  title: "Inline PDF Reader Browser Fixture",
  authors: ["Pet QA"],
  source: "local",
  blocks: [
    {
      index: 0,
      type: "paragraph",
      original: HIGH_ORIGINAL,
      translation: null,
      status: "pending",
    },
    {
      index: 1,
      type: "paragraph",
      original: "Low confidence source text remains visible in the PDF.",
      translation: LOW_TRANSLATION,
      status: "done",
    },
    {
      index: 2,
      type: "paragraph",
      original: "A failed translation must never leave an opaque replacement.",
      translation: null,
      status: "pending",
    },
    {
      index: 3,
      type: "paragraph",
      original: "This block has no reliable layout region.",
      translation: UNMAPPED_TRANSLATION,
      status: "done",
    },
    {
      index: 4,
      type: "paragraph",
      original: "This evidence block is located on page ten.",
      translation: null,
      status: "pending",
    },
  ],
};

type ApiMockState = {
  paper: typeof paper;
  pdfBytes: Buffer;
  annotations: AnnotationRecord[];
  annotationPayloads: AnnotationPayload[];
  annotationPatches: Array<{ id: string; note?: string; kind?: string }>;
  deletedAnnotationIds: string[];
  paperNote: {
    arxiv_id: string;
    markdown: string;
    updated_at: string | null;
    revision: string;
  };
  paperNotePuts: Array<{ markdown: string; base_revision: string }>;
  petPayloads: Array<Record<string, unknown>>;
  selectionTranslationPayloads: Array<Record<string, unknown>>;
  selectionTranslationFailuresRemaining: number;
  selectionTranslationDelayMs: number;
  nextAnnotationId: number;
  analysis: Record<string, unknown> | null;
  chatMessages: Array<Record<string, unknown>>;
  chatRuns: Array<Record<string, unknown>>;
  layoutBuildQueries: string[];
  layoutCacheMissing: boolean;
  layoutError: { code: string; message: string } | null;
  canonicalPdfRequests: number;
  pdfExportCapability: Record<string, unknown>;
  pdfExportRuns: PdfExportRunFixture[];
  pdfExportPollSequence: PdfExportRunFixture[];
  pdfExportPollFailuresRemaining: number;
  pdfExportCapabilityRequests: number;
  pdfExportCreateRequests: number;
  pdfExportCancelRequests: number;
  pdfExportCapabilityGate: Promise<void> | null;
  pdfExportCapabilityErrorStatus: number | null;
  pdfExportCreateError: { code: string; message: string; retryable: boolean } | null;
};

function createApiMockState({ translated = false }: { translated?: boolean } = {}): ApiMockState {
  return {
    paper: {
      ...paper,
      blocks: paper.blocks.map((block) => block.index === 0 && translated
        ? { ...block, translation: HIGH_TRANSLATION, status: "done" }
        : { ...block }),
    },
    pdfBytes,
    annotations: [],
    annotationPayloads: [],
    annotationPatches: [],
    deletedAnnotationIds: [],
    paperNote: {
      arxiv_id: PAPER_ID,
      markdown: "",
      updated_at: null,
      revision: "a".repeat(64),
    },
    paperNotePuts: [],
    petPayloads: [],
    selectionTranslationPayloads: [],
    selectionTranslationFailuresRemaining: 0,
    selectionTranslationDelayMs: 0,
    nextAnnotationId: 1,
    analysis: null,
    chatMessages: [],
    chatRuns: [],
    layoutBuildQueries: [],
    layoutCacheMissing: false,
    layoutError: null,
    canonicalPdfRequests: 0,
    pdfExportCapability: {
      enabled: true,
      error_code: null,
      reason: null,
      notice_url: "/pdf-exports/third-party-notice",
      target_language: "zh-CN",
      output_mode: "monolingual",
      sidecar: {
        name: "BabelDOC",
        wrapper_version: "1.0.1",
        version: "2.0.0",
        commit: "fixture-commit",
        image_digest: "sha256:fixture",
        source_code_url: "https://example.test/upstream",
        modified_source_url: "/pdf-exports/wrapper-source",
        license: "AGPL-3.0",
        license_disclosure_complete: true,
        configured: true,
      },
      modified_source_url: "/pdf-exports/wrapper-source",
      limits: { max_source_bytes: 50_000_000, max_pages: 300, timeout_seconds: 1800 },
    },
    pdfExportRuns: [],
    pdfExportPollSequence: [],
    pdfExportPollFailuresRemaining: 0,
    pdfExportCapabilityRequests: 0,
    pdfExportCreateRequests: 0,
    pdfExportCancelRequests: 0,
    pdfExportCapabilityGate: null,
    pdfExportCapabilityErrorStatus: null,
    pdfExportCreateError: null,
  };
}

const layout = {
  version: 1,
  cache_key: "a".repeat(64),
  source_pdf_sha256: "b".repeat(64),
  block_source_sha256: "c".repeat(64),
  adapter: "hybrid_poppler_mineru",
  adapter_version: "1",
  pdf_url: `/papers/${PAPER_ID}/pdf`,
  page_count: 30,
  pages: Array.from({ length: 30 }, (_, index) => ({
    page: index + 1,
    width: 612,
    height: 792,
    rotation: 0,
    protected_boxes: [],
  })),
  regions: [
    region(HIGH_REGION_ID, 0, box(0.08, 0.1, 0.92, 0.25), 0.97, "replace"),
    region(LOW_REGION_ID, 1, box(0.08, 0.32, 0.92, 0.45), 0.78, "panel_only"),
    region(ERROR_REGION_ID, 2, box(0.08, 0.52, 0.92, 0.65), 0.97, "replace"),
    region(REMOTE_REGION_ID, 4, box(0.1, 0.18, 0.9, 0.31), 0.96, "replace", 10),
  ],
  quality: {
    mappable_count: 4,
    mapped_count: 4,
    replaceable_count: 3,
    panel_only_count: 1,
    unmapped_count: 1,
    mapped_ratio: 0.8,
    average_confidence: 0.92,
    protected_overlap_count: 0,
    protected_count: 0,
    unmapped_block_indexes: [3],
    failure_counts: { low_confidence: 1, unmapped: 1 },
  },
  warnings: [],
  sources: [
    { adapter: "poppler_bbox_layout", adapter_version: "3" },
    {
      adapter: "mineru_middle",
      adapter_version: "2",
      generation: "d".repeat(32),
      is_ocr: false,
    },
  ],
};

const pdfBytes = createPdf(30);

function createPdf(pageCount: number, includeText = true): Buffer {
  const objects = new Map<number, Buffer>();
  const pageObjectNumbers = Array.from({ length: pageCount }, (_, index) => 4 + index * 2);
  objects.set(1, Buffer.from("<< /Type /Catalog /Pages 2 0 R >>", "ascii"));
  objects.set(
    2,
    Buffer.from(
      `<< /Type /Pages /Kids [${pageObjectNumbers.map((number) => `${number} 0 R`).join(" ")}] /Count ${pageCount} >>`,
      "ascii",
    ),
  );
  objects.set(3, Buffer.from("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>", "ascii"));

  for (let index = 0; index < pageCount; index += 1) {
    const pageObject = pageObjectNumbers[index];
    const contentObject = pageObject + 1;
    const content = includeText ? `BT /F1 10 Tf 36 756 Td (Page ${index + 1}) Tj ET` : "";
    objects.set(
      pageObject,
      Buffer.from(
        `<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents ${contentObject} 0 R >>`,
        "ascii",
      ),
    );
    objects.set(
      contentObject,
      Buffer.from(`<< /Length ${Buffer.byteLength(content, "ascii")} >>\nstream\n${content}\nendstream`, "ascii"),
    );
  }

  const header = Buffer.from("%PDF-1.4\n%\xe2\xe3\xcf\xd3\n", "latin1");
  const chunks: Buffer[] = [header];
  const offsets = new Map<number, number>();
  let offset = header.length;
  const maxObject = 3 + pageCount * 2;
  for (let objectNumber = 1; objectNumber <= maxObject; objectNumber += 1) {
    const body = objects.get(objectNumber);
    if (!body) throw new Error(`Missing PDF object ${objectNumber}`);
    const prefix = Buffer.from(`${objectNumber} 0 obj\n`, "ascii");
    const suffix = Buffer.from("\nendobj\n", "ascii");
    offsets.set(objectNumber, offset);
    chunks.push(prefix, body, suffix);
    offset += prefix.length + body.length + suffix.length;
  }

  const xrefOffset = offset;
  const xrefLines = [
    `xref\n0 ${maxObject + 1}`,
    "0000000000 65535 f ",
    ...Array.from({ length: maxObject }, (_, index) => {
      const objectNumber = index + 1;
      return `${String(offsets.get(objectNumber)).padStart(10, "0")} 00000 n `;
    }),
    `trailer\n<< /Size ${maxObject + 1} /Root 1 0 R >>`,
    `startxref\n${xrefOffset}\n%%EOF\n`,
  ];
  chunks.push(Buffer.from(xrefLines.join("\n"), "ascii"));
  return Buffer.concat(chunks);
}

async function fulfillJson(route: Route, body: unknown, status = 200): Promise<void> {
  await route.fulfill({
    status,
    contentType: "application/json; charset=utf-8",
    body: JSON.stringify(body),
  });
}

async function installApiMocks(
  page: Page,
  state: ApiMockState,
  fixtureLayout: typeof layout = layout,
): Promise<void> {
  await page.route(`**/assets/${PAPER_ID}/original.pdf`, async (route) => {
    state.canonicalPdfRequests += 1;
    await route.fulfill({
      status: 200,
      contentType: "application/pdf",
      headers: { "Cache-Control": "no-store" },
      body: state.pdfBytes,
    });
  });
  await page.route(`**${API_PREFIX}/**`, async (route) => {
    const url = new URL(route.request().url());
    const pathname = url.pathname.slice(API_PREFIX.length);
    const method = route.request().method();
    if (pathname === "/pdf-exports/capability" && method === "GET") {
      state.pdfExportCapabilityRequests += 1;
      if (state.pdfExportCapabilityGate) await state.pdfExportCapabilityGate;
      if (state.pdfExportCapabilityErrorStatus !== null) {
        await fulfillJson(
          route,
          { detail: "temporary capability fixture failure" },
          state.pdfExportCapabilityErrorStatus,
        );
        return;
      }
      await fulfillJson(route, state.pdfExportCapability);
      return;
    }
    if (pathname === `/papers/${PAPER_ID}/pdf-exports` && method === "GET") {
      await fulfillJson(route, state.pdfExportRuns);
      return;
    }
    if (pathname === `/papers/${PAPER_ID}/pdf-exports` && method === "POST") {
      state.pdfExportCreateRequests += 1;
      if (state.pdfExportCreateError) {
        await fulfillJson(route, { detail: state.pdfExportCreateError }, 413);
        return;
      }
      const queued = pdfExportRun("queued", {
        id: `pdf-export-${state.pdfExportCreateRequests}`,
      });
      state.pdfExportRuns = [queued, ...state.pdfExportRuns];
      await fulfillJson(route, queued, 202);
      return;
    }
    const pdfExportMatch = pathname.match(
      new RegExp(`^/papers/${PAPER_ID}/pdf-exports/([^/]+)(?:/(cancel|download))?$`),
    );
    if (pdfExportMatch && method === "GET" && !pdfExportMatch[2]) {
      if (state.pdfExportPollFailuresRemaining > 0) {
        state.pdfExportPollFailuresRemaining -= 1;
        await fulfillJson(route, { detail: "temporary fixture failure" }, 503);
        return;
      }
      const current = state.pdfExportPollSequence.shift()
        ?? state.pdfExportRuns.find((candidate) => candidate.id === decodeURIComponent(pdfExportMatch[1]));
      if (!current) {
        await fulfillJson(route, { detail: "not found" }, 404);
        return;
      }
      state.pdfExportRuns = [current, ...state.pdfExportRuns.filter((item) => item.id !== current.id)];
      await fulfillJson(route, current);
      return;
    }
    if (pdfExportMatch?.[2] === "cancel" && method === "POST") {
      state.pdfExportCancelRequests += 1;
      const current = state.pdfExportRuns.find(
        (candidate) => candidate.id === decodeURIComponent(pdfExportMatch[1]),
      ) ?? pdfExportRun("running");
      const cancelled = { ...current, status: "cancelled" as const, completed_at: "2026-07-22T00:02:00Z" };
      state.pdfExportRuns = [cancelled, ...state.pdfExportRuns.filter((item) => item.id !== cancelled.id)];
      await fulfillJson(route, cancelled);
      return;
    }
    if (pdfExportMatch?.[2] === "download" && method === "GET") {
      await route.fulfill({ status: 200, contentType: "application/pdf", body: state.pdfBytes });
      return;
    }
    if (pathname === `/papers/${PAPER_ID}/original-pdf/download` && method === "GET") {
      await route.fulfill({ status: 200, contentType: "application/pdf", body: state.pdfBytes });
      return;
    }
    if (pathname === "/search" && method === "POST") {
      await fulfillJson(route, {
        candidates: [{
          arxiv_id: PAPER_ID,
          title: state.paper.title,
          authors: state.paper.authors,
          source: "arxiv",
          extractable: true,
        }],
      });
      return;
    }
    if (pathname === `/papers/${PAPER_ID}`) {
      await fulfillJson(route, state.paper);
      return;
    }
    if (pathname === `/papers/${PAPER_ID}/translation-layout`) {
      const build = url.searchParams.get("build") ?? "";
      state.layoutBuildQueries.push(build);
      if (build === "false" && state.layoutError) {
        await fulfillJson(route, { detail: state.layoutError }, 409);
        return;
      }
      if (build === "false" && state.layoutCacheMissing) {
        await fulfillJson(route, {
          detail: {
            code: "translation_layout_cache_missing",
            message: "没有与当前 PDF 匹配的精准版面缓存。",
          },
        }, 409);
        return;
      }
      await fulfillJson(route, fixtureLayout);
      return;
    }
    if (
      pathname === `/assets/${PAPER_ID}/original.pdf`
      || pathname === `/papers/${PAPER_ID}/pdf`
    ) {
      await route.fulfill({
        status: 200,
        contentType: "application/pdf",
        headers: { "Cache-Control": "no-store" },
        body: state.pdfBytes,
      });
      return;
    }
    if (pathname === `/papers/${PAPER_ID}/annotations` && method === "GET") {
      await fulfillJson(route, state.annotations);
      return;
    }
    if (pathname === `/papers/${PAPER_ID}/paper-note` && method === "GET") {
      await fulfillJson(route, state.paperNote);
      return;
    }
    if (pathname === `/papers/${PAPER_ID}/paper-note` && method === "PUT") {
      const payload = route.request().postDataJSON() as {
        markdown: string;
        base_revision: string;
      };
      state.paperNotePuts.push(payload);
      if (payload.base_revision !== state.paperNote.revision) {
        await fulfillJson(route, {
          detail: {
            code: "paper_note_revision_conflict",
            message: "这份论文笔记已在另一个页面更新。",
            current_revision: state.paperNote.revision,
          },
        }, 409);
        return;
      }
      state.paperNote = {
        arxiv_id: PAPER_ID,
        markdown: payload.markdown,
        updated_at: "2026-07-23T00:00:00Z",
        revision: String.fromCharCode(98 + state.paperNotePuts.length - 1).repeat(64),
      };
      await fulfillJson(route, state.paperNote);
      return;
    }
    if (pathname === `/papers/${PAPER_ID}/annotations` && method === "POST") {
      const payload = route.request().postDataJSON() as AnnotationPayload;
      state.annotationPayloads.push(payload);
      const annotation: AnnotationRecord = {
        ...payload,
        id: `annotation-${state.nextAnnotationId}`,
        arxiv_id: PAPER_ID,
        note: payload.note ?? "",
        color: payload.color ?? "yellow",
        kind: payload.kind ?? "highlight",
        created_at: `2026-07-21T00:00:0${state.nextAnnotationId}Z`,
        updated_at: `2026-07-21T00:00:0${state.nextAnnotationId}Z`,
      };
      state.nextAnnotationId += 1;
      state.annotations.push(annotation);
      await fulfillJson(route, annotation);
      return;
    }
    if (pathname === `/translate/${PAPER_ID}/selection` && method === "POST") {
      const payload = route.request().postDataJSON() as Record<string, unknown>;
      state.selectionTranslationPayloads.push(payload);
      if (state.selectionTranslationDelayMs > 0) {
        await new Promise((resolve) => setTimeout(resolve, state.selectionTranslationDelayMs));
      }
      if (state.selectionTranslationFailuresRemaining > 0) {
        state.selectionTranslationFailuresRemaining -= 1;
        await fulfillJson(route, {
          detail: {
            code: "deeplx_rate_limited",
            message: "翻译服务当前繁忙，请稍后重试。",
            retryable: true,
          },
        }, 502);
        return;
      }
      const sourceText = String(payload.raw_text ?? "");
      await fulfillJson(route, {
        version: 1,
        provider: "deeplx",
        source_text: sourceText,
        source_text_sha256: payload.text_sha256,
        translation: sourceText === "Page 1" ? "第 1 页" : `译文：${sourceText}`,
        translation_sha256: "d".repeat(64),
        page: payload.page,
        block_index: payload.block_index ?? null,
        region_id: payload.region_id ?? null,
        layout_confidence: payload.layout_confidence ?? null,
        source_edited: payload.source_edited === true,
      });
      return;
    }
    const annotationDelete = pathname.match(
      new RegExp(`^/papers/${PAPER_ID}/annotations/([^/]+)$`),
    );
    if (annotationDelete && method === "PATCH") {
      const annotationId = decodeURIComponent(annotationDelete[1]);
      const annotation = state.annotations.find((item) => item.id === annotationId);
      if (!annotation) {
        await fulfillJson(route, { detail: "not found" }, 404);
        return;
      }
      const payload = route.request().postDataJSON() as {
        note?: string;
        kind?: AnnotationRecord["kind"];
      };
      state.annotationPatches.push({ id: annotationId, ...payload });
      if (payload.note !== undefined) annotation.note = payload.note;
      if (payload.kind !== undefined) annotation.kind = payload.kind;
      annotation.updated_at = "2026-07-23T00:00:00Z";
      await fulfillJson(route, annotation);
      return;
    }
    if (annotationDelete && method === "DELETE") {
      const annotationId = decodeURIComponent(annotationDelete[1]);
      const index = state.annotations.findIndex((annotation) => annotation.id === annotationId);
      if (index < 0) {
        await fulfillJson(route, { detail: "not found" }, 404);
        return;
      }
      state.annotations.splice(index, 1);
      state.deletedAnnotationIds.push(annotationId);
      await fulfillJson(route, { status: "deleted" });
      return;
    }
    if (pathname === `/translate/${PAPER_ID}/block/2` && method === "POST") {
      await new Promise((resolve) => setTimeout(resolve, 80));
      await fulfillJson(route, {
        index: 2,
        translation: "重试后的译文",
        status: "done",
      });
      return;
    }
    if (pathname === "/collections") {
      await fulfillJson(route, []);
      return;
    }
    if (pathname === `/analyze/${PAPER_ID}`) {
      if (state.analysis) await fulfillJson(route, state.analysis);
      else await fulfillJson(route, { detail: "not found" }, 404);
      return;
    }
    if (pathname === `/agent/chat/${PAPER_ID}`) {
      await fulfillJson(route, {
        arxiv_id: PAPER_ID,
        messages: state.chatMessages,
        memories: [],
        skills: [],
        runs: state.chatRuns,
      });
      return;
    }
    if (pathname === `/agent/chat/${PAPER_ID}/messages/stream` && method === "POST") {
      state.petPayloads.push(route.request().postDataJSON() as Record<string, unknown>);
      await route.fulfill({
        status: 200,
        contentType: "text/event-stream; charset=utf-8",
        body: `event: done\ndata: ${JSON.stringify({
          state: {
            arxiv_id: PAPER_ID,
            messages: [],
            memories: [],
            skills: [],
            runs: [],
          },
        })}\n\n`,
      });
      return;
    }
    await fulfillJson(route, { detail: `Unhandled browser fixture route: ${pathname}` }, 404);
  });
}

async function installSseBridge(page: Page): Promise<void> {
  await page.addInitScript(
    ({ apiPrefix, paperId }) => {
      const target = `${apiPrefix}/translate/${paperId}`;
      const nativeFetch = window.fetch.bind(window);
      window.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
        const requestUrl =
          typeof input === "string"
            ? input
            : input instanceof URL
              ? input.toString()
              : input.url;
        const method = (init?.method ?? (input instanceof Request ? input.method : "GET")).toUpperCase();
        if (!requestUrl.endsWith(target) || method !== "POST") return nativeFetch(input, init);

        const encoder = new TextEncoder();
        let streamController: ReadableStreamDefaultController<Uint8Array> | null = null;
        let cancelled = false;
        const generation = ((window as BrowserWindow).__inlineReaderSse?.generation ?? 0) + 1;
        const stream = new ReadableStream<Uint8Array>({
          start(controller) {
            streamController = controller;
          },
          cancel() {
            cancelled = true;
          },
        });
        init?.signal?.addEventListener("abort", () => {
          cancelled = true;
          streamController?.error(new DOMException("Aborted", "AbortError"));
        }, { once: true });
        (window as BrowserWindow).__inlineReaderSse = {
          ready: true,
          generation,
          get cancelled() {
            return cancelled;
          },
          emit(event, data) {
            if (!streamController || cancelled) throw new Error("Translation SSE is not writable");
            const emittedAt = performance.now();
            streamController.enqueue(
              encoder.encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`),
            );
            return emittedAt;
          },
        };
        return new Response(stream, {
          status: 200,
          headers: { "Content-Type": "text/event-stream; charset=utf-8" },
        });
      }) as typeof window.fetch;
    },
    { apiPrefix: API_PREFIX, paperId: PAPER_ID },
  );
}

async function seedLegacySession(page: Page): Promise<void> {
  await page.addInitScript(
    ({ key, paperId }) => {
      if (window.localStorage.getItem(key) !== null) return;
      window.localStorage.setItem(
        key,
        JSON.stringify({
          version: 1,
          paperId,
          activeIndex: 1,
          pdfPage: 1,
          pdfScrollTop: 0,
          pdfZoomPercent: 125,
          rightPaneSide: "translation",
          rightScrollTop: 420,
          updatedAt: "2026-07-21T00:00:00.000Z",
        }),
      );
    },
    { key: SESSION_KEY, paperId: PAPER_ID },
  );
}

async function seedLowZoomSession(page: Page): Promise<void> {
  await page.addInitScript(
    ({ key, paperId }) => {
      window.localStorage.setItem(
        key,
        JSON.stringify({
          version: 2,
          paperId,
          readerMode: "inline_translation",
          activeIndex: null,
          pdfPage: 1,
          pdfScrollTop: 0,
          pdfZoomPercent: 25,
          inspector: null,
          updatedAt: "2026-07-21T00:00:00.000Z",
        }),
      );
    },
    { key: SESSION_KEY, paperId: PAPER_ID },
  );
}

async function openReader(
  page: Page,
  session: "default" | "legacy" | "low_zoom" = "default",
  state: ApiMockState = createApiMockState(),
  fixtureLayout: typeof layout = layout,
): Promise<ApiMockState> {
  await installApiMocks(page, state, fixtureLayout);
  await installSseBridge(page);
  if (session === "legacy") await seedLegacySession(page);
  if (session === "low_zoom") await seedLowZoomSession(page);
  await page.goto(READER_PATH);
  await expect(page.locator('[data-reader-mode="selection_translation"]')).toBeVisible();
  await expect(page.locator(".inline-reader-loading")).toHaveCount(0);
  await expect(page.locator(".inline-reader-page-shell")).toHaveCount(fixtureLayout.page_count);
  await expect(page.locator(".reader-pdf-loading")).toHaveCount(0);
  return state;
}

async function documentOverflow(page: Page): Promise<number> {
  return page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
}

async function selectText(
  page: Page,
  selector: string,
  text: string,
  occurrence = 0,
): Promise<void> {
  await page.locator(selector).evaluate((target, selectionTarget) => {
    const walker = document.createTreeWalker(target, NodeFilter.SHOW_TEXT);
    let textNode: Text | null = null;
    let index = -1;
    let seen = 0;
    while (walker.nextNode()) {
      const candidate = walker.currentNode as Text;
      let cursor = 0;
      while (cursor <= candidate.data.length) {
        const candidateIndex = candidate.data.indexOf(selectionTarget.text, cursor);
        if (candidateIndex < 0) break;
        if (seen === selectionTarget.occurrence) {
          textNode = candidate;
          index = candidateIndex;
          break;
        }
        seen += 1;
        cursor = candidateIndex + selectionTarget.text.length;
      }
      if (textNode) break;
    }
    if (!textNode || index < 0) {
      throw new Error(`Selection text occurrence not found: ${selectionTarget.text}`);
    }
    const range = document.createRange();
    range.setStart(textNode, index);
    range.setEnd(textNode, index + selectionTarget.text.length);
    const selection = window.getSelection();
    selection?.removeAllRanges();
    selection?.addRange(range);
    target.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
  }, { text, occurrence });
}

async function selectPdfTextItem(page: Page, pageNumber = 1): Promise<string> {
  const layer = page.locator(`[data-pdf-text-page="${pageNumber}"][data-text-layer-ready="true"]`);
  await expect(layer).toBeVisible();
  return layer.evaluate((target) => {
    const span = target.querySelector<HTMLElement>("[data-text-item-index]");
    const textNode = span?.firstChild;
    const text = textNode?.textContent ?? "";
    if (!span || !textNode || text.length < 2) throw new Error("PDF TextLayer fixture is missing text");
    const range = document.createRange();
    range.setStart(textNode, 0);
    range.setEnd(textNode, text.length);
    const selection = window.getSelection();
    selection?.removeAllRanges();
    selection?.addRange(range);
    target.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
    return text;
  });
}

async function selectAcrossInspectorFormula(page: Page): Promise<void> {
  await page.locator(".inline-inspector-content").evaluate((target) => {
    const before = document.createElement("span");
    before.textContent = "Text before formula ";
    const formula = document.createElement("span");
    formula.className = "reader-math";
    formula.textContent = "E=mc²";
    const after = document.createElement("span");
    after.textContent = " text after formula";
    target.replaceChildren(before, formula, after);
    const textParts = target.querySelectorAll(":scope > span:not(.reader-math)");
    const first = textParts.item(0).firstChild;
    const last = textParts.item(textParts.length - 1).firstChild;
    if (!first || !last) throw new Error("Formula selection fixture is missing");
    const range = document.createRange();
    range.setStart(first, Math.max(0, (first.textContent?.length ?? 1) - 4));
    range.setEnd(last, Math.min(4, last.textContent?.length ?? 0));
    const selection = window.getSelection();
    selection?.removeAllRanges();
    selection?.addRange(range);
    target.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
  });
}

async function selectAcrossReaderRegions(page: Page, overlaySelector: string): Promise<void> {
  await page.evaluate((selector) => {
    const titleNode = document.querySelector(".reader-title")?.firstChild;
    const overlay = document.querySelector(selector);
    const overlayNode = overlay?.firstChild;
    if (!titleNode || !overlay || !overlayNode) throw new Error("Cross-region fixture is missing");
    const range = document.createRange();
    range.setStart(titleNode, 0);
    range.setEnd(overlayNode, Math.min(2, overlayNode.textContent?.length ?? 0));
    const selection = window.getSelection();
    selection?.removeAllRanges();
    selection?.addRange(range);
    overlay.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
  }, overlaySelector);
}

async function inlineHighlightCount(page: Page): Promise<number> {
  return page.evaluate(() => {
    const names = [
      "reader-inline-annotation",
      "reader-inline-annotation-important",
      "reader-inline-annotation-question",
      "reader-inline-annotation-method",
      "reader-inline-annotation-conclusion",
    ];
    return names.reduce((total, name) => {
      const highlight = CSS.highlights?.get(name);
      return total + (highlight ? (highlight as Highlight & { size: number }).size : 0);
    }, 0);
  });
}

test("paper page prefetches the read-only layout and the reader reuses it", async ({ page }) => {
  const state = createApiMockState();
  await openReader(page, "default", state);

  const routeResponse = await page.request.get(READER_PATH);
  const routeLinkHeader = routeResponse.headers().link ?? "";
  expect(routeLinkHeader).toContain(
    `</assets/${PAPER_ID}/original.pdf>; rel=preload; as=fetch; crossorigin=anonymous; fetchpriority=high`,
  );
  expect(routeLinkHeader).toContain(
    `</api/papers/${PAPER_ID}/translation-layout?build=false>; rel=preload; as=fetch; crossorigin=anonymous`,
  );
  expect(state.layoutBuildQueries).toEqual(["false"]);
  expect(state.canonicalPdfRequests).toBeGreaterThanOrEqual(1);
  await expect(
    page.locator(
      `link[rel="preload"][as="fetch"][href$="/assets/${PAPER_ID}/original.pdf"]`,
    ),
  ).toHaveAttribute("fetchpriority", "high");
  expect(await page.evaluate((paperId) => {
    const url = new URL(`/assets/${paperId}/original.pdf`, window.location.href).toString();
    return ((window as BrowserWindow).__petPdfDocumentLoadTrace ?? []).filter(
      (entry) => entry.url === url,
    );
  }, PAPER_ID)).toEqual([{ url: new URL(`/assets/${PAPER_ID}/original.pdf`, page.url()).toString(), source: "server_layout" }]);
  await expect(page.locator('[data-page-number="1"] .reader-pdf-canvas')).toHaveAttribute(
    "data-display-quality",
    "full",
  );
});

test("client navigation starts one shared PDF preload before the reader effect", async ({ page }) => {
  const state = createApiMockState();
  await installApiMocks(page, state);
  await installSseBridge(page);
  let paperRequests = 0;
  let releasePaperResponse!: () => void;
  const paperResponseGate = new Promise<void>((resolve) => {
    releasePaperResponse = resolve;
  });
  await page.route(`**${API_PREFIX}/papers/${PAPER_ID}`, async (route) => {
    paperRequests += 1;
    if (paperRequests > 1) await paperResponseGate;
    await fulfillJson(route, state.paper);
  });

  await page.goto("/");
  await page.getByPlaceholder("输入论文标题 / arXiv ID / URL").fill(PAPER_ID);
  await page.getByRole("button", { name: "检索", exact: true }).click();
  await page.getByRole("button", { name: "打开阅读", exact: true }).click();

  await expect.poll(() => paperRequests).toBeGreaterThanOrEqual(2);
  await expect.poll(() => state.canonicalPdfRequests).toBe(1);
  releasePaperResponse();

  await expect(page).toHaveURL(new RegExp(`/paper/${PAPER_ID.replaceAll(".", "\\.")}$`));
  await expect(page.locator('[data-reader-mode="selection_translation"]')).toBeVisible();
  await expect(page.locator('[data-page-number="1"] [data-page-rendered="true"]')).toBeAttached();
  await expect(page.locator('[data-page-number="1"] .reader-pdf-canvas')).toBeVisible();
  await expect(page.locator('[data-page-number="1"] .reader-pdf-loading')).toHaveCount(0);
  expect(state.canonicalPdfRequests).toBeGreaterThanOrEqual(1);
  expect(await page.evaluate((paperId) => {
    const url = new URL(`/assets/${paperId}/original.pdf`, window.location.href).toString();
    const target = window as BrowserWindow;
    return {
      loads: (target.__petPdfDocumentLoadTrace ?? []).filter((entry) => entry.url === url),
      events: target.__petPdfDocumentPreloadTrace ?? [],
    };
  }, PAPER_ID)).toEqual({
    loads: [{ url: new URL(`/assets/${PAPER_ID}/original.pdf`, page.url()).toString(), source: "route_bridge" }],
    events: [],
  });
});

test("layout prefetch builds only after an explicit cache-missing response", async ({ page }) => {
  const state = createApiMockState();
  state.layoutCacheMissing = true;
  await openReader(page, "default", state);

  expect(state.layoutBuildQueries).toEqual(["false", "true"]);
});

test("layout prefetch leaves the paper page usable and does not build on other errors", async ({ page }) => {
  const state = createApiMockState();
  state.layoutError = {
    code: "layout_unavailable",
    message: "当前版面来源不可用。",
  };
  await installApiMocks(page, state);
  await installSseBridge(page);

  await page.goto(READER_PATH);

  await expect(page.getByRole("heading", { name: state.paper.title })).toBeVisible();
  await expect(page.locator(".reader-inline-error")).toContainText(
    "这份 PDF 暂时无法生成可靠版面",
  );
  expect(state.layoutBuildQueries.length).toBeGreaterThan(0);
  expect(state.layoutBuildQueries.every((build) => build === "false")).toBe(true);
});

test("reader starts as a persistent resizable split without ever covering the PDF", async ({ page }) => {
  const state = createApiMockState();
  await openReader(page, "default", state);

  const main = page.locator(".inline-reader-main");
  const workbench = page.locator(".inline-reader-workbench");
  const panel = page.getByRole("region", { name: "论文阅读笔记" });
  const separator = page.getByRole("separator", { name: "调整阅读与翻译宽度" });
  const viewport = page.getByTestId("pdf-scroll-viewport");
  const firstPage = page.locator('[data-page-number="1"]');

  await expect(main).toHaveAttribute("data-reader-layout", "split");
  await expect(panel).toBeVisible();
  await expect(panel).toContainText("在左侧 PDF 中划选原文，译文和选区笔记会出现在这里");
  await expect(panel.getByTestId("paper-note-editor")).toBeVisible();
  await expect(panel.getByRole("button", { name: "关闭选区翻译" })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "多 Agent 深度分析" })).toHaveCount(0);
  await expect(separator).toHaveAttribute("aria-valuenow", "60");

  const initialWorkbench = await workbench.boundingBox();
  const initialSeparator = await separator.boundingBox();
  const initialPanel = await panel.boundingBox();
  const initialViewport = await viewport.boundingBox();
  const initialPage = await firstPage.boundingBox();
  expect(initialWorkbench).not.toBeNull();
  expect(initialSeparator).not.toBeNull();
  expect(initialPanel).not.toBeNull();
  expect(initialViewport).not.toBeNull();
  expect(initialPage).not.toBeNull();
  expect(initialWorkbench!.x + initialWorkbench!.width).toBeLessThanOrEqual(initialSeparator!.x + 1);
  expect(initialSeparator!.x + initialSeparator!.width).toBeLessThanOrEqual(initialPanel!.x + 1);
  expect(initialPage!.width).toBeLessThanOrEqual(initialViewport!.width + 1);

  await page.mouse.move(
    initialSeparator!.x + initialSeparator!.width / 2,
    initialSeparator!.y + 40,
  );
  await page.mouse.down();
  await page.mouse.move(initialSeparator!.x + 120, initialSeparator!.y + 40, { steps: 5 });
  await page.mouse.up();
  await expect(separator).not.toHaveAttribute("aria-valuenow", "60");
  const draggedRatio = Number(await separator.getAttribute("aria-valuenow"));
  expect(draggedRatio).toBeGreaterThan(60);
  await expect.poll(() => page.evaluate((key) => localStorage.getItem(key), SPLIT_RATIO_KEY)).toBe(String(draggedRatio));

  const draggedWorkbench = await workbench.boundingBox();
  const draggedPanel = await panel.boundingBox();
  const draggedPage = await firstPage.boundingBox();
  const draggedViewport = await viewport.boundingBox();
  expect(draggedWorkbench!.width).toBeGreaterThan(initialWorkbench!.width);
  expect(draggedWorkbench!.x + draggedWorkbench!.width).toBeLessThanOrEqual(draggedPanel!.x);
  expect(draggedPage!.width).toBeLessThanOrEqual(draggedViewport!.width + 1);

  await separator.press("ArrowLeft");
  await expect(separator).toHaveAttribute("aria-valuenow", String(draggedRatio - 2));
  await page.reload();
  await expect(separator).toHaveAttribute("aria-valuenow", String(draggedRatio - 2));

  const zoomInput = page.getByLabel("PDF 缩放百分比");
  await page.getByRole("button", { name: "放大 PDF" }).click();
  const manualZoom = await zoomInput.inputValue();
  await separator.press("ArrowRight");
  await expect(zoomInput).toHaveValue(manualZoom);
  await page.reload();
  await expect(zoomInput).toHaveValue(manualZoom);
  await page.getByRole("button", { name: "适宽" }).click();
  await separator.press("ArrowLeft");
  const refittedPage = await firstPage.boundingBox();
  const refittedViewport = await viewport.boundingBox();
  expect(refittedPage!.width).toBeLessThanOrEqual(refittedViewport!.width + 1);

  await page.setViewportSize({ width: 760, height: 900 });
  const stackedWorkbench = await workbench.boundingBox();
  const stackedPanel = await panel.boundingBox();
  expect(stackedWorkbench).not.toBeNull();
  expect(stackedPanel).not.toBeNull();
  expect(stackedPanel!.y).toBeGreaterThanOrEqual(stackedWorkbench!.y + stackedWorkbench!.height);
  await expect(separator).toBeHidden();
});

test("trackpad pinch zoom is continuous, anchored and isolated to the PDF viewport", async ({ page }) => {
  const state = createApiMockState();
  await openReader(page, "default", state);

  const viewport = page.getByTestId("pdf-scroll-viewport");
  const firstPage = page.locator('[data-page-number="1"]');
  const zoomInput = page.getByLabel("PDF 缩放百分比");
  const fitButton = page.getByRole("button", { name: "适宽" });
  const panel = page.getByRole("region", { name: "论文阅读笔记" });
  await expect(firstPage).toBeVisible();
  await page.waitForTimeout(350);
  await expect(firstPage.locator(".reader-pdf-loading")).toHaveCount(0);
  await firstPage.evaluate((node) => {
    node.dataset.loadingFlashCount = "0";
    const observer = new MutationObserver(() => {
      if (node.querySelector(".reader-pdf-loading")) {
        node.dataset.loadingFlashCount = String(
          Number(node.dataset.loadingFlashCount ?? "0") + 1,
        );
      }
    });
    observer.observe(node, { childList: true, subtree: true });
    window.setTimeout(() => observer.disconnect(), 1_000);
  });

  const pageBefore = await firstPage.boundingBox();
  const panelBefore = await panel.boundingBox();
  expect(pageBefore).not.toBeNull();
  expect(panelBefore).not.toBeNull();
  const zoomBefore = Number(await zoomInput.inputValue());
  const xRatio = 0.62;
  const yRatio = 0.28;
  const clientX = pageBefore!.x + pageBefore!.width * xRatio;
  const clientY = pageBefore!.y + pageBefore!.height * yRatio;

  const pinchWasNotCancelled = await page.evaluate(
    ({ clientX, clientY }) => {
      const target = document.elementFromPoint(clientX, clientY);
      if (!target) throw new Error("No PDF element under the pinch anchor");
      return target.dispatchEvent(new WheelEvent("wheel", {
        bubbles: true,
        cancelable: true,
        clientX,
        clientY,
        ctrlKey: true,
        deltaY: -17,
        deltaMode: WheelEvent.DOM_DELTA_PIXEL,
      }));
    },
    { clientX, clientY },
  );
  expect(pinchWasNotCancelled).toBe(false);

  await expect.poll(async () => Number(await zoomInput.inputValue())).not.toBe(zoomBefore);
  const zoomAfterPinch = Number(await zoomInput.inputValue());
  expect(zoomAfterPinch).toBeGreaterThan(zoomBefore);
  expect(zoomAfterPinch - zoomBefore).toBeLessThan(10);
  expect(Number.isInteger(zoomAfterPinch * 10)).toBe(true);
  await expect(fitButton).toBeEnabled();
  await page.waitForTimeout(120);
  await expect(firstPage.locator(".reader-pdf-loading")).toHaveCount(0);
  await expect(firstPage).toHaveAttribute("data-loading-flash-count", "0");

  const pageAfter = await firstPage.boundingBox();
  const panelAfter = await panel.boundingBox();
  expect(pageAfter).not.toBeNull();
  expect(panelAfter).not.toBeNull();
  expect(Math.abs(pageAfter!.x + pageAfter!.width * xRatio - clientX)).toBeLessThanOrEqual(3);
  expect(Math.abs(pageAfter!.y + pageAfter!.height * yRatio - clientY)).toBeLessThanOrEqual(3);
  expect(panelAfter!.x).toBeCloseTo(panelBefore!.x, 0);
  expect(panelAfter!.width).toBeCloseTo(panelBefore!.width, 0);

  const scrollBefore = await viewport.evaluate((node) => node.scrollTop);
  const viewportBox = await viewport.boundingBox();
  expect(viewportBox).not.toBeNull();
  await page.mouse.move(
    viewportBox!.x + viewportBox!.width / 2,
    viewportBox!.y + viewportBox!.height / 2,
  );
  await page.mouse.wheel(0, 180);
  await expect.poll(() => viewport.evaluate((node) => node.scrollTop)).toBeGreaterThan(scrollBefore);
  await expect(zoomInput).toHaveValue(String(zoomAfterPinch));
});

test("official PDF TextLayer selection translates, asks Pet and restores v2 highlights", async ({ page }) => {
  const textLayerRegion = region(
    "textlayer-page-1",
    0,
    box(0.04, 0.02, 0.24, 0.075),
    0.99,
    "replace",
  );
  const selectionLayout = {
    ...layout,
    regions: [...layout.regions, textLayerRegion],
  };
  const state = createApiMockState();
  await openReader(page, "default", state, selectionLayout);

  await expect(page.locator('[data-layer="translation"], .inline-translation-region, .inline-preserved-hitarea')).toHaveCount(0);
  await expect(page.getByRole("button", { name: "开始翻译" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /未定位译文/ })).toHaveCount(0);
  await expect(page.locator('[data-layer="text"][data-text-layer-ready="true"]')).not.toHaveCount(0);

  const selectionStartedAt = await page.evaluate(() => performance.now());
  const panel = page.getByRole("region", { name: "论文阅读笔记" });
  await expect(panel).toBeVisible();
  const selectedText = await selectPdfTextItem(page);
  expect(selectedText).toBe("Page 1");
  await expect(page.getByRole("toolbar", { name: "原文选区操作" })).toHaveCount(0);
  await expect(page.locator(".selection-translation-backdrop")).toHaveCount(0);
  await expect.poll(() => state.selectionTranslationPayloads.length).toBe(1);
  const workbenchBox = await page.locator(".inline-reader-workbench").boundingBox();
  const panelBox = await panel.boundingBox();
  expect(workbenchBox).not.toBeNull();
  expect(panelBox).not.toBeNull();
  expect(panelBox!.x).toBeGreaterThanOrEqual(workbenchBox!.x + workbenchBox!.width);
  expect(panelBox!.width).toBeGreaterThanOrEqual(320);
  const selectionActionLatencyMs = await page.evaluate(
    (startedAt) => performance.now() - startedAt,
    selectionStartedAt,
  );
  expect(selectionActionLatencyMs).toBeLessThan(250);
  await panel.getByRole("button", { name: "问 Pet" }).click();
  await expect.poll(() => state.petPayloads.length).toBe(1);
  expect((state.petPayloads[0] as { context?: { reader?: unknown } }).context?.reader).toMatchObject({
    reader_mode: "selection_translation",
    page: 1,
    region_id: "textlayer-page-1",
    selected_text: {
      block_index: 0,
      side: "original",
      text: "Page 1",
    },
  });
  const closePet = page.getByRole("button", { name: "关闭阅读 Pet" });
  if (await closePet.count()) await closePet.click();

  await selectPdfTextItem(page);
  await expect.poll(() => state.selectionTranslationPayloads.length).toBe(2);

  await expect(panel.getByTestId("selection-translation-text")).toHaveText("第 1 页");
  expect(state.selectionTranslationPayloads[0]).toMatchObject({
    version: 2,
    raw_text: "Page 1",
    text_sha256: "e8b8355262d0af49d5243e0225b02368cf12bcbe0d25afd89f7cab1bbbf151fb",
    block_index: 0,
    region_id: "textlayer-page-1",
    source_edited: false,
  });

  await panel.getByLabel("识别到的英文原文").fill("Page one");
  await expect(panel).toContainText("原文已修改，请重新翻译以更新译文");
  await panel.getByRole("button", { name: "重新翻译" }).click();
  await expect(panel.getByTestId("selection-translation-text")).toHaveText("译文：Page one");
  expect(state.selectionTranslationPayloads[2]).toMatchObject({
    raw_text: "Page one",
    block_index: null,
    region_id: null,
    layout_confidence: null,
    source_edited: true,
  });
  await selectPdfTextItem(page);
  await panel.locator('label[data-annotation-kind="question"]').click();
  await panel.getByLabel("选区笔记 · Markdown").fill("这里的定义是否覆盖所有边界情况？");
  await panel.getByRole("button", { name: "保存高亮和笔记" }).click();
  await expect.poll(() => state.annotationPayloads.length).toBe(1);
  expect(state.annotationPayloads[0]).toMatchObject({
    block_index: 0,
    side: "original",
    text: "Page 1",
    note: "这里的定义是否覆盖所有边界情况？",
    kind: "question",
    selector: {
      version: 2,
      source_pdf_sha256: layout.source_pdf_sha256,
      page: 1,
      region_id: "textlayer-page-1",
    },
  });
  await expect.poll(() => inlineHighlightCount(page)).toBe(1);

  const savedNote = panel.locator('[data-annotation-id="annotation-1"]');
  await savedNote.getByRole("button", { name: "编辑" }).click();
  await savedNote.getByLabel("修改语义类型").selectOption("method");
  await savedNote.getByLabel("修改选区笔记").fill("记录作者的具体实现步骤。");
  await savedNote.getByRole("button", { name: "保存修改" }).click();
  await expect.poll(() => state.annotationPatches.length).toBe(1);
  expect(state.annotationPatches[0]).toMatchObject({
    id: "annotation-1",
    note: "记录作者的具体实现步骤。",
    kind: "method",
  });

  const paperNoteEditor = panel.getByTestId("paper-note-editor");
  await paperNoteEditor.fill("# 阅读笔记\n\n## 核心问题\n方法边界需要继续核对。");
  await expect.poll(() => state.paperNotePuts.length).toBe(1);
  await expect(panel.locator('[data-paper-note-status="saved"]')).toBeVisible();

  await page.reload();
  await expect(page.locator(".reader-pdf-loading")).toHaveCount(0);
  await expect.poll(() => inlineHighlightCount(page)).toBe(1);
  await expect(panel.getByTestId("paper-note-editor")).toHaveValue(
    "# 阅读笔记\n\n## 核心问题\n方法边界需要继续核对。",
  );
  await panel.locator(".reader-saved-notes select").selectOption("method");
  await expect(panel.locator('[data-annotation-id="annotation-1"]')).toBeVisible();
  await panel.locator('[data-annotation-id="annotation-1"] .reader-annotation-anchor').click();
  await expect(page.locator(".inline-reader-page-controls span")).toContainText("1 / 30");
  await panel.locator('[data-annotation-id="annotation-1"]').getByRole("button", { name: "删除" }).click();
  await expect.poll(() => state.deletedAnnotationIds).toEqual(["annotation-1"]);
  await expect.poll(() => inlineHighlightCount(page)).toBe(0);
});

test("paper note revision conflicts keep the local Markdown draft", async ({ page }) => {
  const state = createApiMockState();
  await openReader(page, "default", state);
  const panel = page.getByRole("region", { name: "论文阅读笔记" });
  const editor = panel.getByTestId("paper-note-editor");

  state.paperNote = {
    ...state.paperNote,
    markdown: "# 另一个标签页的修改",
    revision: "f".repeat(64),
  };
  await editor.fill("# 我尚未保存的本地草稿");

  await expect(panel.locator('[data-paper-note-status="conflict"]')).toBeVisible();
  await expect(editor).toHaveValue("# 我尚未保存的本地草稿");
  await expect(panel.getByRole("alert")).toContainText("草稿仍保留在编辑器中");
});

test("selection translation exposes natural retry, cancel and stacked narrow states", async ({ page }) => {
  const state = createApiMockState();
  state.selectionTranslationFailuresRemaining = 1;
  await openReader(page, "default", state);

  await selectPdfTextItem(page);
  const panel = page.getByRole("region", { name: "论文阅读笔记" });
  await expect(panel.getByRole("alert")).toContainText("翻译服务当前繁忙");
  await panel.getByRole("button", { name: "重试翻译" }).click();
  await expect(panel.getByTestId("selection-translation-text")).toHaveText("第 1 页");

  state.selectionTranslationDelayMs = 500;
  await selectPdfTextItem(page);
  await panel.getByRole("button", { name: "取消翻译" }).click();
  await expect(panel.getByRole("alert")).toContainText("已取消本次翻译");

  await page.setViewportSize({ width: 360, height: 800 });
  const stackedGeometry = await page.evaluate(() => {
    const workbench = document.querySelector(".inline-reader-workbench")?.getBoundingClientRect();
    const translation = document.querySelector(".selection-translation-panel")?.getBoundingClientRect();
    return workbench && translation
      ? { workbenchBottom: workbench.bottom, translationTop: translation.top }
      : null;
  });
  expect(stackedGeometry).not.toBeNull();
  expect(stackedGeometry!.translationTop).toBeGreaterThanOrEqual(stackedGeometry!.workbenchBottom);
});

test("scanned pages without an official text layer disable selection explicitly", async ({ page }) => {
  const state = createApiMockState();
  state.pdfBytes = createPdf(2, false);
  const scannedLayout = {
    ...layout,
    page_count: 2,
    pages: layout.pages.slice(0, 2),
    regions: [],
    quality: {
      ...layout.quality,
      mappable_count: 0,
      mapped_count: 0,
      replaceable_count: 0,
      panel_only_count: 0,
      unmapped_count: paper.blocks.length,
      mapped_ratio: 0,
      average_confidence: 0,
      unmapped_block_indexes: paper.blocks.map((block) => block.index),
    },
    sources: [{
      adapter: "mineru_middle",
      adapter_version: "8",
      generation: "e".repeat(32),
      is_ocr: true,
    }],
  };
  await openReader(page, "default", state, scannedLayout);

  await expect(page.locator('[data-pdf-text-page="1"]')).toHaveAttribute(
    "data-text-layer-ready",
    "unavailable",
  );
  await expect(page.getByText("这一页没有可选择的文字层")).toBeVisible();
  await expect(page.getByRole("toolbar", { name: "原文选区操作" })).toHaveCount(0);

  await page.getByRole("button", { name: "下一页" }).click();
  await expect(page.locator('[data-pdf-text-page="2"]')).toHaveAttribute(
    "data-text-layer-ready",
    "unavailable",
  );
  await expect(page.getByText("扫描件需先生成可靠 OCR 文字层")).toBeVisible();
});

test("Pet context follows the visible original PDF page", async ({ page }) => {
  const state = createApiMockState({ translated: true });
  await openReader(page, "default", state);

  for (let pageNumber = 2; pageNumber <= 10; pageNumber += 1) {
    await page.getByRole("button", { name: "下一页" }).click();
    await expect(page.locator(".inline-reader-page-controls span")).toContainText(`${pageNumber} / 30`);
  }
  await expect(page.locator(".inline-reader-page-controls span")).toContainText("10 / 30");
  await page.getByRole("button", { name: "打开阅读 Pet" }).click();
  const petInput = page.getByPlaceholder("问当前论文，或让子 Agent 做任务");
  await petInput.fill("这一页讲了什么？");
  await petInput.press("Enter");
  await expect.poll(() => state.petPayloads.length).toBe(1);
  const payload = state.petPayloads[0] as {
    context?: { reader?: { page?: number; region_id?: string; active_block?: { index?: number } } };
  };
  expect(payload.context?.reader).toMatchObject({
    page: 10,
    region_id: REMOTE_REGION_ID,
    active_block: { index: 4 },
  });
});

test("Pet presents background results as messages without exposing Run history", async ({ page }) => {
  const state = createApiMockState();
  const resultData = {
    summary: "Grounded result.",
    evidence: [
      { claim: "消息证据", location: { block_index: 0 } },
      { claim: "未定位证据", location: { block_index: 3 } },
    ],
    limits: [],
    next_questions: [],
  };
  state.chatMessages = [
    {
      id: "assistant-evidence",
      role: "assistant",
      content: "我找到了两条可以回到论文核对的证据。",
      created_at: "2026-07-21T00:00:00Z",
      meta: { result_data: resultData },
    },
  ];
  state.chatRuns = [
    {
      id: "run-evidence",
      arxiv_id: PAPER_ID,
      task_type: "four_agent_analysis",
      title: "证据任务",
      status: "done",
      user_message: "核对证据",
      inputs: [],
      result: "",
      result_data: {
        ...resultData,
        evidence: [{ claim: "后台任务证据", location: { block_index: 4 } }],
      },
      error: "",
      created_at: "2026-07-21T00:00:00Z",
      updated_at: "2026-07-21T00:00:00Z",
      completed_at: "2026-07-21T00:00:00Z",
    },
  ];
  await openReader(page, "default", state);
  await page.getByRole("button", { name: "打开阅读 Pet" }).click();
  await expect(page.getByText("我找到了两条可以回到论文核对的证据。")).toBeVisible();
  await expect(page.getByText(/后台任务（最近/)).toHaveCount(0);
  await expect(page.getByText("证据任务")).toHaveCount(0);
  await expect(page.getByText("后台任务证据")).toHaveCount(0);

  const petEvidence = page.locator(".pet-evidence-details");
  await expect(petEvidence).toHaveCount(1);
  await page.getByRole("button", { name: "关闭", exact: true }).click();
  await page.getByRole("button", { name: "打开阅读 Pet" }).click();
  await expect(petEvidence).toHaveCount(1);

  await petEvidence.nth(0).locator("summary").click();
  await page.getByRole("button", { name: "未定位证据" }).click();
  await expect(page.locator(".inline-unmapped-drawer")).toHaveCount(0);

  await page.getByRole("button", { name: "消息证据" }).click();
  await expect(page.locator(`[data-region-id="${HIGH_REGION_ID}"]`)).toBeVisible();
  await expect(page.locator(".inline-reader-page-controls span")).toContainText("1 / 30");
});

test("PDF export capability waits for the first readable page without hiding the original download", async ({ page }) => {
  const state = createApiMockState();
  let releaseCapability!: () => void;
  state.pdfExportCapabilityGate = new Promise<void>((resolve) => {
    releaseCapability = resolve;
  });

  await openReader(page, "default", state);

  await expect(page.locator('[data-page-number="1"] .reader-pdf-canvas')).toBeVisible();
  await expect(page.getByRole("link", { name: "下载原始 PDF" })).toHaveAttribute(
    "href",
    `${API_PREFIX}/papers/${PAPER_ID}/original-pdf/download`,
  );
  await expect.poll(() => state.pdfExportCapabilityRequests).toBe(1);
  await expect(page.getByText("正在检查中文 PDF…")).toBeVisible();

  releaseCapability();
  await expect(page.getByRole("button", { name: "生成中文 PDF" })).toBeVisible();
});

test("PDF export creates, polls, and exposes a direct Chinese PDF download", async ({ page }) => {
  const state = createApiMockState();
  state.pdfExportPollSequence = [
    pdfExportRun("running", { progress: 0.4, pages_done: 12 }),
    pdfExportRun("done"),
  ];
  await openReader(page, "default", state);

  await page.getByText("导出说明").click();
  await expect(page.getByText("AGPL-3.0")).toBeVisible();
  await expect(page.getByText("Pet 适配器版本")).toBeVisible();
  await expect(page.getByText("1.0.1")).toBeVisible();
  await expect(page.getByText("fixture-commit")).toBeVisible();
  await expect(page.getByRole("link", { name: "查看上游源码" })).toHaveAttribute(
    "href",
    "https://example.test/upstream",
  );
  await expect(page.getByRole("link", { name: "查看完整第三方声明" })).toHaveAttribute(
    "href",
    `${API_PREFIX}/pdf-exports/third-party-notice`,
  );
  await expect(page.getByRole("link", { name: "查看部署修改源码" })).toHaveAttribute(
    "href",
    `${API_PREFIX}/pdf-exports/wrapper-source`,
  );
  await page.getByText("导出说明").click();
  await page.getByRole("button", { name: "生成中文 PDF" }).click();
  expect(state.pdfExportCreateRequests).toBe(1);
  await expect(page.getByText("中文 PDF 已排队")).toBeVisible();
  await expect(page.getByText("正在生成中文 PDF，已完成 12/30 页")).toBeVisible({ timeout: 5_000 });
  await expect(page.getByRole("link", { name: "下载中文 PDF" })).toHaveAttribute(
    "href",
    `${API_PREFIX}/papers/${PAPER_ID}/pdf-exports/pdf-export-1/download`,
    { timeout: 6_000 },
  );
  await expect(page.getByRole("link", { name: "下载原始 PDF" })).toBeVisible();
});

test("PDF export create errors preserve the backend reason in natural copy", async ({ page }) => {
  const state = createApiMockState();
  state.pdfExportCreateError = {
    code: "source_pdf_too_large",
    message: "technical fixture size error",
    retryable: false,
  };
  await openReader(page, "default", state);

  await page.getByRole("button", { name: "生成中文 PDF" }).click();
  await expect(page.locator(".reader-pdf-export-failure")).toContainText(
    "这份 PDF 超出当前导出大小限制。",
  );
  await expect(page.getByRole("link", { name: "下载原始 PDF" })).toBeVisible();
});

test("PDF export cancellation keeps the reader usable and offers a fresh run", async ({ page }) => {
  const state = createApiMockState();
  state.pdfExportRuns = [pdfExportRun("running", { progress: 0.2, pages_done: 6 })];
  await openReader(page, "default", state);

  await page.getByRole("button", { name: "取消生成" }).click();
  await expect(page.getByText("中文 PDF 生成已取消")).toBeVisible();
  await expect(page.getByRole("button", { name: "重新生成" })).toBeVisible();
  await expect(page.getByRole("link", { name: "下载原始 PDF" })).toBeVisible();
  expect(state.pdfExportCancelRequests).toBe(1);
});

test("disabled capability does not hide or prevent cancellation of a running export", async ({ page }) => {
  const state = createApiMockState();
  state.pdfExportCapability = {
    ...state.pdfExportCapability,
    enabled: false,
    error_code: "export_disabled",
    reason: "sidecar_unavailable",
  };
  state.pdfExportRuns = [pdfExportRun("running", { progress: 0.25, pages_done: 7 })];
  await openReader(page, "default", state);

  await expect(page.getByRole("button", { name: "取消生成" })).toBeVisible();
  await expect(page.getByText("中文 PDF 未启用")).toBeVisible();
  await page.getByRole("button", { name: "取消生成" }).click();
  await expect(page.getByText("中文 PDF 生成已取消")).toBeVisible();
  await expect(page.getByText("当前部署不能新建中文 PDF 任务")).toBeVisible();
  await expect(page.getByRole("button", { name: "重新生成" })).toHaveCount(0);
});

test("disabled capability keeps an already completed Chinese PDF downloadable", async ({ page }) => {
  const state = createApiMockState();
  state.pdfExportCapability = {
    ...state.pdfExportCapability,
    enabled: false,
    error_code: "export_disabled",
    reason: "sidecar_not_configured",
  };
  state.pdfExportRuns = [pdfExportRun("done")];
  await openReader(page, "default", state);

  await expect(page.getByRole("link", { name: "下载中文 PDF" })).toHaveAttribute(
    "href",
    `${API_PREFIX}/papers/${PAPER_ID}/pdf-exports/pdf-export-1/download`,
  );
  await expect(page.getByRole("link", { name: "下载原始 PDF" })).toBeVisible();
  await expect(page.getByText("中文 PDF 未启用")).toBeVisible();
});

test("capability failure keeps a running export cancellable and renders unknown progress indeterminately", async ({ page }) => {
  await page.setViewportSize({ width: 360, height: 800 });
  const state = createApiMockState();
  state.pdfExportCapabilityErrorStatus = 503;
  state.pdfExportRuns = [pdfExportRun("running", { progress: null, pages_done: 0 })];
  await openReader(page, "default", state);

  await expect(page.getByRole("button", { name: "取消生成" })).toBeVisible();
  await expect(page.getByRole("button", { name: "重新检查中文 PDF" })).toBeVisible();
  await expect(page.locator(".reader-pdf-export-failure")).toContainText(
    "暂时无法确认是否可以新建中文 PDF；已有任务和文件仍可正常查看。",
  );
  const progress = page.getByRole("progressbar", {
    name: "中文 PDF 正在生成，进度暂不可用",
  });
  await expect(progress).toBeVisible();
  await expect(progress).not.toHaveAttribute("value");
  await expect(page.getByText("正在生成中文 PDF，进度暂不可用")).toBeVisible();
  await expect.poll(async () => documentOverflow(page)).toBeLessThanOrEqual(1);
});

test("capability failure keeps an already completed Chinese PDF downloadable", async ({ page }) => {
  const state = createApiMockState();
  state.pdfExportCapabilityErrorStatus = 500;
  state.pdfExportRuns = [pdfExportRun("done")];
  await openReader(page, "default", state);

  await expect(page.getByRole("link", { name: "下载中文 PDF" })).toHaveAttribute(
    "href",
    `${API_PREFIX}/papers/${PAPER_ID}/pdf-exports/pdf-export-1/download`,
  );
  await expect(page.getByRole("button", { name: "重新检查中文 PDF" })).toBeVisible();
  await expect(page.getByRole("button", { name: "生成中文 PDF" })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "下载原始 PDF" })).toBeVisible();
});

test("known PDF export progress remains determinate and exposes its percentage", async ({ page }) => {
  const state = createApiMockState();
  state.pdfExportRuns = [pdfExportRun("running", { progress: 0.4, pages_done: 0 })];
  await openReader(page, "default", state);

  const progress = page.getByRole("progressbar", { name: "中文 PDF 生成进度 40%" });
  await expect(progress).toBeVisible();
  await expect(progress).toHaveAttribute("value", "0.4");
  await expect(page.getByText("正在生成中文 PDF，已完成 40%")).toBeVisible();
});

test("disabled capability explains why an errored export cannot be retried", async ({ page }) => {
  const state = createApiMockState();
  state.pdfExportCapability = {
    ...state.pdfExportCapability,
    enabled: false,
    error_code: "export_disabled",
    reason: "license_disclosure_incomplete",
  };
  state.pdfExportRuns = [pdfExportRun("error", {
    error_code: "sidecar_crashed",
    error_message: "technical fixture crash",
  })];
  await openReader(page, "default", state);

  await expect(page.locator(".reader-pdf-export-failure")).toContainText(
    "中文 PDF 服务意外退出，请稍后重试。",
  );
  await expect(page.getByText("当前部署不能新建中文 PDF 任务")).toBeVisible();
  await expect(page.getByRole("button", { name: "重试生成" })).toHaveCount(0);
});

test("disabled PDF export explains deployment and license status in plain language", async ({ page }) => {
  const state = createApiMockState();
  state.pdfExportCapability = {
    ...state.pdfExportCapability,
    enabled: false,
    reason: "license_disclosure_incomplete",
  };
  await openReader(page, "default", state);

  await page.getByText("中文 PDF 未启用").click();
  await expect(page.getByText("第三方许可证与源码披露尚未完成，因此中文 PDF 导出保持关闭。")).toBeVisible();
  await expect(page.getByRole("link", { name: "查看上游源码" })).toHaveAttribute(
    "href",
    "https://example.test/upstream",
  );
  await expect(page.getByRole("link", { name: "查看完整第三方声明" })).toHaveAttribute(
    "href",
    `${API_PREFIX}/pdf-exports/third-party-notice`,
  );
  await expect(page.getByRole("link", { name: "下载原始 PDF" })).toBeVisible();
});

test("PDF export failure stays readable at 360px and does not remove the original download", async ({ page }) => {
  await page.setViewportSize({ width: 360, height: 800 });
  const state = createApiMockState();
  state.pdfExportRuns = [pdfExportRun("error", {
    error_code: "sidecar_rate_limited",
    error_message: "technical fixture message",
  })];
  await openReader(page, "default", state);

  await expect(page.locator(".reader-pdf-export-failure")).toContainText("翻译服务当前繁忙，请稍后重试。");
  await expect(page.getByRole("button", { name: "重试生成" })).toBeVisible();
  await expect(page.getByRole("link", { name: "下载原始 PDF" })).toBeVisible();
  await expect.poll(async () => documentOverflow(page)).toBeLessThanOrEqual(1);
});

test("PDF export polling waits for three failures and clears the temporary notice after recovery", async ({ page }) => {
  const state = createApiMockState();
  state.pdfExportRuns = [pdfExportRun("running")];
  state.pdfExportPollFailuresRemaining = 3;
  state.pdfExportPollSequence = [pdfExportRun("done")];
  await openReader(page, "default", state);

  const notice = page.locator(".reader-pdf-export-failure").filter({
    hasText: "进度更新暂时中断",
  });
  await expect(notice).toBeVisible({ timeout: 7_000 });
  await expect(page.getByRole("link", { name: "下载中文 PDF" })).toBeVisible({ timeout: 5_000 });
  await expect(notice).toHaveCount(0);
});
