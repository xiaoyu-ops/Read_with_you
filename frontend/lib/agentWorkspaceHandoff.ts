import type { ReaderAgentContext } from "./readerContext";

const HANDOFF_KEY = "pet:agent-workspace-handoff";
const MAX_HANDOFF_AGE_MS = 30 * 60 * 1000;
const MAX_HANDOFF_BYTES = 24_000;

type AgentWorkspaceHandoff = {
  version: 1;
  arxiv_id: string;
  reader: ReaderAgentContext;
  created_at: number;
};

function isReaderContext(value: unknown): value is ReaderAgentContext {
  if (!value || typeof value !== "object") return false;
  const reader = value as Partial<ReaderAgentContext>;
  return (
    reader.reader_mode === "selection_translation" &&
    (reader.page === null ||
      (typeof reader.page === "number" && Number.isInteger(reader.page) && reader.page > 0)) &&
    (reader.region_id === null || typeof reader.region_id === "string") &&
    (reader.layout_confidence === null ||
      (typeof reader.layout_confidence === "number" &&
        reader.layout_confidence >= 0 &&
        reader.layout_confidence <= 1))
  );
}

export function saveAgentWorkspaceHandoff(
  arxivId: string,
  reader: ReaderAgentContext | null,
): void {
  if (typeof window === "undefined" || !reader) return;
  const payload: AgentWorkspaceHandoff = {
    version: 1,
    arxiv_id: arxivId,
    reader,
    created_at: Date.now(),
  };
  const serialized = JSON.stringify(payload);
  if (serialized.length > MAX_HANDOFF_BYTES) return;
  window.sessionStorage.setItem(HANDOFF_KEY, serialized);
}

export function readAgentWorkspaceHandoff(
  arxivId: string,
): ReaderAgentContext | null {
  if (typeof window === "undefined") return null;
  const raw = window.sessionStorage.getItem(HANDOFF_KEY);
  if (!raw || raw.length > MAX_HANDOFF_BYTES) return null;
  try {
    const payload = JSON.parse(raw) as Partial<AgentWorkspaceHandoff>;
    if (payload.arxiv_id !== arxivId) return null;
    if (
      payload.version !== 1 ||
      typeof payload.created_at !== "number" ||
      Date.now() - payload.created_at > MAX_HANDOFF_AGE_MS ||
      !isReaderContext(payload.reader)
    ) {
      window.sessionStorage.removeItem(HANDOFF_KEY);
      return null;
    }
    return payload.reader;
  } catch {
    window.sessionStorage.removeItem(HANDOFF_KEY);
    return null;
  }
}

export function clearAgentWorkspaceHandoff(arxivId: string): void {
  if (typeof window === "undefined") return;
  const raw = window.sessionStorage.getItem(HANDOFF_KEY);
  if (!raw) return;
  try {
    const payload = JSON.parse(raw) as Partial<AgentWorkspaceHandoff>;
    if (payload.arxiv_id !== arxivId) return;
  } catch {
    // Invalid handoff data is never useful to another paper.
  }
  window.sessionStorage.removeItem(HANDOFF_KEY);
}
