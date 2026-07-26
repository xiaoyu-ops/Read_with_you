"use client";

import { useCallback, useEffect, useState } from "react";

import { readCurrentReading } from "@/lib/currentReading";
import {
  addPaperToCollection,
  createCollection,
  getCollection,
  listCollections,
  listPapers,
  type PaperMeta,
} from "@/lib/api";
import {
  PortableRevisionConflictError,
  chooseLocalLibraryDirectory,
  getLocalLibraryState,
  restorePaperFromLocal,
  restoreWorkspaceFromLocal,
  syncPaperToLocal,
  syncWorkspaceToLocal,
  supportsLocalPaperLibrary,
  useServerLibrary,
  type LocalLibraryState,
  type LocalWorkspaceCollection,
} from "@/lib/localPaperLibrary";

type Conflict = {
  paperId: string;
  message: string;
};

const EMPTY_STATE: LocalLibraryState = {
  supported: false,
  mode: "server",
  directoryName: null,
  permission: "unavailable",
};

const PRIMARY_ACTION_CLASS =
  "inline-flex min-h-11 items-center justify-center rounded-md bg-[hsl(var(--reader-accent))] px-4 py-2 text-sm font-medium text-white transition-opacity hover:opacity-90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[hsl(var(--ring))] focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-45";

const SECONDARY_ACTION_CLASS =
  "inline-flex min-h-11 items-center justify-center rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--background))] px-4 py-2 text-sm font-medium text-[hsl(var(--foreground))] transition-[background-color,border-color] hover:border-[hsl(var(--foreground))]/25 hover:bg-[hsl(var(--muted))]/55 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[hsl(var(--ring))] focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-45";

export function LocalLibrarySettings() {
  const [state, setState] = useState<LocalLibraryState>(EMPTY_STATE);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [conflict, setConflict] = useState<Conflict | null>(null);
  const [migrationFailures, setMigrationFailures] = useState<
    { paperId: string; message: string }[]
  >([]);
  const [migrationPapers, setMigrationPapers] = useState<PaperMeta[]>([]);
  const [selectedPaperIds, setSelectedPaperIds] = useState<string[]>([]);
  const [loadingMigrationPapers, setLoadingMigrationPapers] = useState(false);

  const refresh = useCallback(async () => {
    if (!supportsLocalPaperLibrary()) {
      setState(EMPTY_STATE);
      return;
    }
    setState(await getLocalLibraryState());
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (state.mode !== "local_folder" || state.permission !== "granted") {
      setMigrationPapers([]);
      setSelectedPaperIds([]);
      setLoadingMigrationPapers(false);
      return;
    }
    let active = true;
    setLoadingMigrationPapers(true);
    void listPapers()
      .then((papers) => {
        if (!active) return;
        setMigrationPapers(papers);
        setSelectedPaperIds([]);
      })
      .catch((error) => {
        if (active) setMessage((error as Error).message);
      })
      .finally(() => {
        if (active) setLoadingMigrationPapers(false);
      });
    return () => {
      active = false;
    };
  }, [state.mode, state.permission]);

  if (!state.supported) return null;

  const chooseDirectory = async () => {
    setBusy(true);
    setMessage(null);
    try {
      const next = await chooseLocalLibraryDirectory();
      setState(next);
      setMessage("本地文献库已启用。目录权限只保存在这台浏览器中。");
    } catch (error) {
      const errorName = (error as { name?: string } | null)?.name;
      setMessage(
        errorName === "AbortError"
          ? "未选择文件夹，保存位置没有改变。"
          : (error as Error).message,
      );
    } finally {
      setBusy(false);
    }
  };

  const syncCurrent = async (forceFull = false) => {
    const current = readCurrentReading();
    if (!current) {
      setMessage("请先打开一篇论文，再同步到本地目录。");
      return;
    }
    setBusy(true);
    setMessage(null);
    setConflict(null);
    try {
      await syncPaperToLocal(current.arxiv_id, { forceFull });
      setMessage(`《${current.title}》已完整写入并通过哈希校验。`);
    } catch (error) {
      if (error instanceof PortableRevisionConflictError) {
        setConflict({ paperId: current.arxiv_id, message: error.message });
      } else {
        setMessage((error as Error).message);
      }
    } finally {
      setBusy(false);
    }
  };

  const restoreCurrent = async (conflictPolicy: "reject" | "keep_local") => {
    const current = readCurrentReading();
    const paperId = conflict?.paperId || current?.arxiv_id;
    if (!paperId) return;
    setBusy(true);
    setMessage(null);
    try {
      await restorePaperFromLocal(paperId, conflictPolicy);
      setMessage(
        conflictPolicy === "keep_local"
          ? "已保留本地版本，并恢复到服务端临时工作区。"
          : "已从本地目录恢复到服务端临时工作区。",
      );
      setConflict(null);
    } catch (error) {
      if (error instanceof PortableRevisionConflictError) {
        setConflict({ paperId, message: error.message });
      } else {
        setMessage((error as Error).message);
      }
    } finally {
      setBusy(false);
    }
  };

  const switchToServer = () => {
    useServerLibrary();
    setState((current) => ({
      ...current,
      mode: "server",
      permission: "prompt",
    }));
    setMigrationPapers([]);
    setSelectedPaperIds([]);
    setConflict(null);
    setMessage("已改回这台电脑的默认保存位置；所选文件夹中的已有数据不会被删除。");
  };

  const backupWorkspace = async () => {
    if (!selectedPaperIds.length) {
      setMessage("请先选择要保存到本地目录的论文。");
      return;
    }
    setBusy(true);
    setMessage("正在读取所选论文和专题…");
    setMigrationFailures([]);
    setConflict(null);
    try {
      const selected = new Set(selectedPaperIds);
      const summaries = await listCollections();
      const collections: LocalWorkspaceCollection[] = [];
      for (const summary of summaries) {
        const detail = await getCollection(summary.id);
        const paperIds = detail.papers
          .map((paper) => paper.arxiv_id)
          .filter((paperId) => selected.has(paperId));
        if (paperIds.length) {
          collections.push({
            name: detail.name,
            paper_ids: paperIds,
          });
        }
      }
      const result = await syncWorkspaceToLocal(
        selectedPaperIds,
        collections,
        (completed, total, paperId) => {
          setMessage(`正在保存 ${completed}/${total}：${paperId}`);
        },
      );
      setMigrationFailures(
        result.items
          .filter((item) => item.status === "error")
          .map((item) => ({
            paperId: item.paper_id,
            message: item.message || "保存失败",
          })),
      );
      setMessage(
        result.failed
          ? `已保存 ${result.completed} 篇，${result.failed} 篇失败；工作区清单没有发布，请处理后重试。`
          : `已将所选 ${result.completed} 篇论文和 ${collections.length} 个相关专题完整写入本地目录并校验。`,
      );
    } catch (error) {
      setMessage((error as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const restoreWorkspace = async () => {
    setBusy(true);
    setMessage("正在检查本地工作区…");
    setMigrationFailures([]);
    setConflict(null);
    try {
      const result = await restoreWorkspaceFromLocal(
        (completed, total, paperId) => {
          setMessage(`正在恢复 ${completed}/${total}：${paperId}`);
        },
      );
      const completedPaperIds = new Set(
        result.items
          .filter((item) => item.status === "done")
          .map((item) => item.paper_id),
      );
      const collectionFailures: { paperId: string; message: string }[] = [];
      if (result.workspace) {
        const existingCollections = await listCollections();
        const collectionByName = new Map(
          existingCollections.map((collection) => [collection.name, collection]),
        );
        for (const collection of result.workspace.collections) {
          try {
            let target = collectionByName.get(collection.name);
            if (!target) {
              target = await createCollection(collection.name);
              collectionByName.set(collection.name, target);
            }
            for (const paperId of collection.paper_ids) {
              if (completedPaperIds.has(paperId)) {
                await addPaperToCollection(target.id, paperId);
              }
            }
          } catch (error) {
            collectionFailures.push({
              paperId: `专题：${collection.name}`,
              message: (error as Error).message,
            });
          }
        }
      }
      setMigrationFailures(
        [
          ...result.items
            .filter((item) => item.status === "error")
            .map((item) => ({
              paperId: item.paper_id,
              message: item.message || "恢复失败",
            })),
          ...collectionFailures,
        ],
      );
      setMessage(
        result.failed || collectionFailures.length
          ? `已恢复 ${result.completed} 篇；${result.failed} 篇论文、${collectionFailures.length} 个专题失败，源目录保持不变。`
          : `已从本地目录恢复 ${result.completed} 篇论文${result.workspace ? "及其专题" : ""}。`,
      );
    } catch (error) {
      setMessage((error as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const localEnabled = state.mode === "local_folder";
  const permissionReady = state.permission === "granted";

  return (
    <section aria-label="论文保存位置">
      <div className="border-y border-[hsl(var(--border))]">
        <div className="grid gap-5 py-5 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center">
          <div>
            <h3 className="text-sm font-medium">论文保存位置</h3>
            <p className="mt-1.5 max-w-xl text-sm leading-6 text-[hsl(var(--muted-foreground))]">
              论文、翻译、笔记和对话默认保存在这台电脑。选择文件夹后，陪你读会把这些内容写入该目录，方便备份和迁移。
            </p>
          </div>
          <div className="min-w-0 sm:max-w-64 sm:text-right">
            <p className="text-xs font-medium text-[hsl(var(--muted-foreground))]">
              当前保存位置
            </p>
            <p className="mt-1 break-words text-sm font-medium">
              {localEnabled
                ? state.directoryName || "已选择的本地文件夹"
                : "这台电脑"}
            </p>
            <p className="mt-1 text-xs leading-5 text-[hsl(var(--muted-foreground))]">
              {localEnabled
                ? permissionReady
                  ? "已连接，API Key 不会写入。"
                  : "文件夹权限已失效，请重新选择后再继续同步。"
                : "当前数据不会发送到陪你读网站。"}
            </p>
          </div>
        </div>

        <div className="flex flex-wrap items-center justify-between gap-3 border-t border-[hsl(var(--border))] py-4">
          <p className="text-xs leading-5 text-[hsl(var(--muted-foreground))]">
            文件夹授权只保存在当前浏览器。
          </p>
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              className={PRIMARY_ACTION_CLASS}
              disabled={busy}
              onClick={() => void chooseDirectory()}
            >
              {localEnabled ? "更换文件夹" : "选择本地文件夹"}
            </button>
          </div>
        </div>
      </div>

      {localEnabled && permissionReady && (
        <div className="mt-8 divide-y divide-[hsl(var(--border))] border-y border-[hsl(var(--border))]">
          <div className="grid gap-4 py-5 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center">
            <div>
              <h3 className="text-sm font-medium">当前论文</h3>
              <p className="mt-1 text-xs leading-5 text-[hsl(var(--muted-foreground))]">
                将正在阅读的论文写入文件夹，或从文件夹恢复。
              </p>
            </div>
            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                className={SECONDARY_ACTION_CLASS}
                disabled={busy}
                onClick={() => void syncCurrent()}
              >
                同步当前论文
              </button>
              <button
                type="button"
                className={SECONDARY_ACTION_CLASS}
                disabled={busy}
                onClick={() => void restoreCurrent("reject")}
              >
                恢复当前论文
              </button>
            </div>
          </div>
          <div className="py-5">
            <div className="grid gap-4 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center">
              <div>
                <h3 className="text-sm font-medium">整个论文工作区</h3>
                <p className="mt-1 max-w-xl text-xs leading-5 text-[hsl(var(--muted-foreground))]">
                  只保存你勾选的论文，以及与这些论文有关的专题；每个文件都会校验。
                </p>
              </div>
              <button
                type="button"
                className={SECONDARY_ACTION_CLASS}
                disabled={busy}
                onClick={() => void restoreWorkspace()}
              >
                从此目录恢复
              </button>
            </div>

            <div className="mt-4 rounded-lg border border-[hsl(var(--border))]">
              <div className="flex flex-wrap items-center justify-between gap-3 border-b border-[hsl(var(--border))] px-3 py-2.5">
                <p className="text-xs font-medium">
                  选择论文
                  <span className="ml-2 font-normal text-[hsl(var(--muted-foreground))]">
                    已选 {selectedPaperIds.length}/{migrationPapers.length}
                  </span>
                </p>
                <div className="flex items-center gap-3 text-xs">
                  <button
                    type="button"
                    className="underline-offset-4 hover:underline disabled:cursor-not-allowed disabled:opacity-45"
                    disabled={busy || loadingMigrationPapers || !migrationPapers.length}
                    onClick={() =>
                      setSelectedPaperIds(
                        migrationPapers.map((paper) => paper.arxiv_id),
                      )
                    }
                  >
                    全选
                  </button>
                  <button
                    type="button"
                    className="underline-offset-4 hover:underline disabled:cursor-not-allowed disabled:opacity-45"
                    disabled={busy || !selectedPaperIds.length}
                    onClick={() => setSelectedPaperIds([])}
                  >
                    清空
                  </button>
                </div>
              </div>
              <div className="max-h-64 overflow-y-auto">
                {loadingMigrationPapers ? (
                  <p className="px-3 py-4 text-xs text-[hsl(var(--muted-foreground))]">
                    正在读取论文列表…
                  </p>
                ) : migrationPapers.length ? (
                  migrationPapers.map((paper) => {
                    const checked = selectedPaperIds.includes(paper.arxiv_id);
                    return (
                      <label
                        key={paper.arxiv_id}
                        className="flex cursor-pointer items-start gap-3 border-b border-[hsl(var(--border))]/70 px-3 py-3 last:border-b-0 hover:bg-[hsl(var(--muted))]/35"
                      >
                        <input
                          type="checkbox"
                          className="mt-0.5 size-4 accent-[hsl(var(--reader-accent))]"
                          checked={checked}
                          disabled={busy}
                          onChange={() =>
                            setSelectedPaperIds((current) =>
                              checked
                                ? current.filter((paperId) => paperId !== paper.arxiv_id)
                                : [...current, paper.arxiv_id],
                            )
                          }
                        />
                        <span className="min-w-0">
                          <span className="block text-sm leading-5">{paper.title}</span>
                          <span className="mt-0.5 block font-mono text-[11px] text-[hsl(var(--muted-foreground))]">
                            {paper.arxiv_id}
                          </span>
                        </span>
                      </label>
                    );
                  })
                ) : (
                  <p className="px-3 py-4 text-xs text-[hsl(var(--muted-foreground))]">
                    当前没有可迁移的论文。
                  </p>
                )}
              </div>
            </div>

            <div className="mt-3 flex justify-end">
              <button
                type="button"
                className={PRIMARY_ACTION_CLASS}
                disabled={busy || loadingMigrationPapers || !selectedPaperIds.length}
                onClick={() => void backupWorkspace()}
              >
                保存所选论文到此目录
              </button>
            </div>
          </div>
          <div className="flex flex-wrap items-center justify-between gap-3 py-4">
            <p className="text-xs leading-5 text-[hsl(var(--muted-foreground))]">
              文件夹中的已有数据不会因切换保存位置而删除。
            </p>
            <button
              type="button"
              className={SECONDARY_ACTION_CLASS}
              disabled={busy}
              onClick={switchToServer}
            >
              使用默认保存位置
            </button>
          </div>
        </div>
      )}

      {conflict && (
        <div className="mt-4 rounded-xl border border-amber-300/70 bg-amber-50/70 p-4 text-sm text-amber-950 dark:border-amber-800 dark:bg-amber-950/30 dark:text-amber-100">
          <p>{conflict.message}</p>
          <div className="mt-3 flex flex-wrap gap-2">
            <button
              type="button"
              className={PRIMARY_ACTION_CLASS}
              disabled={busy}
              onClick={() => void restoreCurrent("keep_local")}
            >
              保留本地
            </button>
            <button
              type="button"
              className={SECONDARY_ACTION_CLASS}
              disabled={busy}
              onClick={() => void syncCurrent(true)}
            >
              保留服务端
            </button>
          </div>
        </div>
      )}

      {message && (
        <p className="mt-3 text-xs leading-5 text-[hsl(var(--muted-foreground))]">
          {message}
        </p>
      )}
      {migrationFailures.length > 0 && (
        <details className="mt-3 text-xs text-[hsl(var(--muted-foreground))]">
          <summary className="cursor-pointer">查看 {migrationFailures.length} 个失败项</summary>
          <ul className="mt-2 space-y-1 pl-5">
            {migrationFailures.map((failure) => (
              <li key={failure.paperId}>
                <span className="font-mono">{failure.paperId}</span>：{failure.message}
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}
