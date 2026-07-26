"use client";

import { Fragment, ReactNode, RefObject } from "react";
import type {
  AgentChatMessage,
  AgentPermissionRequestMeta,
  AgentRunResultData,
} from "@/lib/api";
import {
  getReaderEvidenceHint,
  hasReaderEvidenceLocation,
  type ReaderEvidenceInput,
} from "@/lib/readerEvidence";

export type AgentPermissionRequest = AgentPermissionRequestMeta & {
  message_id: string;
};

export type AgentToolTraceStep = {
  tool: string;
  label: string;
  kind: string;
  status: string;
  title?: string;
  source?: string;
  url?: string;
  arguments?: string;
  error?: string;
  evidence_count?: number;
};

export type AgentToolTrace = {
  name: string;
  sequence: string[];
  steps: AgentToolTraceStep[];
  evidence_count: number;
  mock?: boolean;
};

export type AgentMcpConfigDraft = {
  name: string;
  transport: string;
  command?: string;
  args?: string[];
  url?: string | null;
  tool_name?: string;
  raw: Record<string, unknown>;
};

export function getMessageEvidence(
  message: AgentChatMessage,
): AgentRunResultData["evidence"] {
  const evidence = message.meta?.result_data?.evidence;
  if (!Array.isArray(evidence)) return [];
  return evidence.filter(
    (item): item is Record<string, unknown> =>
      Boolean(item) && typeof item === "object" && !Array.isArray(item),
  );
}

export function getMessageLimits(message: AgentChatMessage): string[] {
  const limits = message.meta?.result_data?.limits;
  if (!Array.isArray(limits)) return [];
  return limits.filter((item): item is string => typeof item === "string" && Boolean(item.trim()));
}

export function getPermissionRequest(
  message: AgentChatMessage,
): AgentPermissionRequest | null {
  const data = message.meta?.permission_request;
  if (
    !data ||
    typeof data.scope !== "string" ||
    typeof data.label !== "string" ||
    typeof data.description !== "string" ||
    typeof data.original_message !== "string"
  ) {
    return null;
  }
  return {
    message_id: message.id,
    scope: data.scope,
    label: data.label,
    description: data.description,
    original_message: data.original_message,
    run_id: typeof data.run_id === "string" ? data.run_id : undefined,
    memory_proposal:
      data.memory_proposal && typeof data.memory_proposal.content === "string"
        ? {
            content: data.memory_proposal.content,
            kind:
              typeof data.memory_proposal.kind === "string"
                ? data.memory_proposal.kind
                : "preference",
          }
        : undefined,
  };
}

export function getMcpConfigDraft(
  message: AgentChatMessage,
): AgentMcpConfigDraft | null {
  const draft = message.meta?.mcp_config_draft;
  if (!draft || typeof draft !== "object") return null;
  const data = draft as Record<string, unknown>;
  if (typeof data.name !== "string" || typeof data.transport !== "string") return null;
  return {
    name: data.name,
    transport: data.transport,
    command: typeof data.command === "string" ? data.command : undefined,
    args: Array.isArray(data.args) ? data.args.map((item) => String(item)) : undefined,
    url: typeof data.url === "string" ? data.url : null,
    tool_name: typeof data.tool_name === "string" ? data.tool_name : undefined,
    raw: data,
  };
}

export function appendAgentToolEvent(
  trace: AgentToolTrace | null,
  event: Record<string, unknown>,
): AgentToolTrace {
  const tool = String(event.tool || "tool");
  const status = String(event.status || (event.type === "tool_start" ? "running" : "done"));
  const rawArgumentsText =
    event.arguments && typeof event.arguments === "object"
      ? JSON.stringify(event.arguments)
      : undefined;
  const argumentsText =
    rawArgumentsText && rawArgumentsText.length > 520
      ? `${rawArgumentsText.slice(0, 520)}…`
      : rawArgumentsText;
  const step: AgentToolTraceStep = {
    tool,
    label: tool,
    kind: String(event.type || "tool_event"),
    status,
    title: event.reason ? String(event.reason) : undefined,
    arguments: argumentsText,
    error: event.error ? String(event.error) : undefined,
    evidence_count:
      typeof event.evidence_count === "number" ? event.evidence_count : undefined,
  };
  const existingSteps = [...(trace?.steps || [])];
  const runningIndex = existingSteps.findLastIndex(
    (item) => item.tool === tool && item.status === "running",
  );
  if (event.type !== "tool_start" && runningIndex >= 0) {
    step.arguments = step.arguments || existingSteps[runningIndex].arguments;
    existingSteps[runningIndex] = step;
  } else if (
    event.type === "tool_start" ||
    event.type === "tool_done" ||
    event.type === "tool_error"
  ) {
    existingSteps.push(step);
  }
  return {
    name: trace?.name || "live_tool_trace",
    sequence: [...(trace?.sequence || []), tool].slice(-6),
    steps: existingSteps.slice(-6),
    evidence_count:
      typeof event.evidence_count === "number"
        ? (trace?.evidence_count || 0) + event.evidence_count
        : trace?.evidence_count || 0,
    mock: trace?.mock,
  };
}

export function getToolTrace(message: AgentChatMessage): AgentToolTrace | null {
  const trace = message.meta?.tool_trace;
  if ((!trace || typeof trace !== "object") && Array.isArray(message.meta?.agent_loop_trace)) {
    return message.meta.agent_loop_trace.reduce<AgentToolTrace | null>((current, event) => {
      if (!event || typeof event !== "object") return current;
      const data = event as Record<string, unknown>;
      if (!["tool_start", "tool_done", "tool_error"].includes(String(data.type || ""))) {
        return current;
      }
      return appendAgentToolEvent(current, data);
    }, null);
  }
  if (!trace || !Array.isArray(trace.sequence) || !Array.isArray(trace.steps)) return null;
  const steps = trace.steps
    .filter((item) => Boolean(item) && typeof item === "object")
    .map((item) => ({
      tool: String(item.tool || ""),
      label: String(item.label || item.tool || "工具"),
      kind: String(item.kind || ""),
      status: String(item.status || "done"),
      title: item.title ? String(item.title) : undefined,
      source: item.source ? String(item.source) : undefined,
      url: item.url ? String(item.url) : undefined,
      arguments: item.arguments ? String(item.arguments) : undefined,
      error: item.error ? String(item.error) : undefined,
      evidence_count:
        typeof item.evidence_count === "number" ? item.evidence_count : undefined,
    }))
    .filter((item) => item.tool || item.label);
  if (steps.length === 0) return null;
  return {
    name: String(trace.name || "tool_trace"),
    sequence: trace.sequence.map((item) => String(item)),
    steps,
    evidence_count:
      typeof trace.evidence_count === "number" ? trace.evidence_count : steps.length,
    mock: Boolean(trace.mock),
  };
}

function renderInlineMarkdown(text: string): ReactNode[] {
  const parts = text.split(/(`[^`]+`|\*\*[^*]+\*\*|\[[^\]]+\]\([^)]+\))/g);
  return parts.map((part, index) => {
    if (!part) return null;
    if (part.startsWith("`") && part.endsWith("`")) {
      return <code key={index}>{part.slice(1, -1)}</code>;
    }
    if (part.startsWith("**") && part.endsWith("**")) {
      return <strong key={index}>{renderInlineMarkdown(part.slice(2, -2))}</strong>;
    }
    const link = part.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
    if (link) {
      const href = link[2];
      if (/^https?:\/\//i.test(href)) {
        return (
          <a key={index} href={href} target="_blank" rel="noreferrer">
            {link[1]}
          </a>
        );
      }
      return <Fragment key={index}>{link[1]}</Fragment>;
    }
    return <Fragment key={index}>{part}</Fragment>;
  });
}

export function AgentMessageContent({ content }: { content: string }) {
  const lines = content.split(/\r?\n/);
  const nodes: ReactNode[] = [];
  let listItems: string[] = [];
  let orderedItems: string[] = [];
  let paragraph: string[] = [];

  const flushParagraph = () => {
    if (paragraph.length === 0) return;
    const text = paragraph.join("\n").trim();
    if (text) nodes.push(<p key={`p-${nodes.length}`}>{renderInlineMarkdown(text)}</p>);
    paragraph = [];
  };
  const flushList = () => {
    if (listItems.length > 0) {
      nodes.push(
        <ul key={`ul-${nodes.length}`}>
          {listItems.map((item, index) => (
            <li key={index}>{renderInlineMarkdown(item)}</li>
          ))}
        </ul>,
      );
      listItems = [];
    }
    if (orderedItems.length > 0) {
      nodes.push(
        <ol key={`ol-${nodes.length}`}>
          {orderedItems.map((item, index) => (
            <li key={index}>{renderInlineMarkdown(item)}</li>
          ))}
        </ol>,
      );
      orderedItems = [];
    }
  };

  for (const line of lines) {
    const unordered = line.match(/^\s*[-*]\s+(.+)$/);
    const ordered = line.match(/^\s*\d+[.)]\s+(.+)$/);
    if (!line.trim()) {
      flushParagraph();
      flushList();
    } else if (unordered) {
      flushParagraph();
      if (orderedItems.length > 0) flushList();
      listItems.push(unordered[1]);
    } else if (ordered) {
      flushParagraph();
      if (listItems.length > 0) flushList();
      orderedItems.push(ordered[1]);
    } else {
      flushList();
      paragraph.push(line);
    }
  }
  flushParagraph();
  flushList();
  return nodes.length > 0 ? <>{nodes}</> : null;
}

export function AgentEvidenceDetails({
  evidence,
  onNavigateEvidence,
}: {
  evidence: AgentRunResultData["evidence"];
  onNavigateEvidence?: (evidence: ReaderEvidenceInput) => void;
}) {
  if (evidence.length === 0) return null;
  return (
    <details className="pet-evidence-details mt-2 rounded-md border border-[hsl(var(--border))] px-3 py-2">
      <summary className="cursor-pointer text-[11px] font-mono text-[hsl(var(--muted-foreground))]">
        查看 {evidence.length} 条可核对证据
      </summary>
      <ul className="mt-2 space-y-2 border-t border-[hsl(var(--border))]/70 pt-2">
        {evidence.map((item, index) => {
          const hint = getReaderEvidenceHint(item);
          const canNavigate = Boolean(onNavigateEvidence && hasReaderEvidenceLocation(item));
          return (
            <li
              key={`${hint.label}-${index}`}
              className="text-xs leading-relaxed text-[hsl(var(--muted-foreground))]"
            >
              {canNavigate ? (
                <button
                  type="button"
                  onClick={() => onNavigateEvidence?.(item)}
                  className="reader-evidence-link text-left underline decoration-[hsl(var(--border))] underline-offset-4 hover:text-[hsl(var(--foreground))]"
                >
                  {hint.label}
                </button>
              ) : (
                hint.label
              )}
            </li>
          );
        })}
      </ul>
    </details>
  );
}

export function AgentToolTraceTrail({ trace }: { trace: AgentToolTrace }) {
  return (
    <details className="pet-technical-trace mt-2 rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--muted))]/20 px-3 py-2">
      <summary className="flex cursor-pointer list-none items-center justify-between gap-3 text-[11px] font-mono text-[hsl(var(--muted-foreground))] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[hsl(var(--ring))]">
        <span>查看技术轨迹</span>
        <span>{trace.evidence_count} 条证据</span>
      </summary>
      <div className="mt-2 space-y-2 border-t border-[hsl(var(--border))]/70 pt-2">
        {trace.steps.slice(0, 4).map((step, index) => (
          <div key={`${step.tool}-${index}`} className="flex gap-2">
            <span
              className={`mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full border text-[10px] font-mono ${
                step.status === "error"
                  ? "border-red-300 text-red-600"
                  : "border-[hsl(var(--border))] text-[hsl(var(--foreground))]"
              }`}
            >
              {index + 1}
            </span>
            <div className="min-w-0">
              <p className="text-xs font-medium leading-tight">{step.label}</p>
              {(step.title || step.source || step.url) && (
                <p className="mt-0.5 truncate text-[11px] leading-relaxed text-[hsl(var(--muted-foreground))]">
                  {step.title || step.source || step.url}
                </p>
              )}
              {step.arguments && (
                <p className="mt-1 break-all font-mono text-[10px] leading-relaxed text-[hsl(var(--muted-foreground))]">
                  参数：{step.arguments}
                </p>
              )}
              {step.error && (
                <p className="mt-1 text-[11px] leading-relaxed text-red-600">
                  错误：{step.error}
                </p>
              )}
            </div>
          </div>
        ))}
      </div>
    </details>
  );
}

export function AgentConversationMessages({
  messages,
  sending,
  streamingAssistantText,
  agentStatusMessage,
  liveToolTrace,
  pendingPermission,
  dismissedPermissionIds,
  onApprovePermission,
  onRejectPermission,
  onConfirmMcpDraft,
  onDismissMcpDraft,
  onNavigateEvidence,
  bottomRef,
}: {
  messages: AgentChatMessage[];
  sending: boolean;
  streamingAssistantText: string;
  agentStatusMessage: string;
  liveToolTrace: AgentToolTrace | null;
  pendingPermission: AgentPermissionRequest | null;
  dismissedPermissionIds: Set<string>;
  onApprovePermission: (request: AgentPermissionRequest) => void;
  onRejectPermission: (messageId: string, request: AgentPermissionRequest) => void;
  onConfirmMcpDraft: (message: AgentChatMessage) => void;
  onDismissMcpDraft: (messageId: string) => void;
  onNavigateEvidence?: (evidence: ReaderEvidenceInput) => void;
  bottomRef?: RefObject<HTMLDivElement | null>;
}) {
  const lastMessage = messages.length > 0 ? messages[messages.length - 1] : null;
  return (
    <div className="agent-conversation-messages space-y-3">
      {messages.map((message) => {
        const permission = getPermissionRequest(message);
        const mcpDraft = getMcpConfigDraft(message);
        const trace = getToolTrace(message);
        return (
          <div
            key={message.id}
            className={`pet-chat-row ${
              message.role === "user" ? "pet-chat-row-user" : "pet-chat-row-pet"
            }`}
          >
            {message.role === "assistant" && (
              <span className="pet-chat-avatar" aria-hidden="true">
                <img src="/pet/idle.png" alt="" draggable={false} />
              </span>
            )}
            <div className="pet-chat-stack">
              <p className="pet-chat-name">{message.role === "user" ? "你" : "Pet"}</p>
              <div className="pet-chat-bubble">
                <AgentMessageContent content={message.content} />
              </div>
              {message.role === "assistant" && (
                <AgentEvidenceDetails
                  evidence={getMessageEvidence(message)}
                  onNavigateEvidence={onNavigateEvidence}
                />
              )}
              {message.role === "assistant" && trace && (
                <AgentToolTraceTrail trace={trace} />
              )}
              {message.role === "assistant" && permission && (
                <div className="mt-2 rounded-md border border-[hsl(var(--border))] px-3 py-2">
                  {permission.memory_proposal && (
                    <div className="mb-2 border-l-2 border-[hsl(var(--foreground))]/45 bg-[hsl(var(--muted))]/35 px-2.5 py-2">
                      <p className="text-xs font-mono text-[hsl(var(--muted-foreground))]">
                        准备保存为长期记忆
                      </p>
                      <p className="mt-1 text-sm leading-relaxed">
                        {permission.memory_proposal.content}
                      </p>
                    </div>
                  )}
                  <p className="text-xs leading-relaxed text-[hsl(var(--muted-foreground))]">
                    {permission.description}
                  </p>
                  {pendingPermission?.message_id === message.id ? (
                    <div className="mt-2 flex items-center gap-2">
                      <button
                        type="button"
                        onClick={() => onApprovePermission(permission)}
                        disabled={sending}
                        className="rounded-md bg-[hsl(var(--primary))] px-2.5 py-1.5 text-xs font-medium text-[hsl(var(--primary-foreground))] disabled:opacity-40"
                      >
                        确认{permission.label}
                      </button>
                      <button
                        type="button"
                        onClick={() => onRejectPermission(message.id, permission)}
                        disabled={sending}
                        className="rounded-md border border-[hsl(var(--border))] px-2.5 py-1.5 text-xs font-mono text-[hsl(var(--muted-foreground))] hover:bg-[hsl(var(--muted))] disabled:opacity-40"
                      >
                        暂不
                      </button>
                    </div>
                  ) : (
                    <p className="mt-2 text-xs font-mono text-[hsl(var(--muted-foreground))]">
                      {dismissedPermissionIds.has(message.id) ? "已跳过" : "已处理"}
                    </p>
                  )}
                </div>
              )}
              {message.role === "assistant" && mcpDraft && (
                <div className="mt-2 rounded-md border border-[hsl(var(--border))] px-3 py-2">
                  <p className="text-xs font-mono text-[hsl(var(--muted-foreground))]">
                    MCP 配置草稿
                  </p>
                  <div className="mt-1 space-y-0.5 text-xs font-mono text-[hsl(var(--muted-foreground))]">
                    <p className="truncate">名称：{mcpDraft.name}</p>
                    <p>transport：{mcpDraft.transport}</p>
                    <p className="truncate">
                      入口：
                      {mcpDraft.transport === "http"
                        ? mcpDraft.url || ""
                        : [mcpDraft.command || "", ...(mcpDraft.args ?? [])].join(" ").trim()}
                    </p>
                    {mcpDraft.tool_name && (
                      <p className="truncate">默认工具：{mcpDraft.tool_name}</p>
                    )}
                  </div>
                  {lastMessage?.id === message.id &&
                  !dismissedPermissionIds.has(message.id) ? (
                    <div className="mt-2 flex items-center gap-2">
                      <button
                        type="button"
                        onClick={() => onConfirmMcpDraft(message)}
                        disabled={sending}
                        className="rounded-md bg-[hsl(var(--primary))] px-2.5 py-1.5 text-xs font-medium text-[hsl(var(--primary-foreground))] disabled:opacity-40"
                      >
                        确认写入（保持未启用）
                      </button>
                      <button
                        type="button"
                        onClick={() => onDismissMcpDraft(message.id)}
                        disabled={sending}
                        className="rounded-md border border-[hsl(var(--border))] px-2.5 py-1.5 text-xs font-mono text-[hsl(var(--muted-foreground))] hover:bg-[hsl(var(--muted))] disabled:opacity-40"
                      >
                        暂不
                      </button>
                    </div>
                  ) : (
                    <p className="mt-2 text-xs font-mono text-[hsl(var(--muted-foreground))]">
                      已处理
                    </p>
                  )}
                </div>
              )}
            </div>
          </div>
        );
      })}
      {sending && (
        <div className="pet-chat-row pet-chat-row-pet">
          <span className="pet-chat-avatar" aria-hidden="true">
            <img src="/pet/thinking.png" alt="" draggable={false} />
          </span>
          <div className="pet-chat-stack">
            <p className="pet-chat-name">Pet</p>
            <div className="pet-chat-bubble pet-chat-bubble-thinking" aria-live="polite">
              {streamingAssistantText ? (
                <AgentMessageContent content={streamingAssistantText} />
              ) : (
                <span className="pet-agent-status">
                  <span className="pet-status-pulse" aria-hidden="true" />
                  <span>{agentStatusMessage}</span>
                </span>
              )}
            </div>
            {liveToolTrace && <AgentToolTraceTrail trace={liveToolTrace} />}
          </div>
        </div>
      )}
      <div ref={bottomRef} />
    </div>
  );
}
