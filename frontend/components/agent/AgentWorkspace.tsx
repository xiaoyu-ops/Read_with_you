"use client";

import {
  CSSProperties,
  KeyboardEvent,
  PointerEvent as ReactPointerEvent,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  getPaper,
  getPaperIfExists,
  getPaperNote,
  listAgentChats,
  listAnnotations,
  listPapers,
  type AgentChatMessage,
  type AgentChatSummary,
  type Annotation,
  type PaperDetail,
  type PaperMeta,
  type PaperNote,
} from "@/lib/api";
import { recoverPaperFromLocalIfAvailable } from "@/lib/localPaperLibrary";
import { usePortableCacheLease } from "@/lib/usePortableCacheLease";
import {
  getReaderEvidenceHint,
  hasReaderEvidenceLocation,
  type ReaderEvidenceInput,
} from "@/lib/readerEvidence";
import {
  clearAgentWorkspaceHandoff,
  readAgentWorkspaceHandoff,
} from "@/lib/agentWorkspaceHandoff";
import type { ReaderAgentContext } from "@/lib/readerContext";
import {
  AgentConversationMessages,
  getMessageEvidence,
  getMessageLimits,
} from "./AgentConversationView";
import { useAgentConversation } from "./useAgentConversation";

const LEFT_WIDTH_KEY = "peinidu.agent.left-width";
const RIGHT_WIDTH_KEY = "peinidu.agent.right-width";
const LEFT_COLLAPSED_KEY = "peinidu.agent.left-collapsed";
const RIGHT_COLLAPSED_KEY = "peinidu.agent.right-collapsed";
const LEFT_MIN = 240;
const LEFT_MAX = 320;
const RIGHT_MIN = 320;
const RIGHT_MAX = 420;

function readNumber(key: string, fallback: number, min: number, max: number): number {
  if (typeof window === "undefined") return fallback;
  const parsed = Number.parseFloat(window.localStorage.getItem(key) || "");
  return Number.isFinite(parsed) ? Math.min(max, Math.max(min, parsed)) : fallback;
}

function readBoolean(key: string): boolean {
  return typeof window !== "undefined" && window.localStorage.getItem(key) === "true";
}

function formatTime(value?: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function latestAssistant(messages: AgentChatMessage[]): AgentChatMessage | null {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    if (messages[index].role === "assistant") return messages[index];
  }
  return null;
}

function contextSummary(
  message: AgentChatMessage | null,
  handoff: ReaderAgentContext | null,
): string[] {
  const client = message?.meta?.client_context;
  if ((!client || typeof client !== "object") && !handoff) return ["当前论文"];
  const reader =
    handoff
      ? (handoff as unknown as Record<string, unknown>)
      : client && client.reader && typeof client.reader === "object"
        ? (client.reader as Record<string, unknown>)
        : null;
  if (!reader) return ["当前论文"];
  const selected =
    reader.selected_text && typeof reader.selected_text === "object"
      ? (reader.selected_text as Record<string, unknown>)
      : null;
  const lines: string[] = [];
  if (typeof reader.page === "number") lines.push(`第 ${reader.page} 页`);
  if (selected && typeof selected.text === "string" && selected.text.trim()) {
    lines.push(`选区：${selected.text.trim().slice(0, 160)}`);
  } else {
    lines.push("当前论文");
  }
  if (reader.render_policy && reader.render_policy !== "replace") {
    lines.push("这段暂未可靠定位");
  }
  return lines;
}

function noteExcerpt(note: PaperNote | null): string {
  if (!note?.markdown.trim()) return "还没有整篇论文笔记。";
  return note.markdown
    .replace(/^#{1,6}\s+/gm, "")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 320);
}

function noteKindLabel(kind: Annotation["kind"]): string {
  const labels: Record<Annotation["kind"], string> = {
    highlight: "高亮",
    important: "重要",
    question: "疑问",
    method: "方法",
    conclusion: "结论",
  };
  return labels[kind];
}

export function AgentWorkspace({ arxivId }: { arxivId: string }) {
  const [paper, setPaper] = useState<PaperDetail | null>(null);
  const [papers, setPapers] = useState<PaperMeta[]>([]);
  const [chats, setChats] = useState<AgentChatSummary[]>([]);
  const [annotations, setAnnotations] = useState<Annotation[]>([]);
  const [paperNote, setPaperNote] = useState<PaperNote | null>(null);
  const [workspaceLoading, setWorkspaceLoading] = useState(true);
  const [workspaceError, setWorkspaceError] = useState<string | null>(null);
  const [sessionQuery, setSessionQuery] = useState("");
  const [leftWidth, setLeftWidth] = useState(272);
  const [rightWidth, setRightWidth] = useState(356);
  const [leftCollapsed, setLeftCollapsed] = useState(false);
  const [rightCollapsed, setRightCollapsed] = useState(false);
  const [mobilePanel, setMobilePanel] = useState<"sessions" | "inspector" | null>(null);
  const [handoffContext, setHandoffContext] = useState<ReaderAgentContext | null>(null);
  const [confirmingClear, setConfirmingClear] = useState(false);
  const [clearingConversation, setClearingConversation] = useState(false);
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const conversationMenuRef = useRef<HTMLDetailsElement | null>(null);
  const resizeRef = useRef<{
    panel: "left" | "right";
    startX: number;
    startWidth: number;
  } | null>(null);

  const agentContext = useMemo(
    () => ({
      source: "agent_workspace",
      paper_title: paper?.title || arxivId,
      paper_authors: paper?.authors || [],
      paper_source: paper?.source || "unknown",
      block_count: paper?.blocks.length || 0,
      ...(handoffContext ? { reader: handoffContext } : {}),
    }),
    [arxivId, handoffContext, paper],
  );
  const conversation = useAgentConversation({
    arxivId,
    context: agentContext,
    active: true,
  });
  usePortableCacheLease(arxivId);

  useEffect(() => {
    setLeftWidth(readNumber(LEFT_WIDTH_KEY, 272, LEFT_MIN, LEFT_MAX));
    setRightWidth(readNumber(RIGHT_WIDTH_KEY, 356, RIGHT_MIN, RIGHT_MAX));
    setLeftCollapsed(readBoolean(LEFT_COLLAPSED_KEY));
    setRightCollapsed(readBoolean(RIGHT_COLLAPSED_KEY));
  }, []);

  useEffect(() => {
    setHandoffContext(readAgentWorkspaceHandoff(arxivId));
  }, [arxivId]);

  useEffect(() => {
    let cancelled = false;
    setWorkspaceLoading(true);
    setWorkspaceError(null);
    const loadWorkspacePaper = async () => {
      let current = await getPaperIfExists(arxivId);
      if (!current && (await recoverPaperFromLocalIfAvailable(arxivId))) {
        current = await getPaperIfExists(arxivId);
      }
      return current ?? getPaper(arxivId);
    };
    Promise.all([
      loadWorkspacePaper(),
      listPapers(),
      listAgentChats(),
      listAnnotations(arxivId).catch(() => []),
      getPaperNote(arxivId).catch(() => null),
    ])
      .then(([paperData, paperList, chatList, annotationList, note]) => {
        if (cancelled) return;
        setPaper(paperData);
        setPapers(paperList);
        setChats(chatList);
        setAnnotations(annotationList);
        setPaperNote(note);
      })
      .catch((reason) => {
        if (!cancelled) setWorkspaceError((reason as Error).message);
      })
      .finally(() => {
        if (!cancelled) setWorkspaceLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [arxivId]);

  useEffect(() => {
    const last = conversation.visibleMessages.at(-1);
    const marker = `${last?.id || ""}|${conversation.sending ? "sending" : "idle"}`;
    if (!marker) return;
    requestAnimationFrame(() => bottomRef.current?.scrollIntoView({ block: "end" }));
  }, [conversation.sending, conversation.visibleMessages]);

  useEffect(() => {
    const move = (event: PointerEvent) => {
      const resize = resizeRef.current;
      if (!resize) return;
      if (resize.panel === "left") {
        setLeftWidth(
          Math.min(LEFT_MAX, Math.max(LEFT_MIN, resize.startWidth + event.clientX - resize.startX)),
        );
      } else {
        setRightWidth(
          Math.min(
            RIGHT_MAX,
            Math.max(RIGHT_MIN, resize.startWidth - event.clientX + resize.startX),
          ),
        );
      }
    };
    const stop = () => {
      const resize = resizeRef.current;
      if (!resize) return;
      resizeRef.current = null;
      window.localStorage.setItem(LEFT_WIDTH_KEY, String(leftWidth));
      window.localStorage.setItem(RIGHT_WIDTH_KEY, String(rightWidth));
      document.body.style.removeProperty("cursor");
      document.body.style.removeProperty("user-select");
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", stop);
    return () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", stop);
    };
  }, [leftWidth, rightWidth]);

  const setLeftClosed = (value: boolean) => {
    setLeftCollapsed(value);
    window.localStorage.setItem(LEFT_COLLAPSED_KEY, String(value));
  };
  const setRightClosed = (value: boolean) => {
    setRightCollapsed(value);
    window.localStorage.setItem(RIGHT_COLLAPSED_KEY, String(value));
  };
  const toggleSessions = () => {
    if (window.innerWidth < 1280) {
      setMobilePanel((current) => (current === "sessions" ? null : "sessions"));
      return;
    }
    setLeftClosed(!leftCollapsed);
  };
  const toggleInspector = () => {
    if (window.innerWidth < 768) {
      setMobilePanel((current) => (current === "inspector" ? null : "inspector"));
      return;
    }
    setRightClosed(!rightCollapsed);
  };
  const closeSessions = () => {
    if (window.innerWidth < 1280) setMobilePanel(null);
    else setLeftClosed(true);
  };
  const closeInspector = () => {
    if (window.innerWidth < 768) setMobilePanel(null);
    else setRightClosed(true);
  };

  const beginResize =
    (panel: "left" | "right") => (event: ReactPointerEvent<HTMLDivElement>) => {
      if (event.button !== 0) return;
      resizeRef.current = {
        panel,
        startX: event.clientX,
        startWidth: panel === "left" ? leftWidth : rightWidth,
      };
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";
      event.currentTarget.setPointerCapture(event.pointerId);
    };

  const resizeWithKeyboard =
    (panel: "left" | "right") => (event: KeyboardEvent<HTMLDivElement>) => {
      if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
      event.preventDefault();
      const direction = event.key === "ArrowRight" ? 1 : -1;
      if (panel === "left") {
        const next = Math.min(LEFT_MAX, Math.max(LEFT_MIN, leftWidth + direction * 16));
        setLeftWidth(next);
        window.localStorage.setItem(LEFT_WIDTH_KEY, String(next));
      } else {
        const next = Math.min(RIGHT_MAX, Math.max(RIGHT_MIN, rightWidth - direction * 16));
        setRightWidth(next);
        window.localStorage.setItem(RIGHT_WIDTH_KEY, String(next));
      }
    };

  const filteredChats = chats.filter((chat) => {
    const query = sessionQuery.trim().toLocaleLowerCase();
    if (!query) return true;
    return `${chat.paper_title || ""} ${chat.arxiv_id} ${chat.last_message}`
      .toLocaleLowerCase()
      .includes(query);
  });
  const chatIds = new Set(chats.map((chat) => chat.arxiv_id));
  const newPapers = papers.filter((item) => !chatIds.has(item.arxiv_id)).slice(0, 8);
  const lastAssistant = latestAssistant(conversation.messages);
  const evidence = lastAssistant ? getMessageEvidence(lastAssistant) : [];
  const limits = lastAssistant ? getMessageLimits(lastAssistant) : [];
  const relevantAnnotations = annotations
    .filter((item) => item.note.trim())
    .sort((left, right) => right.updated_at.localeCompare(left.updated_at))
    .slice(0, 6);
  const activeRun = conversation.runs.find((run) => run.status === "running");

  const navigateEvidence = (input: ReaderEvidenceInput) => {
    if (!hasReaderEvidenceLocation(input)) return;
    const hint = getReaderEvidenceHint(input);
    const targetArxivId = hint.arxivId || arxivId;
    window.sessionStorage.setItem(
      "pet:pending-reader-evidence",
      JSON.stringify({ arxivId: targetArxivId, evidence: input }),
    );
    window.location.href = `/paper/${encodeURIComponent(targetArxivId)}`;
  };

  const sendCurrent = async () => {
    if (!conversation.input.trim() || conversation.sending) return;
    const sent = await conversation.sendMessage(conversation.input, agentContext);
    if (sent && handoffContext) {
      clearAgentWorkspaceHandoff(arxivId);
      setHandoffContext(null);
    }
  };

  const clearCurrentConversation = async () => {
    if (clearingConversation) return;
    setClearingConversation(true);
    const cleared = await conversation.clear();
    if (cleared) {
      const refreshed = await listAgentChats().catch(() => null);
      setChats((current) =>
        refreshed || current.filter((chat) => chat.arxiv_id !== arxivId),
      );
      setConfirmingClear(false);
      conversationMenuRef.current?.removeAttribute("open");
    }
    setClearingConversation(false);
  };

  const style = {
    "--agent-left-width": leftCollapsed ? "0px" : `${leftWidth}px`,
    "--agent-right-width": rightCollapsed ? "0px" : `${rightWidth}px`,
  } as CSSProperties;

  if (workspaceError) {
    return (
      <main className="agent-workspace-error">
        <p>{workspaceError}</p>
        {workspaceError.includes("本地文献库需要重新授权") && (
          <a href="/config">前往设置重新授权</a>
        )}
        <a href="/agent">返回 Agent</a>
      </main>
    );
  }

  return (
    <main
      className="agent-workspace"
      style={style}
      data-left-collapsed={leftCollapsed ? "true" : "false"}
      data-right-collapsed={rightCollapsed ? "true" : "false"}
      data-mobile-panel={mobilePanel || "none"}
    >
      <aside className="agent-workspace-sessions" aria-label="论文会话">
        <div className="agent-pane-header">
          <div>
            <p className="agent-pane-eyebrow">论文会话</p>
            <h2>研究线索</h2>
          </div>
          <button type="button" onClick={closeSessions} aria-label="关闭会话列表">
            ×
          </button>
        </div>
        <div className="agent-session-search">
          <input
            value={sessionQuery}
            onChange={(event) => setSessionQuery(event.target.value)}
            placeholder="搜索论文或讨论"
            aria-label="搜索论文会话"
          />
        </div>
        <nav className="agent-session-list" aria-label="最近论文会话">
          {filteredChats.map((chat) => (
            <a
              key={chat.arxiv_id}
              href={chat.paper_exists ? `/agent/${chat.arxiv_id}` : undefined}
              aria-current={chat.arxiv_id === arxivId ? "page" : undefined}
              className="agent-session-item"
              data-active={chat.arxiv_id === arxivId ? "true" : "false"}
            >
              <span className="agent-session-title">{chat.paper_title || chat.arxiv_id}</span>
              <span className="agent-session-preview">{chat.last_message || "尚未开始讨论"}</span>
              <span className="agent-session-meta">
                {chat.message_count} 条 · {formatTime(chat.updated_at)}
              </span>
            </a>
          ))}
          {!sessionQuery && newPapers.length > 0 && (
            <div className="agent-session-new">
              <p>开始新的论文会话</p>
              {newPapers.map((item) => (
                <a key={item.arxiv_id} href={`/agent/${item.arxiv_id}`}>
                  <span>{item.title}</span>
                  <small>{item.arxiv_id}</small>
                </a>
              ))}
            </div>
          )}
        </nav>
        <a className="agent-manage-link" href="/agent/manage">
          管理记忆、Skill 与历史
        </a>
      </aside>

      <div
        className="agent-workspace-divider agent-workspace-divider-left"
        role="separator"
        aria-label="调整会话栏宽度"
        aria-orientation="vertical"
        tabIndex={leftCollapsed ? -1 : 0}
        onPointerDown={beginResize("left")}
        onKeyDown={resizeWithKeyboard("left")}
      />

      <section className="agent-workspace-chat" aria-label="Agent 对话">
        <header className="agent-chat-header">
          <div className="agent-chat-header-actions">
            <button
              type="button"
              onClick={toggleSessions}
              aria-label={leftCollapsed ? "展开会话栏" : "收起会话栏"}
            >
              会话
            </button>
            <button
              type="button"
              onClick={toggleInspector}
              aria-label={rightCollapsed ? "展开资料栏" : "收起资料栏"}
            >
              资料
            </button>
          </div>
          <div className="agent-chat-title">
            <p>{workspaceLoading ? "正在进入研究上下文…" : paper?.title || arxivId}</p>
            <span>{paper?.authors.slice(0, 3).join(" · ") || arxivId}</span>
          </div>
          <div className="agent-chat-header-end">
            <a href={`/paper/${encodeURIComponent(arxivId)}`}>返回原文</a>
            <details
              ref={conversationMenuRef}
              className="agent-conversation-menu"
              onToggle={(event) => {
                if (!event.currentTarget.open) setConfirmingClear(false);
              }}
            >
              <summary aria-label="会话菜单">更多</summary>
              <div className="agent-conversation-menu-panel">
                <a href="/agent/manage">管理记忆、Skill 与历史</a>
                {!confirmingClear ? (
                  <button
                    type="button"
                    onClick={() => setConfirmingClear(true)}
                    disabled={clearingConversation}
                  >
                    清空当前会话
                  </button>
                ) : (
                  <div className="agent-conversation-clear-confirm" role="alert">
                    <p>这会取消当前运行并删除这篇论文的对话记录。</p>
                    <div>
                      <button
                        type="button"
                        onClick={() => setConfirmingClear(false)}
                        disabled={clearingConversation}
                      >
                        取消
                      </button>
                      <button
                        type="button"
                        onClick={() => void clearCurrentConversation()}
                        disabled={clearingConversation}
                      >
                        {clearingConversation ? "清空中…" : "确认清空"}
                      </button>
                    </div>
                  </div>
                )}
              </div>
            </details>
          </div>
        </header>

        <div className="agent-chat-scroll">
          {!conversation.loading && conversation.visibleMessages.length === 0 && (
            <div className="agent-chat-empty">
              <p>从一个具体问题开始。</p>
              <span>可以讨论方法、复现条件、你的笔记，或要求查找外部证据。</span>
            </div>
          )}
          {conversation.loading ? (
            <p className="agent-chat-loading">正在读取这篇论文的对话…</p>
          ) : (
            <AgentConversationMessages
              messages={conversation.visibleMessages}
              sending={conversation.sending}
              streamingAssistantText={conversation.streamingAssistantText}
              agentStatusMessage={conversation.agentStatusMessage}
              liveToolTrace={conversation.liveToolTrace}
              pendingPermission={conversation.pendingPermission}
              dismissedPermissionIds={conversation.dismissedPermissionIds}
              onApprovePermission={(request) =>
                void conversation.approvePermission(request)
              }
              onRejectPermission={(messageId, request) =>
                void conversation.rejectPermission(messageId, request)
              }
              onConfirmMcpDraft={(message) =>
                void conversation.confirmMcpDraft(message)
              }
              onDismissMcpDraft={conversation.dismissMcpDraft}
              onNavigateEvidence={navigateEvidence}
              bottomRef={bottomRef}
            />
          )}
        </div>

        {conversation.error && (
          <p className="agent-chat-error" role="alert">
            {conversation.error}
          </p>
        )}
        <form
          className="agent-chat-composer"
          onSubmit={(event) => {
            event.preventDefault();
            sendCurrent();
          }}
        >
          <textarea
            value={conversation.input}
            onChange={(event) => conversation.setInput(event.target.value)}
            onKeyDown={(event) => {
              if (
                event.key === "Enter" &&
                !event.shiftKey &&
                !event.nativeEvent.isComposing
              ) {
                event.preventDefault();
                sendCurrent();
              }
            }}
            placeholder="问方法、证据、复现条件，或让 Pet 深入研究"
            aria-label="向 Agent 提问"
            rows={2}
          />
          <div>
            <span>{conversation.sending ? conversation.agentStatusMessage : "Enter 发送 · Shift+Enter 换行"}</span>
            <div>
              {activeRun && (
                <button
                  type="button"
                  onClick={() => void conversation.cancelRun(activeRun.id)}
                  className="agent-cancel-button"
                >
                  停止
                </button>
              )}
              <button
                type="submit"
                disabled={conversation.sending || !conversation.input.trim()}
                className="agent-send-button"
              >
                发送
              </button>
            </div>
          </div>
        </form>
      </section>

      <div
        className="agent-workspace-divider agent-workspace-divider-right"
        role="separator"
        aria-label="调整资料栏宽度"
        aria-orientation="vertical"
        tabIndex={rightCollapsed ? -1 : 0}
        onPointerDown={beginResize("right")}
        onKeyDown={resizeWithKeyboard("right")}
      />

      <aside className="agent-workspace-inspector" aria-label="研究资料">
        <div className="agent-pane-header">
          <div>
            <p className="agent-pane-eyebrow">研究边注</p>
            <h2>证据与上下文</h2>
          </div>
          <button type="button" onClick={closeInspector} aria-label="关闭资料栏">
            ×
          </button>
        </div>

        <section className="agent-inspector-section">
          <h3>当前上下文</h3>
          {contextSummary(lastAssistant, handoffContext).map((line) => (
            <p key={line}>{line}</p>
          ))}
        </section>

        <section className="agent-inspector-section">
          <div className="agent-inspector-heading">
            <h3>回答证据</h3>
            <span>{evidence.length}</span>
          </div>
          {evidence.length === 0 ? (
            <p>当前回答没有可定位证据。</p>
          ) : (
            <ol className="agent-evidence-list">
              {evidence.map((item, index) => {
                const hint = getReaderEvidenceHint(item);
                return (
                  <li key={`${hint.label}-${index}`}>
                    <button
                      type="button"
                      onClick={() => navigateEvidence(item)}
                      disabled={!hasReaderEvidenceLocation(item)}
                    >
                      <span>{String(index + 1).padStart(2, "0")}</span>
                      {hint.label}
                    </button>
                  </li>
                );
              })}
            </ol>
          )}
          {limits.length > 0 && (
            <div className="agent-limit-list">
              <h4>回答限制</h4>
              {limits.map((limit) => (
                <p key={limit}>{limit}</p>
              ))}
            </div>
          )}
        </section>

        <section className="agent-inspector-section">
          <div className="agent-inspector-heading">
            <h3>你的论文笔记</h3>
            <a href={`/paper/${encodeURIComponent(arxivId)}`}>打开</a>
          </div>
          <p>{noteExcerpt(paperNote)}</p>
          {relevantAnnotations.length > 0 && (
            <ul className="agent-note-list">
              {relevantAnnotations.map((annotation) => (
                <li key={annotation.id}>
                  <button
                    type="button"
                    onClick={() =>
                      navigateEvidence({
                        claim: annotation.note || annotation.text,
                        location: {
                          block_index: annotation.block_index,
                          region_id:
                            annotation.selector && "region_id" in annotation.selector
                              ? annotation.selector.region_id
                              : null,
                          page:
                            annotation.selector && "page" in annotation.selector
                              ? annotation.selector.page
                              : null,
                        },
                      })
                    }
                  >
                    <span>{noteKindLabel(annotation.kind)}</span>
                    {annotation.note}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>
      </aside>

      {mobilePanel && (
        <button
          type="button"
          className="agent-workspace-scrim"
          aria-label="关闭侧栏"
          onClick={() => setMobilePanel(null)}
        />
      )}
    </main>
  );
}
