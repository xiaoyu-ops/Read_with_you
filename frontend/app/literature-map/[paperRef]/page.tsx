"use client";

import { useParams } from "next/navigation";
import { Header } from "@/components/Header";
import { LiteratureMapWorkspace } from "@/components/literature-map/LiteratureMapWorkspace";

export default function LiteratureMapPage() {
  const params = useParams();
  const paperRef = String(params.paperRef || "");

  return (
    <>
      <Header />
      <LiteratureMapWorkspace paperRef={paperRef} />
    </>
  );
}
