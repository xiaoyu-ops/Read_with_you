/** API 封装 — 调用后端 FastAPI。 */

export const API_BASE = process.env.NEXT_PUBLIC_API_BASE || "http://127.0.0.1:8000";

const SEARCH_CACHE_PREFIX = "peinidu.searchCache.";
const SEARCH_CACHE_TTL_MS = 5 * 60 * 1000;
const TRANSLATION_LAYOUT_CACHE_TTL_MS = 5 * 1000;

type TranslationLayoutCacheEntry = {
  expires_at: number;
  promise: Promise<TranslationLayout>;
};

const translationLayoutCache = new Map<string, TranslationLayoutCacheEntry>();

function notifyPaperDataChanged(paperId: string): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(
    new CustomEvent("peinidu:paper-data-changed", {
      detail: { paperId },
    }),
  );
}

type SearchCacheEntry = {
  saved_at: number;
  candidates: PaperCandidate[];
};

export interface PaperCandidate {
  arxiv_id: string;
  title: string;
  authors: string[];
  abstract: string;
  year?: string | null;
  url: string;
  source: string;
  citation_count?: number | null;
  venue?: string | null;
  match_score?: number | null;
  similarity?: number | null;
  pdf_url?: string | null;
  paper_id?: string | null;
  extractable?: boolean;
}

export interface LiteraturePaper {
  id: string;
  arxiv_id: string | null;
  doi: string | null;
  title: string;
  authors: string[];
  abstract: string;
  year: number | null;
  venue: string | null;
  citation_count: number | null;
  reference_count: number | null;
  is_open_access: boolean;
  pdf_url: string | null;
  url: string;
  similarity: number | null;
  role?: "origin" | "related";
}

export interface LiteratureMapEdge {
  source: string;
  target: string;
  kind: "similarity" | "citation";
  weight: number;
  provenance: string;
}

export interface LiteratureMapWork {
  paper: LiteraturePaper;
  graph_citation_count?: number;
  graph_reference_count?: number;
}

export interface LiteratureMap {
  version: 1;
  origin: LiteraturePaper;
  nodes: LiteraturePaper[];
  edges: LiteratureMapEdge[];
  prior_works: LiteratureMapWork[];
  derivative_works: LiteratureMapWork[];
  status: "complete" | "partial";
  provider: "semantic_scholar";
  retrieved_at: string;
  cached: boolean;
  stale: boolean;
  warnings: string[];
}

export function candidatePaperRef(candidate: Pick<PaperCandidate, "paper_id" | "arxiv_id">): string | null {
  const paperId = candidate.paper_id?.trim();
  if (paperId && /^[a-f0-9]{40}$/i.test(paperId)) return paperId.toLowerCase();
  const arxivId = candidate.arxiv_id?.trim().replace(/v\d+$/i, "");
  return arxivId ? `ARXIV:${arxivId}` : null;
}

export interface Block {
  index: number;
  type: "heading" | "paragraph" | "table" | "code" | "formula" | "figure";
  original: string;
  translation: string | null;
  status: "pending" | "translating" | "done" | "error" | "skip";
  level?: number;
}

export interface PaperDetail {
  arxiv_id: string;
  title: string;
  authors: string[];
  source: string;
  blocks: Block[];
}

export interface PdfExportLimits {
  max_file_bytes?: number | null;
  max_file_size_bytes?: number | null;
  max_source_bytes?: number | null;
  max_pages?: number | null;
  max_output_bytes?: number | null;
  max_concurrent_runs?: number | null;
  timeout_seconds?: number | null;
  [key: string]: number | string | boolean | null | undefined;
}

export interface PdfExportCapability {
  enabled: boolean;
  error_code: string | null;
  reason: string | null;
  notice_url: string;
  target_language: "zh-CN" | string;
  output_mode: "monolingual" | string;
  sidecar: {
    name: string;
    wrapper_version?: string | null;
    version: string;
    commit: string;
    image_digest: string;
    source_code_url: string;
    modified_source_url?: string | null;
    license: string;
    license_disclosure_complete: boolean;
    configured: boolean;
    healthy?: boolean | null;
  };
  limits: PdfExportLimits;
  wrapper_version?: string | null;
  version?: string | null;
  digest?: string | null;
  license?: string | null;
  source_url?: string | null;
  modified_source_url?: string | null;
}

export type PdfExportRunStatus = "queued" | "running" | "done" | "error" | "cancelled";

export interface PdfExportRun {
  id: string;
  arxiv_id: string;
  status: PdfExportRunStatus;
  target_language?: "zh-CN" | string;
  pages_done?: number | null;
  page_count?: number | null;
  progress?: number | null;
  error_code: string | null;
  error_message: string | null;
  retryable?: boolean;
  created_at: string;
  updated_at: string;
  started_at?: string | null;
  completed_at: string | null;
  timestamps?: Record<string, string | null>;
  source_sha256?: string | null;
  output_sha256?: string | null;
  source_pages?: number | null;
  output_pages?: number | null;
  source_bytes?: number | null;
  output_bytes?: number | null;
  download_url?: string | null;
  original_download_url?: string | null;
}

export interface PdfBox {
  page: number;
  x0: number;
  y0: number;
  x1: number;
  y1: number;
  page_width: number;
  page_height: number;
}

export interface BlockPdfMapping {
  block_index: number;
  page: number;
  confidence: number;
  boxes: PdfBox[];
  matched_text: string;
}

export interface BlockPdfMap {
  pdf_url: string;
  page_image_url_template?: string | null;
  page_count: number;
  mappable_count: number;
  mapping_count: number;
  unmapped_count: number;
  mapped_ratio: number;
  average_confidence: number;
  low_confidence_count: number;
  mappings: BlockPdfMapping[];
}

export type TranslationRenderPolicy = "replace" | "preserve" | "panel_only";

export type TranslationLayoutFailureReason =
  | "low_confidence"
  | "unmapped"
  | "overflow"
  | "cross_page"
  | "protected_overlap"
  | "background_complex"
  | "layout_unavailable"
  | "source_pdf_missing"
  | "legacy_mapping_unverified"
  | (string & {});

export interface NormalizedPdfBox {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

export interface TranslationLayoutPage {
  page: number;
  width: number;
  height: number;
  rotation: 0 | 90 | 180 | 270;
  protected_boxes?: NormalizedPdfBox[];
}

export interface TranslationLayoutSource {
  adapter: string;
  adapter_version: string;
  generation?: string | null;
  is_ocr?: boolean | null;
}

export interface TranslationLayoutRegion {
  region_id: string;
  block_index: number;
  page: number;
  flow_order: number;
  kind: string;
  bbox: NormalizedPdfBox;
  line_boxes: NormalizedPdfBox[];
  word_boxes: NormalizedPdfBox[];
  protected_boxes: NormalizedPdfBox[];
  source_block_order: number | null;
  source_line_orders: number[];
  source_word_orders: number[];
  rotation: 0 | 90 | 180 | 270;
  confidence: number;
  render_policy: TranslationRenderPolicy;
  failure_reason: TranslationLayoutFailureReason | null;
  geometry_source?: string | null;
}

export interface TranslationLayoutQuality {
  mappable_count: number;
  mapped_count: number;
  replaceable_count: number;
  panel_only_count: number;
  unmapped_count: number;
  mapped_ratio: number;
  average_confidence: number;
  protected_overlap_count: number;
  protected_count: number;
  unmapped_block_indexes: number[];
  failure_counts: Record<string, number>;
}

export interface TranslationLayout {
  version: number;
  cache_key: string;
  source_pdf_sha256: string;
  block_source_sha256: string;
  adapter: string;
  adapter_version: string;
  pdf_url: string;
  page_count: number;
  pages: TranslationLayoutPage[];
  regions: TranslationLayoutRegion[];
  quality: TranslationLayoutQuality;
  warnings: string[];
  sources?: TranslationLayoutSource[];
}

export interface SelectionTranslationRequest {
  version: 2;
  source_pdf_sha256: string;
  page: number;
  raw_text: string;
  text_sha256: string;
  start: { item_index: number; char_offset: number };
  end: { item_index: number; char_offset: number };
  quote: { exact: string; prefix: string; suffix: string };
  rects: NormalizedPdfBox[];
  block_index: number | null;
  region_id: string | null;
  layout_confidence: number | null;
  source_edited?: boolean;
}

export interface SelectionTranslationResponse {
  version: 1;
  provider: "deeplx";
  source_text: string;
  source_text_sha256: string;
  translation: string;
  translation_sha256: string;
  page: number;
  block_index: number | null;
  region_id: string | null;
  layout_confidence: number | null;
  source_edited: boolean;
}

export interface PaperMeta {
  arxiv_id: string;
  title: string;
  authors: string[];
  source: string;
  status: string;
  created_at?: string | null;
  selection_note_count?: number;
  has_paper_note?: boolean;
  note_updated_at?: string | null;
  note_preview?: string;
}

export interface CollectionSummary {
  id: number;
  name: string;
  paper_count: number;
  contains_paper: boolean;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface CollectionPaper extends PaperMeta {
  added_at?: string | null;
  note_kind_counts?: Record<string, number>;
}

export interface CollectionDetail {
  id: number;
  name: string;
  created_at?: string | null;
  updated_at?: string | null;
  papers: CollectionPaper[];
}

export interface AnnotationSelectorV1 {
  version: 1;
  region_id: string | null;
  start_offset: number;
  end_offset: number;
  occurrence: number;
}

export interface AnnotationSelectorV2 {
  version: 2;
  source_pdf_sha256: string;
  page: number;
  start: { item_index: number; char_offset: number };
  end: { item_index: number; char_offset: number };
  quote: { exact: string; prefix: string; suffix: string };
  rects: NormalizedPdfBox[];
  region_id: string | null;
  layout_confidence: number | null;
}

export type AnnotationSelector = AnnotationSelectorV1 | AnnotationSelectorV2;
export type AnnotationKind =
  | "highlight"
  | "important"
  | "question"
  | "method"
  | "conclusion";

export interface Annotation {
  id: string;
  arxiv_id: string;
  block_index: number;
  side: "original" | "translation";
  text: string;
  note: string;
  color: string;
  kind: AnnotationKind;
  created_at: string;
  updated_at: string;
  selector?: AnnotationSelector | null;
}

export interface PaperNote {
  arxiv_id: string;
  markdown: string;
  updated_at: string | null;
  revision: string;
}

export class PaperNoteRevisionConflictError extends Error {
  currentRevision: string | null;

  constructor(message: string, currentRevision: string | null) {
    super(message);
    this.name = "PaperNoteRevisionConflictError";
    this.currentRevision = currentRevision;
  }
}

export interface AgentTask {
  id: number;
  arxiv_id: string;
  collection_id?: number | null;
  collection_name?: string | null;
  paper_title?: string | null;
  task_type: string;
  status: string;
  summary: string;
  error: string;
  created_at?: string | null;
  updated_at?: string | null;
  completed_at?: string | null;
}

export interface AgentPermissionRequestMeta {
  scope: string;
  label: string;
  description: string;
  original_message: string;
  run_id?: string;
  memory_proposal?: {
    content: string;
    kind: string;
  };
}

export interface AgentToolTraceStepMeta {
  tool: string;
  label?: string;
  kind?: string;
  status?: string;
  title?: string;
  source?: string;
  url?: string;
  arguments?: string;
  error?: string;
  evidence_count?: number;
}

export interface AgentToolTraceMeta {
  name?: string;
  sequence: string[];
  steps: AgentToolTraceStepMeta[];
  evidence_count?: number;
  mock?: boolean;
}

export interface AgentChatMessageMeta {
  pending?: boolean;
  kind?: string;
  run_id?: string | null;
  result_data?: AgentRunResultData | null;
  permission_request?: AgentPermissionRequestMeta | null;
  tool_trace?: AgentToolTraceMeta | null;
  agent_loop_trace?: Array<Record<string, unknown>>;
  agent_loop_limits?: string[];
  client_context?: Record<string, unknown>;
  mcp_config_draft?: Record<string, unknown> | null;
  [key: string]: unknown;
}

export interface AgentChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  created_at: string;
  meta?: AgentChatMessageMeta;
}

export interface AgentMemoryItem {
  id: string;
  kind: string;
  content: string;
  arxiv_id?: string | null;
  source: string;
  created_at: string;
  updated_at?: string | null;
}

export interface AgentSkillItem {
  id: string;
  name: string;
  description: string;
  trigger: string;
  steps: string[];
  source: string;
  updated_at?: string | null;
}

export interface AgentSkillProposalItem {
  id: string;
  action: "create" | "update" | string;
  status: "pending" | "applied" | "rejected" | string;
  skill: AgentSkillItem;
  diff: string;
  created_at: string;
  updated_at: string;
}

export interface AgentCreatedTask {
  id: number;
  task_type: string;
  summary: string;
  status: string;
}

export interface AgentRunResultData {
  summary: string;
  evidence: Array<Record<string, unknown>>;
  limits: string[];
  next_questions: string[];
}

export interface AgentRunItem {
  id: string;
  arxiv_id: string;
  task_type: string;
  title: string;
  status: "running" | "waiting_permission" | "done" | "error" | "cancelled" | string;
  user_message: string;
  inputs: string[];
  result: string;
  result_data?: AgentRunResultData | null;
  error: string;
  task_id?: number | null;
  created_at: string;
  updated_at: string;
  completed_at?: string | null;
}

export interface AgentChatState {
  arxiv_id: string;
  messages: AgentChatMessage[];
  memories: AgentMemoryItem[];
  skills: AgentSkillItem[];
  runs: AgentRunItem[];
}

export interface AgentChatSummary {
  arxiv_id: string;
  paper_title?: string | null;
  paper_exists: boolean;
  message_count: number;
  last_role: string;
  last_message: string;
  updated_at?: string | null;
}

export interface AgentSessionSearchResult {
  arxiv_id: string;
  message_id: string;
  paper_title: string;
  paper_exists: boolean;
  role: string;
  snippet: string;
  created_at?: string | null;
}

export interface AgentChatResponse extends AgentChatState {
  assistant_message: AgentChatMessage;
  created_tasks: AgentCreatedTask[];
  created_runs: AgentRunItem[];
  saved_memory?: AgentMemoryItem | null;
}

export type AgentChatStreamEvent =
  | {
      event: "message";
      data: {
        assistant_message: AgentChatMessage;
        created_tasks: AgentCreatedTask[];
        created_runs: AgentRunItem[];
        saved_memory?: AgentMemoryItem | null;
      };
    }
  | { event: "delta"; data: { text: string } }
  | {
      event: "agent_event";
      data: {
        status: "planning" | "waiting_permission" | "resumed" | "finalizing" | string;
        message: string;
      };
    }
  | { event: "tool_event"; data: Record<string, unknown> }
  | { event: "done"; data: { state: AgentChatState } }
  | { event: string; data: Record<string, unknown> };

export interface CollectionAgentPaper {
  arxiv_id: string;
  title: string;
  status: string;
  has_analysis: boolean;
  annotation_count: number;
  selection_note_count?: number;
  has_paper_note?: boolean;
  note_updated_at?: string | null;
  note_preview?: string;
  summary: string;
  reproducibility_verdict: string;
  reproducibility_confidence: string;
  improvements: string[];
  highlights: string[];
}

export interface CollectionAgentReport {
  collection_id: number;
  collection_name: string;
  generated_at: string;
  paper_count: number;
  analyzed_count: number;
  annotated_count: number;
  missing_analysis: string[];
  papers: CollectionAgentPaper[];
  synthesis: string[];
}

export async function searchPapers(query: string): Promise<PaperCandidate[]> {
  const normalized = query.trim().replace(/\s+/g, " ");
  const cached = readSearchCache(normalized);
  if (cached) return cached;

  const resp = await fetch(`${API_BASE}/search`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query: normalized }),
  });
  if (!resp.ok) throw new Error(`检索失败: ${resp.status}`);
  const data = await resp.json();
  const candidates = data.candidates;
  writeSearchCache(normalized, candidates);
  return candidates;
}

export async function getLiteratureMap(
  paperRef: string,
  maxNodes = 40,
): Promise<LiteratureMap> {
  const normalizedLimit = Math.max(10, Math.min(50, Math.round(maxNodes)));
  const resp = await fetch(
    `${API_BASE}/literature-map/${encodeURIComponent(paperRef)}?max_nodes=${normalizedLimit}`,
  );
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({})) as {
      detail?: string | { message?: string };
    };
    const detail = typeof body.detail === "string"
      ? body.detail
      : body.detail?.message;
    throw new Error(detail || `读取论文图谱失败: ${resp.status}`);
  }
  return resp.json();
}

function searchCacheKey(query: string): string {
  return `${SEARCH_CACHE_PREFIX}${query.toLocaleLowerCase()}`;
}

function readSearchCache(query: string): PaperCandidate[] | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.sessionStorage.getItem(searchCacheKey(query));
    if (!raw) return null;
    const entry = JSON.parse(raw) as Partial<SearchCacheEntry>;
    if (!entry.saved_at || !Array.isArray(entry.candidates)) return null;
    if (Date.now() - entry.saved_at > SEARCH_CACHE_TTL_MS) return null;
    return entry.candidates;
  } catch {
    return null;
  }
}

function writeSearchCache(query: string, candidates: PaperCandidate[]): void {
  if (typeof window === "undefined") return;
  try {
    const entry: SearchCacheEntry = { saved_at: Date.now(), candidates };
    window.sessionStorage.setItem(searchCacheKey(query), JSON.stringify(entry));
  } catch {
    // 搜索缓存只优化体感，写失败不影响主流程。
  }
}

export async function createPaper(
  arxiv_id: string,
  title: string,
  authors: string[],
): Promise<PaperMeta> {
  const resp = await fetch(`${API_BASE}/papers`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ arxiv_id, title, authors }),
  });
  if (!resp.ok) throw new Error(`创建论文失败: ${resp.status}`);
  return resp.json();
}

export async function createMinerUPaper(payload: {
  url: string;
  title?: string;
  file_name?: string;
  page_range?: string;
  language?: string;
}): Promise<PaperMeta> {
  const resp = await fetch(`${API_BASE}/papers/mineru-url`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!resp.ok) {
    const detail = await resp.json().catch(() => ({}));
    throw new Error(detail.detail || `MinerU 解析失败: ${resp.status}`);
  }
  return resp.json();
}

export async function createLocalFilePaper(payload: {
  file: File;
  title?: string;
}): Promise<PaperMeta> {
  const form = new FormData();
  form.append("file", payload.file);
  if (payload.title) form.append("title", payload.title);

  const resp = await fetch(`${API_BASE}/papers/local-file`, {
    method: "POST",
    body: form,
  });
  if (!resp.ok) {
    const detail = await resp.json().catch(() => ({}));
    throw new Error(detail.detail || `本地文件解析失败: ${resp.status}`);
  }
  return resp.json();
}

export async function getPaper(arxiv_id: string): Promise<PaperDetail> {
  const resp = await fetch(`${API_BASE}/papers/${arxiv_id}`);
  if (!resp.ok) throw new Error(`获取论文失败: ${resp.status}`);
  return resp.json();
}

export async function getPaperIfExists(arxiv_id: string): Promise<PaperDetail | null> {
  const resp = await fetch(`${API_BASE}/papers/${arxiv_id}`);
  if (resp.status === 404) return null;
  if (!resp.ok) throw new Error(`获取论文失败: ${resp.status}`);
  return resp.json();
}

function pdfExportPaperPath(arxiv_id: string): string {
  const base = API_BASE.endsWith("/") ? API_BASE.slice(0, -1) : API_BASE;
  return `${base}/papers/${encodeURIComponent(arxiv_id)}`;
}

function pdfExportRunPath(arxiv_id: string, run_id: string): string {
  return `${pdfExportPaperPath(arxiv_id)}/pdf-exports/${encodeURIComponent(run_id)}`;
}

export class PdfExportRequestError extends Error {
  constructor(
    message: string,
    readonly code: string,
    readonly retryable: boolean,
  ) {
    super(message);
  }
}

async function pdfExportResponse<T>(resp: Response, fallback: string): Promise<T> {
  if (!resp.ok) {
    const detail = await resp.json().catch(() => ({})) as { detail?: unknown };
    if (typeof detail.detail === "object" && detail.detail !== null) {
      const payload = detail.detail as { code?: unknown; message?: unknown; retryable?: unknown };
      throw new PdfExportRequestError(
        typeof payload.message === "string" ? payload.message : fallback,
        typeof payload.code === "string" ? payload.code : "request_failed",
        payload.retryable === true,
      );
    }
    if (resp.status === 401 || resp.status === 403) {
      throw new PdfExportRequestError(
        "请先在设置中填写管理员令牌，再创建或取消中文 PDF。",
        "admin_required",
        false,
      );
    }
    const message = typeof detail.detail === "string" ? detail.detail : fallback;
    throw new PdfExportRequestError(message, "request_failed", resp.status >= 500);
  }
  return resp.json() as Promise<T>;
}

export function originalPdfDownloadUrl(arxiv_id: string): string {
  return `${pdfExportPaperPath(arxiv_id)}/original-pdf/download`;
}

export function translatedPdfDownloadUrl(arxiv_id: string, run_id: string): string {
  return `${pdfExportRunPath(arxiv_id, run_id)}/download`;
}

export function pdfExportNoticeUrl(notice_url: string): string {
  const normalized = notice_url.trim();
  if (/^https?:\/\//i.test(normalized)) return normalized;
  const base = API_BASE.endsWith("/") ? API_BASE.slice(0, -1) : API_BASE;
  const path = normalized.startsWith("/") ? normalized : `/${normalized}`;
  return `${base}${path}`;
}

export function pdfExportSourceUrl(source_url: string): string {
  return pdfExportNoticeUrl(source_url);
}

export function pdfExportUnavailableMessage(reason?: string | null): string {
  const normalized = reason?.trim() ?? "";
  const known: Record<string, string> = {
    disabled: "当前部署尚未启用中文 PDF 导出。",
    feature_disabled: "当前部署尚未启用中文 PDF 导出。",
    export_disabled: "当前部署尚未启用中文 PDF 导出。",
    not_configured: "当前部署尚未配置中文 PDF 导出服务。",
    sidecar_not_configured: "当前部署尚未配置中文 PDF 导出服务。",
    sidecar_unavailable: "中文 PDF 导出服务当前不可用，请联系部署管理员。",
    license_disclosure_incomplete: "第三方许可证与源码披露尚未完成，因此中文 PDF 导出保持关闭。",
    source_disclosure_missing: "第三方源码披露尚未完成，因此中文 PDF 导出保持关闭。",
  };
  if (known[normalized]) return known[normalized];
  if (normalized && !normalized.includes("_") && !/^[a-z0-9.-]+$/i.test(normalized)) {
    return normalized;
  }
  return "当前部署暂不提供中文 PDF 导出，原始 PDF 仍可正常下载。";
}

export function pdfExportFailureMessage(
  error_code?: string | null,
  error_message?: string | null,
): string {
  const known: Record<string, string> = {
    source_pdf_missing: "原始 PDF 缺失，请重新导入论文后再生成。",
    source_pdf_too_large: "这份 PDF 超出当前导出大小限制。",
    file_too_large: "这份 PDF 超出当前导出大小限制。",
    page_limit_exceeded: "这份 PDF 页数超出当前导出限制。",
    too_many_pages: "这份 PDF 页数超出当前导出限制。",
    export_timeout: "生成时间超过限制，本次任务已停止。",
    sidecar_timeout: "生成时间超过限制，本次任务已停止。",
    timeout: "生成时间超过限制，本次任务已停止。",
    sidecar_crashed: "中文 PDF 服务意外退出，请稍后重试。",
    sidecar_auth_failed: "翻译服务鉴权失败，请检查部署配置后重试。",
    provider_authentication_failed: "翻译服务鉴权失败，请检查部署配置后重试。",
    authentication_failed: "翻译服务鉴权失败，请检查部署配置后重试。",
    sidecar_rate_limited: "翻译服务当前繁忙，请稍后重试。",
    provider_rate_limited: "翻译服务当前繁忙，请稍后重试。",
    rate_limited: "翻译服务当前繁忙，请稍后重试。",
    sidecar_unavailable: "中文 PDF 服务当前不可用，请稍后重试。",
    output_validation_failed: "生成的中文 PDF 未通过完整性检查，请重试。",
    output_invalid: "生成的中文 PDF 未通过完整性检查，请重试。",
    legacy_output_quarantined: "这份旧导出未通过当前安全证明，请重新生成中文 PDF。",
    admin_required: "请先在设置中填写管理员令牌，再创建或取消中文 PDF。",
    export_queue_full: "当前已有中文 PDF 正在生成，请稍后再试。",
    backend_restarted: "服务重启中断了本次生成，请重新开始。",
    export_disabled: "当前部署尚未启用中文 PDF 导出。",
  };
  const normalizedCode = error_code?.trim() ?? "";
  if (known[normalizedCode]) return known[normalizedCode];
  const backendMessage = error_message?.trim();
  if (backendMessage) return backendMessage;
  return "中文 PDF 生成失败，原始 PDF 和网页译文不受影响。";
}

export async function getPdfExportCapability(): Promise<PdfExportCapability> {
  const base = API_BASE.endsWith("/") ? API_BASE.slice(0, -1) : API_BASE;
  const resp = await fetch(`${base}/pdf-exports/capability`);
  return pdfExportResponse(resp, `读取中文 PDF 导出能力失败: ${resp.status}`);
}

export async function listPdfExports(arxiv_id: string): Promise<PdfExportRun[]> {
  const resp = await fetch(`${pdfExportPaperPath(arxiv_id)}/pdf-exports`);
  return pdfExportResponse(resp, `读取中文 PDF 任务失败: ${resp.status}`);
}

function pdfExportAdminHeaders(adminToken = ""): HeadersInit {
  const normalized = adminToken.trim();
  return normalized ? { "X-Peinidu-Admin-Token": normalized } : {};
}

export async function createPdfExport(
  arxiv_id: string,
  adminToken = "",
): Promise<PdfExportRun> {
  const resp = await fetch(`${pdfExportPaperPath(arxiv_id)}/pdf-exports`, {
    method: "POST",
    headers: pdfExportAdminHeaders(adminToken),
  });
  return pdfExportResponse(resp, `创建中文 PDF 任务失败: ${resp.status}`);
}

export async function getPdfExportRun(
  arxiv_id: string,
  run_id: string,
): Promise<PdfExportRun> {
  const resp = await fetch(pdfExportRunPath(arxiv_id, run_id));
  return pdfExportResponse(resp, `读取中文 PDF 进度失败: ${resp.status}`);
}

export async function cancelPdfExport(
  arxiv_id: string,
  run_id: string,
  adminToken = "",
): Promise<PdfExportRun> {
  const resp = await fetch(`${pdfExportRunPath(arxiv_id, run_id)}/cancel`, {
    method: "POST",
    headers: pdfExportAdminHeaders(adminToken),
  });
  return pdfExportResponse(resp, `取消中文 PDF 任务失败: ${resp.status}`);
}

export async function getPdfMap(arxiv_id: string): Promise<BlockPdfMap> {
  const resp = await fetch(`${API_BASE}/papers/${arxiv_id}/pdf-map`);
  if (!resp.ok) throw new Error(`生成 PDF 映射失败: ${resp.status}`);
  return resp.json();
}

class TranslationLayoutRequestError extends Error {
  constructor(
    message: string,
    readonly code: string,
  ) {
    super(message);
  }
}

async function requestTranslationLayout(
  arxiv_id: string,
  build: boolean,
): Promise<TranslationLayout> {
  const params = new URLSearchParams({ build: String(build) });
  const resp = await fetch(
    `${API_BASE}/papers/${arxiv_id}/translation-layout?${params.toString()}`,
  );
  if (!resp.ok) {
    const detail = await resp.json().catch(() => ({})) as { detail?: unknown };
    const payload = typeof detail.detail === "object" && detail.detail !== null
      ? detail.detail as { code?: unknown; message?: unknown }
      : null;
    const code = typeof payload?.code === "string" ? payload.code : "";
    const backendMessage = typeof payload?.message === "string"
      ? payload.message
      : typeof detail.detail === "string"
        ? detail.detail
        : "";
    const message = code === "source_pdf_missing"
      ? "原始 PDF 缺失，请重新导入后再使用原位译文。"
      : code === "layout_unavailable"
        ? "这份 PDF 暂时无法生成可靠版面，请稍后重试或重新导入。"
        : backendMessage || `生成原位译文版面失败: ${resp.status}`;
    throw new TranslationLayoutRequestError(message, code);
  }
  return resp.json();
}

async function loadTranslationLayout(arxiv_id: string): Promise<TranslationLayout> {
  try {
    return await requestTranslationLayout(arxiv_id, false);
  } catch (error) {
    if (
      error instanceof TranslationLayoutRequestError
      && error.code === "translation_layout_cache_missing"
    ) {
      return requestTranslationLayout(arxiv_id, true);
    }
    throw error;
  }
}

export function getTranslationLayout(arxiv_id: string): Promise<TranslationLayout> {
  const now = Date.now();
  const cached = translationLayoutCache.get(arxiv_id);
  if (cached && cached.expires_at > now) return cached.promise;

  const promise = loadTranslationLayout(arxiv_id);
  const entry = {
    expires_at: now + TRANSLATION_LAYOUT_CACHE_TTL_MS,
    promise,
  };
  translationLayoutCache.set(arxiv_id, entry);
  void promise.catch(() => {
    if (translationLayoutCache.get(arxiv_id) === entry) {
      translationLayoutCache.delete(arxiv_id);
    }
  });
  return promise;
}

export function prefetchTranslationLayout(arxiv_id: string): void {
  void getTranslationLayout(arxiv_id).catch(() => undefined);
}

export async function rebuildTranslationLayout(
  arxiv_id: string,
  adminToken?: string,
): Promise<TranslationLayout> {
  translationLayoutCache.delete(arxiv_id);
  const resp = await fetch(`${API_BASE}/papers/${arxiv_id}/translation-layout/rebuild`, {
    method: "POST",
    headers: adminHeaders(adminToken),
  });
  if (!resp.ok) throw new Error(`重建原位译文版面失败: ${resp.status}`);
  const layout = await resp.json() as TranslationLayout;
  translationLayoutCache.set(arxiv_id, {
    expires_at: Date.now() + TRANSLATION_LAYOUT_CACHE_TTL_MS,
    promise: Promise.resolve(layout),
  });
  notifyPaperDataChanged(arxiv_id);
  return layout;
}

export async function retryBlockTranslation(
  arxiv_id: string,
  block_index: number,
): Promise<{ index: number; translation: string | null; status: string }> {
  const resp = await fetch(`${API_BASE}/translate/${arxiv_id}/block/${block_index}`, {
    method: "POST",
  });
  if (!resp.ok) throw new Error(`重试翻译失败: ${resp.status}`);
  const block = await resp.json();
  notifyPaperDataChanged(arxiv_id);
  return block;
}

export class SelectionTranslationRequestError extends Error {
  constructor(
    message: string,
    readonly code: string,
    readonly retryable: boolean,
  ) {
    super(message);
  }
}

export async function translateSelection(
  arxiv_id: string,
  payload: SelectionTranslationRequest,
  signal?: AbortSignal,
): Promise<SelectionTranslationResponse> {
  const resp = await fetch(
    `${API_BASE}/translate/${encodeURIComponent(arxiv_id)}/selection`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal,
    },
  );
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({})) as { detail?: unknown };
    const detail = typeof body.detail === "object" && body.detail !== null
      ? body.detail as { code?: unknown; message?: unknown; retryable?: unknown }
      : null;
    throw new SelectionTranslationRequestError(
      typeof detail?.message === "string"
        ? detail.message
        : `翻译当前选区失败: ${resp.status}`,
      typeof detail?.code === "string" ? detail.code : "selection_translation_failed",
      detail?.retryable === true,
    );
  }
  return resp.json();
}

export async function listPapers(): Promise<PaperMeta[]> {
  const resp = await fetch(`${API_BASE}/papers`);
  if (!resp.ok) throw new Error(`列出论文失败: ${resp.status}`);
  return resp.json();
}

export async function listCollections(arxiv_id?: string): Promise<CollectionSummary[]> {
  const query = arxiv_id ? `?arxiv_id=${encodeURIComponent(arxiv_id)}` : "";
  const resp = await fetch(`${API_BASE}/collections${query}`);
  if (!resp.ok) throw new Error(`读取文献库失败: ${resp.status}`);
  return resp.json();
}

export async function createCollection(name: string): Promise<CollectionSummary> {
  const resp = await fetch(`${API_BASE}/collections`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  if (!resp.ok) {
    const detail = await resp.json().catch(() => ({}));
    throw new Error(detail.detail || `创建专题失败: ${resp.status}`);
  }
  return resp.json();
}

export async function getCollection(id: number): Promise<CollectionDetail> {
  const resp = await fetch(`${API_BASE}/collections/${id}`);
  if (!resp.ok) throw new Error(`读取专题失败: ${resp.status}`);
  return resp.json();
}

export async function getCollectionAgentReport(
  id: number,
): Promise<CollectionAgentReport | null> {
  const resp = await fetch(`${API_BASE}/collections/${id}/agent-report`);
  if (resp.status === 404) return null;
  if (!resp.ok) throw new Error(`读取专题 Agent 报告失败: ${resp.status}`);
  return resp.json();
}

export async function runCollectionAgentReport(
  id: number,
): Promise<CollectionAgentReport> {
  const resp = await fetch(`${API_BASE}/collections/${id}/agent-report`, {
    method: "POST",
  });
  if (!resp.ok) {
    const detail = await resp.json().catch(() => ({}));
    throw new Error(detail.detail || `专题横向整理失败: ${resp.status}`);
  }
  return resp.json();
}

export async function addPaperToCollection(
  collection_id: number,
  arxiv_id: string,
): Promise<CollectionDetail> {
  const resp = await fetch(`${API_BASE}/collections/${collection_id}/papers`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ arxiv_id }),
  });
  if (!resp.ok) {
    const detail = await resp.json().catch(() => ({}));
    throw new Error(detail.detail || `加入专题失败: ${resp.status}`);
  }
  return resp.json();
}

export async function removePaperFromCollection(
  collection_id: number,
  arxiv_id: string,
): Promise<CollectionDetail> {
  const resp = await fetch(`${API_BASE}/collections/${collection_id}/papers/${encodeURIComponent(arxiv_id)}`, {
    method: "DELETE",
  });
  if (!resp.ok) throw new Error(`移出专题失败: ${resp.status}`);
  return resp.json();
}

export async function listAnnotations(arxiv_id: string): Promise<Annotation[]> {
  const resp = await fetch(`${API_BASE}/papers/${arxiv_id}/annotations`);
  if (!resp.ok) throw new Error(`读取标注失败: ${resp.status}`);
  return resp.json();
}

export async function createAnnotation(
  arxiv_id: string,
  payload: {
    block_index: number;
    side: "original" | "translation";
    text: string;
    note?: string;
    color?: string;
    kind?: AnnotationKind;
    selector?: AnnotationSelector;
  },
): Promise<Annotation> {
  const resp = await fetch(`${API_BASE}/papers/${arxiv_id}/annotations`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!resp.ok) {
    const detail = await resp.json().catch(() => ({}));
    throw new Error(detail.detail || `保存标注失败: ${resp.status}`);
  }
  const annotation = await resp.json();
  notifyPaperDataChanged(arxiv_id);
  return annotation;
}

export async function updateAnnotation(
  arxiv_id: string,
  annotation_id: string,
  payload: {
    note?: string;
    kind?: AnnotationKind;
  },
): Promise<Annotation> {
  const resp = await fetch(`${API_BASE}/papers/${arxiv_id}/annotations/${annotation_id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!resp.ok) {
    const detail = await resp.json().catch(() => ({}));
    throw new Error(detail.detail || `修改标注失败: ${resp.status}`);
  }
  const annotation = await resp.json();
  notifyPaperDataChanged(arxiv_id);
  return annotation;
}

export async function deleteAnnotation(
  arxiv_id: string,
  annotation_id: string,
): Promise<void> {
  const resp = await fetch(`${API_BASE}/papers/${arxiv_id}/annotations/${annotation_id}`, {
    method: "DELETE",
  });
  if (!resp.ok) throw new Error(`删除标注失败: ${resp.status}`);
  notifyPaperDataChanged(arxiv_id);
}

export async function getPaperNote(arxiv_id: string): Promise<PaperNote> {
  const resp = await fetch(`${API_BASE}/papers/${arxiv_id}/paper-note`);
  if (!resp.ok) throw new Error(`读取论文笔记失败: ${resp.status}`);
  return resp.json();
}

export async function savePaperNote(
  arxiv_id: string,
  markdown: string,
  base_revision: string,
): Promise<PaperNote> {
  const resp = await fetch(`${API_BASE}/papers/${arxiv_id}/paper-note`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ markdown, base_revision }),
  });
  if (!resp.ok) {
    const payload = await resp.json().catch(() => ({}));
    const detail = payload.detail;
    if (resp.status === 409) {
      throw new PaperNoteRevisionConflictError(
        typeof detail?.message === "string"
          ? detail.message
          : "这份论文笔记已在另一个页面更新，请先核对再保存。",
        typeof detail?.current_revision === "string" ? detail.current_revision : null,
      );
    }
    throw new Error(
      typeof detail === "string"
        ? detail
        : typeof detail?.message === "string"
          ? detail.message
          : `保存论文笔记失败: ${resp.status}`,
    );
  }
  const note = await resp.json();
  notifyPaperDataChanged(arxiv_id);
  return note;
}

export async function listAgentTasks(): Promise<AgentTask[]> {
  const resp = await fetch(`${API_BASE}/agent/tasks`);
  if (!resp.ok) throw new Error(`读取 Agent 任务失败: ${resp.status}`);
  return resp.json();
}

export async function listAgentChats(): Promise<AgentChatSummary[]> {
  const resp = await fetch(`${API_BASE}/agent/chats`);
  if (!resp.ok) throw new Error(`读取 Agent 对话失败: ${resp.status}`);
  return resp.json();
}

export async function searchAgentSessions(
  query: string,
  limit = 20,
): Promise<AgentSessionSearchResult[]> {
  const params = new URLSearchParams({ q: query, limit: String(limit) });
  const resp = await fetch(`${API_BASE}/agent/sessions/search?${params.toString()}`);
  if (!resp.ok) {
    const detail = await resp.json().catch(() => ({}));
    throw new Error(detail.detail || `搜索 Agent 对话失败: ${resp.status}`);
  }
  return resp.json();
}

export async function getAgentChat(arxiv_id: string): Promise<AgentChatState> {
  const resp = await fetch(`${API_BASE}/agent/chat/${arxiv_id}`);
  if (!resp.ok) throw new Error(`读取 Agent 对话失败: ${resp.status}`);
  return resp.json();
}

export async function clearAgentChat(arxiv_id: string): Promise<AgentChatState> {
  const resp = await fetch(`${API_BASE}/agent/chat/${arxiv_id}`, { method: "DELETE" });
  if (!resp.ok) {
    const detail = await resp.json().catch(() => ({}));
    throw new Error(detail.detail || `清空 Agent 对话失败: ${resp.status}`);
  }
  return resp.json();
}

export async function sendAgentChatMessage(
  arxiv_id: string,
  message: string,
  context: Record<string, unknown> = {},
): Promise<AgentChatResponse> {
  const resp = await fetch(`${API_BASE}/agent/chat/${arxiv_id}/messages`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, context }),
  });
  if (!resp.ok) {
    const detail = await resp.json().catch(() => ({}));
    throw new Error(detail.detail || `发送 Agent 消息失败: ${resp.status}`);
  }
  return resp.json();
}

export async function streamAgentChatMessage(
  arxiv_id: string,
  message: string,
  context: Record<string, unknown> = {},
  onEvent: (event: AgentChatStreamEvent) => void,
): Promise<void> {
  const resp = await fetch(`${API_BASE}/agent/chat/${arxiv_id}/messages/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, context }),
  });
  if (!resp.ok || !resp.body) {
    const fallback = await sendAgentChatMessage(arxiv_id, message, context);
    onEvent({ event: "done", data: { state: fallback } });
    return;
  }

  await consumeAgentSSE(resp, onEvent, async () => {
    const fallback = await sendAgentChatMessage(arxiv_id, message, context);
    onEvent({ event: "done", data: { state: fallback } });
  });
}

async function consumeAgentSSE(
  resp: Response,
  onEvent: (event: AgentChatStreamEvent) => void,
  onInitialFailure?: () => Promise<void>,
): Promise<void> {
  if (!resp.body) throw new Error("Agent SSE 响应没有可读取内容");
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let receivedEvent = false;
  // eslint-disable-next-line no-constant-condition
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const parts = buffer.split("\n\n");
      buffer = parts.pop() || "";
      for (const part of parts) {
        const parsed = parseAgentSSE(part);
        if (parsed) {
          receivedEvent = true;
          onEvent(parsed);
        }
      }
    }
  } catch (e) {
    if (!receivedEvent && onInitialFailure) {
      await onInitialFailure();
      return;
    }
    throw e;
  }
}

export async function resumeAgentRunStream(
  arxiv_id: string,
  run_id: string,
  approved_permission: string,
  onEvent: (event: AgentChatStreamEvent) => void,
): Promise<void> {
  const resp = await fetch(`${API_BASE}/agent/chat/${arxiv_id}/runs/${run_id}/resume/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ approved_permission }),
  });
  if (!resp.ok || !resp.body) {
    const detail = await resp.json().catch(() => ({}));
    throw new Error(detail.detail || `恢复 Agent Run 失败: ${resp.status}`);
  }
  await consumeAgentSSE(resp, onEvent);
}

function parseAgentSSE(raw: string): AgentChatStreamEvent | null {
  let event = "message";
  let dataStr = "";
  for (const line of raw.split("\n")) {
    if (line.startsWith("event: ")) event = line.slice(7).trim();
    else if (line.startsWith("data: ")) dataStr += line.slice(6);
  }
  if (!dataStr) return null;
  try {
    return { event, data: JSON.parse(dataStr) } as AgentChatStreamEvent;
  } catch {
    return { event, data: { raw: dataStr } };
  }
}

/** 确认 Pet MCP 配置向导草稿：写入 config.yaml（服务端强制不启用）。 */
export async function confirmMcpConfigDraft(
  arxiv_id: string,
  server: Record<string, unknown>,
  adminToken?: string,
): Promise<AgentChatResponse> {
  const resp = await fetch(`${API_BASE}/agent/chat/${arxiv_id}/mcp-config/confirm`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(adminToken ? { "X-Peinidu-Admin-Token": adminToken } : {}),
    },
    body: JSON.stringify({ server }),
  });
  if (!resp.ok) {
    const detail = await resp.json().catch(() => ({}));
    if (resp.status === 401) {
      throw new Error(detail.detail || "需要管理员 token：先在设置页填写并保存管理员 token");
    }
    throw new Error(detail.detail || `写入 MCP 配置失败: ${resp.status}`);
  }
  return resp.json();
}

export async function cancelAgentRun(
  arxiv_id: string,
  run_id: string,
): Promise<AgentRunItem> {
  const resp = await fetch(`${API_BASE}/agent/chat/${arxiv_id}/runs/${run_id}/cancel`, {
    method: "POST",
  });
  if (!resp.ok) {
    const detail = await resp.json().catch(() => ({}));
    throw new Error(detail.detail || `取消 Agent Run 失败: ${resp.status}`);
  }
  return resp.json();
}

export async function saveAgentMemory(
  arxiv_id: string,
  content: string,
  kind = "preference",
): Promise<AgentMemoryItem> {
  const resp = await fetch(`${API_BASE}/agent/chat/${arxiv_id}/memory`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content, kind }),
  });
  if (!resp.ok) {
    const detail = await resp.json().catch(() => ({}));
    throw new Error(detail.detail || `保存 Agent 记忆失败: ${resp.status}`);
  }
  return resp.json();
}

export async function listAgentMemories(limit = 100): Promise<AgentMemoryItem[]> {
  const resp = await fetch(`${API_BASE}/agent/memories?limit=${limit}`);
  if (!resp.ok) {
    const detail = await resp.json().catch(() => ({}));
    throw new Error(detail.detail || `读取 Agent 记忆失败: ${resp.status}`);
  }
  return resp.json();
}

export async function listAgentSkillProposals(status?: string): Promise<AgentSkillProposalItem[]> {
  const suffix = status ? `?status=${encodeURIComponent(status)}` : "";
  const resp = await fetch(`${API_BASE}/agent/skill-proposals${suffix}`);
  if (!resp.ok) throw new Error(`读取 Skill 提案失败: ${resp.status}`);
  return resp.json();
}

export async function applyAgentSkillProposal(proposalId: string): Promise<AgentSkillProposalItem> {
  const resp = await fetch(`${API_BASE}/agent/skill-proposals/${proposalId}/apply`, { method: "POST" });
  if (!resp.ok) throw new Error(`应用 Skill 提案失败: ${resp.status}`);
  return resp.json();
}

export async function rejectAgentSkillProposal(proposalId: string): Promise<AgentSkillProposalItem> {
  const resp = await fetch(`${API_BASE}/agent/skill-proposals/${proposalId}/reject`, { method: "POST" });
  if (!resp.ok) throw new Error(`拒绝 Skill 提案失败: ${resp.status}`);
  return resp.json();
}

export async function createAgentMemory(
  content: string,
  kind = "preference",
  arxiv_id?: string | null,
): Promise<AgentMemoryItem> {
  const resp = await fetch(`${API_BASE}/agent/memories`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content, kind, arxiv_id: arxiv_id || null }),
  });
  if (!resp.ok) {
    const detail = await resp.json().catch(() => ({}));
    throw new Error(detail.detail || `保存 Agent 记忆失败: ${resp.status}`);
  }
  return resp.json();
}

export async function updateAgentMemory(
  memoryId: string,
  patch: { content?: string; kind?: string },
): Promise<AgentMemoryItem> {
  const resp = await fetch(`${API_BASE}/agent/memories/${memoryId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  if (!resp.ok) {
    const detail = await resp.json().catch(() => ({}));
    throw new Error(detail.detail || `更新 Agent 记忆失败: ${resp.status}`);
  }
  return resp.json();
}

export async function deleteAgentMemory(memoryId: string): Promise<AgentMemoryItem> {
  const resp = await fetch(`${API_BASE}/agent/memories/${memoryId}`, {
    method: "DELETE",
  });
  if (!resp.ok) {
    const detail = await resp.json().catch(() => ({}));
    throw new Error(detail.detail || `删除 Agent 记忆失败: ${resp.status}`);
  }
  return resp.json();
}

export interface AnalysisResult {
  summary?: string;
  reproducibility?: ReproducibilityReport | null;
  improvements?: string[];
  highlights?: string[];
}

export interface ReproducibilityReport {
  verdict: string;
  confidence: string;
  evidence: Evidence[];
  summary: string;
}

export interface Evidence {
  aspect: string;
  status: string;
  detail: string;
  citation: string;
  location?: {
    block_index: number;
    page?: number | null;
    region_id?: string | null;
  } | null;
}

export async function getAnalysis(arxiv_id: string): Promise<AnalysisResult | null> {
  const resp = await fetch(`${API_BASE}/analyze/${arxiv_id}`);
  if (resp.status === 404) return null;
  if (!resp.ok) throw new Error(`读取分析失败: ${resp.status}`);
  return resp.json();
}

export async function runAnalysis(arxiv_id: string, force = false): Promise<AnalysisResult> {
  const query = force ? "?force=true" : "";
  const resp = await fetch(`${API_BASE}/analyze/${arxiv_id}${query}`, { method: "POST" });
  if (!resp.ok) throw new Error(`分析失败: ${resp.status}`);
  return resp.json();
}

/** SSE 翻译流 URL（供 EventSource 使用） */
export function translateStreamUrl(arxiv_id: string): string {
  return `${API_BASE}/translate/${arxiv_id}`;
}

// ── 配置管理（ccswitch 式交互配置）──

export interface ProviderConfig {
  name: string;
  type: string;
  api_key: string; // 脱敏值（含 ***）或空
  api_key_configured?: boolean;
  api_base?: string | null;
  models: string[];
}

export interface DeepLXConfig {
  base_url: string;
  api_key: string;
  api_key_configured?: boolean;
  timeout_seconds: number;
}

export interface TaskModelsConfig {
  translation: string;
  agent_summary: string;
  agent_reproducibility: string;
  agent_improvement: string;
  agent_highlights: string;
  /** Pet 对话/选区解释/工具汇总；空字符串 = 跟随 default_model */
  agent_chat: string;
  /** LLM 意图分类；空字符串 = 跟随 default_model */
  agent_intent: string;
}

export interface MCPServerConfig {
  name: string;
  transport: "stdio" | "http";
  command: string;
  args: string[];
  url?: string | null;
  enabled: boolean;
  tool_name: string;
  timeout_seconds: number;
  permission_scopes: Array<"mcp_tool" | "external_search" | "long_task" | "browser_control">;
  allowed_tools: string[];
}

export interface MinerUConfig {
  enabled: boolean;
  base_url: string;
  mode: "agent_lite" | "standard";
  api_token: string;
  api_token_configured?: boolean;
  language: string;
  page_range?: string | null;
  enable_table: boolean;
  enable_formula: boolean;
  is_ocr: boolean;
  poll_interval_seconds: number;
  max_wait_seconds: number;
}

export interface AppConfigData {
  llm_providers: ProviderConfig[];
  default_provider: string;
  default_model: string;
  task_models: TaskModelsConfig;
  presets: Record<string, Record<string, { model: string; variant: string }>>;
  default_preset: string;
  mcp_servers: MCPServerConfig[];
  mineru: MinerUConfig;
  deeplx: DeepLXConfig;
  credential_storage?: {
    mode: "system" | "config_or_env";
    available: boolean;
  };
  translation_prompt: string;
  translation_concurrency: number;
  request_timeout: number;
}

export interface DiscoveredModel {
  id: string;
  owned_by: string;
}

export interface MCPTestResult {
  ok: boolean;
  error: string;
  note: string;
  tools: { name: string; description: string }[];
  chosen_tool: string;
  elapsed_ms: number;
}

function adminHeaders(adminToken?: string): HeadersInit {
  return adminToken ? { "X-Peinidu-Admin-Token": adminToken } : {};
}

export async function getConfig(adminToken?: string): Promise<AppConfigData> {
  const resp = await fetch(`${API_BASE}/config`, {
    headers: adminHeaders(adminToken),
  });
  if (!resp.ok) throw new Error(`读取配置失败: ${resp.status}`);
  return resp.json();
}

export async function saveConfig(
  config: AppConfigData,
  adminToken?: string,
): Promise<{ status: string; message: string }> {
  const resp = await fetch(`${API_BASE}/config`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...adminHeaders(adminToken) },
    body: JSON.stringify(config),
  });
  if (!resp.ok) {
    const detail = await resp.json().catch(() => ({}));
    throw new Error(detail.detail || `保存配置失败: ${resp.status}`);
  }
  return resp.json();
}

export async function testMcpServer(
  server: MCPServerConfig,
  adminToken?: string,
): Promise<MCPTestResult> {
  const resp = await fetch(`${API_BASE}/config/mcp/test`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...adminHeaders(adminToken) },
    body: JSON.stringify(server),
  });
  if (!resp.ok) {
    const detail = await resp.json().catch(() => ({}));
    throw new Error(detail.detail || `MCP 测试失败: ${resp.status}`);
  }
  return resp.json();
}

export async function discoverModels(
  base_url: string,
  api_key: string,
  provider_name?: string,
  adminToken?: string,
): Promise<DiscoveredModel[]> {
  const resp = await fetch(`${API_BASE}/config/models`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...adminHeaders(adminToken) },
    body: JSON.stringify({ base_url, api_key, provider_name }),
  });
  if (!resp.ok) {
    const detail = await resp.json().catch(() => ({}));
    throw new Error(detail.detail || `模型发现失败: ${resp.status}`);
  }
  const data = await resp.json();
  return data.models;
}

export async function deleteConfigCredential(
  kind: "llm_provider" | "mineru" | "deeplx",
  provider_name?: string,
  adminToken?: string,
): Promise<void> {
  const resp = await fetch(`${API_BASE}/config/credentials/delete`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...adminHeaders(adminToken) },
    body: JSON.stringify({ kind, provider_name }),
  });
  if (!resp.ok) {
    const detail = await resp.json().catch(() => ({}));
    throw new Error(detail.detail || `删除凭据失败: ${resp.status}`);
  }
}

export async function testDeepLXConfig(adminToken?: string): Promise<void> {
  const resp = await fetch(`${API_BASE}/config/deeplx/test`, {
    method: "POST",
    headers: adminHeaders(adminToken),
  });
  if (!resp.ok) {
    const detail = await resp.json().catch(() => ({}));
    throw new Error(detail.detail || `DeepLX 测试失败: ${resp.status}`);
  }
}
