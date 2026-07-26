/** SSE 封装 — 用 fetch + ReadableStream 消费 POST SSE（EventSource 不支持 POST）。 */

import { translateStreamUrl } from "./api";

export interface SSEEvent {
  event: string;
  data: Record<string, unknown>;
}

export type TranslationTerminalEvent = "complete" | "done";

export type TranslationStreamResult = {
  terminalEvent: TranslationTerminalEvent | null;
  aborted: boolean;
  eventCount: number;
};

export type TranslationStreamOptions = {
  signal?: AbortSignal;
  fetchImpl?: typeof fetch;
};

/**
 * Incremental SSE parser. It accepts arbitrary network chunk boundaries and
 * both LF and CRLF separators. `finish()` also dispatches a final event that
 * is not followed by a blank line, which keeps proxy-truncated buffers
 * observable instead of silently dropping them.
 */
export class IncrementalSSEParser {
  private buffer = "";

  push(chunk: string): SSEEvent[] {
    if (!chunk) return [];
    this.buffer += chunk;
    return this.drain(false);
  }

  finish(): SSEEvent[] {
    return this.drain(true);
  }

  private drain(flush: boolean): SSEEvent[] {
    const events: SSEEvent[] = [];
    let separator = findEventSeparator(this.buffer);
    while (separator) {
      const raw = this.buffer.slice(0, separator.index);
      this.buffer = this.buffer.slice(separator.index + separator.length);
      const event = parseSSEEvent(raw);
      if (event) events.push(event);
      separator = findEventSeparator(this.buffer);
    }

    if (flush && this.buffer.trim()) {
      const event = parseSSEEvent(this.buffer);
      if (event) events.push(event);
      this.buffer = "";
    } else if (flush) {
      this.buffer = "";
    }
    return events;
  }
}

export function isTranslationTerminalEvent(
  event: string,
): event is TranslationTerminalEvent {
  return event === "complete" || event === "done";
}

/**
 * 发起翻译 SSE 流，逐事件回调。
 *
 * 保留原有三个参数的调用方式；第四个参数可传 AbortSignal。正常返回时
 * `terminalEvent` 必须是后端现有的 `complete` 或兼容的新 `done`。若连接
 * 在 terminal event 前结束，会调用 onError 并返回 terminalEvent=null。
 */
export async function streamTranslation(
  arxiv_id: string,
  onEvent: (event: SSEEvent) => void,
  onError?: (error: Error) => void,
  options: TranslationStreamOptions = {},
): Promise<TranslationStreamResult> {
  const result: TranslationStreamResult = {
    terminalEvent: null,
    aborted: false,
    eventCount: 0,
  };
  const fetchImpl = options.fetchImpl ?? fetch;
  const parser = new IncrementalSSEParser();
  const decoder = new TextDecoder();

  const dispatch = (events: readonly SSEEvent[]): boolean => {
    for (const event of events) {
      if (result.terminalEvent !== null) break;
      result.eventCount += 1;
      if (isTranslationTerminalEvent(event.event)) {
        result.terminalEvent = event.event;
        if (typeof window !== "undefined") {
          window.dispatchEvent(
            new CustomEvent("peinidu:paper-data-changed", {
              detail: { paperId: arxiv_id },
            }),
          );
        }
      }
      onEvent(event);
    }
    return result.terminalEvent !== null;
  };

  try {
    const response = await fetchImpl(translateStreamUrl(arxiv_id), {
      method: "POST",
      signal: options.signal,
    });
    if (!response.ok || !response.body) {
      onError?.(new Error(`SSE 连接失败: ${response.status}`));
      return result;
    }

    const reader = response.body.getReader();
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      if (dispatch(parser.push(decoder.decode(value, { stream: true })))) {
        try {
          await reader.cancel();
        } catch {
          // The terminal event already won; transport cleanup errors are irrelevant.
        }
        return result;
      }
    }
    if (dispatch(parser.push(decoder.decode()))) return result;
    if (dispatch(parser.finish())) return result;

    if (result.terminalEvent === null && options.signal?.aborted) {
      result.aborted = true;
      return result;
    }
    if (result.terminalEvent === null) {
      onError?.(new Error("翻译流在完成事件前中断，请刷新后继续。"));
    }
    return result;
  } catch (error) {
    if (isAbortError(error) || options.signal?.aborted) {
      if (result.terminalEvent === null) result.aborted = true;
      return result;
    }
    onError?.(error instanceof Error ? error : new Error(String(error)));
    return result;
  }
}

function findEventSeparator(value: string): { index: number; length: number } | null {
  const match = /\r?\n\r?\n/.exec(value);
  return match ? { index: match.index, length: match[0].length } : null;
}

function parseSSEEvent(raw: string): SSEEvent | null {
  let event = "message";
  const dataLines: string[] = [];
  for (const sourceLine of raw.split(/\r?\n/)) {
    const line = sourceLine.startsWith("\uFEFF") ? sourceLine.slice(1) : sourceLine;
    if (!line || line.startsWith(":")) continue;
    const separator = line.indexOf(":");
    const field = separator < 0 ? line : line.slice(0, separator);
    let value = separator < 0 ? "" : line.slice(separator + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") event = value || "message";
    if (field === "data") dataLines.push(value);
  }
  if (dataLines.length === 0) return null;

  const dataText = dataLines.join("\n");
  try {
    const parsed = JSON.parse(dataText) as unknown;
    return {
      event,
      data: isRecord(parsed) ? parsed : { value: parsed },
    };
  } catch {
    return { event, data: { raw: dataText } };
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isAbortError(error: unknown): boolean {
  return error instanceof Error && error.name === "AbortError";
}
