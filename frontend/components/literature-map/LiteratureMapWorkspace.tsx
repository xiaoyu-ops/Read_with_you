"use client";

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { CollectionPicker } from "@/components/CollectionPicker";
import { LiteratureGraph } from "@/components/literature-map/LiteratureGraph";
import {
  createPaper,
  getLiteratureMap,
  getPaperIfExists,
  listPapers,
  type LiteratureMap,
  type LiteratureMapWork,
  type LiteraturePaper,
} from "@/lib/api";
import { formatCitationCount } from "@/lib/literatureMap";

type WorkspaceView = "graph" | "prior" | "derivative" | "list";
type MobilePanel = "graph" | "list" | "detail";

interface Filters {
  keyword: string;
  yearFrom: string;
  yearTo: string;
  pdfOnly: boolean;
  openAccessOnly: boolean;
  libraryOnly: boolean;
}

const EMPTY_FILTERS: Filters = {
  keyword: "",
  yearFrom: "",
  yearTo: "",
  pdfOnly: false,
  openAccessOnly: false,
  libraryOnly: false,
};

function paperMatches(
  paper: LiteraturePaper,
  filters: Filters,
  libraryIds: Set<string>,
): boolean {
  const keyword = filters.keyword.trim().toLocaleLowerCase();
  const haystack = [paper.title, paper.authors.join(" "), paper.venue || ""]
    .join(" ")
    .toLocaleLowerCase();
  if (keyword && !haystack.includes(keyword)) return false;
  const yearFrom = Number(filters.yearFrom);
  const yearTo = Number(filters.yearTo);
  if (filters.yearFrom && (paper.year == null || paper.year < yearFrom)) return false;
  if (filters.yearTo && (paper.year == null || paper.year > yearTo)) return false;
  if (filters.pdfOnly && !paper.pdf_url && !paper.arxiv_id) return false;
  if (filters.openAccessOnly && !paper.is_open_access) return false;
  if (filters.libraryOnly && (!paper.arxiv_id || !libraryIds.has(paper.arxiv_id))) return false;
  return true;
}

function externalHref(paper: LiteraturePaper, kind: "arxiv" | "doi"): string | null {
  if (kind === "arxiv" && paper.arxiv_id) {
    return `https://arxiv.org/abs/${encodeURIComponent(paper.arxiv_id)}`;
  }
  if (kind === "doi" && paper.doi) {
    return `https://doi.org/${encodeURIComponent(paper.doi)}`;
  }
  return null;
}

export function LiteratureMapWorkspace({ paperRef }: { paperRef: string }) {
  const router = useRouter();
  const [data, setData] = useState<LiteratureMap | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [preparing, setPreparing] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState("");
  const [view, setView] = useState<WorkspaceView>("graph");
  const [relation, setRelation] = useState<"similarity" | "citation">("similarity");
  const [filterOpen, setFilterOpen] = useState(false);
  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS);
  const [libraryIds, setLibraryIds] = useState<Set<string>>(new Set());
  const [mobilePanel, setMobilePanel] = useState<MobilePanel>("graph");

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError(null);
    Promise.all([
      getLiteratureMap(paperRef),
      listPapers().catch(() => []),
    ])
      .then(([map, papers]) => {
        if (!active) return;
        setData(map);
        setSelectedId(map.origin.id);
        setLibraryIds(new Set(papers.map((paper) => paper.arxiv_id)));
      })
      .catch((reason) => {
        if (active) setError((reason as Error).message);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [paperRef]);

  const filteredNodes = useMemo(() => {
    if (!data) return [];
    return data.nodes.filter(
      (paper) => paper.id === data.origin.id || paperMatches(paper, filters, libraryIds),
    );
  }, [data, filters, libraryIds]);
  const visibleIds = useMemo(
    () => new Set(filteredNodes.map((paper) => paper.id)),
    [filteredNodes],
  );
  const filteredEdges = useMemo(
    () => data?.edges.filter(
      (edge) => visibleIds.has(edge.source) && visibleIds.has(edge.target),
    ) || [],
    [data, visibleIds],
  );
  const selected = data?.nodes.find((paper) => paper.id === selectedId) || data?.origin || null;

  const selectPaper = (paperId: string) => {
    setSelectedId(paperId);
    if (window.innerWidth < 768) setMobilePanel("detail");
  };

  const ensurePaper = async (paper: LiteraturePaper): Promise<string> => {
    if (!paper.arxiv_id) throw new Error("这篇论文没有可用于站内阅读的 arXiv 版本。");
    const arxivId = paper.arxiv_id;
    const existing = await getPaperIfExists(arxivId);
    if (!existing) {
      await createPaper(arxivId, paper.title, paper.authors);
    }
    setLibraryIds((current) => new Set(current).add(arxivId));
    return arxivId;
  };

  const openInternal = async (paper: LiteraturePaper, target: "reader" | "pet") => {
    setPreparing(target === "reader" ? "正在准备阅读页…" : "正在准备 Pet 对话…");
    setError(null);
    try {
      const arxivId = await ensurePaper(paper);
      router.push(
        target === "reader"
          ? `/paper/${encodeURIComponent(arxivId)}`
          : `/paper/${encodeURIComponent(arxivId)}?pet=open`,
      );
    } catch (reason) {
      setError((reason as Error).message);
      setPreparing(null);
    }
  };

  if (loading) {
    return (
      <main className="literature-map-loading" aria-busy="true">
        <p>正在从 Semantic Scholar 构建论文关系…</p>
        <span>推荐、参考文献与引用关系会合并为一张可探索图谱。</span>
      </main>
    );
  }

  if (error && !data) {
    return (
      <main className="literature-map-loading">
        <p>论文图谱暂时无法打开</p>
        <span>{error}</span>
        <a href="/?task=map">返回论文关系检索</a>
      </main>
    );
  }

  if (!data || !selected) return null;

  const activeFilters = Object.entries(filters).filter(([, value]) => Boolean(value)).length;
  const currentRows: LiteratureMapWork[] = (view === "prior"
    ? data.prior_works
    : view === "derivative"
      ? data.derivative_works
      : []).filter((row) => paperMatches(row.paper, filters, libraryIds));

  return (
    <main className="literature-map-workspace">
      <header className="literature-map-toolbar">
        <div className="literature-map-toolbar-title">
          <a href="/?task=map" aria-label="返回论文关系检索">←</a>
          <div>
            <p>论文关系</p>
            <span>{data.nodes.length} 篇 · Semantic Scholar</span>
          </div>
        </div>
        <nav aria-label="图谱视图">
          {([
            ["graph", "图谱"],
            ["prior", "先行工作"],
            ["derivative", "后续工作"],
            ["list", "列表"],
          ] as Array<[WorkspaceView, string]>).map(([value, label]) => (
            <button
              key={value}
              type="button"
              aria-pressed={view === value}
              onClick={() => setView(value)}
            >
              {label}
            </button>
          ))}
          <button
            type="button"
            aria-expanded={filterOpen}
            aria-pressed={filterOpen}
            onClick={() => setFilterOpen((current) => !current)}
          >
            筛选{activeFilters ? ` · ${activeFilters}` : ""}
          </button>
        </nav>
        {view === "graph" && (
          <div className="literature-relation-switch" aria-label="关系类型">
            <button
              type="button"
              aria-pressed={relation === "similarity"}
              onClick={() => setRelation("similarity")}
            >
              相似关系
            </button>
            <button
              type="button"
              aria-pressed={relation === "citation"}
              onClick={() => setRelation("citation")}
            >
              引用关系
            </button>
          </div>
        )}
      </header>

      {filterOpen && (
        <section className="literature-map-filters" aria-label="筛选论文">
          <label>
            <span>关键词</span>
            <input
              value={filters.keyword}
              onChange={(event) => setFilters({ ...filters, keyword: event.target.value })}
              placeholder="标题、作者或会议"
            />
          </label>
          <label>
            <span>起始年份</span>
            <input
              type="number"
              inputMode="numeric"
              value={filters.yearFrom}
              onChange={(event) => setFilters({ ...filters, yearFrom: event.target.value })}
              placeholder="例如 2018"
            />
          </label>
          <label>
            <span>截止年份</span>
            <input
              type="number"
              inputMode="numeric"
              value={filters.yearTo}
              onChange={(event) => setFilters({ ...filters, yearTo: event.target.value })}
              placeholder="例如 2026"
            />
          </label>
          <div className="literature-filter-checks">
            <label><input type="checkbox" checked={filters.pdfOnly} onChange={(event) => setFilters({ ...filters, pdfOnly: event.target.checked })} />PDF 可用</label>
            <label><input type="checkbox" checked={filters.openAccessOnly} onChange={(event) => setFilters({ ...filters, openAccessOnly: event.target.checked })} />开放获取</label>
            <label><input type="checkbox" checked={filters.libraryOnly} onChange={(event) => setFilters({ ...filters, libraryOnly: event.target.checked })} />已入库</label>
          </div>
          <button type="button" onClick={() => setFilters(EMPTY_FILTERS)}>清除筛选</button>
        </section>
      )}

      {(data.status === "partial" || data.stale || data.warnings.length > 0) && (
        <div className="literature-map-warning" role="status">
          {data.stale ? "当前使用 7 天内的旧缓存。" : ""}
          {data.warnings.join(" ")}
        </div>
      )}

      <div className="literature-mobile-tabs" aria-label="移动端图谱区域">
        {([
          ["graph", "图谱"],
          ["list", "论文列表"],
          ["detail", "当前论文"],
        ] as Array<[MobilePanel, string]>).map(([value, label]) => (
          <button
            key={value}
            type="button"
            aria-pressed={mobilePanel === value}
            onClick={() => setMobilePanel(value)}
          >
            {label}
          </button>
        ))}
      </div>

      <div className="literature-map-grid">
        <aside className={`literature-map-list ${mobilePanel === "list" ? "is-mobile-active" : ""}`}>
          <div className="literature-panel-heading">
            <strong>相关论文</strong>
            <span>{filteredNodes.length}/{data.nodes.length}</span>
          </div>
          <div className="literature-paper-list">
            {filteredNodes.map((paper) => (
              <button
                type="button"
                key={paper.id}
                className={paper.id === selected.id ? "is-selected" : ""}
                aria-pressed={paper.id === selected.id}
                onClick={() => selectPaper(paper.id)}
              >
                <span className="literature-list-index">
                  {paper.role === "origin" ? "核心" : paper.year || "—"}
                </span>
                <span>
                  <strong>{paper.title}</strong>
                  <small>
                    {paper.authors[0] || "未知作者"} · 引用 {formatCitationCount(paper.citation_count)}
                  </small>
                </span>
              </button>
            ))}
          </div>
        </aside>

        <section className={`literature-map-stage ${mobilePanel === "graph" ? "is-mobile-active" : ""}`}>
          {view === "graph" ? (
            <>
              <div className="literature-stage-caption">
                <span>
                  {relation === "similarity"
                    ? "SPECTER2 语义相近；无 embedding 时只保留 S2 推荐关系"
                    : "箭头从引用论文指向被引用论文"}
                </span>
                <strong>{filteredEdges.filter((edge) => edge.kind === relation).length} 条关系</strong>
              </div>
              <LiteratureGraph
                nodes={filteredNodes}
                edges={filteredEdges}
                relation={relation}
                selectedId={selected.id}
                onSelect={selectPaper}
              />
            </>
          ) : (
            <LiteratureTable
              view={view}
              nodes={filteredNodes}
              rows={currentRows}
              onOpen={(paper) => {
                if (data.nodes.some((node) => node.id === paper.id)) {
                  selectPaper(paper.id);
                  setMobilePanel("detail");
                } else {
                  router.push(`/literature-map/${encodeURIComponent(paper.id)}`);
                }
              }}
            />
          )}
        </section>

        <aside className={`literature-map-detail ${mobilePanel === "detail" ? "is-mobile-active" : ""}`}>
          <div className="literature-detail-kicker">
            <span>{selected.role === "origin" ? "核心论文" : "当前论文"}</span>
            <span>{selected.year || "年份未知"}</span>
          </div>
          <h1>{selected.title}</h1>
          <p className="literature-detail-authors">{selected.authors.join(", ") || "作者未知"}</p>
          <dl>
            <div><dt>引用</dt><dd>{formatCitationCount(selected.citation_count)}</dd></div>
            <div><dt>参考文献</dt><dd>{formatCitationCount(selected.reference_count)}</dd></div>
            <div><dt>来源</dt><dd>{selected.venue || "未标注"}</dd></div>
          </dl>
          <p className="literature-detail-abstract">
            {selected.abstract || "Semantic Scholar 暂未提供摘要。"}
          </p>

          <div className="literature-detail-actions">
            <a href={`/literature-map/${encodeURIComponent(selected.id)}`}>
              以此论文为中心展开
            </a>
            <div>
              {selected.pdf_url && <a href={selected.pdf_url} target="_blank" rel="noreferrer">打开 PDF ↗</a>}
              <a href={selected.url} target="_blank" rel="noreferrer">Semantic Scholar ↗</a>
              {externalHref(selected, "arxiv") && <a href={externalHref(selected, "arxiv")!} target="_blank" rel="noreferrer">arXiv ↗</a>}
              {externalHref(selected, "doi") && <a href={externalHref(selected, "doi")!} target="_blank" rel="noreferrer">DOI ↗</a>}
            </div>
            {selected.arxiv_id ? (
              <div className="literature-product-actions">
                <button type="button" disabled={Boolean(preparing)} onClick={() => openInternal(selected, "reader")}>
                  打开阅读
                </button>
                <button type="button" disabled={Boolean(preparing)} onClick={() => openInternal(selected, "pet")}>
                  问 Pet
                </button>
                <CollectionPicker
                  arxivId={selected.arxiv_id}
                  onBeforeAdd={async () => {
                    setPreparing("正在加入文献库…");
                    try {
                      await ensurePaper(selected);
                    } finally {
                      setPreparing(null);
                    }
                  }}
                />
              </div>
            ) : (
              <p className="literature-external-only">
                这篇论文目前只有外部元数据，可继续展开图谱或前往来源页。
              </p>
            )}
          </div>
          {preparing && <p className="literature-preparing" role="status">{preparing}</p>}
          {error && <p className="literature-detail-error" role="alert">{error}</p>}
        </aside>
      </div>
    </main>
  );
}

function LiteratureTable({
  view,
  nodes,
  rows,
  onOpen,
}: {
  view: WorkspaceView;
  nodes: LiteraturePaper[];
  rows: LiteratureMapWork[];
  onOpen: (paper: LiteraturePaper) => void;
}) {
  const papers = view === "list" ? nodes : rows.map((row) => row.paper);
  const title = view === "prior"
    ? "先行工作"
    : view === "derivative"
      ? "后续工作"
      : "论文列表";
  const description = view === "prior"
    ? "被图中多篇论文共同引用的工作"
    : view === "derivative"
      ? "引用了图中多篇论文的工作"
      : "当前筛选范围内的全部论文";

  return (
    <div className="literature-table-view">
      <header>
        <h2>{title}</h2>
        <p>{description}</p>
      </header>
      {papers.length === 0 ? (
        <p className="literature-empty">当前没有可证明的结果。</p>
      ) : (
        <div className="literature-table-scroll">
          <table>
            <thead>
              <tr><th>论文</th><th>年份</th><th>引用</th><th>图内依据</th></tr>
            </thead>
            <tbody>
              {papers.map((paper, index) => {
                const work = rows[index];
                const graphCount = work?.graph_citation_count ?? work?.graph_reference_count;
                return (
                  <tr key={paper.id}>
                    <td>
                      <button type="button" onClick={() => onOpen(paper)}>
                        <strong>{paper.title}</strong>
                        <span>{paper.authors.slice(0, 2).join(", ")}</span>
                      </button>
                    </td>
                    <td>{paper.year || "—"}</td>
                    <td>{formatCitationCount(paper.citation_count)}</td>
                    <td>{graphCount == null ? "—" : `${graphCount} 篇`}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
