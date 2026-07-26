"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import {
  getLocalLibraryState,
  syncPaperToLocal,
  type LocalLibraryState,
} from "@/lib/localPaperLibrary";
import { PAPER_DATA_CHANGED_EVENT } from "@/lib/paperDataEvents";
import { usePortableCacheLease } from "@/lib/usePortableCacheLease";

type SyncStatus = "idle" | "saving" | "saved" | "permission" | "error";

export function LocalPaperSyncStatus({ paperId }: { paperId: string }) {
  const [library, setLibrary] = useState<LocalLibraryState | null>(null);
  const [status, setStatus] = useState<SyncStatus>("idle");
  const [message, setMessage] = useState("");
  const timerRef = useRef<number | null>(null);
  usePortableCacheLease(paperId);

  const sync = useCallback(async () => {
    setStatus("saving");
    setMessage("正在保存到本地…");
    try {
      await syncPaperToLocal(paperId);
      setStatus("saved");
      setMessage("已保存到本地");
    } catch (error) {
      const text = (error as Error).message;
      setStatus(text.includes("权限") ? "permission" : "error");
      setMessage(text);
    }
  }, [paperId]);

  useEffect(() => {
    let cancelled = false;
    void getLocalLibraryState().then((next) => {
      if (cancelled) return;
      setLibrary(next);
      if (next.mode === "local_folder" && next.permission === "granted") {
        void sync();
      }
    });
    return () => {
      cancelled = true;
    };
  }, [sync]);

  useEffect(() => {
    if (library?.mode !== "local_folder" || library.permission !== "granted") return;
    const onChanged = (event: Event) => {
      const changedPaperId = (event as CustomEvent<{ paperId?: string }>).detail?.paperId;
      if (changedPaperId !== paperId) return;
      if (timerRef.current !== null) window.clearTimeout(timerRef.current);
      timerRef.current = window.setTimeout(() => void sync(), 800);
    };
    window.addEventListener(PAPER_DATA_CHANGED_EVENT, onChanged);
    return () => {
      window.removeEventListener(PAPER_DATA_CHANGED_EVENT, onChanged);
      if (timerRef.current !== null) window.clearTimeout(timerRef.current);
    };
  }, [library, paperId, sync]);

  if (!library || library.mode !== "local_folder") return null;

  return (
    <div className="mb-3 flex min-h-6 items-center justify-end gap-2 text-xs text-[hsl(var(--muted-foreground))]">
      <span>{message}</span>
      {(status === "error" || status === "permission") && (
        <button
          type="button"
          className="underline underline-offset-4"
          onClick={() => void sync()}
        >
          重试
        </button>
      )}
    </div>
  );
}
