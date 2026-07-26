import type { Block } from "./api";
import type { SSEEvent, TranslationTerminalEvent } from "./sse";

export type ReaderTranslationStreamStatus =
  | "idle"
  | "streaming"
  | "complete"
  | "error"
  | "aborted";

export type ReaderTranslationState<TOverlay = unknown> = {
  blocks: readonly Block[];
  overlayByBlock: Readonly<Record<number, TOverlay>>;
  fitRevisionByBlock: Readonly<Record<number, number>>;
  streamStatus: ReaderTranslationStreamStatus;
  streamGeneration: number;
  terminalEvent: TranslationTerminalEvent | null;
  streamError: string | null;
};

export type ReaderTranslationAction<TOverlay = unknown> =
  | { type: "stream_started"; generation: number }
  | { type: "stream_event"; generation: number; event: SSEEvent }
  | { type: "stream_failed"; generation: number; error: string }
  | { type: "stream_aborted"; generation: number }
  | { type: "retry_started"; blockIndex: number }
  | {
      type: "retry_finished";
      blockIndex: number;
      translation: string | null;
      status: Block["status"];
    }
  | { type: "fit_ready"; blockIndex: number; revision: number; overlay: TOverlay }
  | { type: "fit_invalidated"; blockIndex: number }
  | { type: "replace_blocks"; blocks: readonly Block[] };

export type TranslationProgress = {
  done: number;
  failed: number;
  pending: number;
  translating: number;
  total: number;
};

export function createReaderTranslationState<TOverlay = unknown>(
  blocks: readonly Block[],
): ReaderTranslationState<TOverlay> {
  return {
    blocks: blocks.map((block) => ({ ...block })),
    overlayByBlock: {},
    fitRevisionByBlock: {},
    streamStatus: "idle",
    streamGeneration: 0,
    terminalEvent: null,
    streamError: null,
  };
}

export function readerTranslationReducer<TOverlay>(
  state: ReaderTranslationState<TOverlay>,
  action: ReaderTranslationAction<TOverlay>,
): ReaderTranslationState<TOverlay> {
  switch (action.type) {
    case "stream_started":
      if (!validNewGeneration(action.generation, state.streamGeneration)) return state;
      return {
        ...state,
        streamStatus: "streaming",
        streamGeneration: action.generation,
        terminalEvent: null,
        streamError: null,
      };
    case "stream_event":
      return reduceReaderTranslationEvent(state, action.event, action.generation);
    case "stream_failed":
      if (action.generation !== state.streamGeneration) return state;
      return {
        ...state,
        streamStatus: "error",
        streamError: action.error,
      };
    case "stream_aborted":
      if (action.generation !== state.streamGeneration) return state;
      return {
        ...state,
        streamStatus: "aborted",
        streamError: null,
      };
    case "retry_started":
      return updateBlockAndInvalidate(state, action.blockIndex, (block) => ({
        ...block,
        status: "translating",
      }));
    case "retry_finished":
      return updateBlockAndInvalidate(state, action.blockIndex, (block) => ({
        ...block,
        translation: action.status === "done" ? action.translation : block.translation,
        status: action.status,
      }));
    case "fit_ready": {
      const block = state.blocks.find((item) => item.index === action.blockIndex);
      if (
        !block ||
        block.status !== "done" ||
        (state.fitRevisionByBlock[action.blockIndex] ?? 0) !== action.revision
      ) {
        return state;
      }
      return {
        ...state,
        overlayByBlock: {
          ...state.overlayByBlock,
          [action.blockIndex]: action.overlay,
        },
      };
    }
    case "fit_invalidated":
      return invalidateOverlay(state, action.blockIndex);
    case "replace_blocks":
      return replaceBlocks(state, action.blocks);
    default:
      return state;
  }
}

export function reduceReaderTranslationEvent<TOverlay>(
  state: ReaderTranslationState<TOverlay>,
  event: SSEEvent,
  generation: number,
): ReaderTranslationState<TOverlay> {
  if (generation !== state.streamGeneration) return state;
  if (event.event === "block_done") {
    const blockIndex = readBlockIndex(event.data.index);
    const translation = event.data.translation;
    if (blockIndex === null || typeof translation !== "string" || !translation.trim()) {
      return state;
    }
    return updateBlockAndInvalidate(state, blockIndex, (block) => ({
      ...block,
      translation,
      status: "done",
    }));
  }

  if (event.event === "block_error") {
    const blockIndex = readBlockIndex(event.data.index);
    if (blockIndex === null) {
      const message = typeof event.data.error === "string"
        ? event.data.error
        : "翻译失败，请稍后重试。";
      return { ...state, streamStatus: "error", streamError: message };
    }
    return updateBlockAndInvalidate(state, blockIndex, (block) => ({
      ...block,
      status: "error",
    }));
  }

  if (event.event === "complete" || event.event === "done") {
    return {
      ...state,
      streamStatus: "complete",
      terminalEvent: event.event,
      streamError: null,
    };
  }

  return state;
}

export function selectTranslationProgress(
  blocks: readonly Block[],
): TranslationProgress {
  const progress: TranslationProgress = {
    done: 0,
    failed: 0,
    pending: 0,
    translating: 0,
    total: 0,
  };
  for (const block of blocks) {
    if (block.status === "skip") continue;
    progress.total += 1;
    if (block.status === "done") progress.done += 1;
    else if (block.status === "error") progress.failed += 1;
    else if (block.status === "translating") progress.translating += 1;
    else progress.pending += 1;
  }
  return progress;
}

export function selectFitRevision<TOverlay>(
  state: ReaderTranslationState<TOverlay>,
  blockIndex: number,
): number {
  return state.fitRevisionByBlock[blockIndex] ?? 0;
}

export function nextStreamGeneration<TOverlay>(
  state: ReaderTranslationState<TOverlay>,
): number {
  return state.streamGeneration + 1;
}

function updateBlockAndInvalidate<TOverlay>(
  state: ReaderTranslationState<TOverlay>,
  blockIndex: number,
  update: (block: Block) => Block,
): ReaderTranslationState<TOverlay> {
  let found = false;
  const blocks = state.blocks.map((block) => {
    if (block.index !== blockIndex) return block;
    found = true;
    return update(block);
  });
  if (!found) return state;
  const invalidated = invalidateOverlay(state, blockIndex);
  return { ...invalidated, blocks };
}

function invalidateOverlay<TOverlay>(
  state: ReaderTranslationState<TOverlay>,
  blockIndex: number,
): ReaderTranslationState<TOverlay> {
  const overlayByBlock = { ...state.overlayByBlock };
  delete overlayByBlock[blockIndex];
  return {
    ...state,
    overlayByBlock,
    fitRevisionByBlock: {
      ...state.fitRevisionByBlock,
      [blockIndex]: (state.fitRevisionByBlock[blockIndex] ?? 0) + 1,
    },
  };
}

function replaceBlocks<TOverlay>(
  state: ReaderTranslationState<TOverlay>,
  blocks: readonly Block[],
): ReaderTranslationState<TOverlay> {
  const revisions: Record<number, number> = { ...state.fitRevisionByBlock };
  for (const block of state.blocks) {
    revisions[block.index] = (revisions[block.index] ?? 0) + 1;
  }
  for (const block of blocks) {
    if (!(block.index in revisions)) revisions[block.index] = 1;
  }
  return {
    ...state,
    blocks: blocks.map((block) => ({ ...block })),
    overlayByBlock: {},
    fitRevisionByBlock: revisions,
    streamStatus: "idle",
    streamGeneration: state.streamGeneration + 1,
    terminalEvent: null,
    streamError: null,
  };
}

function readBlockIndex(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) && value >= 0
    ? value
    : null;
}

function validNewGeneration(value: number, current: number): boolean {
  return Number.isSafeInteger(value) && value > current;
}
