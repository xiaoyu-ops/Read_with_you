import type { Block as BlockType, TranslationRenderPolicy } from "./api";

export type ReaderPaneSide = "original" | "translation";

export type ReaderAgentContextBlock = {
  index: number;
  type: BlockType["type"];
  original: string;
  translation: string | null;
  status: BlockType["status"];
};

export type ReaderLocationContext = {
  page: number | null;
  region_id: string | null;
  layout_confidence: number | null;
  render_policy: TranslationRenderPolicy | null;
};

export type ReaderAgentContext = {
  reader_mode: "selection_translation";
  active_block: ReaderAgentContextBlock | null;
  previous_block: ReaderAgentContextBlock | null;
  next_block: ReaderAgentContextBlock | null;
  selected_text: {
    block_index: number | null;
    side: ReaderPaneSide;
    text: string;
  } | null;
} & ReaderLocationContext;

export type PetQuestionRequest = {
  message: string;
  context: ReaderAgentContext;
};

export function buildReaderAgentContext(
  blocks: readonly BlockType[],
  activeIndex: number | null,
  selectedText: ReaderAgentContext["selected_text"],
  location: ReaderLocationContext,
): ReaderAgentContext {
  const activeBlockIndex =
    activeIndex === null ? -1 : blocks.findIndex((block) => block.index === activeIndex);
  const page = Number.isInteger(location.page) && Number(location.page) > 0
    ? Number(location.page)
    : null;
  const regionId = typeof location.region_id === "string" && location.region_id.trim()
    ? location.region_id.trim()
    : null;
  const confidence = typeof location.layout_confidence === "number" &&
    Number.isFinite(location.layout_confidence) &&
    location.layout_confidence >= 0 &&
    location.layout_confidence <= 1
    ? location.layout_confidence
    : null;
  const renderPolicy = isRenderPolicy(location.render_policy)
    ? location.render_policy
    : null;
  return {
    reader_mode: "selection_translation",
    active_block: activeBlockIndex >= 0 ? compactBlock(blocks[activeBlockIndex]) : null,
    previous_block: activeBlockIndex > 0 ? compactBlock(blocks[activeBlockIndex - 1]) : null,
    next_block:
      activeBlockIndex >= 0 && activeBlockIndex < blocks.length - 1
        ? compactBlock(blocks[activeBlockIndex + 1])
        : null,
    selected_text: selectedText,
    page,
    region_id: regionId,
    layout_confidence: confidence,
    render_policy: renderPolicy,
  };
}

function isRenderPolicy(value: unknown): value is TranslationRenderPolicy {
  return value === "replace" || value === "preserve" || value === "panel_only";
}

function compactBlock(block: BlockType | undefined): ReaderAgentContextBlock | null {
  if (!block) return null;
  return {
    index: block.index,
    type: block.type,
    original: block.original.slice(0, 1200),
    translation: block.translation ? block.translation.slice(0, 1200) : null,
    status: block.status,
  };
}
