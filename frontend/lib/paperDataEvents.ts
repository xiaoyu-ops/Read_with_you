"use client";

export const PAPER_DATA_CHANGED_EVENT = "peinidu:paper-data-changed";

export function notifyPaperDataChanged(paperId: string): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(
    new CustomEvent(PAPER_DATA_CHANGED_EVENT, {
      detail: { paperId },
    }),
  );
}
