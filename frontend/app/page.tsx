"use client";

import { useState } from "react";
import Image from "next/image";
import { Header } from "@/components/Header";
import { FadeUp } from "@/components/FadeUp";
import { SearchBar } from "@/components/SearchBar";
import { MinerUDocumentImport } from "@/components/MinerUDocumentImport";
import { PaperCandidates } from "@/components/PaperCandidates";
import { searchPapers, createPaper, getPaperIfExists, type PaperCandidate } from "@/lib/api";
import { useRouter } from "next/navigation";

export default function Home() {
  const router = useRouter();
  const [loading, setLoading] = useState(false);
  const [creating, setCreating] = useState(false);
  const [preparingMessage, setPreparingMessage] = useState("");
  const [preparedPaperIds, setPreparedPaperIds] = useState<Set<string>>(new Set());
  const [candidates, setCandidates] = useState<PaperCandidate[]>([]);
  const [error, setError] = useState<string | null>(null);

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
    if (preparedPaperIds.has(c.arxiv_id)) return;
    await createPaper(c.arxiv_id, c.title, c.authors);
    setPreparedPaperIds((prev) => new Set(prev).add(c.arxiv_id));
  };

  return (
    <>
      <Header />
      <main className="mx-auto max-w-3xl px-4 pt-24 pb-16 md:pt-28">
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
          <SearchBar onSearch={handleSearch} loading={loading} />
        </FadeUp>

        <FadeUp delay={2}>
          <MinerUDocumentImport />
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
              creating={creating}
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
