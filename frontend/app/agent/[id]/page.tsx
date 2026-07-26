"use client";

import { useParams } from "next/navigation";
import { Header } from "@/components/Header";
import { AgentWorkspace } from "@/components/agent/AgentWorkspace";

export default function AgentPaperPage() {
  const params = useParams();
  const arxivId = String(params.id || "");

  return (
    <>
      <Header />
      <AgentWorkspace arxivId={arxivId} />
    </>
  );
}
