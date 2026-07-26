"use client";

import { useEffect, useState } from "react";
import { Header } from "@/components/Header";
import { FadeUp } from "@/components/FadeUp";
import { listPapers, type PaperMeta } from "@/lib/api";
import {
  clearCurrentReading,
  readCurrentReading,
  type CurrentReading,
} from "@/lib/currentReading";

function formatTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function authorsText(authors: string[]): string {
  if (authors.length === 0) return "作者未知";
  const names = authors.slice(0, 3).join(", ");
  return authors.length > 3 ? `${names} et al.` : names;
}

export default function ReadingPage() {
  const [current, setCurrent] = useState<CurrentReading | null>(null);
  const [papers, setPapers] = useState<PaperMeta[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setCurrent(readCurrentReading());
    listPapers()
      .then((items) => {
        if (!cancelled) setPapers(items);
      })
      .catch((e) => {
        if (!cancelled) setError((e as Error).message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const clear = () => {
    clearCurrentReading();
    setCurrent(null);
  };

  return (
    <>
      <Header />
      <main className="mx-auto max-w-3xl px-4 pb-16 pt-24 md:pt-28">
        <FadeUp>
          <div className="mb-6 flex items-center gap-3">
            <div className="decorate-bar h-8" />
            <h1 className="text-2xl font-medium tracking-tight">阅读</h1>
          </div>
          <p className="mb-8 max-w-2xl text-sm leading-relaxed text-[hsl(var(--muted-foreground))]">
            选择一篇已经准备好的论文进入阅读页；最近阅读会保留在最上方。
          </p>
        </FadeUp>

        <FadeUp delay={1}>
          {loading && (
            <p className="animate-pulse text-sm font-mono text-[hsl(var(--muted-foreground))]">
              正在读取本地阅读记录…
            </p>
          )}
          {error && <p className="text-sm font-mono text-red-500">{error}</p>}

          {!loading && !error && (
            <div className="space-y-10">
              {current && (
                <section>
                  <div className="mb-3 flex items-end justify-between gap-4">
                    <div>
                      <h2 className="text-lg font-medium tracking-tight">最近阅读</h2>
                      <p className="mt-1 text-xs font-mono text-[hsl(var(--muted-foreground))]">
                        {formatTime(current.updated_at)}
                      </p>
                    </div>
                    <button
                      type="button"
                      onClick={clear}
                      className="text-xs font-mono text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))]"
                    >
                      清除记录
                    </button>
                  </div>
                  <a
                    href={`/paper/${current.arxiv_id}`}
                    className="block border-y border-[hsl(var(--border))] py-4 hover:bg-[hsl(var(--muted))]/45"
                  >
                    <p className="truncate text-sm font-medium tracking-tight">{current.title}</p>
                    <p className="mt-2 text-xs font-mono text-[hsl(var(--muted-foreground))]">
                      {current.arxiv_id} · {current.source} · {current.block_count} blocks
                    </p>
                  </a>
                </section>
              )}

              <section>
                <h2 className="text-lg font-medium tracking-tight">本地论文</h2>
                <p className="mt-1 text-xs font-mono text-[hsl(var(--muted-foreground))]">
                  点击论文后进入阅读页
                </p>
                {papers.length === 0 ? (
                  <div className="mt-4 border-y border-[hsl(var(--border))] py-5">
                    <p className="text-sm leading-relaxed text-[hsl(var(--muted-foreground))]">
                      还没有可阅读的论文。先检索或导入一篇论文后，这里会出现选择列表。
                    </p>
                    <div className="mt-5 flex flex-wrap gap-3">
                      <a
                        href="/"
                        className="rounded-md bg-[hsl(var(--primary))] px-4 py-2 text-sm font-medium text-[hsl(var(--primary-foreground))] hover:opacity-90"
                      >
                        去检索
                      </a>
                      <a
                        href="/library"
                        className="rounded-md border border-[hsl(var(--border))] px-4 py-2 text-sm font-mono text-[hsl(var(--muted-foreground))] hover:bg-[hsl(var(--muted))] hover:text-[hsl(var(--foreground))]"
                      >
                        文献库
                      </a>
                    </div>
                  </div>
                ) : (
                  <div className="mt-4 divide-y divide-[hsl(var(--border))] border-y border-[hsl(var(--border))]">
                    {papers.map((paper) => (
                      <article
                        key={paper.arxiv_id}
                        className="py-4"
                      >
                        <div className="flex flex-col gap-2 md:flex-row md:items-start md:justify-between">
                          <div className="min-w-0">
                            <a
                              href={`/paper/${paper.arxiv_id}`}
                              className="block truncate text-sm font-medium tracking-tight hover:underline underline-offset-4"
                            >
                              {paper.title}
                            </a>
                            <p className="mt-1 text-sm leading-relaxed text-[hsl(var(--muted-foreground))]">
                              {authorsText(paper.authors)}
                            </p>
                            {paper.note_preview && (
                              <p className="mt-2 line-clamp-2 text-sm leading-relaxed text-[hsl(var(--muted-foreground))]">
                                {paper.note_preview}
                              </p>
                            )}
                          </div>
                          <div className="flex shrink-0 flex-wrap items-center gap-3 text-xs font-mono text-[hsl(var(--muted-foreground))] md:justify-end">
                            <span>
                              {(paper.selection_note_count ?? 0) > 0 || paper.has_paper_note
                                ? `笔记 ${paper.selection_note_count ?? 0}${paper.has_paper_note ? " + 主笔记" : ""}`
                                : "尚未记笔记"}
                            </span>
                            {paper.note_updated_at && <span>{formatTime(paper.note_updated_at)}</span>}
                            <a
                              href={`/paper/${paper.arxiv_id}#paper-notes`}
                              className="text-[hsl(var(--foreground))] underline decoration-[hsl(var(--border))] underline-offset-4"
                            >
                              {(paper.selection_note_count ?? 0) > 0 || paper.has_paper_note
                                ? "打开笔记"
                                : "开始笔记"}
                            </a>
                          </div>
                        </div>
                        <p className="mt-2 text-xs font-mono text-[hsl(var(--muted-foreground))]">
                          {paper.arxiv_id} · {paper.status}
                        </p>
                      </article>
                    ))}
                  </div>
                )}
              </section>
            </div>
          )}
        </FadeUp>
      </main>
    </>
  );
}
