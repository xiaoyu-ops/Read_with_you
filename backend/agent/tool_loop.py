"""Hermes-style tool loop runtime for Pet agent requests.

The route layer owns permission and persistence. This module owns the core
runtime loop: choose a tool, execute it through ToolRegistry, decide whether
the result unlocks follow-up tools, and return a compact audit trace.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping

from ..tools.registry import ToolResult

MAX_TOOL_STEPS = 4
MAX_MODEL_ITERATIONS = 6
AGENT_LOOP_TIMEOUT_SECONDS = 90.0
MAX_TOOL_RESULT_CHARS = 12_000
ToolLoopEventCallback = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class AgentLoopState:
    """JSON-safe state for a model -> tool -> model loop."""

    messages: list[dict[str, Any]] = field(default_factory=list)
    granted_scopes: list[str] = field(default_factory=list)
    pending_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    model_iterations: int = 0
    tool_calls: int = 0
    last_call_signature: str = ""
    last_result_fingerprint: str = ""
    repeated_no_progress: int = 0
    failed_tool_calls: int = 0
    failure_fallback_attempted: bool = False
    trace: list[dict[str, Any]] = field(default_factory=list)
    limits: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "messages": [dict(message) for message in self.messages],
            "granted_scopes": list(self.granted_scopes),
            "pending_tool_calls": [dict(call) for call in self.pending_tool_calls],
            "model_iterations": self.model_iterations,
            "tool_calls": self.tool_calls,
            "last_call_signature": self.last_call_signature,
            "last_result_fingerprint": self.last_result_fingerprint,
            "repeated_no_progress": self.repeated_no_progress,
            "failed_tool_calls": self.failed_tool_calls,
            "failure_fallback_attempted": self.failure_fallback_attempted,
            "trace": [dict(event) for event in self.trace],
            "limits": list(self.limits),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AgentLoopState":
        raw_messages = data.get("messages")
        raw_granted_scopes = data.get("granted_scopes")
        raw_pending_tool_calls = data.get("pending_tool_calls")
        raw_trace = data.get("trace")
        raw_limits = data.get("limits")
        return cls(
            messages=[dict(item) for item in raw_messages if isinstance(item, Mapping)]
            if isinstance(raw_messages, list)
            else [],
            granted_scopes=[_text(item, 80) for item in raw_granted_scopes if _text(item, 80)]
            if isinstance(raw_granted_scopes, list)
            else [],
            pending_tool_calls=[dict(item) for item in raw_pending_tool_calls if isinstance(item, Mapping)]
            if isinstance(raw_pending_tool_calls, list)
            else [],
            model_iterations=max(0, int(data.get("model_iterations") or 0)),
            tool_calls=max(0, int(data.get("tool_calls") or 0)),
            last_call_signature=_text(data.get("last_call_signature"), 2_000),
            last_result_fingerprint=_text(data.get("last_result_fingerprint"), 128),
            repeated_no_progress=max(0, int(data.get("repeated_no_progress") or 0)),
            failed_tool_calls=max(0, int(data.get("failed_tool_calls") or 0)),
            failure_fallback_attempted=bool(data.get("failure_fallback_attempted")),
            trace=[dict(item) for item in raw_trace if isinstance(item, Mapping)]
            if isinstance(raw_trace, list)
            else [],
            limits=[_text(item, 160) for item in raw_limits if _text(item, 160)]
            if isinstance(raw_limits, list)
            else [],
        )


@dataclass(frozen=True)
class AgentLoopResult:
    status: Literal["completed", "limited", "waiting_permission", "timeout", "error"]
    final_text: str
    state: AgentLoopState
    pending_permission: str | None = None


def _model_failure_details(exc: Exception) -> tuple[str, str]:
    """Map provider-specific failures to stable, user-facing Pet messages."""

    name = type(exc).__name__.casefold()
    detail = str(exc).casefold()
    status = getattr(exc, "status_code", None)
    if "model" in detail and (
        "not supported" in detail or "not found" in detail or "model_not_found" in detail
    ):
        return "model_not_found", "当前配置的模型不可用，请检查模型名或切换 Provider。"
    if status == 401 or "authentication" in name or "invalid api key" in detail:
        return "model_authentication_failed", "模型服务鉴权失败，请检查 Provider 凭据后重试。"
    if status == 429 or "ratelimit" in name or "rate limit" in detail or "usage limit" in detail or "capacity" in detail:
        return "model_rate_limited", "模型服务当前限流、繁忙或额度已用完，请稍后重试或切换模型。"
    if status == 404 or "notfound" in name:
        return "model_not_found", "当前配置的模型不可用，请检查模型名或切换 Provider。"
    if "timeout" in name or "timed out" in detail:
        return "model_request_timeout", "模型服务响应超时，请稍后重试或缩小问题范围。"
    if "unsupported" in detail and ("parameter" in detail or "tool" in detail):
        return "model_unsupported_request", "当前模型不支持这次请求所需的参数或工具调用，请切换兼容模型。"
    return "model_call_failed", "这次回答没有完成：模型调用失败。请稍后重试。"


@dataclass(frozen=True)
class ToolLoopStep:
    tool_name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    reason: str = ""


@dataclass(frozen=True)
class ToolLoopResult:
    tool_result: ToolResult
    trace: dict[str, Any]
    events: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    limits: tuple[str, ...] = field(default_factory=tuple)


ALLOWED_LOCAL_TOOLS = {"local.external_search", "local.web_search", "local.web_fetch"}


def _text(value: Any, limit: int = 240) -> str:
    text = str(value or "").strip()
    return text[:limit]


def _has_url(text: str) -> bool:
    return re.search(r"https?://", text.casefold()) is not None


def _should_use_web_fetch(message: str) -> bool:
    text = message.casefold()
    if _has_url(text):
        return True
    fetch_terms = (
        "打开",
        "读取",
        "读一下",
        "总结",
        "概括",
        "分析",
        "网页内容",
        "链接内容",
        "fetch",
        "browser fetch",
        "open url",
        "read url",
        "summarize",
    )
    return any(term in text for term in fetch_terms) and any(
        target in text for target in ("网页", "链接", "url", "website", "browser")
    )


def _should_use_web_search(message: str) -> bool:
    text = message.casefold()
    if any(
        term in text
        for term in (
            "web search",
            "browser",
            "网页",
            "网上",
            "网络搜索",
            "网页搜索",
            "浏览器",
            "官网",
            "新闻",
            "博客",
            "website",
            "official site",
            "latest",
            "最新",
        )
    ):
        return True
    return any(term in text for term in ("github", "仓库", "repository", "repo", "代码", "复现"))


def choose_initial_tool(
    registry,
    *,
    scope: str,
    user_message: str,
    context: Mapping[str, Any] | None = None,
    preferred_tool: str | None = None,
) -> str:
    """Choose the first tool to execute, honoring validated upstream plans."""

    context = context or {}
    if preferred_tool and _permission_matches(registry, preferred_tool, scope):
        return preferred_tool
    if preferred_tool and not hasattr(registry, "get"):
        return preferred_tool

    tool_plan = context.get("tool_plan") if isinstance(context.get("tool_plan"), dict) else {}
    planned_tool = str(tool_plan.get("tool_name") or "")
    if planned_tool and _permission_matches(registry, planned_tool, scope):
        return planned_tool

    if scope == "external_search":
        if _should_use_web_fetch(user_message) and _registry_get(registry, "local.web_fetch") is not None:
            return "local.web_fetch"
        if _should_use_web_search(user_message) and _registry_get(registry, "local.web_search") is not None:
            return "local.web_search"
        return "local.external_search"

    specs = [
        spec
        for spec in _registry_list(registry)
        if spec.source == "mcp" and spec.permission_scope == "mcp_tool"
    ]
    if specs:
        haystack = f"{user_message} {context.get('paper_title') or ''}".casefold()
        return max(specs, key=lambda spec: _mcp_route_score(spec, haystack)).name
    return "mock.mcp_tool"


def _registry_get(registry, name: str):
    get = getattr(registry, "get", None)
    if get is None:
        return None
    return get(name)


def _registry_list(registry) -> list[Any]:
    list_fn = getattr(registry, "list", None)
    if list_fn is None:
        return []
    return list_fn()


def _permission_matches(registry, tool_name: str, scope: str) -> bool:
    if tool_name == "mcp_tool":
        return scope == "mcp_tool"
    spec = _registry_get(registry, tool_name)
    if spec is None:
        return not hasattr(registry, "get")
    return spec.permission_scope == scope


def _planned_tool_name(registry, scope: str, raw_name: Any, user_message: str, context: Mapping[str, Any]) -> str:
    name = _text(raw_name, 120)
    if name == "mcp_tool":
        return choose_initial_tool(registry, scope=scope, user_message=user_message, context=context)
    return name


def _tool_calls_from_context(context: Mapping[str, Any]) -> list[Any]:
    direct = context.get("tool_calls")
    if isinstance(direct, list):
        return direct
    tool_plan = context.get("tool_plan") if isinstance(context.get("tool_plan"), dict) else {}
    planned = tool_plan.get("tool_calls")
    return planned if isinstance(planned, list) else []


def _planned_steps_from_context(
    registry,
    *,
    scope: str,
    user_message: str,
    base_arguments: Mapping[str, Any],
    context: Mapping[str, Any],
    budget: int,
    events: list[dict[str, Any]],
    limits: list[str],
) -> list[ToolLoopStep]:
    raw_calls = _tool_calls_from_context(context)
    if not raw_calls:
        return []
    steps: list[ToolLoopStep] = []
    for index, raw_call in enumerate(raw_calls, start=1):
        if len(steps) >= budget:
            limits.append("planned_tool_step_budget_exhausted")
            events.append(
                {
                    "type": "tool_plan_rejected",
                    "status": "rejected",
                    "reason": "step_budget_exhausted",
                    "index": index,
                }
            )
            continue
        if not isinstance(raw_call, Mapping):
            events.append(
                {
                    "type": "tool_plan_rejected",
                    "status": "rejected",
                    "reason": "invalid_step",
                    "index": index,
                }
            )
            continue
        tool_name = _planned_tool_name(
            registry,
            scope,
            raw_call.get("tool_name") or raw_call.get("name") or raw_call.get("tool"),
            user_message,
            context,
        )
        if not tool_name:
            events.append(
                {
                    "type": "tool_plan_rejected",
                    "status": "rejected",
                    "reason": "missing_tool_name",
                    "index": index,
                }
            )
            continue
        if scope == "external_search" and tool_name not in ALLOWED_LOCAL_TOOLS:
            events.append(
                {
                    "type": "tool_plan_rejected",
                    "status": "rejected",
                    "reason": "tool_not_allowed_for_scope",
                    "tool": tool_name,
                    "index": index,
                }
            )
            continue
        if not _permission_matches(registry, tool_name, scope):
            events.append(
                {
                    "type": "tool_plan_rejected",
                    "status": "rejected",
                    "reason": "permission_scope_mismatch",
                    "tool": tool_name,
                    "index": index,
                }
            )
            continue
        supplied_arguments = raw_call.get("arguments")
        if not isinstance(supplied_arguments, Mapping):
            supplied_arguments = {}
        steps.append(
            ToolLoopStep(
                tool_name=tool_name,
                arguments={**dict(base_arguments), **dict(supplied_arguments)},
                reason=_text(raw_call.get("reason"), 160) or "planned_tool_call",
            )
        )
        events.append(
            {
                "type": "tool_plan_accepted",
                "status": "accepted",
                "tool": tool_name,
                "index": index,
            }
        )
    if raw_calls and not steps:
        limits.append("planned_tool_calls_rejected")
    return steps


def _mcp_route_score(spec, haystack: str) -> int:
    server_text = f"{spec.name} {spec.description} {spec.server_name or ''}".casefold()
    score = 0
    github_terms = (
        "github",
        "仓库",
        "repo",
        "repository",
        "代码",
        "源码",
        "复现",
        "implementation",
        "issue",
        "pull request",
        " pr ",
    )
    paper_terms = (
        "paper",
        "arxiv",
        "scholar",
        "论文",
        "相关工作",
        "文献",
        "引用",
        "被引",
        "作者",
        "机构",
        "检索",
        "搜索",
    )
    if "github" in server_text:
        score += sum(3 for term in github_terms if term in haystack)
    if any(term in server_text for term in ("paper", "search", "arxiv", "scholar")):
        score += sum(3 for term in paper_terms if term in haystack)
    if spec.server_name and spec.server_name.casefold() in haystack:
        score += 2
    return score


def _web_search_urls(tool_result: ToolResult, limit: int) -> list[str]:
    urls: list[str] = []
    for item in tool_result.evidence:
        if item.get("kind") != "web_search_result":
            continue
        url = str(item.get("url") or "").strip()
        if url.startswith(("http://", "https://")) and url not in urls:
            urls.append(url)
        if len(urls) >= limit:
            break
    return urls


def _label_for_evidence(tool_name: str, evidence: Mapping[str, Any] | None = None) -> str:
    kind = str((evidence or {}).get("kind") or "")
    if kind == "web_search_result":
        return "网页搜索"
    if kind == "web_fetch_result":
        return "读取网页"
    if kind == "external_paper_search_result":
        return "学术检索"
    if kind == "semantic_scholar_author_result":
        return "作者信息"
    if kind == "mcp_tool_result":
        return "MCP 工具"
    if kind == "mcp_tool_error":
        return "MCP 错误"
    if kind == "tool_error":
        return "工具错误"
    if kind == "planned_tool_step":
        return _text((evidence or {}).get("label"), 80) or "工具计划"
    if tool_name == "local.web_search":
        return "网页搜索"
    if tool_name == "local.web_fetch":
        return "读取网页"
    if tool_name == "local.external_search":
        return "学术检索"
    if tool_name.startswith("mcp:") or tool_name == "mock.mcp_tool":
        return "MCP 工具"
    return tool_name


def _trace_step(tool_name: str, evidence: Mapping[str, Any] | None = None) -> dict[str, Any]:
    evidence = evidence or {}
    kind = str(evidence.get("kind") or "tool_result")
    return {
        "tool": tool_name,
        "label": _label_for_evidence(tool_name, evidence),
        "kind": kind,
        "status": str(evidence.get("status") or ("error" if "error" in kind else "done")),
        "title": _text(evidence.get("title") or evidence.get("name"), 160),
        "source": _text(evidence.get("source") or evidence.get("server_name"), 120),
        "url": _text(evidence.get("url"), 240),
    }


def _combine_tool_results(name: str, results: list[ToolResult], limits: list[str]) -> ToolResult:
    content_sections = [
        f"## {result.name}\n{result.content}"
        for result in results
        if result.content
    ]
    evidence: list[dict[str, Any]] = []
    for result in results:
        evidence.extend(dict(item) for item in result.evidence)
    return ToolResult(
        name=name,
        content="\n\n".join(content_sections),
        evidence=tuple(evidence),
        metadata={
            "mock": any(bool(result.metadata.get("mock")) for result in results),
            "source": "agent_tool_loop",
            "tool_sequence": [result.name for result in results],
            "result_count": len(evidence),
            "limits": limits,
        },
    )


def _error_result(tool_name: str, error: Exception) -> ToolResult:
    message = f"工具执行失败：{tool_name}：{error}"
    return ToolResult(
        name=tool_name,
        content=message,
        evidence=(
            {
                "kind": "tool_error",
                "tool_name": tool_name,
                "status": "error",
                "title": tool_name,
                "error": str(error),
            },
        ),
        metadata={"mock": False, "source": "agent_tool_loop", "error": str(error)},
    )


async def _execute_step(registry, step: ToolLoopStep, scope: str) -> ToolResult:
    try:
        return await registry.execute(
            step.tool_name,
            step.arguments,
            permission_scope=scope,  # type: ignore[arg-type]
        )
    except Exception as e:
        return _error_result(step.tool_name, e)


def _trace_from_results(name: str, results: list[ToolResult], events: list[dict[str, Any]]) -> dict[str, Any]:
    sequence = [result.name for result in results]
    evidence_steps: list[dict[str, Any]] = []
    for result in results:
        for evidence in result.evidence:
            if not isinstance(evidence, dict):
                continue
            tool_name = result.name
            if evidence.get("kind") == "web_search_result":
                tool_name = "local.web_search"
            elif evidence.get("kind") == "web_fetch_result":
                tool_name = "local.web_fetch"
            elif evidence.get("kind") == "external_paper_search_result":
                tool_name = "local.external_search"
            elif str(evidence.get("kind") or "").startswith("mcp_"):
                tool_name = str(evidence.get("tool_name") or result.name)
            evidence_steps.append(_trace_step(tool_name, evidence))
    if not evidence_steps:
        evidence_steps = [_trace_step(name) for name in sequence]
    return {
        "name": name,
        "sequence": sequence,
        "steps": evidence_steps[:8],
        "events": events,
        "evidence_count": sum(len(result.evidence) for result in results),
        "mock": any(bool(result.metadata.get("mock")) for result in results),
    }


async def run_agent_tool_loop(
    registry,
    *,
    scope: str,
    user_message: str,
    base_arguments: Mapping[str, Any],
    context: Mapping[str, Any] | None = None,
    initial_tool: str | None = None,
    max_steps: int = MAX_TOOL_STEPS,
    on_event: ToolLoopEventCallback | None = None,
) -> ToolLoopResult:
    """Execute a bounded multi-step tool loop through ToolRegistry."""

    budget = max(1, min(max_steps, MAX_TOOL_STEPS))
    limits: list[str] = []
    events: list[dict[str, Any]] = []
    context = context or {}
    queue = _planned_steps_from_context(
        registry,
        scope=scope,
        user_message=user_message,
        base_arguments=base_arguments,
        context=context,
        budget=budget,
        events=events,
        limits=limits,
    )
    if not queue:
        tool_name = choose_initial_tool(
            registry,
            scope=scope,
            user_message=user_message,
            context=context,
            preferred_tool=initial_tool,
        )
        queue = [
            ToolLoopStep(
                tool_name=tool_name,
                arguments=dict(base_arguments),
                reason="initial_tool",
            )
        ]
    else:
        tool_name = queue[0].tool_name
    results: list[ToolResult] = []

    while queue and len(results) < budget:
        step = queue.pop(0)
        event = {
            "type": "tool_start",
            "tool": step.tool_name,
            "reason": step.reason,
            "status": "running",
        }
        events.append(event)
        if on_event is not None:
            await on_event(event)
        result = await _execute_step(registry, step, scope)
        results.append(result)
        event = {
            "type": "tool_done" if "error" not in result.metadata else "tool_error",
            "tool": step.tool_name,
            "status": "error" if "error" in result.metadata else "done",
            "evidence_count": len(result.evidence),
        }
        events.append(event)
        if on_event is not None:
            await on_event(event)

        if result.metadata.get("error"):
            continue
        if step.tool_name != "local.web_search" or _registry_get(registry, "local.web_fetch") is None:
            continue
        if any(queued.tool_name == "local.web_fetch" for queued in queue):
            continue
        remaining = budget - len(results)
        for url in _web_search_urls(result, remaining):
            queue.append(
                ToolLoopStep(
                    tool_name="local.web_fetch",
                    arguments={**dict(base_arguments), "url": url},
                    reason="read_search_result",
                )
            )
        if remaining <= 0:
            limits.append("tool_step_budget_exhausted")

    if queue:
        limits.append("tool_step_budget_exhausted")
    if not results:
        results.append(_error_result(tool_name, RuntimeError("没有执行任何工具步骤")))

    final_name = results[0].name if len(results) == 1 else "local.web_research"
    final_result = results[0] if len(results) == 1 else _combine_tool_results(final_name, results, limits)
    trace = _trace_from_results(final_result.name, results, events)
    if limits:
        trace["limits"] = limits
    return ToolLoopResult(
        tool_result=final_result,
        trace=trace,
        events=tuple(events),
        limits=tuple(limits),
    )


def _agent_loop_arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


_TRACE_ARGUMENT_KEYS = (
    "query",
    "search_query",
    "query_mode",
    "url",
    "document",
    "reason",
    "max_results",
    "exclude_arxiv_id",
)


def _agent_loop_trace_arguments(arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Keep technical traces useful without copying the full prompt context."""
    preview: dict[str, Any] = {}
    for key in _TRACE_ARGUMENT_KEYS:
        if key not in arguments:
            continue
        value = arguments[key]
        if isinstance(value, (str, int, float, bool)) or value is None:
            preview[key] = _text(value, 360) if isinstance(value, str) else value
        elif isinstance(value, list):
            preview[key] = [_text(item, 120) for item in value[:4]]
    return preview


def _agent_loop_call_signature(tool_name: str, arguments: Mapping[str, Any]) -> str:
    return json.dumps(
        {"tool": tool_name, "arguments": dict(arguments)},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )


def _compact_tool_json(value: Any, *, depth: int = 0) -> Any:
    if depth >= 5:
        return _text(value, 320)
    if isinstance(value, Mapping):
        return {
            _text(key, 120): _compact_tool_json(item, depth=depth + 1)
            for key, item in list(value.items())[:24]
        }
    if isinstance(value, (list, tuple)):
        return [_compact_tool_json(item, depth=depth + 1) for item in value[:12]]
    if isinstance(value, str):
        return _text(value, 1_200)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _text(value, 320)


def _agent_loop_tool_content(result: ToolResult) -> str:
    payload = {
        "content": result.content,
        "evidence": list(result.evidence),
        "metadata": dict(result.metadata),
    }
    serialized = json.dumps(payload, ensure_ascii=False, default=str)
    if len(serialized) <= MAX_TOOL_RESULT_CHARS:
        return serialized

    compact = {
        "content": _text(result.content, 4_000),
        "evidence": [
            _compact_tool_json(item)
            for item in list(result.evidence)[:8]
        ],
        "metadata": _compact_tool_json(dict(result.metadata)),
    }
    serialized = json.dumps(compact, ensure_ascii=False, default=str)
    if len(serialized) <= MAX_TOOL_RESULT_CHARS:
        return serialized

    minimal = {
        "content": _text(result.content, 2_000),
        "evidence": [
            {
                key: _compact_tool_json(value, depth=3)
                for key, value in item.items()
                if key in {
                    "kind",
                    "title",
                    "claim",
                    "detail",
                    "source",
                    "url",
                    "arxiv_id",
                    "annotation_id",
                    "note_heading",
                    "block_index",
                    "location",
                }
            }
            for item in list(result.evidence)[:4]
            if isinstance(item, Mapping)
        ],
        "metadata": {
            key: _compact_tool_json(value, depth=3)
            for key, value in result.metadata.items()
            if key in {"source", "error", "limits", "result_data", "result_count"}
        },
        "truncated": True,
    }
    serialized = json.dumps(minimal, ensure_ascii=False, default=str)
    if len(serialized) <= MAX_TOOL_RESULT_CHARS:
        return serialized
    fallback = {
        "content": _text(result.content, 1_000),
        "evidence": [
            {
                key: _text(value, 360) if isinstance(value, str) else value
                for key, value in item.items()
                if key in {"kind", "title", "source", "url", "arxiv_id", "block_index"}
            }
            for item in list(result.evidence)[:2]
            if isinstance(item, Mapping)
        ],
        "metadata": {
            "source": _text(result.metadata.get("source"), 160),
            "error": _text(result.metadata.get("error"), 360),
        },
        "truncated": True,
    }
    return json.dumps(fallback, ensure_ascii=False, default=str)


def _agent_loop_result_fingerprint(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


async def _emit_agent_loop_event(
    state: AgentLoopState,
    event: dict[str, Any],
    on_event: ToolLoopEventCallback | None,
) -> None:
    state.trace.append(event)
    if on_event is not None:
        await on_event(event)


async def _finalize_agent_loop_without_tools(
    client,
    state: AgentLoopState,
    *,
    task: str,
    variant: str,
) -> str:
    instruction = {
        "role": "user",
        "content": (
            "工具调用阶段已经结束。请不要再调用工具，只根据以上工具结果给出简洁、诚实的最终回答；"
            "说明仍缺少什么，不要编造。"
        ),
    }
    try:
        raw = await client.acomplete(
            [*state.messages, instruction],
            task=task,
            variant=variant,
        )
    except Exception:
        return "工具阶段已结束，但最终回答生成失败。请查看工具轨迹后重试。"
    final_text = str(raw or "").strip()
    return final_text or "工具阶段已结束，暂时没有足够结果形成可靠回答。"


async def _agent_loop_model_response(
    client,
    state: AgentLoopState,
    *,
    tools: list[dict[str, Any]],
    task: str,
    variant: str,
    on_event: ToolLoopEventCallback | None,
) -> Mapping[str, Any]:
    stream_with_tools = getattr(client, "astream_with_tools", None)
    if on_event is None or stream_with_tools is None:
        return await client.acomplete_with_tools(
            state.messages,
            tools=tools,
            task=task,
            variant=variant,
            tool_choice="auto",
        )

    response: Mapping[str, Any] | None = None
    async for event in stream_with_tools(
        state.messages,
        tools=tools,
        task=task,
        variant=variant,
        tool_choice="auto",
    ):
        if not isinstance(event, Mapping):
            continue
        event_type = str(event.get("type") or "")
        if event_type == "content_delta":
            text = str(event.get("content") or "")
            if text:
                await on_event(
                    {
                        "type": "model_delta",
                        "text": text,
                        "iteration": state.model_iterations,
                        "status": "streaming",
                    }
                )
        elif event_type == "response":
            response = event
    if response is None:
        raise RuntimeError("tool-capable model stream ended without a final response")
    return response


async def _run_iterative_agent_loop(
    client,
    registry,
    *,
    state: AgentLoopState,
    tools: list[dict[str, Any]],
    base_arguments: Mapping[str, Any],
    tool_name_map: Mapping[str, str],
    max_tool_calls: int,
    max_model_iterations: int,
    task: str,
    variant: str,
    on_event: ToolLoopEventCallback | None,
) -> AgentLoopResult:
    while True:
        while state.pending_tool_calls:
            pending = state.pending_tool_calls[0]
            call_id = _text(pending.get("call_id"), 160)
            provider_name = _text(pending.get("provider_name"), 160)
            tool_name = _text(pending.get("tool_name"), 160)
            raw_arguments = pending.get("arguments")
            arguments = dict(raw_arguments) if isinstance(raw_arguments, Mapping) else {}
            if not call_id or not provider_name or not tool_name:
                state.pending_tool_calls.pop(0)
                if "invalid_pending_tool_call" not in state.limits:
                    state.limits.append("invalid_pending_tool_call")
                continue

            spec = registry.get(tool_name)
            required_scope = _text(spec.permission_scope, 80) if spec is not None else ""
            if required_scope and required_scope not in state.granted_scopes:
                await _emit_agent_loop_event(
                    state,
                    {
                        "type": "permission_required",
                        "tool": tool_name,
                        "scope": required_scope,
                        "status": "waiting_permission",
                        "iteration": state.model_iterations,
                    },
                    on_event,
                )
                return AgentLoopResult(
                    status="waiting_permission",
                    final_text=f"需要确认 {required_scope} 权限后继续。",
                    state=state,
                    pending_permission=required_scope,
                )

            signature = _agent_loop_call_signature(tool_name, arguments)
            is_failure_fallback = state.failed_tool_calls > 0
            await _emit_agent_loop_event(
                state,
                {
                    "type": "tool_start",
                    "tool": tool_name,
                    "arguments": _agent_loop_trace_arguments(arguments),
                    "status": "running",
                    "iteration": state.model_iterations,
                },
                on_event,
            )
            try:
                result = await registry.execute(
                    tool_name,
                    arguments,
                    permission_scope=required_scope or None,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                result = _error_result(tool_name, exc)
            state.pending_tool_calls.pop(0)
            state.tool_calls += 1
            if is_failure_fallback:
                state.failure_fallback_attempted = True
            tool_content = _agent_loop_tool_content(result)
            fingerprint = _agent_loop_result_fingerprint(tool_content)
            state.messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": provider_name,
                    "content": tool_content,
                }
            )
            await _emit_agent_loop_event(
                state,
                {
                    "type": "tool_error" if result.metadata.get("error") else "tool_done",
                    "tool": tool_name,
                    "status": "error" if result.metadata.get("error") else "done",
                    "iteration": state.model_iterations,
                    "evidence_count": len(result.evidence),
                    "error": str(result.metadata.get("error") or ""),
                },
                on_event,
            )
            if result.metadata.get("error"):
                state.failed_tool_calls += 1
                if state.failure_fallback_attempted:
                    if "tool_failure_fallback_exhausted" not in state.limits:
                        state.limits.append("tool_failure_fallback_exhausted")
                elif state.pending_tool_calls:
                    skipped = list(state.pending_tool_calls)
                    state.pending_tool_calls.clear()
                    for skipped_call in skipped:
                        skipped_call_id = _text(skipped_call.get("call_id"), 160)
                        skipped_provider_name = _text(skipped_call.get("provider_name"), 160)
                        if not skipped_call_id or not skipped_provider_name:
                            continue
                        state.messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": skipped_call_id,
                                "name": skipped_provider_name,
                                "content": json.dumps(
                                    {
                                        "content": (
                                            "前一个工具已经失败。本轮其余预先生成的调用已跳过；"
                                            "请看到失败结果后，只选择一个合理替代工具，或诚实结束。"
                                        ),
                                        "evidence": [],
                                        "metadata": {
                                            "source": "agent_tool_loop",
                                            "skipped": "previous_tool_failed",
                                        },
                                    },
                                    ensure_ascii=False,
                                ),
                            }
                        )
                    await _emit_agent_loop_event(
                        state,
                        {
                            "type": "tool_calls_skipped",
                            "status": "skipped",
                            "reason": "previous_tool_failed",
                            "count": len(skipped),
                            "iteration": state.model_iterations,
                        },
                        on_event,
                    )
            if signature == state.last_call_signature and fingerprint == state.last_result_fingerprint:
                state.repeated_no_progress += 1
            else:
                state.repeated_no_progress = 1
            state.last_call_signature = signature
            state.last_result_fingerprint = fingerprint
            if state.repeated_no_progress >= 2 and "repeated_tool_call_no_progress" not in state.limits:
                state.limits.append("repeated_tool_call_no_progress")

        if (
            "repeated_tool_call_no_progress" in state.limits
            or state.tool_calls >= max_tool_calls
            or state.failure_fallback_attempted
        ):
            final_text = await _finalize_agent_loop_without_tools(client, state, task=task, variant=variant)
            state.messages.append({"role": "assistant", "content": final_text})
            return AgentLoopResult(
                status="limited" if state.limits else "completed",
                final_text=final_text,
                state=state,
            )

        if state.model_iterations >= max_model_iterations:
            break
        state.model_iterations += 1
        await _emit_agent_loop_event(
            state,
            {"type": "model_start", "iteration": state.model_iterations, "status": "running"},
            on_event,
        )
        try:
            response = await _agent_loop_model_response(
                client,
                state,
                tools=tools,
                task=task,
                variant=variant,
                on_event=on_event,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure_code, failure_text = _model_failure_details(exc)
            state.limits.append(failure_code)
            await _emit_agent_loop_event(
                state,
                {"type": "model_error", "iteration": state.model_iterations, "status": "error", "error": str(exc)},
                on_event,
            )
            return AgentLoopResult(
                status="error",
                final_text=failure_text,
                state=state,
            )

        content = str(response.get("content") or "").strip() if isinstance(response, Mapping) else ""
        raw_calls = response.get("tool_calls") if isinstance(response, Mapping) else None
        tool_calls = raw_calls if isinstance(raw_calls, list) else []
        if not tool_calls:
            if not content:
                state.limits.append("empty_model_response")
                return AgentLoopResult(
                    status="error",
                    final_text="模型没有返回可用回答，请重试。",
                    state=state,
                )
            state.messages.append({"role": "assistant", "content": content})
            await _emit_agent_loop_event(
                state,
                {"type": "model_done", "iteration": state.model_iterations, "status": "done"},
                on_event,
            )
            return AgentLoopResult(status="completed", final_text=content, state=state)

        assistant_tool_calls: list[dict[str, Any]] = []
        normalized_calls: list[tuple[str, str, dict[str, Any], str]] = []
        for index, raw_call in enumerate(tool_calls, start=1):
            if not isinstance(raw_call, Mapping):
                continue
            provider_name = _text(raw_call.get("name"), 160)
            if not provider_name:
                continue
            arguments = _agent_loop_arguments(raw_call.get("arguments"))
            call_id = _text(raw_call.get("id"), 160) or f"call_{state.model_iterations}_{index}"
            tool_name = _text(tool_name_map.get(provider_name) or provider_name, 160)
            assistant_tool_calls.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": provider_name,
                        "arguments": json.dumps(arguments, ensure_ascii=False, default=str),
                    },
                }
            )
            normalized_calls.append((call_id, provider_name, arguments, tool_name))

        if not normalized_calls:
            state.limits.append("invalid_model_tool_calls")
            final_text = await _finalize_agent_loop_without_tools(client, state, task=task, variant=variant)
            state.messages.append({"role": "assistant", "content": final_text})
            return AgentLoopResult(status="limited", final_text=final_text, state=state)

        remaining_budget = max_tool_calls - state.tool_calls
        if state.failed_tool_calls > 0 and not state.failure_fallback_attempted:
            remaining_budget = min(remaining_budget, 1)
        if len(normalized_calls) > remaining_budget:
            limit_code = (
                "tool_failure_fallback_limited"
                if state.failed_tool_calls > 0
                else "tool_call_budget_exhausted"
            )
            if limit_code not in state.limits:
                state.limits.append(limit_code)
            normalized_calls = normalized_calls[:remaining_budget]
            assistant_tool_calls = assistant_tool_calls[:remaining_budget]
        if not normalized_calls:
            final_text = await _finalize_agent_loop_without_tools(client, state, task=task, variant=variant)
            state.messages.append({"role": "assistant", "content": final_text})
            return AgentLoopResult(status="limited", final_text=final_text, state=state)

        state.messages.append(
            {
                "role": "assistant",
                "content": content,
                "tool_calls": assistant_tool_calls,
            }
        )
        for call_id, provider_name, supplied_arguments, tool_name in normalized_calls:
            spec = registry.get(tool_name)
            arguments = (
                dict(supplied_arguments)
                if spec is not None and spec.source == "mcp" and tool_name.startswith("mcp_")
                else {**dict(base_arguments), **supplied_arguments}
            )
            state.pending_tool_calls.append(
                {
                    "call_id": call_id,
                    "provider_name": provider_name,
                    "tool_name": tool_name,
                    "arguments": arguments,
                }
            )

    state.limits.append("model_iteration_budget_exhausted")
    final_text = await _finalize_agent_loop_without_tools(client, state, task=task, variant=variant)
    state.messages.append({"role": "assistant", "content": final_text})
    return AgentLoopResult(status="limited", final_text=final_text, state=state)


async def run_iterative_agent_loop(
    client,
    registry,
    *,
    messages: list[dict[str, Any]] | None = None,
    state: AgentLoopState | None = None,
    tools: list[dict[str, Any]],
    scope: str | None = None,
    base_arguments: Mapping[str, Any] | None = None,
    tool_name_map: Mapping[str, str] | None = None,
    max_tool_calls: int = MAX_TOOL_STEPS,
    max_model_iterations: int = MAX_MODEL_ITERATIONS,
    timeout_seconds: float = AGENT_LOOP_TIMEOUT_SECONDS,
    task: str = "agent_chat",
    variant: str = "low",
    on_event: ToolLoopEventCallback | None = None,
) -> AgentLoopResult:
    """Run a bounded iterative model -> tool -> model loop.

    A provided scope is added to this request's granted scopes. When a planned
    tool needs another scope, the loop returns ``waiting_permission`` with the
    exact pending call preserved in ``state``. Reusing that state with the
    approved scope resumes execution before any new model call.
    """

    if state is None:
        state = AgentLoopState(messages=[dict(message) for message in (messages or [])])
    elif messages and not state.messages:
        state.messages = [dict(message) for message in messages]
    normalized_scope = _text(scope, 80)
    if normalized_scope and normalized_scope not in state.granted_scopes:
        state.granted_scopes.append(normalized_scope)
    max_tool_calls = max(1, min(int(max_tool_calls), MAX_TOOL_STEPS))
    max_model_iterations = max(1, min(int(max_model_iterations), MAX_MODEL_ITERATIONS))
    try:
        return await asyncio.wait_for(
            _run_iterative_agent_loop(
                client,
                registry,
                state=state,
                tools=tools,
                base_arguments=base_arguments or {},
                tool_name_map=tool_name_map or {},
                max_tool_calls=max_tool_calls,
                max_model_iterations=max_model_iterations,
                task=task,
                variant=variant,
                on_event=on_event,
            ),
            timeout=max(0.1, float(timeout_seconds)),
        )
    except asyncio.TimeoutError:
        state.limits.append("agent_loop_timeout")
        return AgentLoopResult(
            status="timeout",
            final_text="这次处理超过了安全时限，已经停止。你可以缩小问题范围后重试。",
            state=state,
        )
