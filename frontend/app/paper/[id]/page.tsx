"use client";

import { useEffect, useState, useCallback } from "react";
import dynamic from "next/dynamic";
import { useParams } from "next/navigation";
import { Header } from "@/components/Header";
import { InlinePdfReader } from "@/components/InlinePdfReader";
import { LocalPaperSyncStatus } from "@/components/LocalPaperSyncStatus";
import {
  getPaperIfExists,
  prefetchTranslationLayout,
  type PaperDetail,
} from "@/lib/api";
import { saveCurrentReading } from "@/lib/currentReading";
import { recoverPaperFromLocalIfAvailable } from "@/lib/localPaperLibrary";
import type { PetQuestionRequest, ReaderAgentContext } from "@/lib/readerContext";
import type { ReaderEvidenceInput, ReaderNavigationRequest } from "@/lib/readerEvidence";

const PetAssistant = dynamic(
  () => import("@/components/PetAssistant").then((module) => module.PetAssistant),
  { ssr: false },
);

export default function PaperPage() {
  const params = useParams();
  const arxivId = params.id as string;
  const [paper, setPaper] = useState<PaperDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [readerReady, setReaderReady] = useState(false);
  const [openPetOnReady, setOpenPetOnReady] = useState(false);
  const [readerContext, setReaderContext] = useState<ReaderAgentContext | null>(null);
  const [petQuestion, setPetQuestion] = useState<
    (PetQuestionRequest & { id: number }) | null
  >(null);
  const [readerNavigation, setReaderNavigation] = useState<ReaderNavigationRequest | null>(null);

  useEffect(() => {
    setOpenPetOnReady(
      new URLSearchParams(window.location.search).get("pet") === "open",
    );
    setReaderContext(null);
    setReaderNavigation(null);
    setPetQuestion(null);
    setReaderReady(false);
    const pendingEvidence = window.sessionStorage.getItem("pet:pending-reader-evidence");
    if (pendingEvidence) {
      try {
        const parsed = JSON.parse(pendingEvidence) as {
          arxivId?: unknown;
          evidence?: ReaderEvidenceInput;
        };
        if (parsed.arxivId === arxivId && parsed.evidence) {
          setReaderNavigation({ id: Date.now(), evidence: parsed.evidence });
          window.sessionStorage.removeItem("pet:pending-reader-evidence");
        }
      } catch {
        window.sessionStorage.removeItem("pet:pending-reader-evidence");
      }
    }
    (async () => {
      try {
        let p = await getPaperIfExists(arxivId);
        if (!p && (await recoverPaperFromLocalIfAvailable(arxivId))) {
          p = await getPaperIfExists(arxivId);
        }
        if (!p) throw new Error(`论文未找到: ${arxivId}`);
        prefetchTranslationLayout(arxivId);
        setPaper(p);
        saveCurrentReading(p);
      } catch (e) {
        setError((e as Error).message);
      } finally {
        setLoading(false);
      }
    })();
  }, [arxivId]);

  const onAskPet = useCallback((request: PetQuestionRequest) => {
    setPetQuestion({ ...request, id: Date.now() });
  }, []);

  const onNavigateEvidence = useCallback((evidence: ReaderEvidenceInput) => {
    setReaderNavigation((current) => ({
      id: (current?.id ?? 0) + 1,
      evidence,
    }));
  }, []);
  const onFirstPageReady = useCallback(() => {
    setReaderReady(true);
  }, []);

  if (loading) {
    return (
      <>
        <Header />
        <main className="max-w-[96rem] mx-auto px-4 md:px-6 xl:px-8 pt-20 pb-16">
          <p className="text-sm font-mono text-[hsl(var(--muted-foreground))] animate-pulse">
            加载论文中…
          </p>
        </main>
      </>
    );
  }

  if (error || !paper) {
    return (
      <>
        <Header />
        <main className="max-w-3xl mx-auto px-4 pt-24 pb-16">
          <p className="text-sm text-red-500 font-mono">{error || "论文未找到"}</p>
          <a
            href="/"
            className="mt-4 inline-flex text-xs font-mono text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors"
          >
            ← 返回检索
          </a>
          {error?.includes("本地文献库需要重新授权") && (
            <a
              href="/config"
              className="ml-4 mt-4 inline-flex text-xs font-mono text-[hsl(var(--foreground))] underline underline-offset-4"
            >
              前往设置重新授权
            </a>
          )}
        </main>
      </>
    );
  }

  return (
    <>
      <Header />
      <main className="paper-workspace max-w-[96rem] mx-auto px-4 md:px-6 xl:px-8 pt-20 pb-16">
        <div>
          <a
            href="/"
            className="reader-back-link"
          >
            <span aria-hidden="true">←</span>
            返回检索
          </a>
          <LocalPaperSyncStatus paperId={arxivId} />
          <InlinePdfReader
            paper={paper}
            onAgentContextChange={setReaderContext}
            onAskPet={onAskPet}
            navigationRequest={readerNavigation}
            onFirstPageReady={onFirstPageReady}
          />
        </div>
      </main>
      {readerReady && (
        <PetAssistant
          paper={paper}
          readerContext={readerContext}
          askRequest={petQuestion}
          onNavigateEvidence={onNavigateEvidence}
          initialOpen={openPetOnReady}
        />
      )}
    </>
  );
}
