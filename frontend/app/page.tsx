"use client";

import { useEffect, useState } from "react";
import Image from "next/image";
import { Header } from "@/components/Header";
import { FadeUp } from "@/components/FadeUp";
import { SearchBar } from "@/components/SearchBar";
import { MinerUDocumentImport } from "@/components/MinerUDocumentImport";
import { PaperCandidates } from "@/components/PaperCandidates";
import {
  candidatePaperRef,
  createPaper,
  getPaperIfExists,
  searchPapers,
  type PaperCandidate,
} from "@/lib/api";
import { useRouter } from "next/navigation";

export type HomeTask = "read" | "map";

export default function Home() {
  const router = useRouter();
  const [task, setTask] = useState<HomeTask>("read");
  const [loading, setLoading] = useState(false);
  const [creating, setCreating] = useState(false);
  const [preparingMessage, setPreparingMessage] = useState("");
  const [preparedPaperIds, setPreparedPaperIds] = useState<Set<string>>(new Set());
  const [candidates, setCandidates] = useState<PaperCandidate[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const value = new URLSearchParams(window.location.search).get("task");
    setTask(value === "map" ? "map" : "read");
  }, []);

  const changeTask = (next: HomeTask) => {
    setTask(next);
    router.replace(next === "map" ? "/?task=map" : "/?task=read", { scroll: false });
  };

  const handleSearch = async (query: string) => {
    setLoading(true);
    setError(null);
    setPreparingMessage("");
    setCandidates([]);
    try {
      const results = await searchPapers(query);
      setCandidates(results);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  };

  const handleSelect = async (c: PaperCandidate) => {
    if (!c.arxiv_id) return;
    setCreating(true);
    setError(null);
    setPreparingMessage("正在检查本地是否已有这篇论文…");
    try {
      const existing = await getPaperIfExists(c.arxiv_id);
      if (existing) {
        setPreparedPaperIds((prev) => new Set(prev).add(c.arxiv_id));
        router.push(`/paper/${c.arxiv_id}`);
        return;
      }
      setPreparingMessage("正在准备阅读页：提取论文正文（ar5iv / LaTeX）…");
      await createPaper(c.arxiv_id, c.title, c.authors);
      setPreparedPaperIds((prev) => new Set(prev).add(c.arxiv_id));
      router.push(`/paper/${c.arxiv_id}`);
    } catch (e) {
      setError((e as Error).message);
      setCreating(false);
    }
  };

  const handleBeforeAddToLibrary = async (c: PaperCandidate) => {
    if (!c.arxiv_id) throw new Error("这篇论文暂时没有可提取的 arXiv 版本。");
    if (preparedPaperIds.has(c.arxiv_id)) return;
    await createPaper(c.arxiv_id, c.title, c.authors);
    setPreparedPaperIds((prev) => new Set(prev).add(c.arxiv_id));
  };

  const handleViewMap = (candidate: PaperCandidate) => {
    const paperRef = candidatePaperRef(candidate);
    if (!paperRef) {
      setError("这篇候选缺少可用于构图的论文标识。");
      return;
    }
    router.push(`/literature-map/${encodeURIComponent(paperRef)}`);
  };

  return (
    <>
      <Header />
      <main className="mx-auto max-w-4xl px-4 pt-24 pb-16 md:pt-28">
        <FadeUp>
          <div className="mb-6 flex items-center justify-center gap-2.5 md:gap-3">
            <h1 aria-label="陪你读" className="home-wordmark text-4xl md:text-5xl">
              <span aria-hidden="true">陪你</span>
              <span aria-hidden="true" className="home-wordmark-accent">
                读
              </span>
            </h1>
            <Image
              src="/mascot/home-mascot.png"
              alt="陪你读首页吉祥物"
              width={890}
              height={1095}
              priority
              className="h-14 w-auto select-none object-contain drop-shadow-md md:h-16"
            />
          </div>
          <p className="mx-auto mb-8 max-w-2xl text-center text-sm leading-relaxed text-[hsl(var(--muted-foreground))]">
            原始 PDF 划选翻译、Markdown 笔记与 Pet 研究助手，数据默认留在你的电脑
          </p>
        </FadeUp>

        <FadeUp delay={1}>
          <div className="home-task-switch" role="tablist" aria-label="选择检索任务">
            <button
              type="button"
              role="tab"
              aria-selected={task === "read"}
              className="home-task-tab"
              onClick={() => changeTask("read")}
            >
              找论文精读
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={task === "map"}
              className="home-task-tab"
              onClick={() => changeTask("map")}
            >
              看论文关系
            </button>
          </div>
          <SearchBar
            onSearch={handleSearch}
            loading={loading}
            placeholder={task === "map"
              ? "输入一篇核心论文的标题 / arXiv ID / URL"
              : "输入论文标题 / arXiv ID / URL"}
          />
          <p className="mt-2 text-xs leading-relaxed text-[hsl(var(--muted-foreground))]">
            {task === "map"
              ? "先确认正确论文，再查看相似论文、引用脉络与先行/后续工作。"
              : "先确认正确论文，再提取原始 PDF 进入精读工作台。"}
          </p>
        </FadeUp>

        <FadeUp delay={2}>
          <div className="mt-8 border-t border-[hsl(var(--border))] pt-6">
            <p className="mb-3 text-xs font-mono text-[hsl(var(--muted-foreground))]">
              或导入 PDF
            </p>
            <MinerUDocumentImport />
          </div>
        </FadeUp>

        {error && (
          <FadeUp delay={1}>
            <p className="mt-4 text-sm font-mono text-red-500">{error}</p>
          </FadeUp>
        )}

        {candidates.length > 0 && (
          <FadeUp delay={2} className="mt-8">
            <PaperCandidates
              candidates={candidates}
              onSelect={handleSelect}
              onViewMap={handleViewMap}
              creating={creating}
              task={task}
              onBeforeAddToLibrary={handleBeforeAddToLibrary}
            />
          </FadeUp>
        )}

        {creating && (
          <FadeUp className="mt-8">
            <p className="animate-pulse text-sm font-mono text-[hsl(var(--muted-foreground))]">
              {preparingMessage || "正在准备阅读页…"}
            </p>
          </FadeUp>
        )}
      </main>
    </>
  );
}
