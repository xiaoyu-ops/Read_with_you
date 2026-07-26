"use client";

import type React from "react";
import { useEffect, useState } from "react";
import katex from "katex";
import { API_BASE } from "@/lib/api";

type MathPart =
  | { type: "text"; value: string }
  | { type: "math"; value: string; display: boolean };

type Highlight = {
  id: string;
  text: string;
  note?: string;
};

type StructuredTableCell = {
  text: string;
  header?: boolean;
  colspan?: number;
  rowspan?: number;
};

type StructuredTable = {
  kind: "table";
  rows: StructuredTableCell[][];
};

function renderMath(value: string, displayMode: boolean): string {
  try {
    return katex.renderToString(value, {
      displayMode,
      throwOnError: false,
      strict: false,
      trust: false,
    });
  } catch {
    return value;
  }
}

function splitMath(text: string): MathPart[] {
  const parts: MathPart[] = [];
  let buffer = "";
  let i = 0;

  while (i < text.length) {
    if (text.startsWith("$$", i)) {
      const end = text.indexOf("$$", i + 2);
      if (end !== -1) {
        if (buffer) parts.push({ type: "text", value: buffer });
        parts.push({ type: "math", value: text.slice(i + 2, end).trim(), display: true });
        buffer = "";
        i = end + 2;
        continue;
      }
    }

    if (text[i] === "$") {
      const end = text.indexOf("$", i + 1);
      if (end !== -1) {
        if (buffer) parts.push({ type: "text", value: buffer });
        parts.push({ type: "math", value: text.slice(i + 1, end).trim(), display: false });
        buffer = "";
        i = end + 1;
        continue;
      }
    }

    buffer += text[i];
    i += 1;
  }

  if (buffer) parts.push({ type: "text", value: buffer });
  return parts;
}

function renderHighlightedText(text: string, highlights: Highlight[]) {
  const usable = highlights
    .filter((highlight) => highlight.text.trim())
    .sort((a, b) => b.text.length - a.text.length);
  if (usable.length === 0) return text;

  const nodes: React.ReactNode[] = [];
  let cursor = 0;
  let key = 0;

  while (cursor < text.length) {
    let match: { highlight: Highlight; index: number } | null = null;
    for (const highlight of usable) {
      const index = text.indexOf(highlight.text, cursor);
      if (index === -1) continue;
      if (!match || index < match.index) match = { highlight, index };
    }

    if (!match) {
      nodes.push(text.slice(cursor));
      break;
    }

    if (match.index > cursor) {
      nodes.push(text.slice(cursor, match.index));
    }
    nodes.push(
      <mark
        key={`${match.highlight.id}-${key}`}
        className="reader-annotation-mark"
        title={match.highlight.note || "用户标注"}
      >
        {match.highlight.text}
      </mark>,
    );
    cursor = match.index + match.highlight.text.length;
    key += 1;
  }

  return nodes;
}

export function RichText({
  text,
  highlights = [],
}: {
  text: string;
  highlights?: Highlight[];
}) {
  return (
    <>
      {splitMath(text).map((part, index) => {
        if (part.type === "text") {
          return <span key={index}>{renderHighlightedText(part.value, highlights)}</span>;
        }
        return (
          <span
            key={index}
            className={part.display ? "reader-math reader-math-display" : "reader-math"}
            dangerouslySetInnerHTML={{ __html: renderMath(part.value, part.display) }}
          />
        );
      })}
    </>
  );
}

export function FormulaBlock({ text }: { text: string }) {
  return (
    <div
      className="reader-formula"
      dangerouslySetInnerHTML={{ __html: renderMath(text, true) }}
    />
  );
}

function parseMarkdownTable(markdown: string): string[][] {
  return markdown
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line.startsWith("|") && line.endsWith("|"))
    .filter((line) => !/^\|\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|$/.test(line))
    .map((line) =>
      line
        .slice(1, -1)
        .split("|")
        .map((cell) => cell.trim()),
    );
}

function parseStructuredTable(value: string): StructuredTable | null {
  try {
    const data = JSON.parse(value) as StructuredTable;
    if (data?.kind !== "table" || !Array.isArray(data.rows)) return null;
    return data;
  } catch {
    return null;
  }
}

export function RichTable({ markdown }: { markdown: string }) {
  const structured = parseStructuredTable(markdown);
  if (structured) {
    return (
      <div className="reader-table-wrap">
        <table className="reader-table">
          <tbody>
            {structured.rows.map((row, rowIndex) => (
              <tr key={rowIndex}>
                {row.map((cell, cellIndex) => {
                  const Tag = cell.header ? "th" : "td";
                  return (
                    <Tag
                      key={cellIndex}
                      colSpan={cell.colspan || 1}
                      rowSpan={cell.rowspan || 1}
                    >
                      <RichText text={cell.text} />
                    </Tag>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  }

  const rows = parseMarkdownTable(markdown);
  if (rows.length === 0) {
    return (
      <pre className="reader-code-fallback">
        {markdown}
      </pre>
    );
  }

  const [header, ...body] = rows;
  return (
    <div className="reader-table-wrap">
      <table className="reader-table">
        <thead>
          <tr>
            {header.map((cell, index) => (
              <th key={index}>
                <RichText text={cell} />
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {body.map((row, rowIndex) => (
            <tr key={rowIndex}>
              {header.map((_, cellIndex) => (
                <td key={cellIndex}>
                  <RichText text={row[cellIndex] || ""} />
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function FigureBlock({ value }: { value: string }) {
  const [preview, setPreview] = useState<{ src: string; alt: string } | null>(null);
  let data: { images?: string[]; caption?: string } = {};
  try {
    data = JSON.parse(value);
  } catch {
    data = { caption: value };
  }

  const images = data.images || [];
  const caption = data.caption || "";
  useEffect(() => {
    if (!preview) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setPreview(null);
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [preview]);

  return (
    <>
      <figure className="reader-figure">
        {images.length > 0 && (
          <div className="reader-figure-images">
            {images.map((src, index) => {
              const url = src.startsWith("http") ? src : `${API_BASE}${src}`;
              const alt = caption || `Figure ${index + 1}`;
              return (
                <button
                  key={`${src}-${index}`}
                  type="button"
                  className="reader-figure-image-button"
                  aria-label="放大图片"
                  onClick={(event) => {
                    event.stopPropagation();
                    setPreview({ src: url, alt });
                  }}
                >
                  <img
                    src={url}
                    alt={alt}
                    className="reader-figure-img"
                    loading="lazy"
                  />
                </button>
              );
            })}
          </div>
        )}
        {caption && (
          <figcaption className="reader-figure-caption">
            <RichText text={caption} />
          </figcaption>
        )}
      </figure>

      {preview && (
        <div
          className="reader-image-preview"
          role="dialog"
          aria-modal="true"
          onClick={() => setPreview(null)}
        >
          <button
            type="button"
            className="reader-image-preview-close"
            aria-label="关闭图片预览"
            onClick={(event) => {
              event.stopPropagation();
              setPreview(null);
            }}
          >
            关闭
          </button>
          <img
            src={preview.src}
            alt={preview.alt}
            className="reader-image-preview-img"
            onClick={(event) => event.stopPropagation()}
          />
        </div>
      )}
    </>
  );
}
