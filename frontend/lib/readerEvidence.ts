export type ReaderEvidenceInput = unknown;

export type ReaderEvidenceHint = {
  arxivId: string | null;
  blockIndex: number | null;
  page: number | null;
  regionId: string | null;
  noteHeading: string | null;
  label: string;
};

export type ReaderNavigationRequest = {
  id: number;
  evidence: ReaderEvidenceInput;
};

const BLOCK_REFERENCE = /(?:\bblock\s*#?\s*|段落\s*#?\s*)(\d+)\b/i;
const PAGE_REFERENCE = /(?:\bpage\s*#?\s*|第?\s*)(\d+)\s*页\b|\bpage\s*#?\s*(\d+)\b/i;

export function getReaderEvidenceHint(input: ReaderEvidenceInput): ReaderEvidenceHint {
  const record = isRecord(input) ? input : {};
  const nested = isRecord(record.location) ? record.location : {};
  const searchable = evidenceStrings(input);
  const blockIndex = nonNegativeInteger(
    nested.block_index ?? nested.blockIndex ?? record.block_index ?? record.blockIndex,
  ) ?? parseReference(searchable, BLOCK_REFERENCE);
  // Only a structured location may assert a region.  A page is context for a
  // trusted block/region anchor, never a navigable citation on its own.
  const regionId = nonEmptyString(nested.region_id ?? nested.regionId);
  const hasAnchor = blockIndex !== null || regionId !== null;
  const page = hasAnchor
    ? positiveInteger(nested.page ?? record.page) ?? parsePageReference(searchable)
    : null;
  const noteHeading = nonEmptyString(record.note_heading ?? record.noteHeading);
  return {
    arxivId: nonEmptyString(record.arxiv_id ?? record.arxivId),
    blockIndex,
    page,
    regionId,
    noteHeading,
    label: evidenceLabel(input),
  };
}

export function hasReaderEvidenceLocation(input: ReaderEvidenceInput): boolean {
  const hint = getReaderEvidenceHint(input);
  return hint.blockIndex !== null || hint.regionId !== null || hint.noteHeading !== null;
}

function evidenceStrings(input: ReaderEvidenceInput): string[] {
  if (typeof input === "string") return [input];
  if (!isRecord(input)) return [];
  return [input.source, input.citation]
    .filter((item): item is string => typeof item === "string" && Boolean(item.trim()));
}

function evidenceLabel(input: ReaderEvidenceInput): string {
  if (typeof input === "string") return input.trim().slice(0, 180) || "查看论文证据";
  if (!isRecord(input)) return "查看论文证据";
  const value = [input.claim, input.detail, input.title, input.citation, input.source]
    .find((item) => typeof item === "string" && Boolean(item.trim()));
  return typeof value === "string" ? value.trim().slice(0, 180) : "查看论文证据";
}

function positiveInteger(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) && value > 0
    ? value
    : null;
}

function nonNegativeInteger(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) && value >= 0
    ? value
    : null;
}

function nonEmptyString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function parseReference(values: readonly string[], pattern: RegExp): number | null {
  for (const value of values) {
    const match = pattern.exec(value);
    const parsed = match?.[1] ? Number.parseInt(match[1], 10) : Number.NaN;
    if (Number.isInteger(parsed) && parsed >= 0) return parsed;
  }
  return null;
}

function parsePageReference(values: readonly string[]): number | null {
  for (const value of values) {
    const match = PAGE_REFERENCE.exec(value);
    const raw = match?.[1] || match?.[2];
    const parsed = raw ? Number.parseInt(raw, 10) : Number.NaN;
    if (Number.isInteger(parsed) && parsed > 0) return parsed;
  }
  return null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}
