"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  cancelAgentRun,
  clearAgentChat,
  confirmMcpConfigDraft,
  getAgentChat,
  resumeAgentRunStream,
  streamAgentChatMessage,
  type AgentChatMessage,
  type AgentChatState,
  type AgentChatStreamEvent,
  type AgentRunItem,
} from "@/lib/api";
import { notifyPaperDataChanged } from "@/lib/paperDataEvents";
import {
  appendAgentToolEvent,
  getMcpConfigDraft,
  getPermissionRequest,
  type AgentPermissionRequest,
  type AgentToolTrace,
} from "./AgentConversationView";

const IDLE_STATUS = "正在理解你的问题";

type UseAgentConversationOptions = {
  arxivId: string;
  context: Record<string, unknown>;
  active?: boolean;
};

function mergeRuns(current: AgentRunItem[], incoming: AgentRunItem[]): AgentRunItem[] {
  if (incoming.length === 0) return current;
  const byId = new Map(current.map((run) => [run.id, run]));
  incoming.forEach((run) => byId.set(run.id, run));
  return Array.from(byId.values()).sort((left, right) =>
    String(left.created_at).localeCompare(String(right.created_at)),
  );
}

export function useAgentConversation({
  arxivId,
  context,
  active = true,
}: UseAgentConversationOptions) {
  const [messages, setMessages] = useState<AgentChatMessage[]>([]);
  const [pendingUserMessage, setPendingUserMessage] =
    useState<AgentChatMessage | null>(null);
  const [runs, setRuns] = useState<AgentRunItem[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [liveToolTrace, setLiveToolTrace] = useState<AgentToolTrace | null>(null);
  const [streamingAssistantText, setStreamingAssistantText] = useState("");
  const [agentStatusMessage, setAgentStatusMessage] = useState(IDLE_STATUS);
  const [dismissedPermissionIds, setDismissedPermissionIds] = useState<Set<string>>(
    () => new Set(),
  );
  const [handledPermissionIds, setHandledPermissionIds] = useState<Set<string>>(
    () => new Set(),
  );
  const skipNextLoadRef = useRef(false);
  const requestGenerationRef = useRef(0);

  const visibleMessages = useMemo(
    () => (pendingUserMessage ? [...messages, pendingUserMessage] : messages),
    [messages, pendingUserMessage],
  );
  const lastMessage = messages.length > 0 ? messages[messages.length - 1] : null;
  const hasRunningRun = runs.some((run) => run.status === "running");
  const lastPermissionRequest =
    lastMessage &&
    lastMessage.role === "assistant" &&
    !dismissedPermissionIds.has(lastMessage.id) &&
    !handledPermissionIds.has(lastMessage.id)
      ? getPermissionRequest(lastMessage)
      : null;
  const pendingPermission =
    lastPermissionRequest &&
    (!lastPermissionRequest.run_id ||
      runs.some(
        (run) =>
          run.id === lastPermissionRequest.run_id &&
          run.status === "waiting_permission",
      ))
      ? lastPermissionRequest
      : null;

  const applyState = useCallback((state: AgentChatState) => {
    setPendingUserMessage(null);
    setMessages(state.messages);
    setRuns(state.runs);
  }, []);

  const resetLiveState = useCallback(() => {
    setLiveToolTrace(null);
    setStreamingAssistantText("");
    setAgentStatusMessage(IDLE_STATUS);
  }, []);

  const handleStreamEvent = useCallback(
    (event: AgentChatStreamEvent) => {
      if (event.event === "message") {
        const incoming = event.data.created_runs || [];
        if (Array.isArray(incoming) && incoming.length > 0) {
          setRuns((current) => mergeRuns(current, incoming as AgentRunItem[]));
        }
      } else if (event.event === "delta") {
        setStreamingAssistantText((current) => current + event.data.text);
      } else if (event.event === "agent_event") {
        setAgentStatusMessage(String(event.data.message || "正在处理"));
      } else if (event.event === "tool_event") {
        setLiveToolTrace((current) => appendAgentToolEvent(current, event.data));
      } else if (event.event === "done") {
        applyState((event.data as { state: AgentChatState }).state);
        resetLiveState();
        notifyPaperDataChanged(arxivId);
      }
    },
    [applyState, arxivId, resetLiveState],
  );

  const refresh = useCallback(async () => {
    const generation = ++requestGenerationRef.current;
    setLoading(true);
    setError(null);
    try {
      const state = await getAgentChat(arxivId);
      if (generation === requestGenerationRef.current) applyState(state);
    } catch (reason) {
      if (generation === requestGenerationRef.current) {
        setError((reason as Error).message);
      }
    } finally {
      if (generation === requestGenerationRef.current) setLoading(false);
    }
  }, [applyState, arxivId]);

  useEffect(() => {
    requestGenerationRef.current += 1;
    setMessages([]);
    setPendingUserMessage(null);
    setRuns([]);
    setDismissedPermissionIds(new Set());
    setHandledPermissionIds(new Set());
    setError(null);
    resetLiveState();
  }, [arxivId, resetLiveState]);

  useEffect(() => {
    if (!active) return;
    if (skipNextLoadRef.current) {
      skipNextLoadRef.current = false;
      return;
    }
    void refresh();
  }, [active, refresh]);

  useEffect(() => {
    if (!hasRunningRun || sending) return;
    let cancelled = false;
    let failures = 0;
    let pollErrorShown = false;
    const timer = window.setInterval(() => {
      getAgentChat(arxivId)
        .then((state) => {
          if (cancelled) return;
          failures = 0;
          setRuns(state.runs);
          if (active) setMessages(state.messages);
          if (active && pollErrorShown) {
            pollErrorShown = false;
            setError(null);
          }
        })
        .catch(() => {
          failures += 1;
          if (active && failures >= 3 && !pollErrorShown) {
            pollErrorShown = true;
            setError("后台任务状态刷新失败，正在重试…");
          }
        });
    }, 1500);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [active, arxivId, hasRunningRun, sending]);

  const sendMessage = useCallback(
    async (
      message: string,
      requestContext: Record<string, unknown> = context,
      restoreInput = true,
    ) => {
      const trimmed = message.trim();
      if (!trimmed || sending) return false;
      if (!active) skipNextLoadRef.current = true;
      const optimisticMessage: AgentChatMessage = {
        id: `pending-${Date.now()}`,
        role: "user",
        content: trimmed,
        created_at: new Date().toISOString(),
        meta: { pending: true },
      };
      setSending(true);
      setError(null);
      setInput("");
      setPendingUserMessage(optimisticMessage);
      resetLiveState();
      try {
        await streamAgentChatMessage(arxivId, trimmed, requestContext, handleStreamEvent);
        return true;
      } catch (reason) {
        setPendingUserMessage(null);
        setError((reason as Error).message);
        if (restoreInput) setInput(trimmed);
        return false;
      } finally {
        resetLiveState();
        setSending(false);
      }
    },
    [active, arxivId, context, handleStreamEvent, resetLiveState, sending],
  );

  const approvePermission = useCallback(
    async (request: AgentPermissionRequest) => {
      setSending(true);
      setError(null);
      resetLiveState();
      setAgentStatusMessage("正在继续处理");
      setHandledPermissionIds((current) => new Set(current).add(request.message_id));
      try {
        if (request.run_id) {
          await resumeAgentRunStream(
            arxivId,
            request.run_id,
            request.scope,
            handleStreamEvent,
          );
        } else {
          await streamAgentChatMessage(
            arxivId,
            request.original_message,
            { ...context, approved_permission: request.scope },
            handleStreamEvent,
          );
        }
      } catch (reason) {
        setError((reason as Error).message);
        setHandledPermissionIds((current) => {
          const next = new Set(current);
          next.delete(request.message_id);
          return next;
        });
      } finally {
        resetLiveState();
        setSending(false);
      }
    },
    [arxivId, context, handleStreamEvent, resetLiveState],
  );

  const rejectPermission = useCallback(
    async (messageId: string, request: AgentPermissionRequest) => {
      setSending(true);
      setError(null);
      try {
        if (request.run_id) {
          const cancelled = await cancelAgentRun(arxivId, request.run_id);
          setRuns((current) =>
            current.map((run) => (run.id === request.run_id ? cancelled : run)),
          );
        }
        setDismissedPermissionIds((current) => new Set(current).add(messageId));
      } catch (reason) {
        setError((reason as Error).message);
      } finally {
        setSending(false);
      }
    },
    [arxivId],
  );

  const cancelRun = useCallback(
    async (runId: string) => {
      setError(null);
      try {
        const cancelled = await cancelAgentRun(arxivId, runId);
        setRuns((current) =>
          current.map((run) => (run.id === runId ? cancelled : run)),
        );
      } catch (reason) {
        setError((reason as Error).message);
      }
    },
    [arxivId],
  );

  const clear = useCallback(async () => {
    setError(null);
    try {
      applyState(await clearAgentChat(arxivId));
      setDismissedPermissionIds(new Set());
      setHandledPermissionIds(new Set());
      return true;
    } catch (reason) {
      setError((reason as Error).message);
      return false;
    }
  }, [applyState, arxivId]);

  const confirmMcpDraft = useCallback(
    async (message: AgentChatMessage) => {
      const draft = getMcpConfigDraft(message);
      if (!draft) return;
      setSending(true);
      setError(null);
      try {
        const adminToken = window.localStorage.getItem("peinidu_admin_token") || "";
        const response = await confirmMcpConfigDraft(
          arxivId,
          draft.raw,
          adminToken || undefined,
        );
        applyState(response);
        setDismissedPermissionIds((current) => new Set(current).add(message.id));
      } catch (reason) {
        setError((reason as Error).message);
      } finally {
        setSending(false);
      }
    },
    [applyState, arxivId],
  );

  const dismissMcpDraft = useCallback((messageId: string) => {
    setDismissedPermissionIds((current) => new Set(current).add(messageId));
  }, []);

  return {
    messages,
    visibleMessages,
    runs,
    input,
    setInput,
    loading,
    sending,
    error,
    setError,
    liveToolTrace,
    streamingAssistantText,
    agentStatusMessage,
    dismissedPermissionIds,
    pendingPermission,
    hasRunningRun,
    sendMessage,
    approvePermission,
    rejectPermission,
    cancelRun,
    clear,
    confirmMcpDraft,
    dismissMcpDraft,
    refresh,
  };
}
