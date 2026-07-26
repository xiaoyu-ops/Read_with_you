"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { createLocalFilePaper, createMinerUPaper } from "@/lib/api";

const LANGUAGES = ["en", "ch", "latin", "japan", "korean"];
type ImportMode = "url" | "file";

export function MinerUDocumentImport() {
  const router = useRouter();
  const [mode, setMode] = useState<ImportMode>("url");
  const [url, setUrl] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [title, setTitle] = useState("");
  const [pageRange, setPageRange] = useState("");
  const [language, setLanguage] = useState("en");
  const [parsing, setParsing] = useState(false);
  const [message, setMessage] = useState<{ type: "ok" | "err"; text: string } | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (mode === "url" && !url.trim()) return;
    if (mode === "file" && !file) return;

    setParsing(true);
    setMessage(null);
    try {
      let paper;
      if (mode === "url") {
        paper = await createMinerUPaper({
          url: url.trim(),
          title: title.trim() || undefined,
          page_range: pageRange.trim() || undefined,
          language,
        });
      } else {
        if (!file) return;
        paper = await createLocalFilePaper({
          file,
          title: title.trim() || undefined,
        });
      }
      setMessage({ type: "ok", text: "解析完成，正在进入阅读页" });
      router.push(`/paper/${paper.arxiv_id}`);
    } catch (err) {
      setMessage({ type: "err", text: (err as Error).message });
    } finally {
      setParsing(false);
    }
  };

  return (
    <section className="mt-5 rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--muted))]/35 p-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-sm font-medium tracking-tight">文档导入</h2>
          <p className="mt-1 text-xs font-mono text-[hsl(var(--muted-foreground))]">
            导入 PDF 文档，完成后进入同一阅读页
          </p>
        </div>
        <div className="flex rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--background))] p-0.5 text-xs font-mono">
          {(["url", "file"] as const).map((item) => (
            <button
              key={item}
              type="button"
              aria-pressed={mode === item}
              onClick={() => {
                setMode(item);
                setMessage(null);
              }}
              className={`rounded px-2.5 py-1.5 transition-colors ${
                mode === item
                  ? "bg-[hsl(var(--foreground))] text-[hsl(var(--background))]"
                  : "text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))]"
              }`}
            >
              {item === "url" ? "PDF URL" : "本地文件"}
            </button>
          ))}
        </div>
      </div>

      <form onSubmit={submit} className="space-y-3">
        {mode === "url" ? (
          <div className="flex flex-col gap-2 sm:flex-row">
            <input
              type="url"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="https://example.com/paper.pdf"
              className="min-w-0 flex-1 rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--background))] px-3 py-2 text-sm font-mono text-[hsl(var(--foreground))] placeholder:text-[hsl(var(--muted-foreground))] focus:outline-none focus:ring-1 focus:ring-[hsl(var(--foreground))]"
            />
            <button
              type="submit"
              disabled={parsing || !url.trim()}
              className="rounded-md bg-[hsl(var(--primary))] px-4 py-2 text-sm font-mono text-[hsl(var(--primary-foreground))] transition-opacity hover:opacity-90 disabled:opacity-40"
            >
              {parsing ? "解析中…" : "解析 PDF"}
            </button>
          </div>
        ) : (
          <div className="flex flex-col gap-2 sm:flex-row">
            <input
              id="local-pdf-upload"
              type="file"
              accept="application/pdf,.pdf"
              className="sr-only"
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            />
            <label
              htmlFor="local-pdf-upload"
              className="flex min-w-0 flex-1 cursor-pointer items-center rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--background))] px-3 py-2 text-sm font-mono text-[hsl(var(--foreground))] hover:border-[hsl(var(--foreground))]"
            >
              <span className="truncate">
                {file ? file.name : "选择本地 PDF 文件"}
              </span>
            </label>
            <button
              type="submit"
              disabled={parsing || !file}
              className="rounded-md bg-[hsl(var(--primary))] px-4 py-2 text-sm font-mono text-[hsl(var(--primary-foreground))] transition-opacity hover:opacity-90 disabled:opacity-40"
            >
              {parsing ? "解析中…" : "上传 PDF"}
            </button>
          </div>
        )}

        <div className={`grid grid-cols-1 gap-2 ${mode === "url" ? "sm:grid-cols-[minmax(0,1fr)_7rem_7rem]" : ""}`}>
          <input
            type="text"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="标题，可选"
            className="min-w-0 rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--background))] px-3 py-2 text-sm text-[hsl(var(--foreground))] placeholder:text-[hsl(var(--muted-foreground))] focus:outline-none focus:ring-1 focus:ring-[hsl(var(--foreground))]"
          />
          {mode === "url" && (
            <>
              <input
                type="text"
                value={pageRange}
                onChange={(e) => setPageRange(e.target.value)}
                placeholder="页码 1-10"
                className="min-w-0 rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--background))] px-3 py-2 text-sm font-mono text-[hsl(var(--foreground))] placeholder:text-[hsl(var(--muted-foreground))] focus:outline-none focus:ring-1 focus:ring-[hsl(var(--foreground))]"
              />
              <select
                value={language}
                onChange={(e) => setLanguage(e.target.value)}
                className="min-w-0 rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--background))] px-3 py-2 text-sm font-mono text-[hsl(var(--foreground))] focus:outline-none focus:ring-1 focus:ring-[hsl(var(--foreground))]"
              >
                {LANGUAGES.map((item) => (
                  <option key={item} value={item}>
                    {item}
                  </option>
                ))}
              </select>
            </>
          )}
        </div>
      </form>

      {message && (
        <p
          className={`mt-3 break-words text-xs font-mono ${
            message.type === "ok" ? "text-green-600" : "text-red-500"
          }`}
        >
          {message.text}
        </p>
      )}
    </section>
  );
}
