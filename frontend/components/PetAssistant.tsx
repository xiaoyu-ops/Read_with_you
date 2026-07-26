"use client";

import {
  FormEvent,
  PointerEvent as ReactPointerEvent,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { PaperDetail } from "@/lib/api";
import { saveAgentWorkspaceHandoff } from "@/lib/agentWorkspaceHandoff";
import type { PetQuestionRequest, ReaderAgentContext } from "@/lib/readerContext";
import type { ReaderEvidenceInput } from "@/lib/readerEvidence";
import { AgentConversationMessages } from "./agent/AgentConversationView";
import { useAgentConversation } from "./agent/useAgentConversation";

const HIDDEN_KEY = "peinidu.pet.hidden";
const POS_KEY = "peinidu.pet.pos";

// 状态素材由 scripts/cut_pet_assets.py 从 source.png 裁出，同画布、脚底对齐
const PET_POSES = ["idle", "talking", "thinking", "working", "confirm"] as const;

type PetStatus = "idle" | "talking" | "thinking" | "working" | "confirm" | "error";

type PetPosition = { x: number; y: number };

const DRAG_THRESHOLD = 5;

function clampPetPosition(x: number, y: number, width: number, height: number): PetPosition {
  const margin = 8;
  const maxX = Math.max(margin, window.innerWidth - width - margin);
  const maxY = Math.max(margin, window.innerHeight - height - margin);
  return {
    x: Math.min(Math.max(x, margin), maxX),
    y: Math.min(Math.max(y, margin), maxY),
  };
}

function dockPetPosition(y: number, width: number, height: number): PetPosition {
  const margin = 8;
  const mobile = window.innerWidth <= 640;
  const x = mobile
    ? window.innerWidth - width + 18
    : window.innerWidth - width + 36;
  const maxY = Math.max(margin, window.innerHeight - height - margin);
  return {
    x,
    y: Math.min(Math.max(y, margin), maxY),
  };
}

function readStoredPetPosition(): PetPosition | null {
  try {
    const raw = window.localStorage.getItem(POS_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<PetPosition>;
    if (typeof parsed.x !== "number" || typeof parsed.y !== "number") return null;
    return parsed as PetPosition;
  } catch {
    return null;
  }
}

function petStatusLabel(status: PetStatus): string {
  if (status === "working") return "执行中";
  if (status === "thinking") return "思考中";
  if (status === "error") return "需要处理";
  if (status === "confirm") return "待确认";
  if (status === "talking") return "对话中";
  return "待机";
}

function readerContextLabel(context: ReaderAgentContext | null): string {
  if (!context) return "当前论文";
  const location = context.page ? ` · P${context.page}` : "";
  const limitation = context.render_policy && context.render_policy !== "replace"
    ? " · 暂未可靠定位"
    : "";
  if (context.selected_text) {
    return `选区 #${context.selected_text.block_index}${location}${limitation}`;
  }
  if (context.active_block) {
    return `当前段落 #${context.active_block.index}${location}${limitation}`;
  }
  return "当前论文";
}

export function PetAssistant({
  paper,
  readerContext,
  askRequest,
  onNavigateEvidence,
  initialOpen = false,
}: {
  paper: PaperDetail;
  readerContext: ReaderAgentContext | null;
  askRequest?: (PetQuestionRequest & { id: number }) | null;
  onNavigateEvidence?: (evidence: ReaderEvidenceInput) => void;
  initialOpen?: boolean;
}) {
  const [hidden, setHidden] = useState(false);
  const [open, setOpen] = useState(initialOpen);
  const [menu, setMenu] = useState<{ x: number; y: number } | null>(null);
  const [speaking, setSpeaking] = useState(false);
  const [spriteBroken, setSpriteBroken] = useState(false);
  const [petPos, setPetPos] = useState<PetPosition | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const lastScrolledMarkerRef = useRef<string | null>(null);
  const spokenMessageRef = useRef<string | null>(null);
  const speakTimerRef = useRef<number | null>(null);
  const petButtonRef = useRef<HTMLButtonElement | null>(null);
  const handledAskRequestRef = useRef<number | null>(null);
  const dragRef = useRef<{
    pointerId: number;
    startX: number;
    startY: number;
    baseX: number;
    baseY: number;
    width: number;
    height: number;
    moved: boolean;
  } | null>(null);
  const suppressClickRef = useRef(false);

  useEffect(() => {
    if (initialOpen) setOpen(true);
  }, [initialOpen]);

  const context = useMemo(
    () => ({
      source: "pet",
      paper_title: paper.title,
      paper_authors: paper.authors,
      paper_source: paper.source,
      block_count: paper.blocks.length,
      reader: readerContext,
    }),
    [paper.authors, paper.blocks.length, paper.source, paper.title, readerContext],
  );
  const conversation = useAgentConversation({
    arxivId: paper.arxiv_id,
    context,
    active: open,
  });
  const {
    messages,
    visibleMessages,
    input,
    setInput,
    loading,
    sending,
    error,
    liveToolTrace,
    streamingAssistantText,
    agentStatusMessage,
    dismissedPermissionIds,
    pendingPermission,
    hasRunningRun,
  } = conversation;
  const status: PetStatus = error
    ? "error"
    : sending
      ? "thinking"
      : pendingPermission
        ? "confirm"
        : hasRunningRun
          ? "working"
          : speaking
            ? "talking"
            : "idle";

  useEffect(() => {
    setHidden(window.localStorage.getItem(HIDDEN_KEY) === "true");
    const stored = readStoredPetPosition();
    if (stored) {
      const size = window.innerWidth <= 640 ? 60 : 84;
      setPetPos(dockPetPosition(stored.y, size, size));
    }
  }, []);

  // 视口变化时把 Pet 夹回可视范围
  useEffect(() => {
    const onResize = () => {
      setPetPos((current) => {
        if (!current) return current;
        const rect = petButtonRef.current?.getBoundingClientRect();
        return open
          ? clampPetPosition(
              current.x,
              current.y,
              rect?.width ?? 84,
              rect?.height ?? 84,
            )
          : dockPetPosition(current.y, rect?.width ?? 84, rect?.height ?? 84);
      });
    };
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [open]);

  useEffect(() => {
    setPetPos((current) => {
      if (!current) return current;
      const rect = petButtonRef.current?.getBoundingClientRect();
      return open
        ? clampPetPosition(
            current.x,
            current.y,
            rect?.width ?? 84,
            rect?.height ?? 84,
          )
        : dockPetPosition(current.y, rect?.width ?? 84, rect?.height ?? 84);
    });
  }, [open]);

  // 新的 assistant 回复到达时做短暂口型动画；首次加载历史消息不触发。
  // 定时器放在 ref 里：轮询会频繁替换 messages 数组，若在 effect cleanup 中清定时器，
  // 会在"消息未变"的重跑里把口型窗口清掉且不再补设，speaking 卡死为 true
  useEffect(() => {
    let lastAssistantId: string | null = null;
    for (let i = messages.length - 1; i >= 0; i -= 1) {
      if (messages[i].role === "assistant") {
        lastAssistantId = messages[i].id;
        break;
      }
    }
    if (!lastAssistantId) return;
    if (spokenMessageRef.current === null || spokenMessageRef.current === lastAssistantId) {
      spokenMessageRef.current = lastAssistantId;
      return;
    }
    spokenMessageRef.current = lastAssistantId;
    setSpeaking(true);
    if (speakTimerRef.current !== null) window.clearTimeout(speakTimerRef.current);
    speakTimerRef.current = window.setTimeout(() => {
      speakTimerRef.current = null;
      setSpeaking(false);
    }, 2400);
  }, [messages]);

  useEffect(
    () => () => {
      if (speakTimerRef.current !== null) window.clearTimeout(speakTimerRef.current);
    },
    [],
  );

  // 只在"出现新消息 / 开始发送"时滚到底部；运行中 1.5s 轮询刷新 messages
  // 数组身份但内容未变，不应该把正在回看历史的用户强行拽到底部
  useEffect(() => {
    if (!open) {
      lastScrolledMarkerRef.current = null; // 重新打开时允许滚一次到底
      return;
    }
    const lastId = visibleMessages.length > 0 ? visibleMessages[visibleMessages.length - 1].id : "";
    const marker = `${lastId}|${sending ? "sending" : "idle"}`;
    if (lastScrolledMarkerRef.current === marker) return;
    lastScrolledMarkerRef.current = marker;
    bottomRef.current?.scrollIntoView({ block: "end" });
  }, [visibleMessages, open, sending]);

  useEffect(() => {
    if (!menu) return;
    const close = () => setMenu(null);
    window.addEventListener("click", close);
    window.addEventListener("keydown", close);
    return () => {
      window.removeEventListener("click", close);
      window.removeEventListener("keydown", close);
    };
  }, [menu]);

  const hidePet = () => {
    window.localStorage.setItem(HIDDEN_KEY, "true");
    setHidden(true);
    setOpen(false);
    setMenu(null);
  };

  const resetPet = () => {
    window.localStorage.removeItem(HIDDEN_KEY);
    window.localStorage.removeItem(POS_KEY);
    setHidden(false);
    setPetPos(null);
    setMenu(null);
  };

  const handlePetPointerDown = (event: ReactPointerEvent<HTMLButtonElement>) => {
    if (event.button !== 0) return;
    const rect = event.currentTarget.getBoundingClientRect();
    event.currentTarget.setPointerCapture(event.pointerId);
    dragRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      baseX: rect.left,
      baseY: rect.top,
      width: rect.width,
      height: rect.height,
      moved: false,
    };
  };

  const handlePetPointerMove = (event: ReactPointerEvent<HTMLButtonElement>) => {
    const drag = dragRef.current;
    if (!drag || event.pointerId !== drag.pointerId) return;
    const dx = event.clientX - drag.startX;
    const dy = event.clientY - drag.startY;
    if (!drag.moved && Math.hypot(dx, dy) < DRAG_THRESHOLD) return;
    drag.moved = true;
    setPetPos(
      open
        ? clampPetPosition(drag.baseX + dx, drag.baseY + dy, drag.width, drag.height)
        : dockPetPosition(drag.baseY + dy, drag.width, drag.height),
    );
  };

  const handlePetPointerEnd = (event: ReactPointerEvent<HTMLButtonElement>) => {
    const drag = dragRef.current;
    if (!drag || event.pointerId !== drag.pointerId) return;
    dragRef.current = null;
    if (!drag.moved) return;
    suppressClickRef.current = true; // 拖拽松手后的 click 不当作“打开聊天”
    const finalPos = open
      ? clampPetPosition(
          drag.baseX + (event.clientX - drag.startX),
          drag.baseY + (event.clientY - drag.startY),
          drag.width,
          drag.height,
        )
      : dockPetPosition(
          drag.baseY + (event.clientY - drag.startY),
          drag.width,
          drag.height,
        );
    setPetPos(finalPos);
    window.localStorage.setItem(POS_KEY, JSON.stringify(finalPos));
  };

  const handlePetClick = () => {
    if (suppressClickRef.current) {
      suppressClickRef.current = false;
      return;
    }
    setOpen((current) => !current);
  };

  // 聊天窗口跟随 Pet 就近弹出；未拖拽过时保持默认右下角布局
  const chatPlacement = useMemo(() => {
    if (!petPos || typeof window === "undefined") return null;
    const viewportWidth = window.innerWidth;
    const viewportHeight = window.innerHeight;
    const width = Math.min(400, viewportWidth - 32);
    const height = Math.min(544, viewportHeight - 144);
    const rect = petButtonRef.current?.getBoundingClientRect();
    const buttonWidth = rect?.width ?? 84;
    const buttonHeight = rect?.height ?? 84;
    const left = Math.min(
      Math.max(petPos.x + buttonWidth - width, 16),
      Math.max(16, viewportWidth - width - 16),
    );
    let top = petPos.y - height - 12;
    if (top < 8) {
      top = Math.min(petPos.y + buttonHeight + 12, Math.max(8, viewportHeight - height - 8));
    }
    return { left, top, right: "auto", bottom: "auto" } as const;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [petPos, open]);

  const send = async (event?: FormEvent) => {
    event?.preventDefault();
    const message = input.trim();
    if (!message || sending) return;
    await conversation.sendMessage(message);
  };

  useEffect(() => {
    if (!askRequest || handledAskRequestRef.current === askRequest.id || sending) return;
    handledAskRequestRef.current = askRequest.id;
    setHidden(false);
    setOpen(true);
    void conversation.sendMessage(
      askRequest.message,
      {
        ...context,
        reader: askRequest.context,
      },
    );
  }, [askRequest, context, conversation, sending]);

  const openResearchWorkspace = () => {
    saveAgentWorkspaceHandoff(paper.arxiv_id, readerContext);
    window.location.assign(`/agent/${encodeURIComponent(paper.arxiv_id)}`);
  };

  if (hidden) {
    return (
      <button
        type="button"
        onClick={resetPet}
        className="pet-restore-button"
      >
        显示 Pet
      </button>
    );
  }

  return (
    <>
      {open && (
        <section
          id="reader-pet-panel"
          style={chatPlacement ?? undefined}
          className="fixed bottom-28 right-4 z-40 flex h-[min(34rem,calc(100vh-9rem))] w-[min(25rem,calc(100vw-2rem))] flex-col overflow-hidden rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--background))] shadow-xl"
        >
          <div className="flex items-start justify-between gap-3 border-b border-[hsl(var(--border))] px-4 py-3">
            <div className="min-w-0">
              <h2 className="text-sm font-medium tracking-tight">阅读 Pet</h2>
              <p className="mt-0.5 truncate text-xs font-mono text-[hsl(var(--muted-foreground))]">
                {paper.title}
              </p>
              <p className="mt-1 text-xs font-mono text-[hsl(var(--muted-foreground))]">
                {readerContextLabel(readerContext)}
              </p>
            </div>
            <div className="flex shrink-0 items-center gap-1">
              <button
                type="button"
                onClick={openResearchWorkspace}
                className="rounded-md px-2 py-1 text-xs font-mono text-[hsl(var(--muted-foreground))] hover:bg-[hsl(var(--muted))] hover:text-[hsl(var(--foreground))]"
              >
                进入研究工作台
              </button>
              <button
                type="button"
                onClick={() => setOpen(false)}
                className="rounded-md px-2 py-1 text-xs font-mono text-[hsl(var(--muted-foreground))] hover:bg-[hsl(var(--muted))] hover:text-[hsl(var(--foreground))]"
              >
                关闭
              </button>
            </div>
          </div>

          <div className="flex-1 overflow-y-auto px-4 py-3">
            {loading && (
              <p className="text-sm font-mono text-[hsl(var(--muted-foreground))]">
                正在进入当前论文上下文…
              </p>
            )}

            {!loading && (
              <AgentConversationMessages
                messages={visibleMessages}
                sending={sending}
                streamingAssistantText={streamingAssistantText}
                agentStatusMessage={agentStatusMessage}
                liveToolTrace={liveToolTrace}
                pendingPermission={pendingPermission}
                dismissedPermissionIds={dismissedPermissionIds}
                onApprovePermission={(request) => void conversation.approvePermission(request)}
                onRejectPermission={(messageId, request) =>
                  void conversation.rejectPermission(messageId, request)
                }
                onConfirmMcpDraft={(message) => void conversation.confirmMcpDraft(message)}
                onDismissMcpDraft={conversation.dismissMcpDraft}
                onNavigateEvidence={onNavigateEvidence}
                bottomRef={bottomRef}
              />
            )}

          </div>

          {error && (
            <p className="border-t border-[hsl(var(--border))] px-4 py-2 text-xs text-red-500">
              {error}
            </p>
          )}

          <form onSubmit={send} className="flex gap-2 border-t border-[hsl(var(--border))] p-3">
            <input
              value={input}
              onChange={(event) => setInput(event.target.value)}
              placeholder="问当前论文，或让子 Agent 做任务"
              className="min-w-0 flex-1 rounded-md border border-[hsl(var(--border))] bg-transparent px-3 py-2 text-sm outline-none focus:border-[hsl(var(--foreground))]"
            />
            <button
              type="submit"
              disabled={sending || !input.trim()}
              className="rounded-md bg-[hsl(var(--primary))] px-3 py-2 text-sm font-medium text-[hsl(var(--primary-foreground))] disabled:opacity-40"
            >
              发送
            </button>
          </form>
        </section>
      )}

      <button
        ref={petButtonRef}
        type="button"
        aria-label={open ? "关闭阅读 Pet" : "打开阅读 Pet"}
        aria-controls="reader-pet-panel"
        aria-expanded={open}
        onClick={handlePetClick}
        onPointerDown={handlePetPointerDown}
        onPointerMove={handlePetPointerMove}
        onPointerUp={handlePetPointerEnd}
        onPointerCancel={handlePetPointerEnd}
        onContextMenu={(event) => {
          event.preventDefault();
          setMenu({
            x: Math.max(8, Math.min(event.clientX, window.innerWidth - 144)),
            y: Math.max(8, Math.min(event.clientY, window.innerHeight - 88)),
          });
        }}
        style={petPos ? { left: petPos.x, top: petPos.y } : undefined}
        className="pet-assistant-button pet-assistant-dock fixed z-40"
        data-open={open ? "true" : "false"}
        data-status={status}
        data-status-label={petStatusLabel(status)}
      >
        {spriteBroken ? (
          <span className="pet-assistant-avatar" aria-hidden="true">
            <span className="pet-assistant-hair" />
            <span className="pet-assistant-face">
              <span className="pet-assistant-eye" />
              <span className="pet-assistant-eye" />
            </span>
            <span className="pet-assistant-cape" />
          </span>
        ) : (
          <span className="pet-sprite" data-status={status} aria-hidden="true">
            {PET_POSES.map((pose) => (
              <img
                key={pose}
                src={`/pet/${pose}.png`}
                alt=""
                data-pose={pose}
                draggable={false}
                decoding="async"
                onError={() => setSpriteBroken(true)}
              />
            ))}
            <span className="pet-sprite-dots" />
          </span>
        )}
        <span className="sr-only">{petStatusLabel(status)}</span>
      </button>

      {menu && (
        <div
          className="fixed z-50 min-w-32 rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--background))] p-1 text-xs font-mono shadow-lg"
          style={{ left: menu.x, top: menu.y }}
        >
          <button
            type="button"
            onClick={hidePet}
            className="block w-full rounded px-3 py-2 text-left text-[hsl(var(--muted-foreground))] hover:bg-[hsl(var(--muted))] hover:text-[hsl(var(--foreground))]"
          >
            隐藏
          </button>
          <button
            type="button"
            onClick={resetPet}
            className="block w-full rounded px-3 py-2 text-left text-[hsl(var(--muted-foreground))] hover:bg-[hsl(var(--muted))] hover:text-[hsl(var(--foreground))]"
          >
            重置
          </button>
        </div>
      )}
    </>
  );
}
