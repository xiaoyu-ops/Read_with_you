"use client";

import type { PaperDetail } from "./api";

export const CURRENT_READING_KEY = "peinidu.currentReading";
export const CURRENT_READING_EVENT = "peinidu:current-reading-updated";

export type CurrentReading = {
  arxiv_id: string;
  title: string;
  authors: string[];
  source: string;
  block_count: number;
  updated_at: string;
};

function isCurrentReading(value: unknown): value is CurrentReading {
  if (!value || typeof value !== "object") return false;
  const item = value as Partial<CurrentReading>;
  return (
    typeof item.arxiv_id === "string" &&
    typeof item.title === "string" &&
    Array.isArray(item.authors) &&
    typeof item.source === "string" &&
    typeof item.block_count === "number" &&
    typeof item.updated_at === "string"
  );
}

export function readCurrentReading(): CurrentReading | null {
  try {
    const raw = window.localStorage.getItem(CURRENT_READING_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    return isCurrentReading(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

export function saveCurrentReading(paper: PaperDetail): CurrentReading {
  const item: CurrentReading = {
    arxiv_id: paper.arxiv_id,
    title: paper.title,
    authors: paper.authors,
    source: paper.source,
    block_count: paper.blocks.length,
    updated_at: new Date().toISOString(),
  };
  window.localStorage.setItem(CURRENT_READING_KEY, JSON.stringify(item));
  window.dispatchEvent(new CustomEvent(CURRENT_READING_EVENT, { detail: item }));
  return item;
}

export function clearCurrentReading(): void {
  window.localStorage.removeItem(CURRENT_READING_KEY);
  window.dispatchEvent(new CustomEvent(CURRENT_READING_EVENT, { detail: null }));
}
