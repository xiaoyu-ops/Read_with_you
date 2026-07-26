"use client";

import { useEffect, useMemo, useState } from "react";
import { Header } from "@/components/Header";
import { FadeUp } from "@/components/FadeUp";
import {
  clearAgentChat,
  applyAgentSkillProposal,
  createAgentMemory,
  deleteAgentMemory,
  listAgentChats,
  listAgentMemories,
  listAgentSkillProposals,
  listAgentTasks,
  listPapers,
  searchAgentSessions,
  rejectAgentSkillProposal,
  updateAgentMemory,
  type AgentChatSummary,
  type AgentMemoryItem,
  type AgentSessionSearchResult,
  type AgentSkillProposalItem,
  type AgentTask,
  type PaperMeta,
} from "@/lib/api";

const STATUS_LABELS: Record<string, string> = {
  running: "运行中",
  done: "完成",
  error: "失败",
  cancelled: "已取消",
};

const TASK_LABELS: Record<string, string> = {
  selection_explanation: "选区解释",
  external_tool_request: "外部工具请求",
  four_agent_analysis: "Agent 深度分析",
  collection_cross_review: "专题横向整理",
  reproducibility_deep_dive: "可复现性深挖",
  method_explanation: "方法拆解",
  annotation_questions: "标注问题整理",
  collection_compare: "专题横向比较",
};

function formatTime(value?: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function statusClass(status: string): string {
  if (status === "done") return "text-green-600";
  if (status === "error") return "text-red-500";
  if (status === "cancelled") return "text-[hsl(var(--muted-foreground))]";
  return "text-[hsl(var(--foreground))]";
}

function roleLabel(role: string): string {
  return role === "user" ? "你" : "Pet";
}

function displayTaskSummary(summary: string): string {
  return summary
    .replace(/四\s*Agent\s*深度分析/g, "Agent 深度分析")
    .replace(/四\s*Agent\s*分析/g, "Agent 分析");
}

const MEMORY_KIND_LABELS: Record<string, string> = {
  preference: "偏好",
  correction: "纠正",
  criterion: "判断标准",
};

export function AgentManagementPage() {
  const [chats, setChats] = useState<AgentChatSummary[]>([]);
  const [papers, setPapers] = useState<PaperMeta[]>([]);
  const [tasks, setTasks] = useState<AgentTask[]>([]);
  const [memories, setMemories] = useState<AgentMemoryItem[]>([]);
  const [skillProposals, setSkillProposals] = useState<AgentSkillProposalItem[]>([]);
  const [skillProposalError, setSkillProposalError] = useState<string | null>(null);
  const [skillProposalBusy, setSkillProposalBusy] = useState<string | null>(null);
  const [sessionQuery, setSessionQuery] = useState("");
  const [debouncedSessionQuery, setDebouncedSessionQuery] = useState("");
  const [sessionResults, setSessionResults] = useState<AgentSessionSearchResult[]>([]);
  const [sessionSearching, setSessionSearching] = useState(false);
  const [sessionError, setSessionError] = useState<string | null>(null);
  const [sessionNotice, setSessionNotice] = useState<string | null>(null);
  const [pendingClearChatId, setPendingClearChatId] = useState<string | null>(null);
  const [clearingChatId, setClearingChatId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [memoryError, setMemoryError] = useState<string | null>(null);
  const [memoryNotice, setMemoryNotice] = useState<string | null>(null);
  const [memoryInput, setMemoryInput] = useState("");
  const [memoryKind, setMemoryKind] = useState("preference");
  const [memoryFilter, setMemoryFilter] = useState("");
  const [editingMemoryId, setEditingMemoryId] = useState<string | null>(null);
  const [editingMemoryContent, setEditingMemoryContent] = useState("");
  const [pendingDeleteMemoryId, setPendingDeleteMemoryId] = useState<string | null>(null);
  const [memoryBusy, setMemoryBusy] = useState(false);

  useEffect(() => {
    Promise.all([listAgentChats(), listPapers(), listAgentTasks(), listAgentMemories(), listAgentSkillProposals()])
      .then(([chatData, paperData, taskData, memoryData, proposalData]) => {
        setChats(chatData);
        setPapers(paperData);
        setTasks(taskData);
        setMemories(memoryData);
        setSkillProposals(proposalData);
      })
      .catch((e) => setError((e as Error).message))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setDebouncedSessionQuery(sessionQuery.trim());
    }, 250);
    return () => window.clearTimeout(timer);
  }, [sessionQuery]);

  useEffect(() => {
    if (!debouncedSessionQuery) {
      setSessionResults([]);
      setSessionSearching(false);
      setSessionError(null);
      return;
    }
    let cancelled = false;
    setSessionSearching(true);
    setSessionError(null);
    searchAgentSessions(debouncedSessionQuery)
      .then((results) => {
        if (!cancelled) setSessionResults(results);
      })
      .catch((e) => {
        if (!cancelled) setSessionError((e as Error).message);
      })
      .finally(() => {
        if (!cancelled) setSessionSearching(false);
      });
    return () => {
      cancelled = true;
    };
  }, [debouncedSessionQuery]);

  const chatIds = useMemo(() => new Set(chats.map((chat) => chat.arxiv_id)), [chats]);
  const papersWithoutChat = papers.filter((paper) => !chatIds.has(paper.arxiv_id)).slice(0, 6);
  const filteredMemories = useMemo(() => {
    const query = memoryFilter.trim().toLocaleLowerCase();
    return [...memories]
      .reverse()
      .filter((memory) => {
        if (!query) return true;
        return [memory.content, memory.kind, memory.source, memory.arxiv_id || ""]
          .join(" ")
          .toLocaleLowerCase()
          .includes(query);
      });
  }, [memories, memoryFilter]);

  const addMemory = async () => {
    const content = memoryInput.trim();
    if (!content) return;
    setMemoryBusy(true);
    setMemoryError(null);
    setMemoryNotice(null);
    try {
      const saved = await createAgentMemory(content, memoryKind);
      setMemories((current) => [...current.filter((item) => item.id !== saved.id), saved]);
      setMemoryInput("");
      setMemoryNotice("已保存阅读记忆");
    } catch (e) {
      setMemoryError((e as Error).message);
    } finally {
      setMemoryBusy(false);
    }
  };

  const saveMemoryEdit = async (memoryId: string) => {
    const content = editingMemoryContent.trim();
    if (!content) return;
    setMemoryBusy(true);
    setMemoryError(null);
    setMemoryNotice(null);
    try {
      const saved = await updateAgentMemory(memoryId, { content });
      setMemories((current) => [...current.filter((item) => item.id !== memoryId), saved]);
      setEditingMemoryId(null);
      setEditingMemoryContent("");
      setMemoryNotice("已更新阅读记忆");
    } catch (e) {
      setMemoryError((e as Error).message);
    } finally {
      setMemoryBusy(false);
    }
  };

  const resolveSkillProposal = async (proposalId: string, approved: boolean) => {
    setSkillProposalBusy(proposalId);
    setSkillProposalError(null);
    try {
      const updated = approved
        ? await applyAgentSkillProposal(proposalId)
        : await rejectAgentSkillProposal(proposalId);
      setSkillProposals((current) => current.map((item) => item.id === proposalId ? updated : item));
    } catch (e) {
      setSkillProposalError((e as Error).message);
    } finally {
      setSkillProposalBusy(null);
    }
  };

  const removeMemory = async (memoryId: string) => {
    setMemoryBusy(true);
    setMemoryError(null);
    setMemoryNotice(null);
    try {
      await deleteAgentMemory(memoryId);
      setMemories((current) => current.filter((item) => item.id !== memoryId));
      setPendingDeleteMemoryId(null);
      setMemoryNotice("已删除阅读记忆");
    } catch (e) {
      setMemoryError((e as Error).message);
    } finally {
      setMemoryBusy(false);
    }
  };

  const removeChatHistory = async (arxivId: string) => {
    setClearingChatId(arxivId);
    setSessionError(null);
    setSessionNotice(null);
    try {
      await clearAgentChat(arxivId);
      setChats((current) => current.filter((chat) => chat.arxiv_id !== arxivId));
      setSessionResults((current) => current.filter((item) => item.arxiv_id !== arxivId));
      setPendingClearChatId(null);
      setSessionNotice("已清除这篇论文的对话记录");
    } catch (e) {
      setSessionError((e as Error).message);
    } finally {
      setClearingChatId(null);
    }
  };

  return (
    <>
      <Header />
      <main className="mx-auto max-w-5xl px-4 pb-16 pt-24 md:pt-28">
        <FadeUp>
          <p className="mb-2 text-xs font-mono tracking-[0.08em] text-[hsl(var(--muted-foreground))]">
            Agent 治理
          </p>
          <h1 className="text-2xl font-medium tracking-tight">记忆、Skill 与执行历史</h1>
          <p className="mb-8 max-w-2xl text-sm leading-relaxed text-[hsl(var(--muted-foreground))]">
            这里管理长期阅读偏好、待审核能力提案和完整运行记录；论文研究本身回到对应工作台继续。
          </p>
        </FadeUp>

        <FadeUp delay={1}>
          {loading && (
            <p className="animate-pulse text-sm font-mono text-[hsl(var(--muted-foreground))]">
              读取 Agent 工作区中…
            </p>
          )}
          {error && <p className="text-sm font-mono text-red-500">{error}</p>}
        </FadeUp>

        {!loading && !error && (
          <div className="space-y-10">
            <FadeUp delay={1}>
              <section>
                <div className="mb-4 flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
                  <div>
                    <h2 className="text-lg font-medium tracking-tight">最近对话</h2>
                    <p className="mt-1 text-xs font-mono text-[hsl(var(--muted-foreground))]">
                      继续上一次聊天，或搜索以前讨论过的内容
                    </p>
                  </div>
                  <input
                    value={sessionQuery}
                    onChange={(event) => {
                      setSessionQuery(event.target.value);
                      setSessionError(null);
                      setSessionNotice(null);
                    }}
                    placeholder="搜索历史讨论"
                    aria-label="搜索历史讨论"
                    className="w-full rounded-md border border-[hsl(var(--border))] bg-transparent px-3 py-2 text-sm outline-none focus-visible:border-[hsl(var(--foreground))] focus-visible:ring-1 focus-visible:ring-[hsl(var(--foreground))] sm:max-w-sm"
                  />
                </div>

                <div aria-live="polite" className="mb-2 min-h-5 text-xs font-mono">
                  {sessionError && <span className="text-red-500">{sessionError}</span>}
                  {!sessionError && sessionNotice && (
                    <span className="text-[hsl(var(--muted-foreground))]">{sessionNotice}</span>
                  )}
                </div>

                {sessionQuery.trim() ? (
                  sessionSearching || sessionQuery.trim() !== debouncedSessionQuery ? (
                    <p className="border-y border-[hsl(var(--border))] py-5 text-sm text-[hsl(var(--muted-foreground))]">
                      正在搜索历史讨论…
                    </p>
                  ) : sessionError ? null : sessionResults.length === 0 ? (
                    <p className="border-y border-[hsl(var(--border))] py-5 text-sm leading-relaxed text-[hsl(var(--muted-foreground))]">
                      没有找到匹配的讨论。试试论文名、术语或你记得的一小段表达。
                    </p>
                  ) : (
                    <div className="divide-y divide-[hsl(var(--border))] border-y border-[hsl(var(--border))]">
                      {sessionResults.map((result, resultIndex) => (
                        <div
                          key={`${result.arxiv_id}-${result.message_id}`}
                          className="border-l-2 border-[hsl(var(--foreground))]/30 py-4 pl-4"
                        >
                          {result.paper_exists ? (
                            <a
                              href={`/paper/${result.arxiv_id}`}
                              aria-label={`打开 ${result.paper_title} 的历史讨论`}
                              className="block hover:text-[hsl(var(--foreground))]"
                            >
                              <p className="truncate text-sm font-medium tracking-tight">
                                {result.paper_title}
                              </p>
                              <p className="mt-1 line-clamp-3 text-sm leading-relaxed text-[hsl(var(--muted-foreground))]">
                                {roleLabel(result.role)}：{result.snippet}
                              </p>
                              <p className="mt-2 text-xs font-mono text-[hsl(var(--muted-foreground))]">
                                {result.arxiv_id} · {formatTime(result.created_at)}
                              </p>
                            </a>
                          ) : (
                            <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                              <div className="min-w-0">
                                <div className="flex flex-wrap items-center gap-2">
                                  <p className="truncate text-sm font-medium tracking-tight">
                                    {result.paper_title || result.arxiv_id}
                                  </p>
                                  <span className="text-xs font-mono text-[hsl(var(--muted-foreground))]">
                                    论文已删除
                                  </span>
                                </div>
                                <p className="mt-1 line-clamp-3 text-sm leading-relaxed text-[hsl(var(--muted-foreground))]">
                                  {roleLabel(result.role)}：{result.snippet}
                                </p>
                                <p className="mt-2 text-xs font-mono text-[hsl(var(--muted-foreground))]">
                                  {result.arxiv_id} · {formatTime(result.created_at)}
                                </p>
                              </div>
                              {sessionResults.findIndex(
                                (item) => item.arxiv_id === result.arxiv_id,
                              ) === resultIndex && (
                              <div className="flex shrink-0 items-center gap-2">
                                <button
                                  type="button"
                                  onClick={() => {
                                    if (pendingClearChatId === result.arxiv_id) {
                                      void removeChatHistory(result.arxiv_id);
                                    } else {
                                      setPendingClearChatId(result.arxiv_id);
                                    }
                                  }}
                                  disabled={clearingChatId === result.arxiv_id}
                                  className="rounded-md border border-[hsl(var(--border))] px-3 py-2 text-xs font-mono text-red-500 hover:bg-red-500/10 focus-visible:ring-1 focus-visible:ring-red-500 disabled:opacity-40"
                                >
                                  {pendingClearChatId === result.arxiv_id ? "确认清除" : "清除记录"}
                                </button>
                                {pendingClearChatId === result.arxiv_id && (
                                  <button
                                    type="button"
                                    onClick={() => setPendingClearChatId(null)}
                                    className="rounded-md px-2 py-2 text-xs font-mono text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] focus-visible:ring-1 focus-visible:ring-[hsl(var(--foreground))]"
                                  >
                                    取消
                                  </button>
                                )}
                              </div>
                              )}
                            </div>
                          )}
                        </div>
                      ))}
                    </div>
                  )
                ) : chats.length === 0 ? (
                  <p className="border-y border-[hsl(var(--border))] py-5 text-sm leading-relaxed text-[hsl(var(--muted-foreground))]">
                    暂无 Pet 对话。打开任意论文后点击阅读 Pet，就会在这里留下会话入口。
                  </p>
                ) : (
                  <div className="divide-y divide-[hsl(var(--border))] border-y border-[hsl(var(--border))]">
                    {chats.map((chat) =>
                      chat.paper_exists ? (
                        <a
                          key={chat.arxiv_id}
                          href={`/paper/${chat.arxiv_id}`}
                          className="block py-4 hover:bg-[hsl(var(--muted))]/45"
                        >
                          <div className="flex flex-col gap-2 md:flex-row md:items-start md:justify-between">
                            <div className="min-w-0">
                              <p className="truncate text-sm font-medium tracking-tight">
                                {chat.paper_title || chat.arxiv_id}
                              </p>
                              <p className="mt-1 line-clamp-2 text-sm leading-relaxed text-[hsl(var(--muted-foreground))]">
                                {roleLabel(chat.last_role)}：{chat.last_message}
                              </p>
                            </div>
                            <div className="flex shrink-0 items-center gap-3 text-xs font-mono text-[hsl(var(--muted-foreground))]">
                              <span>{chat.message_count} 条</span>
                              <span>{formatTime(chat.updated_at)}</span>
                            </div>
                          </div>
                        </a>
                      ) : (
                        <div
                          key={chat.arxiv_id}
                          className="flex flex-col gap-3 py-4 sm:flex-row sm:items-start sm:justify-between"
                        >
                          <div className="min-w-0">
                            <div className="flex flex-wrap items-center gap-2">
                              <p className="truncate text-sm font-medium tracking-tight">
                                {chat.paper_title || chat.arxiv_id}
                              </p>
                              <span className="text-xs font-mono text-[hsl(var(--muted-foreground))]">
                                论文已删除
                              </span>
                            </div>
                            <p className="mt-1 line-clamp-2 text-sm leading-relaxed text-[hsl(var(--muted-foreground))]">
                              {roleLabel(chat.last_role)}：{chat.last_message}
                            </p>
                            <p className="mt-2 text-xs font-mono text-[hsl(var(--muted-foreground))]">
                              {chat.message_count} 条 · {formatTime(chat.updated_at)}
                            </p>
                          </div>
                          <div className="flex shrink-0 items-center gap-2">
                            <button
                              type="button"
                              onClick={() => {
                                if (pendingClearChatId === chat.arxiv_id) {
                                  void removeChatHistory(chat.arxiv_id);
                                } else {
                                  setPendingClearChatId(chat.arxiv_id);
                                }
                              }}
                              disabled={clearingChatId === chat.arxiv_id}
                              className="rounded-md border border-[hsl(var(--border))] px-3 py-2 text-xs font-mono text-red-500 hover:bg-red-500/10 focus-visible:ring-1 focus-visible:ring-red-500 disabled:opacity-40"
                            >
                              {pendingClearChatId === chat.arxiv_id ? "确认清除" : "清除记录"}
                            </button>
                            {pendingClearChatId === chat.arxiv_id && (
                              <button
                                type="button"
                                onClick={() => setPendingClearChatId(null)}
                                className="rounded-md px-2 py-2 text-xs font-mono text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] focus-visible:ring-1 focus-visible:ring-[hsl(var(--foreground))]"
                              >
                                取消
                              </button>
                            )}
                          </div>
                        </div>
                      ),
                    )}
                  </div>
                )}
              </section>
            </FadeUp>

            <FadeUp delay={2}>
              <section>
                <div className="mb-4 flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
                  <div>
                    <h2 className="text-lg font-medium tracking-tight">阅读记忆</h2>
                    <p className="mt-1 text-xs font-mono text-[hsl(var(--muted-foreground))]">
                      管理 Pet 会在不同论文中持续遵守的偏好与纠正
                    </p>
                  </div>
                  <span className="text-xs font-mono text-[hsl(var(--muted-foreground))]">
                    {memories.length} / 100
                  </span>
                </div>

                <div className="grid gap-2 sm:grid-cols-[9rem_1fr_auto]">
                  <select
                    value={memoryKind}
                    onChange={(event) => setMemoryKind(event.target.value)}
                    aria-label="记忆类型"
                    className="rounded-md border border-[hsl(var(--border))] bg-transparent px-3 py-2 text-sm outline-none focus:border-[hsl(var(--foreground))]"
                  >
                    <option value="preference">偏好</option>
                    <option value="correction">纠正</option>
                    <option value="criterion">判断标准</option>
                  </select>
                  <input
                    value={memoryInput}
                    onChange={(event) => setMemoryInput(event.target.value)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" && !event.nativeEvent.isComposing) {
                        event.preventDefault();
                        void addMemory();
                      }
                    }}
                    placeholder="例如：以后复现判断优先看代码和超参数"
                    aria-label="新增阅读记忆"
                    className="min-w-0 rounded-md border border-[hsl(var(--border))] bg-transparent px-3 py-2 text-sm outline-none focus:border-[hsl(var(--foreground))]"
                  />
                  <button
                    type="button"
                    onClick={() => void addMemory()}
                    disabled={memoryBusy || !memoryInput.trim()}
                    className="rounded-md bg-[hsl(var(--primary))] px-4 py-2 text-sm font-medium text-[hsl(var(--primary-foreground))] disabled:opacity-40"
                  >
                    保存
                  </button>
                </div>

                <div className="mt-3 flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
                  <input
                    value={memoryFilter}
                    onChange={(event) => setMemoryFilter(event.target.value)}
                    placeholder="筛选记忆内容、类型或论文 ID"
                    aria-label="筛选阅读记忆"
                    className="w-full rounded-md border border-[hsl(var(--border))] bg-transparent px-3 py-2 text-sm outline-none focus:border-[hsl(var(--foreground))] sm:max-w-sm"
                  />
                  <div aria-live="polite" className="min-h-5 text-xs font-mono">
                    {memoryError && <span className="text-red-500">{memoryError}</span>}
                    {!memoryError && memoryNotice && (
                      <span className="text-[hsl(var(--muted-foreground))]">{memoryNotice}</span>
                    )}
                  </div>
                </div>

                {filteredMemories.length === 0 ? (
                  <p className="mt-4 border-y border-[hsl(var(--border))] py-5 text-sm leading-relaxed text-[hsl(var(--muted-foreground))]">
                    {memories.length === 0
                      ? "还没有阅读记忆。保存一条长期偏好后，Pet 会在后续论文中继续遵守。"
                      : "没有匹配的记忆，换一个关键词试试。"}
                  </p>
                ) : (
                  <div className="mt-4 divide-y divide-[hsl(var(--border))] border-y border-[hsl(var(--border))]">
                    {filteredMemories.map((memory) => (
                      <div key={memory.id} className="py-3">
                        {editingMemoryId === memory.id ? (
                          <div className="flex flex-col gap-2 sm:flex-row">
                            <input
                              value={editingMemoryContent}
                              onChange={(event) => setEditingMemoryContent(event.target.value)}
                              aria-label="编辑阅读记忆"
                              className="min-w-0 flex-1 rounded-md border border-[hsl(var(--border))] bg-transparent px-3 py-2 text-sm outline-none focus:border-[hsl(var(--foreground))]"
                            />
                            <div className="flex gap-2">
                              <button
                                type="button"
                                onClick={() => void saveMemoryEdit(memory.id)}
                                disabled={memoryBusy || !editingMemoryContent.trim()}
                                className="rounded-md bg-[hsl(var(--primary))] px-3 py-2 text-xs font-medium text-[hsl(var(--primary-foreground))] disabled:opacity-40"
                              >
                                保存修改
                              </button>
                              <button
                                type="button"
                                onClick={() => {
                                  setEditingMemoryId(null);
                                  setEditingMemoryContent("");
                                }}
                                className="rounded-md border border-[hsl(var(--border))] px-3 py-2 text-xs font-mono text-[hsl(var(--muted-foreground))] hover:bg-[hsl(var(--muted))]"
                              >
                                取消
                              </button>
                            </div>
                          </div>
                        ) : (
                          <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
                            <div className="min-w-0">
                              <p className="text-sm leading-relaxed">{memory.content}</p>
                              <p className="mt-1 text-xs font-mono text-[hsl(var(--muted-foreground))]">
                                {MEMORY_KIND_LABELS[memory.kind] || memory.kind}
                                {memory.arxiv_id ? ` · ${memory.arxiv_id}` : " · 全局"}
                                {` · ${formatTime(memory.updated_at || memory.created_at)}`}
                              </p>
                            </div>
                            <div className="flex shrink-0 gap-2">
                              <button
                                type="button"
                                onClick={() => {
                                  setEditingMemoryId(memory.id);
                                  setEditingMemoryContent(memory.content);
                                  setPendingDeleteMemoryId(null);
                                }}
                                className="rounded-md border border-[hsl(var(--border))] px-3 py-2 text-xs font-mono text-[hsl(var(--muted-foreground))] hover:bg-[hsl(var(--muted))] hover:text-[hsl(var(--foreground))]"
                              >
                                编辑
                              </button>
                              <button
                                type="button"
                                onClick={() => {
                                  if (pendingDeleteMemoryId === memory.id) {
                                    void removeMemory(memory.id);
                                  } else {
                                    setPendingDeleteMemoryId(memory.id);
                                    setEditingMemoryId(null);
                                  }
                                }}
                                disabled={memoryBusy}
                                className="rounded-md border border-[hsl(var(--border))] px-3 py-2 text-xs font-mono text-red-500 hover:bg-red-500/10 disabled:opacity-40"
                              >
                                {pendingDeleteMemoryId === memory.id ? "确认删除" : "删除"}
                              </button>
                              {pendingDeleteMemoryId === memory.id && (
                                <button
                                  type="button"
                                  onClick={() => setPendingDeleteMemoryId(null)}
                                  className="rounded-md px-2 py-2 text-xs font-mono text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))]"
                                >
                                  取消
                                </button>
                              )}
                            </div>
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                )}
              </section>
            </FadeUp>

            <FadeUp delay={3}>
              <section>
                <h2 className="text-lg font-medium tracking-tight">Skill 提案</h2>
                <p className="mt-1 text-xs font-mono text-[hsl(var(--muted-foreground))]">
                  Pet 只能提出可复用流程；批准前不会改变实际能力。
                </p>
                {skillProposalError && <p className="mt-3 text-xs text-red-500">{skillProposalError}</p>}
                {skillProposals.filter((item) => item.status === "pending").length === 0 ? (
                  <p className="mt-4 border-y border-[hsl(var(--border))] py-4 text-sm text-[hsl(var(--muted-foreground))]">暂无待审核提案。</p>
                ) : (
                  <div className="mt-4 space-y-3 border-y border-[hsl(var(--border))] py-3">
                    {skillProposals.filter((item) => item.status === "pending").map((proposal) => (
                      <article key={proposal.id} className="rounded-md border border-[hsl(var(--border))] p-3">
                        <p className="text-sm font-medium">{proposal.skill.name}</p>
                        <p className="mt-1 text-sm text-[hsl(var(--muted-foreground))]">{proposal.skill.description}</p>
                        <pre className="mt-2 whitespace-pre-wrap text-xs font-mono text-[hsl(var(--muted-foreground))]">{proposal.diff}</pre>
                        <div className="mt-3 flex gap-2">
                          <button type="button" disabled={skillProposalBusy === proposal.id} onClick={() => void resolveSkillProposal(proposal.id, true)} className="rounded-md bg-[hsl(var(--primary))] px-3 py-1.5 text-xs font-medium text-[hsl(var(--primary-foreground))] disabled:opacity-40">批准并应用</button>
                          <button type="button" disabled={skillProposalBusy === proposal.id} onClick={() => void resolveSkillProposal(proposal.id, false)} className="rounded-md border border-[hsl(var(--border))] px-3 py-1.5 text-xs text-[hsl(var(--muted-foreground))] disabled:opacity-40">拒绝</button>
                        </div>
                      </article>
                    ))}
                  </div>
                )}
              </section>
            </FadeUp>

            <FadeUp delay={3}>
              <section>
                <h2 className="text-lg font-medium tracking-tight">新建聊天</h2>
                <p className="mt-1 text-xs font-mono text-[hsl(var(--muted-foreground))]">
                  选择论文进入阅读页后，从 Pet 开始新对话
                </p>
                {papersWithoutChat.length === 0 ? (
                  <p className="mt-4 border-y border-[hsl(var(--border))] py-5 text-sm leading-relaxed text-[hsl(var(--muted-foreground))]">
                    暂无可开始新聊天的论文。
                  </p>
                ) : (
                  <div className="mt-4 divide-y divide-[hsl(var(--border))] border-y border-[hsl(var(--border))]">
                    {papersWithoutChat.map((paper) => (
                      <a
                        key={paper.arxiv_id}
                        href={`/paper/${paper.arxiv_id}`}
                        className="flex items-center justify-between gap-4 py-3 hover:bg-[hsl(var(--muted))]/45"
                      >
                        <div className="min-w-0">
                          <p className="truncate text-sm font-medium tracking-tight">{paper.title}</p>
                          <p className="mt-1 text-xs font-mono text-[hsl(var(--muted-foreground))]">
                            {paper.arxiv_id}
                          </p>
                        </div>
                        <span className="shrink-0 text-xs font-mono text-[hsl(var(--muted-foreground))]">
                          开始
                        </span>
                      </a>
                    ))}
                  </div>
                )}
              </section>
            </FadeUp>

            <FadeUp delay={4}>
              <section>
                <h2 className="text-lg font-medium tracking-tight">执行历史</h2>
                <p className="mt-1 text-xs font-mono text-[hsl(var(--muted-foreground))]">
                  后台 Agent Run 与深度分析记录
                </p>
                {tasks.length === 0 ? (
                  <p className="mt-4 border-y border-[hsl(var(--border))] py-5 text-sm leading-relaxed text-[hsl(var(--muted-foreground))]">
                    暂无任务历史。
                  </p>
                ) : (
                  <div className="mt-4 overflow-x-auto border-y border-[hsl(var(--border))]">
                    <table className="w-full min-w-[720px] text-left text-sm">
                      <thead className="text-xs font-mono text-[hsl(var(--muted-foreground))]">
                        <tr className="border-b border-[hsl(var(--border))]">
                          <th className="py-2 pr-4 font-normal">状态</th>
                          <th className="py-2 pr-4 font-normal">任务</th>
                          <th className="py-2 pr-4 font-normal">论文</th>
                          <th className="py-2 pr-4 font-normal">时间</th>
                          <th className="py-2 font-normal">结果</th>
                        </tr>
                      </thead>
                      <tbody>
                        {tasks.slice(0, 20).map((task) => (
                          <tr
                            key={task.id}
                            className="border-b border-[hsl(var(--border))] last:border-b-0"
                          >
                            <td className={`py-3 pr-4 text-xs font-mono ${statusClass(task.status)}`}>
                              {STATUS_LABELS[task.status] || task.status}
                            </td>
                            <td className="py-3 pr-4">
                              {TASK_LABELS[task.task_type] || task.task_type}
                            </td>
                            <td className="py-3 pr-4">
                              <a
                                href={
                                  task.collection_id
                                    ? `/library/${task.collection_id}`
                                    : `/paper/${task.arxiv_id}`
                                }
                                className="block max-w-xs truncate hover:underline underline-offset-4"
                              >
                                {task.collection_name || task.paper_title || task.arxiv_id}
                              </a>
                              <span className="text-xs font-mono text-[hsl(var(--muted-foreground))]">
                                {task.collection_id ? `专题 ${task.collection_id}` : task.arxiv_id}
                              </span>
                            </td>
                            <td className="py-3 pr-4 text-xs font-mono text-[hsl(var(--muted-foreground))]">
                              {formatTime(task.created_at)}
                            </td>
                            <td className="py-3 text-xs text-[hsl(var(--muted-foreground))]">
                              {task.status === "error"
                                ? task.error || "执行失败"
                                : displayTaskSummary(task.summary) || "已记录"}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </section>
            </FadeUp>
          </div>
        )}
      </main>
    </>
  );
}

export function AgentLandingPage() {
  const [papers, setPapers] = useState<PaperMeta[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([listAgentChats(), listPapers()])
      .then(([chatData, paperData]) => {
        const latest = chatData.find((chat) => chat.paper_exists);
        if (latest) {
          window.location.replace(`/agent/${encodeURIComponent(latest.arxiv_id)}`);
          return;
        }
        setPapers(paperData);
      })
      .catch((reason) => setError((reason as Error).message))
      .finally(() => setLoading(false));
  }, []);

  return (
    <>
      <Header />
      <main className="agent-landing">
        <p className="agent-pane-eyebrow">Agent 研究工作台</p>
        <h1>选择一篇论文开始研究</h1>
        <p>对话、证据与论文笔记会在同一个工作台中保持连续。</p>
        {loading && <span>正在寻找最近的论文会话…</span>}
        {error && <span className="text-red-500">{error}</span>}
        {!loading && !error && papers.length === 0 && (
          <a href="/">先检索并保存一篇论文</a>
        )}
        {!loading && papers.length > 0 && (
          <div className="agent-landing-papers">
            {papers.slice(0, 12).map((paper) => (
              <a key={paper.arxiv_id} href={`/agent/${encodeURIComponent(paper.arxiv_id)}`}>
                <strong>{paper.title}</strong>
                <span>{paper.authors.slice(0, 3).join(" · ")}</span>
                <small>{paper.arxiv_id}</small>
              </a>
            ))}
          </div>
        )}
        <a className="agent-manage-link" href="/agent/manage">
          管理记忆、Skill 与历史
        </a>
      </main>
    </>
  );
}
