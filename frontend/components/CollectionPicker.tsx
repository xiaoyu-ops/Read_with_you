"use client";

import { useEffect, useState } from "react";
import {
  addPaperToCollection,
  createCollection,
  listCollections,
  type CollectionSummary,
} from "@/lib/api";

export function CollectionPicker({
  arxivId,
  onBeforeAdd,
}: {
  arxivId: string;
  onBeforeAdd?: () => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [collections, setCollections] = useState<CollectionSummary[]>([]);
  const [newName, setNewName] = useState("");
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = async () => {
    const data = await listCollections(arxivId);
    setCollections(data);
  };

  useEffect(() => {
    if (!open) return;
    setLoading(true);
    setError(null);
    refresh()
      .catch((e) => setError((e as Error).message))
      .finally(() => setLoading(false));
  }, [open]);

  const addToExisting = async (collection: CollectionSummary) => {
    setLoading(true);
    setError(null);
    setMessage(null);
    try {
      await onBeforeAdd?.();
      await addPaperToCollection(collection.id, arxivId);
      await refresh();
      setMessage(`已加入「${collection.name}」`);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  };

  const createAndAdd = async () => {
    const name = newName.trim();
    if (!name) return;
    setLoading(true);
    setError(null);
    setMessage(null);
    try {
      const collection = await createCollection(name);
      await onBeforeAdd?.();
      await addPaperToCollection(collection.id, arxivId);
      setNewName("");
      await refresh();
      setMessage(`已创建并加入「${collection.name}」`);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="relative">
      <button
        onClick={() => setOpen((value) => !value)}
        className="rounded-md border border-[hsl(var(--border))] px-3 py-1 text-[hsl(var(--muted-foreground))] hover:bg-[hsl(var(--muted))] hover:text-[hsl(var(--foreground))] transition-colors disabled:opacity-40"
      >
        加入文献库
      </button>

      {open && (
        <div className="absolute right-0 z-40 mt-2 w-80 rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--background))] p-3 shadow-lg">
          <div className="mb-3 flex items-center justify-between">
            <p className="text-xs font-mono text-[hsl(var(--muted-foreground))]">
              选择专题
            </p>
            <a
              href="/library"
              className="text-xs font-mono text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))]"
            >
              管理
            </a>
          </div>

          <div className="max-h-52 space-y-1 overflow-y-auto">
            {loading && collections.length === 0 && (
              <p className="px-2 py-1.5 text-xs font-mono text-[hsl(var(--muted-foreground))]">
                读取中…
              </p>
            )}
            {!loading && collections.length === 0 && (
              <p className="px-2 py-1.5 text-xs font-mono text-[hsl(var(--muted-foreground))]">
                还没有专题
              </p>
            )}
            {collections.map((collection) => (
              <button
                key={collection.id}
                onClick={() => addToExisting(collection)}
                disabled={loading || collection.contains_paper}
                className="flex w-full items-center justify-between rounded-md px-2 py-1.5 text-left text-xs font-mono hover:bg-[hsl(var(--muted))] disabled:cursor-default disabled:opacity-60"
              >
                <span className="truncate">{collection.name}</span>
                <span className="ml-3 shrink-0 text-[hsl(var(--muted-foreground))]">
                  {collection.contains_paper ? "已加入" : `${collection.paper_count} 篇`}
                </span>
              </button>
            ))}
          </div>

          <div className="mt-3 flex gap-2 border-t border-[hsl(var(--border))] pt-3">
            <input
              type="text"
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") createAndAdd();
              }}
              placeholder="新专题名称"
              className="min-w-0 flex-1 rounded-md border border-[hsl(var(--border))] bg-transparent px-2 py-1.5 text-xs font-mono focus:outline-none focus:ring-1 focus:ring-[hsl(var(--foreground))]"
            />
            <button
              onClick={createAndAdd}
              disabled={loading || !newName.trim()}
              className="rounded-md bg-[hsl(var(--primary))] px-2.5 py-1.5 text-xs font-mono text-[hsl(var(--primary-foreground))] hover:opacity-90 disabled:opacity-40"
            >
              创建
            </button>
          </div>

          {message && (
            <p className="mt-2 text-xs font-mono text-green-600">{message}</p>
          )}
          {error && <p className="mt-2 text-xs font-mono text-red-500">{error}</p>}
        </div>
      )}
    </div>
  );
}
