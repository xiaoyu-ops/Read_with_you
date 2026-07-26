"use client";

import { useEffect, useState } from "react";
import { Header } from "@/components/Header";
import { FadeUp } from "@/components/FadeUp";
import {
  createCollection,
  listCollections,
  type CollectionSummary,
} from "@/lib/api";

export default function LibraryPage() {
  const [collections, setCollections] = useState<CollectionSummary[]>([]);
  const [name, setName] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = async () => {
    const data = await listCollections();
    setCollections(data);
  };

  useEffect(() => {
    refresh()
      .catch((e) => setError((e as Error).message))
      .finally(() => setLoading(false));
  }, []);

  const handleCreate = async () => {
    const cleanName = name.trim();
    if (!cleanName) return;
    setSaving(true);
    setError(null);
    try {
      await createCollection(cleanName);
      setName("");
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <>
      <Header />
      <main className="max-w-3xl mx-auto px-4 pt-24 md:pt-28 pb-16">
        <FadeUp>
          <div className="flex items-center gap-3 mb-6">
            <div className="decorate-bar h-8" />
            <h1 className="text-2xl font-medium tracking-tight">文献库</h1>
          </div>
          <div className="mb-8 flex gap-2">
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") handleCreate();
              }}
              placeholder="新建专题，例如 Long Context"
              className="min-w-0 flex-1 rounded-md border border-[hsl(var(--border))] bg-transparent px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-[hsl(var(--foreground))]"
            />
            <button
              onClick={handleCreate}
              disabled={saving || !name.trim()}
              className="rounded-md bg-[hsl(var(--primary))] px-4 py-2 text-sm font-mono text-[hsl(var(--primary-foreground))] hover:opacity-90 disabled:opacity-40"
            >
              {saving ? "创建中…" : "新建"}
            </button>
          </div>
        </FadeUp>

        <FadeUp delay={1}>
          {loading && (
            <p className="text-sm font-mono text-[hsl(var(--muted-foreground))] animate-pulse">
              读取文献库中…
            </p>
          )}
          {error && <p className="text-sm text-red-500 font-mono">{error}</p>}
          {!loading && collections.length === 0 && (
            <p className="text-sm text-[hsl(var(--muted-foreground))] leading-relaxed">
              还没有专题。阅读论文时可以直接加入文献库，也可以先在这里创建专题。
            </p>
          )}
          <div className="divide-y divide-[hsl(var(--border))]">
            {collections.map((collection) => (
              <a
                key={collection.id}
                href={`/library/${collection.id}`}
                className="group flex items-center justify-between py-4 transition-colors hover:text-[hsl(var(--foreground))]"
              >
                <div className="min-w-0">
                  <h2 className="truncate text-base font-medium tracking-tight">
                    {collection.name}
                  </h2>
                  <p className="mt-1 text-xs font-mono text-[hsl(var(--muted-foreground))]">
                    {collection.paper_count} 篇论文
                  </p>
                </div>
                <span className="ml-4 text-xs font-mono text-[hsl(var(--muted-foreground))] group-hover:text-[hsl(var(--foreground))]">
                  打开
                </span>
              </a>
            ))}
          </div>
        </FadeUp>
      </main>
    </>
  );
}
