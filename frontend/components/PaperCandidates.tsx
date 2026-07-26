"use client";

import { CollectionPicker } from "./CollectionPicker";
import type { PaperCandidate } from "@/lib/api";

/** 格式化引用数：182524 → 18.3万；1234 → 1.2k */
function formatCitations(n: number | null | undefined): string {
  if (n == null) return "";
  if (n >= 10000) return `${(n / 10000).toFixed(1)}万`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}

export function PaperCandidates({
  candidates,
  onSelect,
  onViewMap,
  creating,
  task,
  onBeforeAddToLibrary,
}: {
  candidates: PaperCandidate[];
  onSelect: (c: PaperCandidate) => void;
  onViewMap: (c: PaperCandidate) => void;
  creating: boolean;
  task: "read" | "map";
  onBeforeAddToLibrary?: (c: PaperCandidate) => Promise<void>;
}) {
  if (candidates.length === 0) {
    return (
      <p className="text-sm text-[hsl(var(--muted-foreground))] font-mono py-8 text-center">
        未找到匹配论文，请尝试更精确的标题或 arXiv ID。
      </p>
    );
  }
  return (
    <div className="space-y-2">
      <p className="text-xs font-mono text-[hsl(var(--muted-foreground))] mb-4">
        找到 {candidates.length} 篇候选（按相关度/引用数降序），请选择正确的论文
      </p>
      {candidates.map((c, i) => {
        const canExtract = c.extractable !== false && Boolean(c.arxiv_id);
        const canMap = Boolean(c.paper_id || c.arxiv_id);
        const readingAction = (
          <button
            key="read"
            onClick={() => canExtract && onSelect(c)}
            disabled={creating || !canExtract}
            className={task === "read"
              ? "rounded-md bg-[hsl(var(--primary))] px-3 py-1.5 text-[hsl(var(--primary-foreground))] transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40"
              : "rounded-md border border-[hsl(var(--border))] px-3 py-1.5 text-[hsl(var(--foreground))] transition-colors hover:bg-[hsl(var(--muted))] disabled:cursor-not-allowed disabled:opacity-40"}
          >
            {canExtract ? "打开阅读" : "无可提取版本"}
          </button>
        );
        const mapAction = (
          <button
            key="map"
            onClick={() => canMap && onViewMap(c)}
            disabled={!canMap}
            className={task === "map"
              ? "rounded-md bg-[hsl(var(--primary))] px-3 py-1.5 text-[hsl(var(--primary-foreground))] transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40"
              : "rounded-md border border-[hsl(var(--border))] px-3 py-1.5 text-[hsl(var(--foreground))] transition-colors hover:bg-[hsl(var(--muted))] disabled:cursor-not-allowed disabled:opacity-40"}
          >
            查看图谱
          </button>
        );
        return (
          <article
            key={`${c.arxiv_id || c.paper_id || c.title}-${i}`}
            className="group rounded-md border border-[hsl(var(--border))] p-4 transition-colors hover:border-[hsl(var(--foreground))]"
          >
            <div className="flex items-start justify-between gap-4">
              <div className="min-w-0 flex-1">
                {/* 标题 */}
                <h3 className="text-sm font-medium group-hover:underline underline-offset-4 transition-colors">
                  {c.title}
                </h3>
                {/* 作者 · 年份 · 出处 */}
                <p className="mt-1 text-xs text-[hsl(var(--muted-foreground))] font-mono">
                  {c.authors.slice(0, 3).join(", ")}
                  {c.authors.length > 3 ? " et al." : ""}
                  {c.year ? ` · ${c.year}` : ""}
                  {c.venue ? ` · ${c.venue}` : ""}
                </p>
                {/* 引用数 · 标题相似度 */}
                <div className="mt-1.5 flex flex-wrap items-center gap-3 text-xs font-mono">
                  {c.citation_count != null && (
                    <span className="text-[hsl(var(--muted-foreground))]">
                      引用 {formatCitations(c.citation_count)}
                    </span>
                  )}
                  {c.similarity != null && (
                    <span className="text-[hsl(var(--muted-foreground))]">
                      相似度 {c.similarity}%
                    </span>
                  )}
                  {/* 来源标记 */}
                  <span className="text-[hsl(var(--muted-foreground))]/60 text-[10px]">
                    {SOURCE_LABELS[c.source] || c.source}
                  </span>
                  {!canExtract && (
                    <span className="text-[10px] text-[hsl(var(--muted-foreground))]">
                      可查看图谱，暂不支持站内阅读
                    </span>
                  )}
                </div>
                {/* 摘要 */}
                {c.abstract && (
                  <p className="mt-2 text-xs text-[hsl(var(--muted-foreground))] line-clamp-2 leading-relaxed">
                    {c.abstract}
                  </p>
                )}
              </div>
              {/* 右侧 arXiv ID */}
              <span className="text-xs font-mono text-[hsl(var(--muted-foreground))] shrink-0 mt-0.5">
                {c.arxiv_id || "S2-only"}
              </span>
            </div>
            <div className="mt-4 flex flex-wrap items-center justify-end gap-2 text-xs font-mono">
              {canExtract && onBeforeAddToLibrary && (
                <CollectionPicker
                  arxivId={c.arxiv_id}
                  onBeforeAdd={() => onBeforeAddToLibrary(c)}
                />
              )}
              {task === "map"
                ? [readingAction, mapAction]
                : [mapAction, readingAction]}
            </div>
          </article>
        );
      })}
    </div>
  );
}

const SOURCE_LABELS: Record<string, string> = {
  arxiv: "arXiv",
  s2: "S2",
  s2_match: "S2⭐",
  merged: "合并",
};
