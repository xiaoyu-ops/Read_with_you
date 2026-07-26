"use client";

import { useLayoutEffect } from "react";

import { ensurePdfDocumentPreload } from "./InlinePdfReader";


export function PaperPdfPreloadBridge({
  paperId,
  pdfUrl,
}: {
  paperId: string;
  pdfUrl: string;
}) {
  useLayoutEffect(() => {
    ensurePdfDocumentPreload(paperId, pdfUrl);
  }, [paperId, pdfUrl]);

  return null;
}
