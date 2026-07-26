"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { Header } from "@/components/Header";
import { FadeUp } from "@/components/FadeUp";
import {
  getCollection,
  getCollectionAgentReport,
  removePaperFromCollection,
  runCollectionAgentReport,
  type CollectionAgentReport,
  type CollectionDetail,
} from "@/lib/api";

const NOTE_KIND_LABELS: Record<string, string> = {
  highlight: "高亮",
  important: "重要",
  question: "疑问",
  method: "方法",
  conclusion: "结论",
};

function noteTime(value?: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" });
}

export default function CollectionDetailPage() {
  const params = useParams();
  const collectionId = Number(params.id);
  const [collection, setCollection] = useState<CollectionDetail | null>(null);
  const [report, setReport] = useState<CollectionAgentReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [agentRunning, setAgentRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [agentError, setAgentError] = useState<string | null>(null);

  const refresh = async () => {
    const data = await getCollection(collectionId);
    setCollection(data);
  };

  const refreshReport = async () => {
    const data = await getCollectionAgentReport(collectionId);
    setReport(data);
  };

  useEffect(() => {
    if (!Number.isFinite(collectionId)) {
      setError("专题 ID 无效");
      setLoading(false);
      return;
    }
    Promise.all([refresh(), refreshReport()])
      .catch((e) => setError((e as Error).message))
      .finally(() => setLoading(false));
  }, [collectionId]);

  const handleRunAgent = async () => {
    if (!collection) return;
    setAgentRunning(true);
    setAgentError(null);
    try {
      const data = await runCollectionAgentReport(collection.id);
      setReport(data);
    } catch (e) {
      setAgentError((e as Error).message);
    } finally {
      setAgentRunning(false);
    }
  };

  const handleRemove = async (arxivId: string) => {
    if (!collection) return;
    setError(null);
    try {
      const data = await removePaperFromCollection(collection.id, arxivId);
      setCollection(data);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <>
      <Header />
      <main className="max-w-3xl mx-auto px-4 pt-24 md:pt-28 pb-16">
        <FadeUp>
          <a
            href="/library"
            className="mb-6 inline-flex text-xs font-mono text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors"
          >
            ← 返回文献库
          </a>

          <div className="flex items-center gap-3 mb-6">
            <div className="decorate-bar h-8" />
            <h1 className="text-2xl font-medium tracking-tight">
              {collection?.name || "专题"}
            </h1>
          </div>
        </FadeUp>

        <FadeUp delay={1}>
          {loading && (
            <p className="text-sm font-mono text-[hsl(var(--muted-foreground))] animate-pulse">
              读取专题中…
            </p>
          )}
          {error && <p className="mb-4 text-sm text-red-500 font-mono">{error}</p>}
          {collection && (
            <section className="mb-8 border-y border-[hsl(var(--border))] py-4">
              <div className="flex items-start justify-between gap-4">
                <div>
                  <h2 className="text-base font-medium tracking-tight">专题 Agent</h2>
                  <p className="mt-1 text-sm leading-relaxed text-[hsl(var(--muted-foreground))]">
                    汇总专题内论文的单篇分析和“你的笔记”，并始终分开标注来源。
                  </p>
                </div>
                <button
                  onClick={handleRunAgent}
                  disabled={agentRunning || collection.papers.length === 0}
                  className="shrink-0 rounded-md bg-[hsl(var(--primary))] px-3 py-2 text-xs font-mono text-[hsl(var(--primary-foreground))] hover:opacity-90 disabled:opacity-40"
                >
                  {agentRunning ? "整理中…" : report ? "重新整理" : "横向整理"}
                </button>
              </div>

              {agentError && (
                <p className="mt-3 text-sm text-red-500 font-mono">{agentError}</p>
              )}

              {report && (
                <div className="mt-4 space-y-4">
                  <div className="flex flex-wrap gap-4 text-xs font-mono text-[hsl(var(--muted-foreground))]">
                    <span>{report.paper_count} 篇论文</span>
                    <span>{report.analyzed_count} 篇已分析</span>
                    <span>{report.annotated_count} 篇有笔记/高亮</span>
                    <a
                      href="/agent"
                      className="hover:text-[hsl(var(--foreground))]"
                    >
                      任务中心
                    </a>
                  </div>

                  <div className="space-y-1">
                    {report.synthesis.map((item, index) => (
                      <p key={index} className="text-sm leading-relaxed">
                        {item}
                      </p>
                    ))}
                  </div>

                  <div className="divide-y divide-[hsl(var(--border))]">
                    {report.papers.map((paper) => (
                      <div key={paper.arxiv_id} className="py-3">
                        <div className="flex items-start justify-between gap-3">
                          <div className="min-w-0">
                            <a
                              href={`/paper/${paper.arxiv_id}`}
                              className="block truncate text-sm font-medium hover:underline underline-offset-4"
                            >
                              {paper.title}
                            </a>
                            <p className="mt-1 text-xs font-mono text-[hsl(var(--muted-foreground))]">
                              {paper.has_analysis ? "已分析" : "待分析"} · 选区笔记 {paper.selection_note_count ?? 0}
                              {paper.has_paper_note ? " · 有整篇笔记" : ""}
                            </p>
                          </div>
                          {paper.reproducibility_verdict && (
                            <span className="shrink-0 text-xs font-mono text-[hsl(var(--muted-foreground))]">
                              {paper.reproducibility_verdict}
                            </span>
                          )}
                        </div>
                        {paper.summary && (
                          <p className="mt-2 line-clamp-2 text-sm text-[hsl(var(--muted-foreground))]">
                            论文分析：{paper.summary}
                          </p>
                        )}
                        {paper.note_preview && (
                          <p className="mt-1 line-clamp-2 text-sm text-[hsl(var(--muted-foreground))]">
                            你的笔记：{paper.note_preview}
                          </p>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </section>
          )}
          {!loading && collection && collection.papers.length === 0 && (
            <p className="text-sm text-[hsl(var(--muted-foreground))] leading-relaxed">
              这个专题里还没有论文。打开任意论文后，可以从阅读页加入这里。
            </p>
          )}
          {collection && collection.papers.length > 0 && (
            <div className="divide-y divide-[hsl(var(--border))]">
              {collection.papers.map((paper) => (
                <div key={paper.arxiv_id} className="py-4">
                  <div className="flex items-start justify-between gap-4">
                    <div className="min-w-0">
                      <a
                        href={`/paper/${paper.arxiv_id}`}
                        className="block text-base font-medium tracking-tight hover:underline underline-offset-4"
                      >
                        {paper.title}
                      </a>
                      <p className="mt-1 text-xs font-mono text-[hsl(var(--muted-foreground))]">
                        {paper.arxiv_id} · {paper.status}
                      </p>
                      {paper.authors.length > 0 && (
                        <p className="mt-1 line-clamp-1 text-xs text-[hsl(var(--muted-foreground))]">
                          {paper.authors.join(", ")}
                        </p>
                      )}
                      <div className="mt-3 flex flex-wrap items-center gap-2 text-xs font-mono text-[hsl(var(--muted-foreground))]">
                        <span>
                          {(paper.selection_note_count ?? 0) > 0 || paper.has_paper_note
                            ? `选区笔记 ${paper.selection_note_count ?? 0}${paper.has_paper_note ? " · 整篇笔记已保存" : ""}`
                            : "尚未记笔记"}
                        </span>
                        {Object.entries(paper.note_kind_counts ?? {})
                          .filter(([, count]) => count > 0)
                          .map(([kind, count]) => (
                            <span
                              key={kind}
                              className="rounded-full border border-[hsl(var(--border))] px-2 py-0.5"
                            >
                              {NOTE_KIND_LABELS[kind] || kind} {count}
                            </span>
                          ))}
                        {paper.note_updated_at && <span>更新 {noteTime(paper.note_updated_at)}</span>}
                      </div>
                      {paper.note_preview && (
                        <a
                          href={`/paper/${paper.arxiv_id}#paper-notes`}
                          className="mt-2 block line-clamp-2 text-sm leading-relaxed text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))]"
                        >
                          {paper.note_preview}
                        </a>
                      )}
                    </div>
                    <div className="flex shrink-0 flex-col items-end gap-2">
                      <a
                        href={`/paper/${paper.arxiv_id}#paper-notes`}
                        className="rounded-md border border-[hsl(var(--border))] px-2.5 py-1 text-xs font-mono text-[hsl(var(--foreground))] hover:bg-[hsl(var(--muted))]"
                      >
                        {(paper.selection_note_count ?? 0) > 0 || paper.has_paper_note
                          ? "打开笔记"
                          : "开始笔记"}
                      </a>
                      <button
                        onClick={() => handleRemove(paper.arxiv_id)}
                        className="rounded-md border border-[hsl(var(--border))] px-2.5 py-1 text-xs font-mono text-[hsl(var(--muted-foreground))] hover:bg-[hsl(var(--muted))] hover:text-red-500 transition-colors"
                      >
                        移出
                      </button>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </FadeUp>
      </main>
    </>
  );
}
