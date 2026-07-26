"""Agent 对话工作区路由。"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from ..agent.evidence_location import enrich_result_data_locations
from ..agent.orchestrator import analyze_paper
from ..agent.tool_loop import (
    AgentLoopResult,
    AgentLoopState,
    choose_initial_tool,
    run_agent_tool_loop,
    run_iterative_agent_loop,
)
from ..llm.client import get_client
from ..llm.config import get_config, reset_config, save_config
from ..llm.models import MCPServerConfig
from ..storage import files
from ..storage.agent_session_index import search_agent_sessions, sync_agent_session_index
from ..storage.paper_note_index import (
    build_notes_context,
    search_collection_notes,
    search_paper_notes,
    view_paper_note,
)
from ..storage.agent_workspace import (
    add_memory,
    append_message,
    cancel_run,
    clear_chat,
    create_run,
    delete_memory,
    get_run,
    infer_agent_intent,
    load_chat,
    load_memories,
    load_runs,
    load_skills,
    load_skill_proposals,
    get_skill_proposal,
    create_skill_proposal,
    apply_skill_proposal,
    reject_skill_proposal,
    list_chat_summaries,
    should_save_memory,
    update_memory,
    update_run,
)
from ..storage.db import (
    create_agent_task,
    get_collection,
    get_paper,
    list_collections,
    update_agent_task,
    update_status,
)
from ..tools import ToolCall, ToolResult, ToolSpec, build_agent_tool_registry
from ..tools.mcp import register_mcp_tool_catalog
from .routes_config import _require_admin

router = APIRouter(prefix="/agent", tags=["agent-chat"])


class AgentMessage(BaseModel):
    id: str
    role: str
    content: str
    created_at: str
    meta: dict = Field(default_factory=dict)


class AgentMemoryItem(BaseModel):
    id: str
    kind: str
    content: str
    arxiv_id: str | None = None
    source: str
    created_at: str
    updated_at: str | None = None


class AgentSkillItem(BaseModel):
    id: str
    name: str
    description: str
    trigger: str
    task_type: str | None = None
    trigger_keywords: list[str] = Field(default_factory=list)
    steps: list[str]
    source: str
    updated_at: str | None = None


class AgentSkillProposalItem(BaseModel):
    id: str
    action: str
    status: str
    skill: AgentSkillItem
    diff: str
    created_at: str
    updated_at: str


class AgentSkillProposalRequest(BaseModel):
    action: str = "create"
    skill: AgentSkillItem


class AgentRunItem(BaseModel):
    id: str
    arxiv_id: str
    task_type: str
    title: str
    status: str
    user_message: str
    inputs: list[str] = Field(default_factory=list)
    result: str = ""
    result_data: dict | None = None
    error: str = ""
    task_id: int | None = None
    created_at: str
    updated_at: str
    completed_at: str | None = None


def _result_data(
    summary: object = "",
    *,
    evidence: object = None,
    limits: object = None,
    next_questions: object = None,
) -> dict:
    """Keep Run results readable by old clients and structurally stable for new ones."""

    return {
        "summary": _short_text(summary, 2_000),
        "evidence": [dict(item) for item in evidence if isinstance(item, dict)][:12]
        if isinstance(evidence, list)
        else [],
        "limits": [_short_text(item, 500) for item in limits if _short_text(item, 500)][:8]
        if isinstance(limits, list)
        else [],
        "next_questions": [
            _short_text(item, 320) for item in next_questions if _short_text(item, 320)
        ][:6]
        if isinstance(next_questions, list)
        else [],
    }


def _normalize_result_data(value: object, fallback: str = "") -> dict:
    if not isinstance(value, dict):
        return _result_data(fallback or value)
    return _result_data(
        value.get("summary") or fallback,
        evidence=value.get("evidence"),
        limits=value.get("limits"),
        next_questions=value.get("next_questions"),
    )


def _enrich_result_data_for_paper(arxiv_id: str, data: dict) -> dict:
    """Attach server-verified coordinates, including cross-paper note evidence."""

    raw_evidence = data.get("evidence")
    if not isinstance(raw_evidence, list):
        return data
    enriched_evidence: list[dict] = []
    for raw_item in raw_evidence:
        if not isinstance(raw_item, dict):
            continue
        evidence_paper_id = str(raw_item.get("arxiv_id") or arxiv_id)
        try:
            document = files.load_document(evidence_paper_id)
            valid_block_indexes = (
                {block.index for block in document.blocks} if document is not None else None
            )
            layout = files.load_translation_layout(evidence_paper_id)
        except (OSError, TypeError, ValueError):
            valid_block_indexes = None
            layout = None
        single = enrich_result_data_locations(
            {"evidence": [raw_item]},
            layout,
            valid_block_indexes=valid_block_indexes,
        )
        items = single.get("evidence")
        if isinstance(items, list) and items:
            enriched_evidence.append(items[0])
    return {**data, "evidence": enriched_evidence}


def _format_result_data(data: dict) -> str:
    normalized = _normalize_result_data(data)
    lines = [normalized["summary"] or "没有生成明确结论。"]
    if normalized["evidence"]:
        lines.append("\n证据：")
        for item in normalized["evidence"]:
            claim = _short_text(item.get("claim") or item.get("detail") or item.get("title"), 500)
            source = _short_text(item.get("source") or item.get("citation"), 180)
            lines.append(f"- {claim}{f'（{source}）' if source else ''}")
    if normalized["limits"]:
        lines.append("\n限制：")
        lines.extend(f"- {item}" for item in normalized["limits"])
    if normalized["next_questions"]:
        lines.append("\n可继续追问：")
        lines.extend(f"- {item}" for item in normalized["next_questions"])
    return "\n".join(lines).strip()


class AgentChatState(BaseModel):
    arxiv_id: str
    messages: list[AgentMessage]
    memories: list[AgentMemoryItem]
    skills: list[AgentSkillItem]
    runs: list[AgentRunItem]


class AgentChatSummary(BaseModel):
    arxiv_id: str
    paper_title: str | None = None
    paper_exists: bool = True
    message_count: int
    last_role: str
    last_message: str
    updated_at: str | None = None


class AgentSessionSearchResult(BaseModel):
    arxiv_id: str
    message_id: str
    paper_title: str
    paper_exists: bool
    role: str
    snippet: str
    created_at: str | None = None


MAX_AGENT_CONTEXT_BYTES = 24_000


class AgentChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    context: dict = Field(default_factory=dict)

    @field_validator("context")
    @classmethod
    def context_size_limit(cls, value: dict) -> dict:
        size = len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
        if size > MAX_AGENT_CONTEXT_BYTES:
            raise ValueError(f"context too large: {size} bytes > {MAX_AGENT_CONTEXT_BYTES}")
        return value


class AgentRunResumeRequest(BaseModel):
    approved_permission: str = Field(min_length=1, max_length=80)


class AgentMemoryRequest(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    kind: str = "preference"


class AgentMemoryCreateRequest(AgentMemoryRequest):
    arxiv_id: str | None = None


class AgentMemoryUpdateRequest(BaseModel):
    content: str | None = Field(default=None, min_length=1, max_length=4000)
    kind: str | None = Field(default=None, min_length=1, max_length=80)

    @model_validator(mode="after")
    def require_change(self):
        if self.content is None and self.kind is None:
            raise ValueError("至少提供 content 或 kind")
        return self


class AgentCreatedTask(BaseModel):
    id: int
    task_type: str
    summary: str
    status: str


class AgentChatResponse(AgentChatState):
    assistant_message: AgentMessage
    created_tasks: list[AgentCreatedTask] = Field(default_factory=list)
    created_runs: list[AgentRunItem] = Field(default_factory=list)
    saved_memory: AgentMemoryItem | None = None


TASK_SUMMARIES = {
    "selection_explanation": "子 Agent 计划：解释当前选区或段落，并给出前后文线索",
    "external_tool_request": "外部资料检索与工具结果整理",
    "four_agent_analysis": "子 Agent 计划：生成摘要、可复现性、改进点和亮点报告",
    "reproducibility_deep_dive": "子 Agent 计划：深挖可复现性证据与缺口",
    "method_explanation": "子 Agent 计划：拆解方法主链路和关键假设",
    "annotation_questions": "子 Agent 计划：把用户标注整理成追问清单",
    "collection_compare": "子 Agent 计划：进入专题上下文做横向比较",
}

TASK_TITLES = {
    "selection_explanation": "选区解释",
    "external_tool_request": "外部工具请求",
    "four_agent_analysis": "四 Agent 阅读报告",
    "reproducibility_deep_dive": "可复现性深挖",
    "method_explanation": "方法拆解",
    "annotation_questions": "标注问题整理",
    "collection_compare": "专题横向比较",
}

PERMISSION_LABELS = {
    "external_search": "外部检索",
    "mcp_tool": "MCP/工具调用",
    "long_task": "长任务",
    "memory_write": "保存记忆",
    "browser_control": "浏览器控制",
}

PERMISSION_DESCRIPTIONS = {
    "external_search": "需要联网访问 arXiv、Semantic Scholar 或其他外部服务。",
    "mcp_tool": "可能调用 MCP 接入的工具或本地/外部能力。",
    "long_task": "可能创建耗时较长、消耗 LLM 额度的后台任务。",
    "memory_write": "会把这条长期偏好或纠正写入本地阅读记忆。",
    "browser_control": "可能打开交互网页、点击、输入或使用当前浏览器登录态。",
}

RELATED_PAPER_HINTS = (
    "相似",
    "类似",
    "相近",
    "相关论文",
    "相关文献",
    "相关文章",
    "同类论文",
    "推荐论文",
    "推荐文章",
    "related paper",
    "related papers",
    "similar paper",
    "similar papers",
)

TITLE_QUERY_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "by",
    "for",
    "from",
    "in",
    "is",
    "of",
    "on",
    "or",
    "the",
    "through",
    "to",
    "toward",
    "towards",
    "using",
    "via",
    "with",
}


def _build_state(arxiv_id: str) -> AgentChatState:
    chat = load_chat(arxiv_id)
    messages = chat["messages"]
    if not messages:
        messages = [
            {
                "id": "welcome",
                "role": "assistant",
                "content": "你可以直接说想怎么读这篇论文：判断可复现性、解释方法、找改进点、整理标注，或和专题里的论文做对比。",
                "created_at": chat.get("updated_at") or "",
                "meta": {"kind": "welcome"},
            }
        ]
    return AgentChatState(
        arxiv_id=arxiv_id,
        messages=[AgentMessage(**item) for item in messages],
        memories=[AgentMemoryItem(**item) for item in load_memories()],
        skills=[AgentSkillItem(**item) for item in load_skills()],
        runs=[AgentRunItem(**item) for item in load_runs(arxiv_id)],
    )


def _short_text(value: object, limit: int = 260) -> str:
    text = str(value or "").replace("\n", " ").strip()
    return text[:limit]


READER_INLINE_MODE = "inline_translation"
READER_SELECTION_MODE = "selection_translation"
READER_MODES = {READER_INLINE_MODE, READER_SELECTION_MODE}
READER_PRECISE_LAYOUT_CONFIDENCE = 0.90
READER_SIDES = {"original", "translation"}
READER_RENDER_POLICIES = {"replace", "preserve", "panel_only"}


def _normalize_reader_context(context: dict) -> dict | None:
    reader = context.get("reader")
    if not isinstance(reader, dict):
        return None

    selected = reader.get("selected_text")
    selected = selected if isinstance(selected, dict) else None
    selection_side = selected.get("side") if selected else None
    if not isinstance(selection_side, str) or selection_side not in READER_SIDES:
        legacy_side = reader.get("right_pane_side")
        selection_side = (
            legacy_side
            if isinstance(legacy_side, str) and legacy_side in READER_SIDES
            else None
        )

    page_value = reader.get("page")
    page = page_value if type(page_value) is int and page_value > 0 else None
    region_value = reader.get("region_id")
    region_id = (
        region_value.strip()
        if isinstance(region_value, str) and region_value.strip()
        else None
    )
    confidence_value = reader.get("layout_confidence")
    layout_confidence = (
        float(confidence_value)
        if type(confidence_value) in (int, float) and 0 <= confidence_value <= 1
        else None
    )
    render_policy_value = reader.get("render_policy")
    render_policy = (
        render_policy_value
        if isinstance(render_policy_value, str)
        and render_policy_value in READER_RENDER_POLICIES
        else None
    )
    reader_mode = reader.get("reader_mode")
    reader_mode = reader_mode if reader_mode in READER_MODES else None

    return {
        "reader_mode": reader_mode,
        "page": page,
        "region_id": region_id,
        "layout_confidence": layout_confidence,
        "render_policy": render_policy,
        "selection_side": selection_side,
        "selected": selected,
        "active": (
            reader.get("active_block")
            if isinstance(reader.get("active_block"), dict)
            else None
        ),
        "previous": (
            reader.get("previous_block") if isinstance(reader.get("previous_block"), dict) else None
        ),
        "next": reader.get("next_block") if isinstance(reader.get("next_block"), dict) else None,
        "has_location_fields": any(
            key in reader
            for key in (
                "reader_mode",
                "page",
                "region_id",
                "layout_confidence",
                "render_policy",
            )
        ),
    }


def _reader_side_label(side: object) -> str:
    if side == "original":
        return "原文"
    if side == "translation":
        return "译文"
    return "侧别未知"


def _has_reader_location_context(reader: dict) -> bool:
    return bool(
        reader["selected"]
        or reader["active"]
        or reader["previous"]
        or reader["next"]
        or reader["has_location_fields"]
    )


def _reader_location_limitations(reader: dict) -> list[str]:
    mode = reader["reader_mode"]
    page = reader["page"]
    region_id = reader["region_id"]
    confidence = reader["layout_confidence"]
    render_policy = reader["render_policy"]
    limitations: list[str] = []
    if mode not in READER_MODES:
        limitations.append("缺少有效 reader_mode")
    if page is None:
        limitations.append("缺少 page")
    if region_id is None:
        limitations.append("缺少 region_id")
    if confidence is None:
        limitations.append("缺少 layout_confidence")
    elif confidence < READER_PRECISE_LAYOUT_CONFIDENCE:
        limitations.append(
            f"layout_confidence={confidence:.3f} 低于 {READER_PRECISE_LAYOUT_CONFIDENCE:.2f}"
        )
    if render_policy is None:
        limitations.append("缺少 render_policy")
    elif mode == READER_INLINE_MODE and render_policy != "replace":
        limitations.append(f"render_policy={render_policy}")
    return limitations


def _reader_location_notice(reader: dict) -> str:
    if not _has_reader_location_context(reader) or not _reader_location_limitations(reader):
        return ""
    return (
        "当前上下文可以用于解释选区或段落内容，但没有通过可靠的 PDF page/region "
        "定位门，无法确认它在 PDF 原页上的精准位置。"
    )


def _reader_location_section(reader: dict) -> str | None:
    if not _has_reader_location_context(reader):
        return None

    mode = reader["reader_mode"]
    page = reader["page"]
    region_id = reader["region_id"]
    confidence = reader["layout_confidence"]
    render_policy = reader["render_policy"]
    lines = [
        "阅读定位:",
        f"- reader_mode: {mode or '未提供（legacy reader context）'}",
        f"- PDF page: {page if page is not None else '未提供'}",
        f"- region_id: {_short_text(region_id, 160) if region_id else '未提供'}",
        (
            f"- layout_confidence: {confidence:.3f}"
            if confidence is not None
            else "- layout_confidence: 未提供"
        ),
        f"- render_policy: {render_policy or '未提供'}",
    ]
    limitations = _reader_location_limitations(reader)

    if limitations:
        lines.append(
            "- 定位限制: "
            + "、".join(limitations)
            + "；只能基于选区/block 内容回答，不能声称已精准定位到 PDF 原页区域。"
        )
    else:
        lines.append("- 定位可靠性: 可作为当前 PDF page/region 的定位线索。")
    return "\n".join(lines)


def _reader_context_reply(context: dict) -> str | None:
    reader = _normalize_reader_context(context)
    if reader is None:
        return None

    selected = reader["selected"]
    if selected and selected.get("text"):
        side = _reader_side_label(reader["selection_side"])
        block_index = selected.get("block_index", "?")
        location_notice = _reader_location_notice(reader)
        return (
            f"我看到了你选中的{side} #{block_index}：{_short_text(selected.get('text'))}。"
            "你可以直接问我解释这段、指出术语、翻译得更贴近原文，或让子 Agent 顺着这里查复现证据。"
            + (f" {location_notice}" if location_notice else "")
        )

    active = reader["active"]
    if active:
        original = _short_text(active.get("original"))
        translation = _short_text(active.get("translation"))
        if original or translation:
            block_index = active.get("index", "?")
            block_type = active.get("type", "block")
            body = original or translation
            extra = f" 译文线索：{translation}" if original and translation else ""
            location_notice = _reader_location_notice(reader)
            return (
                f"我当前定位在 {block_type} #{block_index}：{body}{extra}。"
                "你可以问我解释这一段、找前后文关系，或把它交给方法/复现相关子任务。"
                + (f" {location_notice}" if location_notice else "")
            )

    return None


def _has_reader_context(context: dict) -> bool:
    reader = _normalize_reader_context(context)
    if reader is None:
        return False
    selected = reader["selected"]
    return bool((selected and selected.get("text")) or reader["active"])


def _selected_text_context(context: dict) -> dict | None:
    reader = _normalize_reader_context(context)
    if reader is None:
        return None
    selected = reader["selected"]
    if selected and selected.get("text"):
        return selected
    return None


def _is_mcp_status_question(message: str) -> bool:
    text = message.casefold()
    has_mcp_subject = any(term in text for term in ("mcp", "工具", "tool"))
    if not has_mcp_subject:
        return False
    if any(
        term in text
        for term in (
            "用 mcp",
            "用mcp",
            "请使用",
            "使用 mcp",
            "使用mcp",
            "执行 mcp",
            "执行mcp",
            "工具搜索",
            "调用 mcp",
            "调用mcp",
            "用工具",
            "调用工具",
            "帮我用",
            "帮我查",
            "查这篇",
            "查论文",
            "查仓库",
            "查相关",
        )
    ):
        return False
    return any(
        term in text
        for term in (
            "接入",
            "接了",
            "配置",
            "有哪些",
            "有什么",
            "什么",
            "列表",
            "当前",
            "现在",
            "可用",
            "启用",
            "支持",
            "连了",
            "装了",
            "status",
            "configured",
            "available",
        )
    )


def _contextual_task_type(message: str, context: dict, task_type: str | None) -> str | None:
    if task_type or not _has_reader_context(context):
        return task_type
    text = message.lower()
    # 注意：不要加入"这"/"this"这类高频字，几乎所有中文消息都会命中，
    # 会把普通提问劫持成 selection_explanation 并触发 LLM Run
    keywords = (
        "解释",
        "意思",
        "看不懂",
        "说明",
        "术语",
        "翻译",
        "explain",
        "meaning",
    )
    if _selected_text_context(context) and any(keyword in text for keyword in keywords):
        return "selection_explanation"
    active_refs = (
        "这段",
        "这一段",
        "当前段",
        "当前这段",
        "这里",
        "这句",
        "这一句",
        "这句话",
        "这部分",
        "这块",
        "这个公式",
        "这个术语",
        "这个词",
        "这是什么意思",
    )
    if any(ref in text for ref in active_refs) and any(keyword in text for keyword in keywords):
        return "selection_explanation"
    return task_type


# 用户明确指着选区说话时（"这段/这句/选区/选中"），选区解释优先于方法拆解：
# "解释这段"应该解释选中的内容，而不是整篇方法；"解释这里的方法"仍走方法拆解
SELECTION_TASK_HINTS = ("这段", "这句", "选区", "选中")


def _prefer_selection_explanation(message: str, context: dict, task_type: str | None) -> str | None:
    if task_type not in (None, "method_explanation"):
        return task_type
    reader = context.get("reader") if isinstance(context.get("reader"), dict) else None
    selected = reader.get("selected_text") if isinstance(reader, dict) else None
    if not (isinstance(selected, dict) and selected.get("text")):
        return task_type
    if any(hint in message for hint in SELECTION_TASK_HINTS):
        return "selection_explanation"
    return task_type


# ── LLM 意图分类（立项文档 19.2 第 1 条：意图判定去死板化）──────────────
# 每条消息先用一次轻量 LLM 调用判定任务类型 / 权限范围 / 是否保存记忆；
# 分类失败（未配置 key、超时、坏 JSON、未知类别）时回退关键词规则管线，
# 权限不变量不变：外部检索 / MCP / 长任务仍必须先出确认卡才会执行。

INTENT_LLM_TIMEOUT_SECONDS = 6.0

# category -> (task_type, permission_scope)
INTENT_CATEGORY_ROUTES: dict[str, tuple[str | None, str | None]] = {
    "chat": (None, None),
    "mcp_status": (None, None),
    "selection_explanation": ("selection_explanation", None),
    "method_explanation": ("method_explanation", None),
    "reproducibility_deep_dive": ("reproducibility_deep_dive", None),
    "annotation_questions": ("annotation_questions", None),
    "collection_compare": ("collection_compare", None),
    "four_agent_analysis": ("four_agent_analysis", None),
    "external_search": (None, "external_search"),
    "mcp_tool": (None, "mcp_tool"),
    "long_task": (None, "long_task"),
    "mcp_config_wizard": (None, None),
}

INTENT_SYSTEM_PROMPT = (
    "你是「陪你读」论文阅读助手的意图分类器。根据用户消息、阅读上下文和最近对话，"
    "输出一个 JSON 对象，判断消息属于哪个类别、是否要保存长期偏好记忆。\n\n"
    "类别定义：\n"
    "- chat: 普通对话或提问（论文内容、通用背景、闲聊、对上一轮的追问），直接回答即可。\n"
    "- selection_explanation: 明确要求解释当前选中文字或当前段落（有选区，或明确说\"这段/这句/这里\"且有阅读上下文）。\n"
    "- method_explanation: 要求系统性拆解论文的方法/算法/模型结构（不是解释某一小段）。\n"
    "- reproducibility_deep_dive: 要求深挖论文可复现性证据（代码、数据集、超参数、硬件）。\n"
    "- annotation_questions: 要求把用户的标注/划线整理成问题清单。\n"
    "- collection_compare: 要求跨论文/专题横向比较。\n"
    "- four_agent_analysis: 要求生成整篇论文的完整阅读报告（摘要+可复现性+改进点+亮点）。\n"
    "- external_search: 需要联网或外部检索才能回答（引用数、作者主页、相关工作检索、代码仓库、"
    "读取/总结网页或 URL、最新动态、新闻博客）。\n"
    "- mcp_tool: 用户明确要求使用 MCP 或外部工具做事（\"用 MCP 查…\"）。\n"
    "- long_task: 明确要求跑一个耗时的完整后台任务（\"后台跑完整分析\"）。\n"
    "- mcp_status: 询问当前接入/配置了哪些 MCP 或工具（配置状态自省，不是要调用工具）。\n"
    "- mcp_config_wizard: 要求接入/配置/添加一个新的 MCP server（\"帮我接入 GitHub MCP\"\"配置一个论文搜索工具\"），"
    "属于产品配置操作，不是调用工具也不是询问状态。\n\n"
    "save_memory 仅当用户在陈述一条\"以后都要遵守\"的偏好或纠正（如\"以后术语保留英文\"）时为 true；"
    "普通提问即使包含\"记住/记不住/问题\"等字样也是 false。\n\n"
    "判定规则：\n"
    "- 没有选区、也没有\"这段/这句/这里\"这类明确指代时，\"啥意思\"\"不对\"\"为什么\"这类短追问一律是 chat"
    "（结合最近对话回答），不是 selection_explanation。\n"
    "- 问\"现在接入了什么 MCP/有哪些工具\"是 mcp_status；说\"用 MCP 帮我查 X\"才是 mcp_tool；"
    "说\"帮我接入/配置/添加一个 MCP\"是 mcp_config_wizard。\n"
    "- 只要回答依赖论文之外、模型也未必可靠知道的实时信息，就选 external_search。\n"
    "- 拿不准时选 chat。\n\n"
    "只输出 JSON，不要额外解释："
    '{"category": "chat", "save_memory": false, "confidence": "high|medium|low", "reason": "不超过30字"}'
)


def _intent_llm_enabled() -> bool:
    return os.environ.get("PEINIDU_LLM_INTENT", "1").strip().lower() not in ("0", "false", "off")


def _intent_reader_summary(context: dict) -> str:
    reader = _normalize_reader_context(context) or {}
    selected = reader.get("selected")
    active = reader.get("active")
    lines = []
    if selected and selected.get("text"):
        side = _reader_side_label(reader.get("selection_side"))
        lines.append(
            f"选区: 有（{side} #{selected.get('block_index', '?')} "
            f"{_short_text(selected.get('text'), 80)}）"
        )
    else:
        lines.append("选区: 无")
    if active:
        lines.append(f"当前段落: 有（#{active.get('index', '?')} {active.get('type', 'block')}）")
    else:
        lines.append("当前段落: 无")
    return "\n".join(lines)


def _build_intent_prompt(arxiv_id: str, message: str, context: dict) -> list[dict]:
    history_lines = [
        f"{item['role']}: {_short_text(item['content'], 80)}"
        for item in _chat_history_items(arxiv_id, message)[-4:]
    ]
    return [
        {"role": "system", "content": INTENT_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"阅读上下文:\n{_intent_reader_summary(context)}\n\n"
                "最近对话:\n" + ("\n".join(history_lines) or "无") + "\n\n"
                f"用户消息: {message}"
            ),
        },
    ]


async def _classify_message_llm(arxiv_id: str, message: str, context: dict) -> dict | None:
    """LLM 意图分类；任何失败返回 None，由调用方回退关键词规则。"""
    if not _intent_llm_enabled():
        return None
    try:
        raw = await asyncio.wait_for(
            get_client().acomplete(
                _build_intent_prompt(arxiv_id, message, context),
                task="agent_intent",
                variant="low",
            ),
            timeout=INTENT_LLM_TIMEOUT_SECONDS,
        )
        data = _extract_json_object(raw)
    except Exception:
        return None
    category = str(data.get("category") or "").strip()
    route = INTENT_CATEGORY_ROUTES.get(category)
    if route is None:
        return None
    task_type, permission_scope = route
    # 分类说要解释选区但阅读上下文为空时，降级为普通对话，避免空跑 Run
    if task_type == "selection_explanation" and not _has_reader_context(context):
        category, task_type = "chat", None
    confidence = str(data.get("confidence") or "medium")
    if confidence not in ("high", "medium", "low"):
        confidence = "medium"
    return {
        "category": category,
        "task_type": task_type,
        "permission_scope": permission_scope,
        "save_memory": bool(data.get("save_memory")),
        "confidence": confidence,
        "reason": _short_text(data.get("reason"), 120),
        "source": "llm_intent",
    }


def _permission_from_llm_intent(llm_intent: dict, message: str, context: dict) -> dict | None:
    scope = llm_intent.get("permission_scope")
    if not scope or scope not in PERMISSION_LABELS:
        return None
    if context.get("approved_permission") == scope:
        return None
    return {
        "scope": scope,
        "label": PERMISSION_LABELS[scope],
        "description": PERMISSION_DESCRIPTIONS[scope],
        "original_message": message,
    }


def _is_related_paper_lookup(message: str) -> bool:
    text = message.casefold()
    return any(hint in text for hint in RELATED_PAPER_HINTS)


def _title_search_terms(title: str) -> list[str]:
    # 冒号前通常是方法名/缩写，后半句更适合做宽检索。
    title_part = title.split(":", 1)[1] if ":" in title else title
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9-]*", title_part.casefold())
    terms: list[str] = []
    for token in tokens:
        for piece in token.split("-"):
            if len(piece) < 3 or piece in TITLE_QUERY_STOPWORDS:
                continue
            if piece not in terms:
                terms.append(piece)
    return terms[:10]


def _related_paper_search_query(paper_title: str, user_message: str) -> str:
    terms = _title_search_terms(paper_title)
    if terms:
        return " ".join(terms)
    return paper_title or user_message


TOOL_PLAN_TIMEOUT_SECONDS = 8.0

TOOL_PLAN_NATIVE_NAME_MAP = {
    "local_external_search": "local.external_search",
    "local_web_search": "local.web_search",
    "local_web_fetch": "local.web_fetch",
    "mcp_tool": "mcp_tool",
    "local_selection_explanation": "local.selection_explanation",
    "local_method_explanation": "local.method_explanation",
    "local_reproducibility_deep_dive": "local.reproducibility_deep_dive",
    "local_four_agent_analysis": "local.four_agent_analysis",
    "local_annotation_questions": "local.annotation_questions",
    "local_collection_compare": "local.collection_compare",
    "local_memory_save": "local.memory_save",
    "local_session_search": "local.session_search",
    "local_notes_search": "local.notes_search",
    "local_notes_view": "local.notes_view",
    "local_browser_control": "local.browser_control",
}

TOOL_PLAN_NATIVE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "local_external_search",
            "description": "学术检索计划步骤，查 arXiv / Semantic Scholar，适合相关论文、作者、引用、论文代码线索。可与同一 external_search 权限下的 web_search/web_fetch 组成最多 4 步有序计划。",
            "parameters": {
                "type": "object",
                "properties": {
                    "search_query": {"type": "string", "description": "给检索工具使用的短查询。"},
                    "query_mode": {
                        "type": "string",
                        "enum": ["paper_lookup", "related_papers"],
                        "description": "相关论文使用 related_papers，否则 paper_lookup。",
                    },
                    "lookup_targets": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": ["papers", "authors", "citation_metrics"],
                        },
                        "description": "要检索的证据类型；作者或指标必须显式选择，默认仅 papers。",
                    },
                    "author_scope": {
                        "type": "string",
                        "enum": ["none", "first_author", "paper_authors"],
                        "description": "仅在需要作者/指标证据时选择当前论文的作者范围。",
                    },
                    "author_queries": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "需要查询的明确作者姓名；优先于 author_scope。",
                    },
                    "reason": {"type": "string", "description": "为什么需要调用该工具。"},
                },
                "required": ["search_query", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "local_web_search",
            "description": "普通网页搜索计划步骤，适合 GitHub、官网、博客、新闻、复现页面。可与同一 external_search 权限下的 external_search/web_fetch 组成最多 4 步有序计划。",
            "parameters": {
                "type": "object",
                "properties": {
                    "search_query": {"type": "string", "description": "网页搜索查询。"},
                    "reason": {"type": "string", "description": "为什么需要网页搜索。"},
                },
                "required": ["search_query", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "local_web_fetch",
            "description": "网页读取计划步骤，适合读取用户给出的 URL。可与同一 external_search 权限下的 web_search/external_search 组成最多 4 步有序计划；不要为未知搜索结果编造 URL。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "要读取的 http/https URL。"},
                    "reason": {"type": "string", "description": "为什么需要读取该 URL。"},
                },
                "required": ["url", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mcp_tool",
            "description": "MCP/外部工具计划步骤。仅当用户明确要求 MCP 或已配置外部工具时使用；不要与 external_search 权限工具混用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "传给 MCP 工具的用户请求。"},
                    "reason": {"type": "string", "description": "为什么需要 MCP/工具服务。"},
                },
                "required": ["query", "reason"],
            },
        },
    },
]

LOCAL_AGENT_TASK_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "local_selection_explanation",
            "description": "解释用户当前划选文本或当前论文段落。只读本地论文上下文，不需要额外权限。",
            "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "local_method_explanation",
            "description": "系统拆解当前论文的方法、算法主链路和关键假设。会调用 LiteLLM 分析，因此需要 long_task 确认。",
            "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "local_reproducibility_deep_dive",
            "description": "深挖论文的代码、数据集、超参数和硬件复现证据。会调用 LiteLLM 分析，因此需要 long_task 确认。",
            "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "local_four_agent_analysis",
            "description": "用户明确要求完整四 Agent、摘要+可复现性+改进点+亮点综合报告时必须调用。成本较高，必须 long_task 确认。",
            "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "local_annotation_questions",
            "description": "把当前论文的用户标注整理成可继续追问的问题。只读本地标注和使用 LiteLLM 整理，不需要外部权限。",
            "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "local_collection_compare",
            "description": "比较当前论文所在专题内的多篇论文。会调用 LiteLLM 生成横向结论，因此需要 long_task 确认。",
            "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}},
        },
    },
]

LOCAL_MEMORY_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "local_memory_save",
            "description": (
                "保存用户明确要求以后持续遵守的阅读偏好、纠正或判断标准。"
                "调用后必须等待用户确认；不要把临时问题、论文事实或一次性任务保存为长期记忆。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "独立、简洁、可长期复用的记忆内容。"},
                    "kind": {
                        "type": "string",
                        "enum": ["preference", "correction", "criterion"],
                        "description": "偏好、纠正或判断标准。",
                    },
                    "reason": {"type": "string", "description": "为什么这是需要长期保存的记忆。"},
                },
                "required": ["content", "kind", "reason"],
            },
        },
    }
]

LOCAL_SESSION_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "local_session_search",
            "description": (
                "搜索用户与 Pet 以前在其他论文会话中的正常讨论。"
                "仅当用户明确询问之前讨论过什么、在哪篇论文聊过某个主题时使用；"
                "普通论文问题不要调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "要在历史讨论中查找的关键词或短语。"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                "required": ["query"],
            },
        },
    }
]

LOCAL_NOTE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "local_notes_search",
            "description": (
                "只读搜索当前论文，或用户明确指定的当前专题中，亲自保存的整篇 Markdown "
                "笔记、选区笔记和语义高亮。"
                "结果是“你的笔记”，不是论文事实；本地检索不需要额外权限。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "要在当前论文笔记中查找的关键词；需要概览全部笔记时传空字符串。",
                    },
                    "scope": {
                        "type": "string",
                        "enum": ["current_paper", "current_collection"],
                        "description": "默认当前论文；只有用户明确要求比较当前专题笔记时才使用 current_collection。",
                    },
                    "collection_id": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "专题页已提供时使用；缺失时从当前论文所在专题解析。",
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 10},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "local_notes_view",
            "description": (
                "只读查看一条选区笔记或整篇论文 Markdown 的一个标题章节。"
                "只能读取 local_notes_search 返回的 annotation_id 或 heading。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "arxiv_id": {
                        "type": "string",
                        "description": "检索结果所属论文；省略时读取当前论文。",
                    },
                    "annotation_id": {"type": "string"},
                    "heading": {"type": "string"},
                },
            },
        },
    },
]

LOCAL_SKILL_TOOLS = [
    {"type": "function", "function": {"name": "local_skills_list", "description": "列出已生效的阅读 skill；只读。", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "local_skill_view", "description": "查看一个已生效 skill 的触发条件和步骤；只读。", "parameters": {"type": "object", "properties": {"skill_id": {"type": "string"}}, "required": ["skill_id"]}}},
    {"type": "function", "function": {"name": "local_skill_propose", "description": "仅当复杂任务形成可复用流程时提出 skill create/update 提案。不会生效 skill，必须等用户批准。", "parameters": {"type": "object", "properties": {"action": {"type": "string", "enum": ["create", "update"]}, "id": {"type": "string"}, "name": {"type": "string"}, "description": {"type": "string"}, "trigger": {"type": "string"}, "task_type": {"type": "string"}, "trigger_keywords": {"type": "array", "items": {"type": "string"}}, "steps": {"type": "array", "items": {"type": "string"}}}, "required": ["action", "name", "description", "trigger", "steps"]}}},
]

LOCAL_BROWSER_TOOLS = [
    {"type": "function", "function": {"name": "local_browser_control", "description": "用户要求打开并继续点击、输入、按键、登录态交互或读取交互后页面时必须使用；普通网页搜索不能代替本工具。任何此类动作都需要 browser_control 确认。", "parameters": {"type": "object", "properties": {"action": {"type": "string", "enum": ["open", "snapshot", "click", "fill", "press"]}, "url": {"type": "string"}, "target": {"type": "string"}, "text": {"type": "string"}, "reason": {"type": "string"}}, "required": ["action", "reason"]}}},
]

TOOL_PLAN_SYSTEM_PROMPT = (
    "你是 Hermes 风格的后端请求计划器。你的任务不是回答用户，而是判断 Pet 下一步应该"
    "直接回答、请求权限后调用工具，还是交给后台任务。输出必须是 JSON object。\n\n"
    "可用工具：\n"
    "- local.external_search: 学术检索，查 arXiv / Semantic Scholar，适合相关论文、作者、引用、论文代码线索。\n"
    "- local.web_search: 普通网页搜索，适合 GitHub、官网、博客、新闻、复现页面。\n"
    "- local.web_fetch: 读取用户给出的 URL。\n"
    "- mcp_tool: 用户明确要求 MCP 或已配置外部工具时使用。\n\n"
    "计划规则：\n"
    "- tool_request 必须给出 1-4 个有序 tool_calls；后端会按顺序校验执行。\n"
    "- 同一个 tool_calls 计划只能使用一个 permission_scope：external_search 工具不能和 mcp_tool 混在一起。\n"
    "- 用户给了具体 URL 时用 local.web_fetch；没有 URL 时不要编造 fetch URL，可先 local.web_search。\n"
    "- 需要“查相关论文 + 找代码/复现仓库”时，可在 external_search scope 下规划 local.external_search 后接 local.web_search。\n"
    "- 不确定时选择最少步骤，优先 1-2 步；不要为了显得复杂而加工具。\n\n"
    "权限边界：\n"
    "- 任何外部联网检索都必须 permission_scope=external_search。\n"
    "- 明确使用 MCP/工具服务时 permission_scope=mcp_tool。\n"
    "- 纯论文内容问答 action=chat，不要请求权限。\n"
    "- 查相似论文/相关论文/related papers 必须 action=tool_request, tool_name=local.external_search, "
    "query_mode=related_papers，并说明需要联网查相关论文。\n\n"
    "JSON schema:\n"
    "{"
    '"action":"chat|tool_request|background_task",'
    '"permission_scope":"external_search|mcp_tool|long_task|null",'
    '"tool_name":"local.external_search|local.web_search|local.web_fetch|mcp_tool|null",'
    '"query_mode":"paper_lookup|related_papers|web_search|web_fetch|mcp_tool|null",'
    '"search_query":"给工具用的短查询，可为空",'
    '"tool_calls":[{"tool_name":"local.external_search|local.web_search|local.web_fetch|mcp_tool","arguments":{},"reason":"为什么调用，按执行顺序排列"}],'
    '"user_facing_reason":"给用户看的权限理由，不超过60字",'
    '"confidence":"high|medium|low"'
    "}\n"
    "只输出 JSON，不要解释。"
)


def _build_tool_plan_prompt(arxiv_id: str, message: str, context: dict) -> list[dict]:
    doc = files.load_document(arxiv_id)
    title = str(context.get("paper_title") or (doc.title if doc else arxiv_id))
    authors = ", ".join(_context_authors(context)) or "unknown"
    history_lines = [
        f"{item['role']}: {_short_text(item['content'], 90)}"
        for item in _chat_history_items(arxiv_id, message)[-4:]
    ]
    return [
        {"role": "system", "content": TOOL_PLAN_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"当前论文标题: {title}\n"
                f"arXiv: {arxiv_id}\n"
                f"作者: {authors}\n"
                f"阅读上下文:\n{_intent_reader_summary(context)}\n\n"
                "最近对话:\n" + ("\n".join(history_lines) or "无") + "\n\n"
                f"用户消息: {message}"
            ),
        },
    ]


def _normalize_tool_plan(data: dict, message: str, context: dict) -> dict | None:
    action = str(data.get("action") or "chat").strip()
    if action not in ("chat", "tool_request", "background_task"):
        return None
    scope_raw = data.get("permission_scope")
    scope = str(scope_raw).strip() if scope_raw is not None else ""
    if scope in ("", "none", "null", "None"):
        scope = ""
    if scope and scope not in PERMISSION_LABELS:
        return None
    tool_name_raw = data.get("tool_name")
    tool_name = str(tool_name_raw).strip() if tool_name_raw is not None else ""
    if tool_name in ("", "none", "null", "None"):
        tool_name = ""
    allowed_tools = {"local.external_search", "local.web_search", "local.web_fetch", "mcp_tool"}
    if tool_name and tool_name not in allowed_tools:
        return None
    query_mode_raw = data.get("query_mode")
    query_mode = str(query_mode_raw).strip() if query_mode_raw is not None else ""
    if query_mode in ("", "none", "null", "None"):
        query_mode = ""
    if _is_related_paper_lookup(message):
        action = "tool_request"
        scope = "external_search"
        tool_name = "local.external_search"
        query_mode = "related_papers"
    if action != "tool_request" and scope in ("external_search", "mcp_tool"):
        action = "tool_request"
    confidence = str(data.get("confidence") or "medium").strip()
    if confidence not in ("high", "medium", "low"):
        confidence = "medium"
    paper_title = str(context.get("paper_title") or "")
    search_query = _short_text(data.get("search_query"), 220)
    if query_mode == "related_papers" and not search_query:
        search_query = _related_paper_search_query(paper_title, message)
    tool_calls: list[dict] = []
    raw_tool_calls = data.get("tool_calls")

    def call_scope(call_tool: str) -> str:
        return "mcp_tool" if call_tool == "mcp_tool" else "external_search"

    raw_call_scopes: set[str] = set()
    if isinstance(raw_tool_calls, list):
        for raw_call in raw_tool_calls[:4]:
            if not isinstance(raw_call, dict):
                continue
            call_tool = str(raw_call.get("tool_name") or raw_call.get("name") or raw_call.get("tool") or "").strip()
            if call_tool in allowed_tools:
                raw_call_scopes.add(call_scope(call_tool))
        if len(raw_call_scopes) > 1:
            return None
        if scope and raw_call_scopes and scope not in raw_call_scopes:
            return None

    if isinstance(raw_tool_calls, list):
        for raw_call in raw_tool_calls[:4]:
            if not isinstance(raw_call, dict):
                continue
            call_tool = str(raw_call.get("tool_name") or raw_call.get("name") or raw_call.get("tool") or "").strip()
            if call_tool in ("", "none", "null", "None"):
                continue
            if call_tool not in allowed_tools:
                continue
            call_arguments = raw_call.get("arguments")
            if not isinstance(call_arguments, dict):
                call_arguments = {}
            if scope and call_scope(call_tool) != scope:
                continue
            tool_calls.append(
                {
                    "tool_name": call_tool,
                    "arguments": call_arguments,
                    "reason": _short_text(raw_call.get("reason"), 160),
                }
            )
    if action == "tool_request" and raw_tool_calls and not tool_calls:
        return None
    if tool_calls:
        first_call = tool_calls[0]
        first_tool = str(first_call.get("tool_name") or "")
        first_arguments = first_call.get("arguments") if isinstance(first_call.get("arguments"), dict) else {}
        if not scope:
            scope = call_scope(first_tool)
        if not tool_name:
            tool_name = first_tool
        if not query_mode:
            query_mode = _short_text(first_arguments.get("query_mode"), 80)
        if not search_query:
            search_query = _short_text(first_arguments.get("search_query") or first_arguments.get("query"), 220)
    if action == "tool_request":
        if not scope:
            return None
        if not tool_name:
            tool_name = "mcp_tool" if scope == "mcp_tool" else "local.external_search"
    if action == "tool_request" and not tool_calls and tool_name:
        arguments: dict[str, Any] = {}
        if search_query:
            arguments["search_query"] = search_query
        if query_mode:
            arguments["query_mode"] = query_mode
        tool_calls.append(
            {
                "tool_name": tool_name,
                "arguments": arguments,
                "reason": "执行已确认的工具请求",
            }
        )
    reason = _short_text(data.get("user_facing_reason"), 160)
    if action == "tool_request" and not reason:
        if scope == "external_search":
            reason = "需要联网检索外部资料后才能可靠回答。"
        elif scope == "mcp_tool":
            reason = "需要调用已配置的 MCP/工具能力后才能回答。"
    return {
        "action": action,
        "permission_scope": scope or None,
        "tool_name": tool_name or None,
        "query_mode": query_mode or None,
        "search_query": search_query,
        "tool_calls": tool_calls,
        "user_facing_reason": reason,
        "confidence": confidence,
        "source": "llm_tool_plan",
    }


def _native_tool_call_arguments(tool_name: str, raw_arguments: dict) -> dict:
    if tool_name == "local.web_search":
        search_query = _short_text(raw_arguments.get("search_query") or raw_arguments.get("query"), 220)
        arguments = {"query": search_query} if search_query else {}
        if search_query:
            arguments["search_query"] = search_query
        return arguments
    if tool_name == "local.web_fetch":
        url = _short_text(raw_arguments.get("url"), 500)
        return {"url": url} if url else {}
    if tool_name == "local.external_search":
        arguments: dict[str, Any] = {}
        search_query = _short_text(raw_arguments.get("search_query") or raw_arguments.get("query"), 220)
        query_mode = _short_text(raw_arguments.get("query_mode"), 80)
        if search_query:
            arguments["search_query"] = search_query
        if query_mode:
            arguments["query_mode"] = query_mode
        return arguments
    return {key: value for key, value in raw_arguments.items() if key != "reason"}


def _tool_plan_from_native_tool_calls(response: dict, message: str, context: dict) -> dict | None:
    raw_calls = response.get("tool_calls")
    if not isinstance(raw_calls, list) or not raw_calls:
        return None
    calls: list[dict] = []
    scopes: set[str] = set()
    for raw_call in raw_calls[:4]:
        if not isinstance(raw_call, dict):
            continue
        native_name = str(raw_call.get("name") or "").strip()
        tool_name = TOOL_PLAN_NATIVE_NAME_MAP.get(native_name)
        if not tool_name:
            continue
        raw_arguments = raw_call.get("arguments")
        if not isinstance(raw_arguments, dict):
            raw_arguments = {}
        scope = "mcp_tool" if tool_name == "mcp_tool" else "external_search"
        scopes.add(scope)
        calls.append(
            {
                "tool_name": tool_name,
                "arguments": _native_tool_call_arguments(tool_name, raw_arguments),
                "reason": _short_text(raw_arguments.get("reason"), 160),
            }
        )
    if not calls or len(scopes) != 1:
        return None
    scope = next(iter(scopes))
    first = calls[0]
    tool_name = str(first.get("tool_name") or "")
    first_arguments = first.get("arguments") if isinstance(first.get("arguments"), dict) else {}
    query_mode = _short_text(first_arguments.get("query_mode"), 80)
    search_query = _short_text(first_arguments.get("search_query") or first_arguments.get("query"), 220)
    reason = _short_text(first.get("reason"), 160)
    return _normalize_tool_plan(
        {
            "action": "tool_request",
            "permission_scope": scope,
            "tool_name": tool_name,
            "query_mode": query_mode,
            "search_query": search_query,
            "tool_calls": calls,
            "user_facing_reason": reason or "需要调用工具后才能可靠回答。",
            "confidence": "high",
        },
        message,
        context,
    )


async def _plan_agent_action_llm(arxiv_id: str, message: str, context: dict) -> dict | None:
    if not _intent_llm_enabled():
        return None
    client = get_client()
    try:
        complete_with_tools = getattr(client, "acomplete_with_tools", None)
        if complete_with_tools is not None:
            native_response = await asyncio.wait_for(
                complete_with_tools(
                    _build_tool_plan_prompt(arxiv_id, message, context),
                    tools=TOOL_PLAN_NATIVE_TOOLS,
                    task="agent_intent",
                    variant="low",
                    tool_choice="auto",
                ),
                timeout=TOOL_PLAN_TIMEOUT_SECONDS,
            )
            native_plan = _tool_plan_from_native_tool_calls(native_response, message, context)
            if native_plan is not None:
                native_plan["source"] = "native_tool_call"
                return native_plan
    except Exception:
        pass
    try:
        raw = await asyncio.wait_for(
            client.acomplete(
                _build_tool_plan_prompt(arxiv_id, message, context),
                task="agent_intent",
                variant="low",
            ),
            timeout=TOOL_PLAN_TIMEOUT_SECONDS,
        )
        data = _extract_json_object(raw)
        return _normalize_tool_plan(data, message, context)
    except Exception:
        return None


def _should_use_tool_planner(message: str, context: dict) -> bool:
    if context.get("approved_permission") in ("external_search", "mcp_tool"):
        return True
    if _is_mcp_config_request(message) or _is_mcp_status_question(message):
        return False
    text = message.casefold()
    if _is_related_paper_lookup(message):
        return True
    if re.search(r"https?://", text):
        return True
    tool_needles = (
        "外部",
        "联网",
        "检索",
        "搜索",
        "查相关",
        "相关工作",
        "相关论文",
        "相关文献",
        "github",
        "repository",
        "repo",
        "代码仓库",
        "arxiv",
        "semantic scholar",
        "google scholar",
        "谷歌学术",
        "网页",
        "官网",
        "博客",
        "新闻",
        "外面",
        "有没有人复现",
        "复现成功",
        "引用数",
        "被引",
        "citation",
        "citations",
        "h-index",
        "h index",
        "用 mcp",
        "用mcp",
        "调用 mcp",
        "调用mcp",
        "调用工具",
    )
    if any(term in text for term in tool_needles):
        return True
    if any(term in text for term in ("查一下", "查找", "查资料", "帮我查", "找一下")):
        return any(
            target in text
            for target in (
                "作者",
                "机构",
                "单位",
                "主页",
                "最新",
                "代码",
                "论文",
                "文献",
                "仓库",
                "外面",
            )
        )
    return False


def _permission_from_tool_plan(tool_plan: dict | None, message: str, context: dict) -> dict | None:
    if not tool_plan or tool_plan.get("action") != "tool_request":
        return None
    scope = tool_plan.get("permission_scope")
    if not scope or scope not in PERMISSION_LABELS:
        return None
    if context.get("approved_permission") == scope:
        return None
    return {
        "scope": scope,
        "label": PERMISSION_LABELS[scope],
        "description": PERMISSION_DESCRIPTIONS[scope],
        "original_message": message,
        "reason": tool_plan.get("user_facing_reason") or "",
        "tool_plan": tool_plan,
    }


def _permission_request(message: str, context: dict) -> dict | None:
    text = message.lower()
    scope = None
    if _is_mcp_status_question(message):
        return None
    if any(keyword in text for keyword in ("mcp", "msp", "工具", "tool", "调用工具")):
        scope = "mcp_tool"
    elif any(
        keyword in text
        for keyword in (
            "外部",
            "联网",
            "检索",
            "搜索",
            "网络搜索",
            "网页搜索",
            "网上搜",
            "搜网页",
            "查相关",
            "相关工作",
            "arxiv",
            "semantic scholar",
            "google scholar",
            "谷歌学术",
            "引用数",
            "被引",
            "citation",
            "citations",
            "h-index",
            "h index",
            "web search",
            "browser",
            "fetch",
            "浏览器",
            "官网",
            "网页",
            "链接",
            "url",
            "http://",
            "https://",
            "打开网页",
            "读取网页",
            "总结网页",
            "新闻",
            "博客",
            "相关论文",
            "相关文献",
            "相似论文",
            "相似文章",
            "类似论文",
            "类似文章",
            "related papers",
            "similar papers",
        )
    ) or (
        any(keyword in text for keyword in ("查一下", "查找", "查资料", "帮我查", "找一下"))
        and any(
            target in text
            for target in (
                "代码仓库",
                "代码",
                "github",
                "repository",
                "作者",
                "主页",
                "最新",
                "网页",
                "官网",
                "新闻",
                "博客",
                "网络",
                "外部",
                "相关工作",
                "相关论文",
                "相关文献",
                "相似",
                "类似",
                "相关文章",
                "同类论文",
                "推荐论文",
            )
        )
    ) or (
        any(keyword in text for keyword in ("一作", "第一作者", "作者", "机构", "单位"))
        and any(keyword in text for keyword in ("引用", "被引", "citation", "h-index", "h index"))
    ):
        scope = "external_search"
    elif any(keyword in text for keyword in ("长任务", "后台跑", "跑完整", "全部分析")):
        scope = "long_task"

    if scope is None:
        return None
    if context.get("approved_permission") == scope:
        return None
    return {
        "scope": scope,
        "label": PERMISSION_LABELS[scope],
        "description": PERMISSION_DESCRIPTIONS[scope],
        "original_message": message,
    }


def _permission_confirmation_message(permission: dict) -> str:
    scope = permission.get("scope")
    original_message = str(permission.get("original_message") or "")
    reason = _short_text(permission.get("reason"), 160)
    if reason:
        suffix = "你确认后，我再执行并把依据整理回来。"
        return f"可以做。{reason}{suffix}"
    if scope == "external_search" and _is_related_paper_lookup(original_message):
        return (
            "可以做。我需要联网到 arXiv / Semantic Scholar 查相似论文；"
            "你确认后，我会排除当前论文，把候选和依据整理回来。"
        )
    if scope == "external_search":
        return (
            "可以做，但需要联网检索外部资料。"
            "你确认后，我会把查到的来源和依据整理回来。"
        )
    if scope == "mcp_tool":
        return "这个请求需要确认：MCP/工具调用。你确认后，我再执行并把结果整理回来。"
    if scope == "long_task":
        return "这个请求会创建一个较长的后台任务。你确认后，我再开始执行。"
    return f"这个请求需要确认：{permission['label']}。{permission['description']}确认后我再执行。"


def _paper_opening(arxiv_id: str) -> str:
    doc = files.load_document(arxiv_id)
    blocks = doc.blocks if doc else []
    fallback = ""
    for block in blocks:
        if block.type != "paragraph" or not block.original.strip():
            continue
        text = block.original.replace("\n", " ").strip()
        if _is_layout_junk_text(text):
            continue
        if not fallback:
            fallback = text
        if len(text) >= 40 and text.count("\\") < 3:
            return text[:260]
    return fallback[:260]


def _is_layout_junk_text(text: str) -> bool:
    clean = text.strip()
    if not clean:
        return True
    # 跳过混入正文的排版指令碎片（如 "0.1pt \\contournumber 10"）
    if "\\" in clean and len(clean) < 40:
        return True
    return False


def _pdf_front_page_text(arxiv_id: str) -> str:
    """Best-effort first-page text for author/affiliation metadata questions."""
    if shutil.which("pdftotext") is None:
        return ""
    pdf_path = files.paper_dir(arxiv_id) / "original.pdf"
    if not pdf_path.exists():
        return ""
    try:
        result = subprocess.run(
            ["pdftotext", "-f", "1", "-l", "1", "-layout", str(pdf_path), "-"],
            check=True,
            capture_output=True,
            text=True,
            timeout=4,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    text = re.sub(r"\n{3,}", "\n\n", result.stdout).strip()
    return text[:2400]


def _context_authors(context: dict) -> list[str]:
    authors = context.get("paper_authors")
    if not isinstance(authors, list):
        return []
    return [str(author).strip() for author in authors if str(author).strip()]


def _paper_metadata_context(arxiv_id: str, context: dict) -> str:
    authors = _context_authors(context)
    lines = []
    if authors:
        lines.append("作者元数据: " + ", ".join(authors))
    front_page = _pdf_front_page_text(arxiv_id)
    if front_page:
        lines.append("PDF 首页文字线索:\n" + front_page)
    return "\n\n".join(lines)


def _metadata_fallback_reply(arxiv_id: str, message: str, context: dict) -> str | None:
    text = message.lower()
    is_metadata_question = any(
        keyword in text
        for keyword in (
            "作者",
            "谁写",
            "谁写的",
            "机构",
            "单位",
            "哪家",
            "学校",
            "affiliation",
            "institution",
            "author",
        )
    )
    if not is_metadata_question:
        return None

    authors = _context_authors(context)
    front_page = _pdf_front_page_text(arxiv_id)
    org_candidates: list[str] = []
    for raw_line in front_page.splitlines():
        line = re.sub(r"\s{2,}", " ", raw_line).strip()
        if not line or "@" in line:
            continue
        if re.search(r"\b(university|institute|college|lab|labs|research|kuleuven|ku leuven)\b", line, re.I):
            if line not in org_candidates:
                org_candidates.append(line)

    parts = []
    if authors:
        parts.append("作者：" + "、".join(authors[:8]) + (" 等" if len(authors) > 8 else ""))
    if org_candidates:
        parts.append("首页机构线索：" + "；".join(org_candidates[:4]))
    if parts:
        return "；".join(parts) + "。"
    return "当前论文元数据里没有可靠的作者或机构字段，PDF 首页文字也没有提取到可用机构线索。"


def _known_institution_background(name: str) -> str | None:
    normalized = name.lower()
    if "ku leuven" in normalized or "kuleuven" in normalized or "鲁汶大学" in normalized:
        return (
            "基于通用背景，KU Leuven（比利时鲁汶大学）是欧洲历史很久、研究实力很强的综合性大学，"
            "在工程、计算机科学、HCI、生命科学等方向都有较活跃的研究产出。"
            "这不是论文内部证据，而是学校背景介绍；如果你要看最新排名或该团队具体水平，最好再做外部检索确认。"
        )
    return None


def _institution_background_reply(arxiv_id: str, message: str, context: dict) -> str | None:
    text = message.lower()
    if not any(
        keyword in text
        for keyword in (
            "这个大学",
            "这所大学",
            "大学怎么样",
            "学校怎么样",
            "机构怎么样",
            "ku leuven",
            "鲁汶大学",
            "好不好",
            "水平",
        )
    ):
        return None

    haystack = "\n".join(
        part
        for part in (
            message,
            " ".join(_context_authors(context)),
            _pdf_front_page_text(arxiv_id),
        )
        if part
    )
    return _known_institution_background(haystack)


def _mcp_server_purpose(name: str, tool_name: str) -> str:
    key = f"{name} {tool_name}".casefold()
    if "playwright" in key or "browser_" in key:
        return "在独立浏览器 profile 中执行需要确认的多步网页交互"
    if "github" in key or "gitmcp" in key or "search_repositories" in key:
        return "查论文复现仓库、代码仓库、issue/PR 等 GitHub 线索"
    if "paper" in key or "论文" in key:
        return "查本地论文索引、相关工作和文献线索"
    return "提供 MCP 工具能力"


def _mcp_env_note(server) -> str:
    raw = " ".join([server.command, *server.args])
    env_names = sorted(set(re.findall(r"\b[A-Z][A-Z0-9_]{2,}\b", raw)))
    if not env_names:
        return ""
    states = [
        f"{name}={'已设置' if os.environ.get(name) else '未设置'}"
        for name in env_names
    ]
    return "；环境变量：" + "，".join(states)


def _mcp_status_reply_text() -> str:
    config = get_config()
    servers = list(config.mcp_servers)
    if not servers:
        return "当前配置里没有登记 MCP server。真正需要接入 MCP 时，要先在设置页或 `config/config.yaml` 里添加 server。"

    enabled = [server for server in servers if server.enabled]
    disabled = [server for server in servers if not server.enabled]
    lines = [
        f"当前配置里有 {len(servers)} 个 MCP server，其中 {len(enabled)} 个启用。"
    ]
    if enabled:
        lines.append("已启用：")
        for server in enabled:
            tool = server.tool_name or "自动选择 tools/list 里的工具"
            purpose = _mcp_server_purpose(server.name, tool)
            lines.append(
                f"- `{server.name}`：{purpose}；transport={server.transport}；tool={tool}{_mcp_env_note(server)}。"
            )
    if disabled:
        disabled_names = "、".join(f"`{server.name}`" for server in disabled[:6])
        lines.append(f"未启用：{disabled_names}。")
    lines.append("这是配置状态说明，不会真的调用 MCP；当你让我“用 MCP 查论文/仓库”时，我仍会先要权限确认。")
    return "\n".join(lines)


def _mcp_status_reply(message: str) -> str | None:
    if not _is_mcp_status_question(message):
        return None
    return _mcp_status_reply_text()


# ── Pet MCP 配置向导（2026-07-05 登记任务）────────────────────────
# 草稿只从内置目录确定性生成，不让 LLM 编造 command；确认写入永不静默启用。

MCP_WIZARD_URL_RE = re.compile(r"https?://[^\s一-鿿\"'）】]+", re.I)
GITHUB_REPO_URL_RE = re.compile(
    r"https?://(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)",
    re.I,
)
GITHUB_REPO_SLUG_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)(?![A-Za-z0-9_.-])"
)
PLAYWRIGHT_MCP_ALLOWED_TOOLS = [
    "browser_navigate",
    "browser_snapshot",
    "browser_click",
    "browser_type",
    "browser_press_key",
    "browser_wait_for",
]


def _is_mcp_config_request(message: str) -> bool:
    """窄关键词兜底：明确的"帮我接入/配置一个 MCP"诉求；状态自省与工具调用不算。"""
    text = message.casefold()
    if not any(term in text for term in ("mcp", "工具服务", "tool server")):
        return False
    return any(
        term in text
        for term in (
            "帮我接入",
            "帮我配置",
            "帮我添加",
            "帮我装",
            "接入一个",
            "配置一个",
            "添加一个",
            "新增一个",
            "装一个",
            "接一个",
            "接个",
        )
    )


def _unique_mcp_server_name(base_name: str) -> str:
    existing = {server.name for server in get_config().mcp_servers}
    if base_name not in existing:
        return base_name
    suffix = 2
    while f"{base_name}-{suffix}" in existing:
        suffix += 1
    return f"{base_name}-{suffix}"


def _gitmcp_repo_from_message(message: str) -> tuple[str, str] | None:
    match = GITHUB_REPO_URL_RE.search(message)
    if match is None and any(
        term in message.casefold() for term in ("gitmcp", "github", "仓库", "repo")
    ):
        match = GITHUB_REPO_SLUG_RE.search(message)
    if match is None:
        return None
    owner = match.group(1).strip(". ")
    repo = match.group(2).rstrip(".,;，。；").removesuffix(".git").strip(". ")
    if not owner or not repo or owner in {".", ".."} or repo in {".", ".."}:
        return None
    return owner, repo


def _build_mcp_config_draft(message: str) -> dict:
    """从内置目录选一份 MCP server 配置草稿：本地论文搜索 / GitHub 仓库 / 用户给的 http URL。"""
    text = message.casefold()
    url_match = MCP_WIZARD_URL_RE.search(message)
    gitmcp_repo = _gitmcp_repo_from_message(message)
    if "playwright" in text or ("浏览器" in message and "mcp" in text):
        base_name = "playwright-official"
        server = {
            "name": base_name,
            "transport": "http",
            "command": "",
            "args": [],
            "url": "http://browser:8931/mcp",
            "enabled": False,
            "tool_name": "",
            "timeout_seconds": 60.0,
            "permission_scopes": ["browser_control"],
            "allowed_tools": list(PLAYWRIGHT_MCP_ALLOWED_TOOLS),
        }
        note = "先用 docker compose --profile browser 启动可选服务；仅暴露必要动作，启用后每个 Run 首次调用需确认浏览器控制权限。"
    elif gitmcp_repo:
        owner, repo = gitmcp_repo
        safe_owner = re.sub(r"[^a-z0-9-]+", "-", owner.casefold()).strip("-")
        safe_repo = re.sub(r"[^a-z0-9-]+", "-", repo.casefold()).strip("-")
        base_name = f"gitmcp-{safe_owner}-{safe_repo}"[:80]
        server = {
            "name": base_name,
            "transport": "http",
            "command": "",
            "args": [],
            "url": f"https://gitmcp.io/{owner}/{repo}",
            "enabled": False,
            "tool_name": "",
            "timeout_seconds": 20.0,
            "permission_scopes": ["mcp_tool"],
        }
        note = "GitMCP 公开只读仓库，无需 Token；私有仓库请改用需要认证的官方 GitHub MCP。"
    elif url_match:
        base_name = "custom-http-mcp"
        server = {
            "name": base_name,
            "transport": "http",
            "command": "",
            "args": [],
            "url": url_match.group(0).rstrip(".,;，。；"),
            "enabled": False,
            "tool_name": "",
            "timeout_seconds": 12.0,
            "permission_scopes": ["mcp_tool"],
        }
        note = "Streamable HTTP MCP：支持 initialize、session header、JSON 与 SSE 响应。"
    elif any(term in text for term in ("github", "仓库", "repo", "代码")):
        base_name = "github-official"
        server = {
            "name": base_name,
            "transport": "stdio",
            "command": "docker",
            "args": [
                "run",
                "-i",
                "--rm",
                "-e",
                "GITHUB_PERSONAL_ACCESS_TOKEN",
                "ghcr.io/github/github-mcp-server",
            ],
            "url": None,
            "enabled": False,
            "tool_name": "search_repositories",
            "timeout_seconds": 20.0,
            "permission_scopes": ["mcp_tool"],
        }
        note = (
            "需要本机 Docker 和环境变量 GITHUB_PERSONAL_ACCESS_TOKEN"
            "（在后端进程环境或 .env 配置后重启，不要写进仓库配置）。"
        )
    else:
        base_name = "local-paper-search"
        server = {
            "name": base_name,
            "transport": "stdio",
            "command": "python",
            "args": ["-m", "backend.tools.mcp_search_server"],
            "url": None,
            "enabled": False,
            "tool_name": "paper_search",
            "timeout_seconds": 12.0,
            "permission_scopes": ["mcp_tool"],
        }
        note = "本地论文搜索 MCP（arXiv + Semantic Scholar 双源），无需额外依赖。"

    server["name"] = _unique_mcp_server_name(base_name)
    location = server["url"] if server["transport"] == "http" else " ".join(
        [server["command"], *server["args"]]
    )
    reply = (
        "我准备了一份 MCP 配置草稿：\n"
        f"- 名称：`{server['name']}`\n"
        f"- transport：{server['transport']}\n"
        f"- 入口：`{location}`\n"
        f"- 默认工具：{server['tool_name'] or '自动从 tools/list 选择'}\n"
        "- 初始状态：不启用\n"
        f"{note}\n"
        "点「确认写入」我会把它加进 config.yaml（保持未启用，也不会执行任何命令）；"
        "然后去设置页测试连接，确认可用后再启用。"
    )
    return {"server": server, "note": note, "reply": reply}


def _is_insufficient_reply(reply: str) -> bool:
    return any(
        phrase in reply
        for phrase in (
            "无法判断",
            "无法直接回答",
            "证据不足",
            "没有足够信息",
            "没有对该校",
            "没有相关信息",
            "另行搜索",
        )
    )


def _paper_context_reply(arxiv_id: str, context: dict) -> str:
    reader_reply = _reader_context_reply(context)
    if reader_reply:
        return reader_reply

    doc = files.load_document(arxiv_id)
    title = str(context.get("paper_title") or (doc.title if doc else arxiv_id))
    preview = _paper_opening(arxiv_id)
    if preview:
        return (
            f"我在当前论文《{title}》里。你可以直接问总结、方法解释、可复现性、改进点或亮点。"
            f"开头线索：{preview}"
        )
    return f"我在当前论文《{title}》里。你可以直接问总结、方法解释、可复现性、改进点或亮点。"


# 普通对话带入的最近消息条数（在当前消息之前）
CHAT_HISTORY_WINDOW = 12
CHAT_HISTORY_ITEM_CHARS = 400
CHAT_MEMORY_LIMIT = 8
CHAT_MEMORY_ITEM_CHARS = 500
KEY_CONTEXT_BLOCKS_PER_SECTION = 4
KEY_CONTEXT_BLOCK_CHARS = 700
RELATED_CONTEXT_BLOCK_LIMIT = 8
NOTES_CONTEXT_CHAR_BUDGET = 2_000
READER_SELECTED_CHARS = 1_200
READER_ACTIVE_CHARS = 1_200
READER_NEIGHBOR_CHARS = 700

KEY_CONTEXT_SECTION_KEYWORDS = {
    "Abstract": ("abstract", "摘要"),
    "Introduction": ("introduction", "intro", "引言", "背景"),
    "Method": (
        "method",
        "approach",
        "methodology",
        "model",
        "architecture",
        "algorithm",
        "方法",
        "模型",
        "算法",
        "架构",
    ),
    "Conclusion": ("conclusion", "discussion", "总结", "结论", "局限"),
}

RELEVANCE_QUERY_EXPANSIONS = {
    "这篇": ("abstract", "introduction", "contribution", "propose", "paper"),
    "讲什么": ("abstract", "introduction", "contribution", "propose", "paper"),
    "介绍": ("abstract", "introduction", "contribution", "propose", "paper"),
    "核心": ("abstract", "introduction", "contribution", "main", "key"),
    "贡献": ("contribution", "propose", "novel", "main", "key"),
    "方法": ("method", "approach", "methodology", "model", "architecture", "algorithm"),
    "模型": ("model", "architecture", "method", "approach"),
    "算法": ("algorithm", "method", "approach"),
    "实验": ("experiment", "evaluation", "result", "benchmark", "baseline"),
    "数据": ("dataset", "data", "benchmark", "corpus"),
    "代码": ("code", "github", "repository", "implementation", "available"),
    "仓库": ("code", "github", "repository", "implementation"),
    "复现": ("reproduc", "code", "dataset", "hyperparameter", "hardware"),
    "相关工作": ("related work", "prior work", "previous work", "baseline"),
    "像不像": ("related work", "prior work", "compare", "similar"),
    "作者": ("author", "affiliation", "institution"),
    "机构": ("affiliation", "institution", "university", "lab"),
    "学校": ("affiliation", "institution", "university"),
}

CHAT_SYSTEM_PROMPT = (
    "你是「陪你读」阅读页里的 Pet 阅读伙伴，用中文陪用户读论文。"
    "你的产品身份是「陪你读」的论文阅读伙伴；底层模型由本地配置决定。"
    "当用户问你是什么模型、来自哪个 provider 或是否是某个厂商模型时，不要臆测或自称具体 provider，"
    "只说明自己通过「陪你读」配置的 LLM 接口工作。"
    "回答策略按四类处理："
    "1. 论文事实：作者、贡献、方法、实验、结论、复现证据等必须基于给定的论文上下文、全文相关 blocks、"
    "阅读上下文和对话历史回答；证据不足就明确说不足，不要编造。"
    "2. 通用背景：如果用户询问作者、机构、学校、领域概念或常识背景，可以使用模型通用知识回答，"
    "并明确标注“这是通用背景，不是论文内部证据”。"
    "3. 最新信息/外部事实：排名、实时声誉、网页、代码仓库是否仍可访问、作者最新动态等需要外部检索；"
    "没有工具结果时只给出检索建议，不要假装已经查过。"
    "4. 工具/联网/查资料请求：如果上下文显示用户要调用工具、联网或查资料，说明需要权限确认后再执行，"
    "不要把未执行的工具结果写成事实。"
    "对话风格："
    "先直接回答用户当前的问题，不要复述问题、不要罗列上下文，用户自己能看到论文原文。"
    "结合最近对话理解“它/这个/为什么”这类指代；用户在追问上一轮时接着上一轮说，不要重新自我介绍。"
    "长度自适应：简单问题两三句话说清楚；复杂问题可以分点，先给一句话结论再展开，通常不超过 300 字。"
    "阅读上下文里有选区或当前段落时，优先围绕它回答。"
    "如果阅读定位中有“定位限制”，回答时必须自然说明无法精准定位，"
    "不能声称已精准定位到 PDF page/region。"
    "语气自然、具体，像一个懂论文的同伴；不要客服腔，不要以“希望对你有帮助”之类的空话收尾。"
    "匹配到的 Skill 只代表可复用阅读流程，不是论文事实证据；回答时可按流程组织思路。"
    "如果用户的需求更适合后台子任务（选区解释、可复现性深挖、方法拆解、四 Agent 报告），可以在结尾用一句话建议。"
    "上下文来源必须分开：论文 block 和当前选区称为“论文原文”，检索或抓取结果称为“外部网页”，"
    "用户保存的内容称为“你的笔记”。不得把三者互相冒充。"
    "事实性结论优先附带上下文中真实存在的 block、网页 URL 或笔记锚点；没有可靠证据时明确写出限制，"
    "不得编造 page、block、region、URL 或用户原话。"
    "当前论文、当前选区和已给出的相关 blocks 足以回答时直接回答，不得无故调用外部搜索。"
    "只有问题的对象、范围或期望产物确实无法确定时才追问一次；能作保守解释或直接行动时不要机械追问。"
    "像“帮我比较一下”“把这个做深一点”“处理一下这些内容”这类既无明确对象又无可解析指代的请求，"
    "必须只用一句话追问所指对象或期望产物，不得先回答、列菜单、擅自补全任务。"
    "用户要求完整四 Agent 报告时调用 local_four_agent_analysis；要求点击、输入、按键或有状态打开页面时，"
    "只有可用工具列表中存在 browser_control 工具才调用；未启用时直接说明需要先启用可选浏览器服务，"
    "普通网页搜索不能冒充浏览器交互。"
)


def _match_key_context_section(text: str) -> str | None:
    lowered = text.lower()
    for label, keywords in KEY_CONTEXT_SECTION_KEYWORDS.items():
        if any(keyword in lowered for keyword in keywords):
            return label
    return None


def _block_context_line(block, limit: int = KEY_CONTEXT_BLOCK_CHARS) -> str:
    text = _short_text(getattr(block, "original", ""), limit)
    if not text or _is_layout_junk_text(text):
        return ""
    line = f"#{getattr(block, 'index', '?')} ({getattr(block, 'type', 'block')}) {text}"
    translation = _short_text(getattr(block, "translation", ""), 260)
    if translation:
        line += f"\n译文: {translation}"
    return line


def _add_key_context_block(
    sections: dict[str, list[str]],
    seen: set[int],
    label: str,
    block,
) -> None:
    if len(sections[label]) >= KEY_CONTEXT_BLOCKS_PER_SECTION:
        return
    index = getattr(block, "index", None)
    if isinstance(index, int) and index in seen:
        return
    line = _block_context_line(block)
    if not line:
        return
    sections[label].append(line)
    if isinstance(index, int):
        seen.add(index)


def _paper_key_context_blocks(doc) -> dict[str, list[str]]:
    if doc is None:
        return {}

    sections: dict[str, list[str]] = {
        label: [] for label in KEY_CONTEXT_SECTION_KEYWORDS.keys()
    }
    seen: set[int] = set()
    active_label: str | None = None

    for block in doc.blocks:
        block_type = getattr(block, "type", "")
        text = _short_text(getattr(block, "original", ""), 260)
        if not text or _is_layout_junk_text(text):
            continue

        if block_type == "heading":
            active_label = _match_key_context_section(text)
            if active_label:
                _add_key_context_block(sections, seen, active_label, block)
            continue

        if active_label and block_type == "paragraph":
            _add_key_context_block(sections, seen, active_label, block)
            if len(sections[active_label]) >= KEY_CONTEXT_BLOCKS_PER_SECTION:
                active_label = None

    # 兜底：有些提取结果没有清晰 heading，就按关键词扫正文候选段。
    for block in doc.blocks:
        if getattr(block, "type", "") not in ("heading", "paragraph"):
            continue
        text = _short_text(getattr(block, "original", ""), 700)
        if not text or _is_layout_junk_text(text):
            continue
        label = _match_key_context_section(text)
        if label:
            _add_key_context_block(sections, seen, label, block)

    if not sections["Abstract"]:
        for block in doc.blocks:
            if getattr(block, "type", "") == "paragraph":
                _add_key_context_block(sections, seen, "Abstract", block)
                break

    return {label: blocks for label, blocks in sections.items() if blocks}


def _search_terms(user_message: str, context: dict) -> list[str]:
    parts = [user_message]
    reader = context.get("reader")
    if isinstance(reader, dict):
        selected = reader.get("selected_text")
        if isinstance(selected, dict):
            parts.append(str(selected.get("text") or ""))
        active = reader.get("active_block")
        if isinstance(active, dict):
            parts.append(str(active.get("original") or ""))
    query = " ".join(part for part in parts if part).lower()

    terms = set(re.findall(r"[a-z0-9][a-z0-9_-]{1,}|[\u4e00-\u9fff]{2,}", query))
    for trigger, expansions in RELEVANCE_QUERY_EXPANSIONS.items():
        if trigger.lower() in query:
            terms.update(expansions)
    return sorted(terms, key=lambda item: (-len(item), item))


def _score_block_for_terms(block_text: str, terms: list[str]) -> float:
    lowered = block_text.lower()
    score = 0.0
    for term in terms:
        if term not in lowered:
            continue
        count = min(lowered.count(term), 3)
        score += 2.0 + count
        if " " in term or len(term) >= 6:
            score += 1.0
    return score


def _related_context_blocks(
    doc,
    user_message: str,
    context: dict,
    limit: int = RELATED_CONTEXT_BLOCK_LIMIT,
) -> list[str]:
    if doc is None:
        return []
    terms = _search_terms(user_message, context)
    if not terms:
        return []

    scored: list[tuple[float, int, str]] = []
    for block in doc.blocks:
        if getattr(block, "type", "") not in ("heading", "paragraph"):
            continue
        line = _block_context_line(block)
        if not line:
            continue
        searchable = " ".join(
            part
            for part in (
                str(getattr(block, "original", "") or ""),
                str(getattr(block, "translation", "") or ""),
            )
            if part
        )
        score = _score_block_for_terms(searchable, terms)
        if score <= 0:
            continue
        if getattr(block, "type", "") == "heading":
            score += 0.5
        scored.append((score, int(getattr(block, "index", 0)), line))

    scored.sort(key=lambda item: (-item[0], item[1]))
    return [line for _, _, line in scored[:limit]]


def _chat_history_items(arxiv_id: str, user_message: str) -> list[dict[str, str]]:
    def is_user_duplicate(item: dict) -> bool:
        return (
            item.get("role") == "user"
            and str(item.get("content", "")).strip() == user_message.strip()
        )

    def is_chat_context_item(item: dict) -> bool:
        if not item.get("content"):
            return False
        if item.get("role") != "assistant":
            return True
        meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
        if meta.get("kind") in {"permission_request", "agent_run_result", "welcome"}:
            return False
        if meta.get("mcp_config_draft"):
            return False
        if meta.get("created_runs"):
            return False
        return True

    history = [item for item in load_chat(arxiv_id)["messages"] if is_chat_context_item(item)]
    if (
        history
        and history[-1].get("role") == "user"
        and is_user_duplicate(history[-1])
    ):
        history = history[:-1]
    window = history[-CHAT_HISTORY_WINDOW:]
    return [
        {
            "role": "用户" if item.get("role") == "user" else "Pet",
            "content": _short_text(item.get("content"), CHAT_HISTORY_ITEM_CHARS),
        }
        for item in window
    ]


def _memory_items() -> list[str]:
    return [
        _short_text(item.get("content", ""), CHAT_MEMORY_ITEM_CHARS)
        for item in load_memories(limit=CHAT_MEMORY_LIMIT)
        if str(item.get("content", "")).strip()
    ]


def _matched_skills(user_message: str, limit: int = 3) -> list[dict]:
    text = user_message.lower()
    matched: list[tuple[int, dict]] = []
    for skill in load_skills():
        keywords = skill.get("trigger_keywords")
        if not isinstance(keywords, list):
            keywords = []
        score = sum(3 for keyword in keywords if str(keyword).lower() in text)
        name = str(skill.get("name") or "").lower()
        if name and name in text:
            score += 2
        if score <= 0:
            continue
        matched.append((score, skill))
    matched.sort(key=lambda item: (-item[0], str(item[1].get("id") or "")))
    return [skill for _, skill in matched[:limit]]


def _build_context_pack(arxiv_id: str, user_message: str, context: dict) -> dict:
    doc = files.load_document(arxiv_id)
    authors = _context_authors(context)
    title = str(context.get("paper_title") or (doc.title if doc else arxiv_id))
    source = str(context.get("paper_source") or (doc.source if doc else "") or "unknown")
    return {
        "paper": {
            "title": title,
            "authors": authors,
            "source": source,
            "arxiv_id": arxiv_id,
            "metadata_text": _paper_metadata_context(arxiv_id, context),
        },
        "reader": _reader_context_sections(context),
        "global_context": {
            "opening": _paper_opening(arxiv_id),
            "key_blocks": _paper_key_context_blocks(doc),
            "related_blocks": _related_context_blocks(doc, user_message, context),
        },
        "history": _chat_history_items(arxiv_id, user_message),
        "memories": _memory_items(),
        "notes": context.get("notes_context") if isinstance(context.get("notes_context"), dict) else {},
        "matched_skills": _matched_skills(user_message),
    }


def _render_key_context_blocks(key_blocks: dict[str, list[str]]) -> str:
    rendered: list[str] = []
    for label, lines in key_blocks.items():
        if lines:
            rendered.append(f"{label}:\n" + "\n".join(f"- {line}" for line in lines))
    return "\n\n".join(rendered)


def _render_matched_skills(skills: list[dict]) -> str:
    lines: list[str] = []
    for skill in skills:
        steps = skill.get("steps") if isinstance(skill.get("steps"), list) else []
        step_text = "；".join(str(step) for step in steps[:4])
        task_type = str(skill.get("task_type") or "")
        task_note = f" -> {task_type}" if task_type else ""
        lines.append(
            f"- {skill.get('name', skill.get('id'))}{task_note}: "
            f"{skill.get('description', '')}。流程：{step_text or '未声明'}"
        )
    return "\n".join(lines)


def _render_notes_context(notes: dict) -> str:
    if not notes:
        return "无"
    counts = notes.get("kind_counts") if isinstance(notes.get("kind_counts"), dict) else {}
    count_text = "、".join(
        f"{kind} {count}"
        for kind, count in counts.items()
        if type(count) is int and count > 0
    )
    lines = [
        "以下内容来自“你的笔记”，不是论文原文，回答时必须明确区分。",
        f"- 整篇 Markdown：{'有' if notes.get('has_paper_note') else '无'}",
        f"- 选区笔记：{int(notes.get('selection_note_count') or 0)} 条"
        + (f"（{count_text}）" if count_text else ""),
    ]
    current = notes.get("current_note")
    if isinstance(current, dict) and str(current.get("markdown") or "").strip():
        lines.append(f"- 当前选区对应笔记：{_short_text(current.get('markdown'), 800)}")
    relevant = notes.get("relevant") if isinstance(notes.get("relevant"), list) else []
    for item in relevant[:3]:
        if not isinstance(item, dict):
            continue
        label = str(item.get("heading") or "笔记片段")
        lines.append(f"- 相关笔记「{label}」：{_short_text(item.get('snippet'), 800)}")
    return "\n".join(lines)


def _build_chat_prompt(arxiv_id: str, user_message: str, context: dict) -> list[dict]:
    pack = _build_context_pack(arxiv_id, user_message, context)
    paper = pack["paper"]
    title = paper["title"]
    authors = "、".join(paper["authors"]) if paper["authors"] else "无"
    metadata = paper["metadata_text"]
    history_lines = [f"{item['role']}: {item['content']}" for item in pack["history"]]
    memories = "\n".join(f"- {item}" for item in pack["memories"])
    reader_text = "\n\n".join(pack["reader"].values())
    opening = pack["global_context"]["opening"]
    key_blocks = _render_key_context_blocks(pack["global_context"]["key_blocks"])
    related_blocks = "\n".join(
        f"- {line}" for line in pack["global_context"]["related_blocks"]
    )
    notes_context = _render_notes_context(pack["notes"])
    matched_skills = _render_matched_skills(pack["matched_skills"])
    return [
        {
            "role": "system",
            "content": CHAT_SYSTEM_PROMPT,
        },
        {
            "role": "user",
            "content": (
                f"论文: {title}\n"
                f"arXiv: {arxiv_id}\n\n"
                "论文元数据:\n"
                f"- 标题: {title}\n"
                f"- 作者: {authors}\n"
                f"- 来源: {paper['source']}\n"
                f"- arXiv ID: {paper['arxiv_id']}\n\n"
                f"论文元数据/首页线索:\n{metadata or '无'}\n\n"
                f"用户记忆/偏好:\n{memories or '无'}\n\n"
                f"你的论文笔记（有界召回）:\n{notes_context}\n\n"
                f"阅读上下文:\n{reader_text or '无'}\n\n"
                f"论文全局上下文（关键 blocks）:\n{key_blocks or '无'}\n\n"
                f"全文相关 blocks（轻量检索 top-k）:\n{related_blocks or '无'}\n\n"
                f"匹配到的 Skill（可复用阅读流程）:\n{matched_skills or '无'}\n\n"
                f"论文开头线索:\n{opening or '无'}\n\n"
                "最近对话:\n" + ("\n".join(history_lines) or "无") + "\n\n"
                f"用户当前消息: {user_message}"
            ),
        },
    ]


DEGRADED_REPLY_NOTE = "\n\n（提示：LLM 服务暂时不可用，这是本地规则的简答；稍后再问我可以答得更好。）"


async def _chat_reply(
    arxiv_id: str,
    user_message: str,
    context: dict,
    llm_category: str | None = None,
) -> str:
    """普通对话回复：LiteLLM 带对话窗口/记忆/阅读上下文，失败或未配 key 时回退规则回复。

    走 task=agent_chat（默认跟随 default_model，通常是快模型）保证交互延迟；
    LLM 失败时的规则兜底会附加降级提示，不再无声降级。
    llm_category 非 None 表示 LLM 意图分类已判定类别（mcp_status 会在上游直接回答），
    此时跳过 MCP 状态关键词检查，避免“用什么工具实现”这类普通提问被配置说明劫持。
    """
    identity_reply = _identity_boundary_reply(user_message)
    if identity_reply:
        return identity_reply
    if llm_category is None:
        mcp_status_reply = _mcp_status_reply(user_message)
        if mcp_status_reply:
            return mcp_status_reply
    metadata_reply = _metadata_fallback_reply(arxiv_id, user_message, context)
    institution_reply = _institution_background_reply(arxiv_id, user_message, context)
    llm_failed = False
    try:
        raw = await get_client().acomplete(
            _build_chat_prompt(arxiv_id, user_message, context),
            task="agent_chat",
            variant="medium",
        )
        reply = raw.strip()
        if reply:
            if institution_reply and _is_insufficient_reply(reply):
                return institution_reply
            return reply
    except Exception:
        llm_failed = True  # 静默回退规则回复，不打断对话
    note = DEGRADED_REPLY_NOTE if llm_failed else ""
    if institution_reply:
        return institution_reply + note
    if metadata_reply:
        return metadata_reply + note
    return _paper_context_reply(arxiv_id, context) + note


def _has_rule_priority_reply(arxiv_id: str, user_message: str, context: dict, llm_category: str | None) -> bool:
    """Conservative guard: avoid streaming when a rule reply may replace LLM output."""
    if _identity_boundary_reply(user_message):
        return True
    if llm_category is None and _mcp_status_reply(user_message):
        return True
    if _metadata_fallback_reply(arxiv_id, user_message, context):
        return True
    if _institution_background_reply(arxiv_id, user_message, context):
        return True
    return False


async def _plain_chat_stream_plan(arxiv_id: str, payload: AgentChatRequest) -> dict | None:
    """Return routing data only when this request is safe for token streaming."""
    approved_permission = payload.context.get("approved_permission")
    if approved_permission or _should_use_tool_planner(payload.message, payload.context):
        return None

    llm_intent = await _classify_message_llm(arxiv_id, payload.message, payload.context)
    if llm_intent is not None:
        intent = llm_intent
        task_type = llm_intent.get("task_type")
    else:
        intent = infer_agent_intent(payload.message)
        task_type = intent["task_type"]
        task_type = _contextual_task_type(payload.message, payload.context, task_type)
        task_type = _prefer_selection_explanation(payload.message, payload.context, task_type)
        intent["task_type"] = task_type

    if llm_intent is not None:
        wizard_request = llm_intent.get("category") == "mcp_config_wizard"
    else:
        wizard_request = _is_mcp_config_request(payload.message)
    if wizard_request or task_type:
        return None

    wants_memory = (
        bool(llm_intent.get("save_memory"))
        if llm_intent is not None
        else should_save_memory(payload.message)
    )
    if wants_memory:
        return None

    if llm_intent is not None:
        permission = _permission_from_llm_intent(llm_intent, payload.message, payload.context)
    else:
        permission = _permission_request(payload.message, payload.context)
    if permission:
        return None

    llm_category = llm_intent.get("category") if llm_intent is not None else None
    if llm_category == "mcp_status":
        return None
    if llm_category not in (None, "chat"):
        return None
    if _has_rule_priority_reply(arxiv_id, payload.message, payload.context, llm_category):
        return None
    return {"intent": intent, "llm_category": llm_category}


async def _stream_chat_reply_text(arxiv_id: str, user_message: str, context: dict):
    chunks: list[str] = []
    llm_failed = False
    try:
        async for delta in get_client().astream(
            _build_chat_prompt(arxiv_id, user_message, context),
            task="agent_chat",
            variant="medium",
        ):
            chunks.append(delta)
            yield delta
    except Exception:
        llm_failed = True

    reply = "".join(chunks).strip()
    if reply:
        if llm_failed:
            yield DEGRADED_REPLY_NOTE
        return

    fallback = _paper_context_reply(arxiv_id, context)
    if llm_failed:
        fallback += DEGRADED_REPLY_NOTE
    yield fallback


def _assistant_reply(
    arxiv_id: str,
    task_type: str | None,
    saved_memory: bool,
    context: dict,
) -> str:
    if task_type:
        if task_type == "external_tool_request":
            prefix = "我先把这个偏好记下来。" if saved_memory else ""
            body = "我去查一下外部资料，结果回来后会把候选和依据发在这里；你可以继续读论文。"
            return f"{prefix}{body}" if prefix else body
        task_summary = TASK_SUMMARIES.get(task_type, f"子 Agent 计划：{task_type}")
        prefix = "我先把这个偏好记下来。" if saved_memory else "我理解你的目标了。"
        return f"{prefix}{task_summary}。我会放到后台执行；你可以继续问问题，不需要等它。"
    if saved_memory:
        return "我先把这条纠正记进阅读记忆。你接下来可以告诉我，要用它去调整摘要、复现判断、方法解释，还是专题比较。"
    return _paper_context_reply(arxiv_id, context)


def _identity_boundary_reply(user_message: str) -> str | None:
    text = user_message.lower()
    provider_words = (
        "deepseek",
        "qwen",
        "通义",
        "千问",
        "openai",
        "gpt",
        "claude",
        "provider",
        "模型",
        "厂商",
    )
    identity_words = ("你是", "你不是", "你到底", "什么模型", "哪家", "来自")
    if not any(word in text for word in provider_words):
        return None
    if not any(word in user_message for word in identity_words):
        return None
    return (
        "我是「陪你读」的论文阅读伙伴，负责围绕当前论文、段落和标注帮你阅读与分析。"
        "底层模型由本地配置决定，我不会臆测或自称某个具体 provider。"
    )


def _analysis_repro_summary(arxiv_id: str) -> str:
    analysis = files.load_analysis(arxiv_id) or {}
    repro = analysis.get("reproducibility") or {}
    if not repro:
        return "还没有缓存的可复现性报告。可以先运行四 Agent 阅读报告，再回来深挖证据。"
    evidence = repro.get("evidence") or []
    rows = [
        f"{item.get('aspect', '未知维度')}：{item.get('status', '未提')}，{item.get('detail', '')}"
        for item in evidence[:4]
    ]
    body = "\n".join(f"- {row}" for row in rows)
    return (
        f"结论：{repro.get('verdict', 'unknown')}，置信度 {repro.get('confidence', 'unknown')}。\n"
        f"{repro.get('summary', '')}\n{body}"
    ).strip()


def _method_summary(arxiv_id: str) -> str:
    doc = files.load_document(arxiv_id)
    if doc is None:
        return "没有找到论文结构化正文，暂时无法拆解方法。"
    keywords = (
        "method",
        "approach",
        "algorithm",
        "model",
        "方法",
        "算法",
        "模型",
    )
    blocks = [
        block
        for block in doc.blocks
        if block.type in ("heading", "paragraph")
        and any(keyword in block.original.lower() for keyword in keywords)
    ][:5]
    if not blocks:
        return "没有定位到明显的方法段落。你可以划选一段方法描述后直接追问。"
    lines = [f"#{block.index} {block.original.replace(chr(10), ' ')[:180]}" for block in blocks]
    return "我先定位了这些方法相关片段：\n" + "\n".join(f"- {line}" for line in lines)


def _method_fallback_result(arxiv_id: str, error: str | None = None) -> str:
    result = _method_summary(arxiv_id)
    note = f"\n\nLLM 方法解释未完成，已使用规则兜底：{error}" if error else ""
    return f"{result}{note}".strip()


def _annotation_summary(arxiv_id: str) -> str:
    annotations = files.load_annotations(arxiv_id)
    if not annotations:
        return "当前论文还没有标注。你可以先划线或写备注，再让我把这些关注点整理成问题清单。"
    lines = [
        f"#{item.get('block_index')} {item.get('note') or item.get('text', '')[:120]}"
        for item in annotations[-6:]
    ]
    return "我把最近标注整理成这些追问线索：\n" + "\n".join(f"- {line}" for line in lines)


def _reader_context_sections(context: dict) -> dict[str, str]:
    reader = _normalize_reader_context(context)
    if reader is None:
        return {}

    selected = reader["selected"]
    active = reader["active"]
    previous = reader["previous"]
    next_block = reader["next"]

    sections: dict[str, str] = {}
    if selected:
        side = _reader_side_label(reader["selection_side"])
        sections["selected"] = (
            f"选区: {side} #{selected.get('block_index', '?')}\n"
            f"{_short_text(selected.get('text'), READER_SELECTED_CHARS)}"
        )
    if active:
        sections["active"] = (
            f"当前 block: #{active.get('index', '?')} ({active.get('type', 'block')})\n"
            f"原文: {_short_text(active.get('original'), READER_ACTIVE_CHARS)}\n"
            f"译文: {_short_text(active.get('translation'), READER_ACTIVE_CHARS)}"
        )
    if previous:
        sections["previous"] = (
            f"上一 block: #{previous.get('index', '?')}\n"
            f"原文: {_short_text(previous.get('original'), READER_NEIGHBOR_CHARS)}\n"
            f"译文: {_short_text(previous.get('translation'), READER_NEIGHBOR_CHARS)}"
        )
    if next_block:
        sections["next"] = (
            f"下一 block: #{next_block.get('index', '?')}\n"
            f"原文: {_short_text(next_block.get('original'), READER_NEIGHBOR_CHARS)}\n"
            f"译文: {_short_text(next_block.get('translation'), READER_NEIGHBOR_CHARS)}"
        )
    location = _reader_location_section(reader)
    if location:
        sections["location"] = location
    return sections


def _selection_fallback_result(context: dict, error: str | None = None) -> str:
    sections = _reader_context_sections(context)
    selected = sections.get("selected")
    active = sections.get("active")
    target = selected or active
    if not target:
        return "没有拿到选区或当前段落上下文。请先点击段落或选中文本后再问。"

    reader = _normalize_reader_context(context)
    location_notice = _reader_location_notice(reader) if reader else ""
    location_note = f"{location_notice}\n\n" if location_notice else ""
    note = f"\n\nLLM 调用未完成，已使用本地兜底：{error}" if error else ""
    return (
        "我先基于当前上下文做一个保守解释。\n\n"
        f"{target}\n\n"
        f"{location_note}"
        "可继续追问：\n"
        "- 这段里的关键术语分别指什么？\n"
        "- 它和上一段/下一段是什么关系？\n"
        "- 这里是否涉及方法假设或复现实验条件？"
        f"{note}"
    )


def _extract_json_object(text: str) -> dict:
    match = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    raw = match.group(1).strip() if match else text.strip()
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("模型输出不是 JSON object")
    return data


def _build_selection_prompt(arxiv_id: str, user_message: str, context: dict) -> list[dict]:
    doc = files.load_document(arxiv_id)
    title = str(context.get("paper_title") or (doc.title if doc else arxiv_id))
    sections = _reader_context_sections(context)
    context_text = "\n\n".join(section for section in sections.values() if section)
    return [
        {
            "role": "system",
            "content": (
                "你是「陪你读」里的论文阅读伙伴。只基于用户当前论文选区、当前 block 和前后文回答。"
                "不要编造论文没有给出的信息；如果证据不足，明确说不足。"
                "如果阅读定位中有“定位限制”，回答时必须自然说明无法精准定位，"
                "不能声称已精准定位到 PDF page/region。"
                "回答要简洁，优先解释术语、句子作用、与前后文关系，并给出 1-3 个可继续追问的问题。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"论文: {title}\n"
                f"arXiv: {arxiv_id}\n\n"
                f"用户问题: {user_message}\n\n"
                f"阅读上下文:\n{context_text or '无'}"
            ),
        },
    ]


async def _selection_explanation_data(arxiv_id: str, user_message: str, context: dict) -> dict:
    if not _has_reader_context(context):
        return _result_data(
            _selection_fallback_result(context),
            limits=["没有可用的选区或当前段落上下文。"],
        )
    try:
        messages = _build_selection_prompt(arxiv_id, user_message, context)
        result = await get_client().acomplete(
            messages,
            task="agent_chat",
            variant="low",
        )
        reader = _normalize_reader_context(context)
        selected = reader.get("selected") if reader else None
        active = reader.get("active") if reader else None
        block_value = (
            selected.get("block_index") if isinstance(selected, dict) else None
        )
        if type(block_value) is not int and isinstance(active, dict):
            block_value = active.get("index")
        location = (
            {"block_index": block_value}
            if type(block_value) is int and block_value >= 0
            else None
        )
        if location is not None and isinstance(reader.get("region_id"), str):
            location["region_id"] = reader["region_id"]
        evidence = {
            "claim": "回答基于当前阅读选区和相邻段落。",
            "source": "reader_context",
            "confidence": "medium",
        }
        if location is not None:
            evidence["location"] = location
        return _result_data(
            result.strip() or _selection_fallback_result(context, "模型返回为空"),
            evidence=[evidence],
        )
    except Exception as e:
        return _result_data(
            _selection_fallback_result(context, str(e)),
            limits=[f"选区解释模型调用失败：{_short_text(e, 240)}"],
        )


async def _selection_explanation(arxiv_id: str, user_message: str, context: dict) -> str:
    return _format_result_data(await _selection_explanation_data(arxiv_id, user_message, context))


def _repro_fallback_result(arxiv_id: str, error: str | None = None) -> str:
    result = _analysis_repro_summary(arxiv_id)
    note = f"\n\nLLM 深挖未完成，已使用缓存/规则兜底：{error}" if error else ""
    return f"{result}{note}".strip()


def _repro_context_text(arxiv_id: str, context: dict) -> tuple[str, bool]:
    parts: list[str] = []

    analysis = files.load_analysis(arxiv_id) or {}
    repro = analysis.get("reproducibility") or {}
    if repro:
        evidence = repro.get("evidence") or []
        evidence_lines = [
            (
                f"- {item.get('aspect', '未知维度')} | {item.get('status', '未提')} | "
                f"{item.get('detail', '')} | citation: {item.get('citation', '')}"
            )
            for item in evidence[:8]
        ]
        parts.append(
            "已有可复现性报告:\n"
            f"verdict: {repro.get('verdict', 'unknown')}\n"
            f"confidence: {repro.get('confidence', 'unknown')}\n"
            f"summary: {repro.get('summary', '')}\n"
            + "\n".join(evidence_lines)
        )

    reader_sections = _reader_context_sections(context)
    if reader_sections:
        parts.append("用户当前阅读上下文:\n" + "\n\n".join(reader_sections.values()))

    doc = files.load_document(arxiv_id)
    if doc is not None:
        keywords = (
            "dataset",
            "data set",
            "code",
            "github",
            "hyperparameter",
            "implementation",
            "training",
            "hardware",
            "gpu",
            "experiment",
            "reproduc",
            "数据集",
            "代码",
            "超参数",
            "硬件",
            "实验",
        )
        blocks = [
            block
            for block in doc.blocks
            if block.type in ("heading", "paragraph")
            and any(keyword in block.original.lower() for keyword in keywords)
        ][:12]
        if blocks:
            lines = [
                f"#{block.index} ({block.type}) {block.original.replace(chr(10), ' ')[:700]}"
                for block in blocks
            ]
            parts.append("正文中与复现相关的候选段落:\n" + "\n".join(lines))

    return "\n\n".join(part for part in parts if part.strip()), bool(parts)


def _build_repro_prompt(arxiv_id: str, user_message: str, context: dict) -> list[dict]:
    doc = files.load_document(arxiv_id)
    title = str(context.get("paper_title") or (doc.title if doc else arxiv_id))
    memories = "\n".join(f"- {item.get('content', '')}" for item in load_memories(limit=8))
    context_text, _ = _repro_context_text(arxiv_id, context)
    return [
        {
            "role": "system",
            "content": (
                "你是「陪你读」的可复现性深挖 Agent。"
                "只基于给定论文上下文、已有分析缓存和用户当前选区回答；不要编造代码、数据集或硬件信息。"
                "请严格输出 JSON object，不要包裹额外解释。"
                "JSON schema: {"
                "\"summary\":\"一句话结论\","
                "\"evidence\":[{\"claim\":\"判断或解释\",\"source\":\"block/page/section\",\"confidence\":\"high|medium|low\"}],"
                "\"limits\":[\"证据不足或未覆盖的地方\"],"
                "\"next_questions\":[\"用户可以继续追问的问题\"]"
                "}。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"论文: {title}\n"
                f"arXiv: {arxiv_id}\n\n"
                f"用户请求: {user_message}\n\n"
                f"用户记忆/偏好:\n{memories or '无'}\n\n"
                f"可用上下文:\n{context_text}"
            ),
        },
    ]


def _format_repro_deep_dive(data: dict) -> str:
    summary = _short_text(data.get("summary"), 800) or "没有生成明确结论。"
    evidence = data.get("evidence") if isinstance(data.get("evidence"), list) else []
    limits = data.get("limits") if isinstance(data.get("limits"), list) else []
    next_questions = (
        data.get("next_questions") if isinstance(data.get("next_questions"), list) else []
    )

    lines = [f"结论：{summary}"]
    if evidence:
        lines.append("\n证据：")
        for item in evidence[:8]:
            if isinstance(item, dict):
                claim = _short_text(item.get("claim"), 500)
                source = _short_text(item.get("source"), 120) or "未标注来源"
                confidence = _short_text(item.get("confidence"), 40) or "low"
                lines.append(f"- {claim}（来源：{source}；置信度：{confidence}）")
            else:
                lines.append(f"- {_short_text(item, 500)}")
    if limits:
        lines.append("\n限制：")
        lines.extend(f"- {_short_text(item, 500)}" for item in limits[:6])
    if next_questions:
        lines.append("\n可继续追问：")
        lines.extend(f"- {_short_text(item, 240)}" for item in next_questions[:4])
    return "\n".join(lines).strip()


async def _reproducibility_deep_dive_data(
    arxiv_id: str,
    user_message: str,
    context: dict,
) -> dict:
    _, has_context = _repro_context_text(arxiv_id, context)
    if not has_context:
        return _result_data(
            _repro_fallback_result(arxiv_id),
            limits=["没有找到论文正文、已有分析或当前阅读上下文。"],
        )
    try:
        raw = await get_client().acomplete(
            _build_repro_prompt(arxiv_id, user_message, context),
            task="agent_reproducibility",
            variant="low",
        )
        data = _extract_json_object(raw)
        return _normalize_result_data(data)
    except Exception as e:
        return _result_data(
            _repro_fallback_result(arxiv_id, str(e)),
            limits=[f"可复现性深挖模型调用失败：{_short_text(e, 240)}"],
        )


async def _reproducibility_deep_dive(
    arxiv_id: str,
    user_message: str,
    context: dict,
) -> str:
    return _format_repro_deep_dive(
        await _reproducibility_deep_dive_data(arxiv_id, user_message, context)
    )


def _method_context_text(arxiv_id: str, context: dict) -> tuple[str, bool]:
    parts: list[str] = []

    reader_sections = _reader_context_sections(context)
    if reader_sections:
        parts.append("用户当前阅读上下文:\n" + "\n\n".join(reader_sections.values()))

    doc = files.load_document(arxiv_id)
    if doc is not None:
        keywords = (
            "method",
            "approach",
            "algorithm",
            "model",
            "architecture",
            "attention",
            "encoder",
            "decoder",
            "training",
            "objective",
            "方法",
            "算法",
            "模型",
            "架构",
        )
        blocks = [
            block
            for block in doc.blocks
            if block.type in ("heading", "paragraph")
            and any(keyword in block.original.lower() for keyword in keywords)
        ][:12]
        if blocks:
            lines = [
                f"#{block.index} ({block.type}) {block.original.replace(chr(10), ' ')[:700]}"
                for block in blocks
            ]
            parts.append("正文中与方法相关的候选段落:\n" + "\n".join(lines))

    return "\n\n".join(part for part in parts if part.strip()), bool(parts)


def _build_method_prompt(arxiv_id: str, user_message: str, context: dict) -> list[dict]:
    doc = files.load_document(arxiv_id)
    title = str(context.get("paper_title") or (doc.title if doc else arxiv_id))
    context_text, _ = _method_context_text(arxiv_id, context)
    return [
        {
            "role": "system",
            "content": (
                "你是「陪你读」的方法拆解 Agent。"
                "只基于给定论文上下文解释方法，不要编造论文没有说明的模块、公式或实验。"
                "请严格输出 JSON object，不要包裹额外解释。"
                "JSON schema: {"
                "\"summary\":\"一句话解释当前方法或选区\","
                "\"steps\":[\"方法主链路的步骤\"],"
                "\"terms\":[{\"term\":\"术语\",\"meaning\":\"含义\"}],"
                "\"assumptions\":[\"显式或隐含假设\"],"
                "\"next_questions\":[\"用户可以继续追问的问题\"]"
                "}。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"论文: {title}\n"
                f"arXiv: {arxiv_id}\n\n"
                f"用户请求: {user_message}\n\n"
                f"可用上下文:\n{context_text}"
            ),
        },
    ]


def _format_method_explanation(data: dict) -> str:
    summary = _short_text(data.get("summary"), 800) or "没有生成明确的方法解释。"
    steps = data.get("steps") if isinstance(data.get("steps"), list) else []
    terms = data.get("terms") if isinstance(data.get("terms"), list) else []
    assumptions = data.get("assumptions") if isinstance(data.get("assumptions"), list) else []
    next_questions = (
        data.get("next_questions") if isinstance(data.get("next_questions"), list) else []
    )

    lines = [f"方法解释：{summary}"]
    if steps:
        lines.append("\n主链路：")
        lines.extend(f"- {_short_text(item, 500)}" for item in steps[:8])
    if terms:
        lines.append("\n关键术语：")
        for item in terms[:8]:
            if isinstance(item, dict):
                term = _short_text(item.get("term"), 120)
                meaning = _short_text(item.get("meaning"), 400)
                lines.append(f"- {term}：{meaning}")
            else:
                lines.append(f"- {_short_text(item, 500)}")
    if assumptions:
        lines.append("\n假设/注意点：")
        lines.extend(f"- {_short_text(item, 500)}" for item in assumptions[:6])
    if next_questions:
        lines.append("\n可继续追问：")
        lines.extend(f"- {_short_text(item, 240)}" for item in next_questions[:4])
    return "\n".join(lines).strip()


async def _method_explanation_data(
    arxiv_id: str,
    user_message: str,
    context: dict,
) -> dict:
    _, has_context = _method_context_text(arxiv_id, context)
    if not has_context:
        return _result_data(
            _method_fallback_result(arxiv_id),
            limits=["没有找到论文正文或当前阅读上下文。"],
        )
    try:
        raw = await get_client().acomplete(
            _build_method_prompt(arxiv_id, user_message, context),
            task="agent_summary",
            variant="low",
        )
        data = _extract_json_object(raw)
        terms = data.get("terms") if isinstance(data.get("terms"), list) else []
        evidence = [
            {
                "claim": _short_text(item.get("meaning"), 500),
                "source": _short_text(item.get("term"), 160),
                "confidence": "medium",
            }
            for item in terms
            if isinstance(item, dict) and item.get("meaning")
        ]
        assumptions = data.get("assumptions") if isinstance(data.get("assumptions"), list) else []
        return _result_data(
            _format_method_explanation(data),
            evidence=evidence,
            limits=assumptions,
            next_questions=data.get("next_questions"),
        )
    except Exception as e:
        return _result_data(
            _method_fallback_result(arxiv_id, str(e)),
            limits=[f"方法拆解模型调用失败：{_short_text(e, 240)}"],
        )


async def _method_explanation(
    arxiv_id: str,
    user_message: str,
    context: dict,
) -> str:
    return _format_result_data(await _method_explanation_data(arxiv_id, user_message, context))


def _four_agent_result_data(analysis: dict, *, cached: bool) -> dict:
    reproducibility = analysis.get("reproducibility") if isinstance(analysis.get("reproducibility"), dict) else {}
    evidence = reproducibility.get("evidence") if isinstance(reproducibility.get("evidence"), list) else []
    return _result_data(
        _short_text(analysis.get("summary"), 2_000) or "四 Agent 没有生成摘要。",
        evidence=[item for item in evidence if isinstance(item, dict)],
        limits=[
            f"可复现性结论：{reproducibility.get('verdict', 'unknown')}（置信度 {reproducibility.get('confidence', 'unknown')}）",
            *[
                f"可改进：{_short_text(item, 360)}"
                for item in (analysis.get("improvements") or [])[:3]
            ],
        ],
        next_questions=[
            "要我进一步解释哪一个方法模块？",
            "要我根据这份报告继续核对复现证据吗？",
        ] if cached else ["要我继续把报告中的某一项展开成段落级证据吗？"],
    )


async def _four_agent_analysis_data(arxiv_id: str, user_message: str, context: dict) -> dict:
    analysis = files.load_analysis(arxiv_id)
    if isinstance(analysis, dict):
        return _four_agent_result_data(analysis, cached=True)
    doc = files.load_document(arxiv_id)
    if doc is None:
        return _result_data(
            "没有找到论文结构化正文，暂时不能运行四 Agent 阅读报告。",
            limits=["请先完成论文提取。"],
        )
    try:
        result = await analyze_paper(doc.blocks)
        analysis = result.model_dump()
        files.save_analysis(arxiv_id, analysis)
        await update_status(arxiv_id, "analyzed")
        return _four_agent_result_data(analysis, cached=False)
    except Exception as e:
        return _result_data(
            "四 Agent 阅读报告没有完成。",
            limits=[f"LiteLLM 分析调用失败：{_short_text(e, 320)}"],
        )


async def _four_agent_analysis_result(arxiv_id: str, user_message: str, context: dict) -> str:
    return _format_result_data(await _four_agent_analysis_data(arxiv_id, user_message, context))


def _tool_name_for_scope(
    scope: str,
    registry=None,
    user_message: str = "",
    context: dict | None = None,
) -> str:
    if registry is None:
        if scope == "external_search":
            return "local.web_fetch" if _should_use_web_fetch(user_message) else (
                "local.web_search" if _should_use_web_search(user_message) else "local.external_search"
            )
        return "mock.mcp_tool"
    return choose_initial_tool(
        registry,
        scope=scope,
        user_message=user_message,
        context=context or {},
    )


def _should_use_web_fetch(user_message: str) -> bool:
    text = user_message.casefold()
    has_url = re.search(r"https?://", text) is not None
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
    if has_url:
        return True
    return any(term in text for term in fetch_terms) and any(
        target in text for target in ("网页", "链接", "url", "website", "browser")
    )


def _should_use_web_search(user_message: str) -> bool:
    text = user_message.casefold()
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
    if any(term in text for term in ("github", "仓库", "repository", "repo", "代码", "复现")):
        return True
    return False


def _build_tool_result_prompt(
    arxiv_id: str,
    user_message: str,
    context: dict,
    tool_result: ToolResult,
) -> list[dict]:
    doc = files.load_document(arxiv_id)
    title = str(context.get("paper_title") or (doc.title if doc else arxiv_id))
    evidence_json = json.dumps(list(tool_result.evidence), ensure_ascii=False, indent=2)
    metadata_json = json.dumps(dict(tool_result.metadata), ensure_ascii=False, indent=2)
    return [
        {
            "role": "system",
            "content": (
                "你是「陪你读」的工具结果汇总助手。工具结果只能作为 evidence/context 使用，"
                "不要把工具没有给出的内容补成事实。若工具结果是 mock 或占位结果，必须明确告诉用户。"
                "用中文回答，控制在 200 字以内。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"论文: {title}\n"
                f"arXiv: {arxiv_id}\n\n"
                f"用户原始请求: {user_message}\n\n"
                f"工具名: {tool_result.name}\n"
                f"工具结果:\n{tool_result.content}\n\n"
                f"工具 evidence:\n{evidence_json or '[]'}\n\n"
                f"工具 metadata:\n{metadata_json or '{}'}"
            ),
        },
    ]


def _tool_result_fallback(scope: str, tool_result: ToolResult, error: str | None = None) -> str:
    note = f"\n\nLLM 汇总未完成，已保留工具结果：{error}" if error else ""
    if tool_result.metadata.get("mock"):
        evidence_note = "这是工具返回的 evidence/context，不是论文内部证据；后续接入真实工具后会用实际结果替换。"
    else:
        evidence_note = "这是工具返回的 evidence/context，不是论文内部证据；请以工具标注的数据来源为准。"
    return (
        f"已确认权限：{PERMISSION_LABELS.get(scope, scope)}。\n"
        f"{tool_result.content}\n\n"
        f"{evidence_note}"
        f"{note}"
    )


def _tool_arguments(arxiv_id: str, paper_title: str, user_message: str, context: dict) -> dict:
    tool_plan = context.get("tool_plan") if isinstance(context.get("tool_plan"), dict) else {}
    arguments = {
        "query": user_message,
        "arxiv_id": arxiv_id,
        "paper_title": paper_title,
        "paper_authors": _context_authors(context),
        "paper_metadata": _paper_metadata_context(arxiv_id, context),
        "exclude_arxiv_id": arxiv_id,
        "exclude_title": paper_title,
    }
    if tool_plan.get("search_query"):
        arguments["search_query"] = str(tool_plan.get("search_query"))
    if tool_plan.get("query_mode"):
        arguments["query_mode"] = str(tool_plan.get("query_mode"))
    if tool_plan.get("query_mode") == "related_papers" or (
        context.get("approved_permission") and _is_related_paper_lookup(user_message)
    ):
        arguments.update(
            {
                "query_mode": "related_papers",
                "search_query": str(tool_plan.get("search_query") or _related_paper_search_query(paper_title, user_message)),
                "exclude_arxiv_id": arxiv_id,
                "exclude_title": paper_title,
                "max_results": 8,
            }
        )
    return arguments


async def _external_tool_request_result(arxiv_id: str, user_message: str, context: dict) -> str:
    return await _external_tool_request_result_with_events(arxiv_id, user_message, context)


async def _external_tool_request_result_with_events(
    arxiv_id: str,
    user_message: str,
    context: dict,
    on_tool_event=None,
) -> str:
    scope = str(context.get("approved_permission") or "mcp_tool")
    if scope not in ("external_search", "mcp_tool"):
        scope = "mcp_tool"
    doc = files.load_document(arxiv_id)
    paper_title = str(context.get("paper_title") or (doc.title if doc else arxiv_id))
    registry = build_agent_tool_registry()
    tool_name = _tool_name_for_scope(scope, registry, user_message, context)
    loop_result = await run_agent_tool_loop(
        registry,
        scope=scope,
        user_message=user_message,
        base_arguments=_tool_arguments(arxiv_id, paper_title, user_message, context),
        context=context,
        initial_tool=tool_name,
        on_event=on_tool_event,
    )
    tool_result = loop_result.tool_result
    context["tool_trace"] = loop_result.trace
    context["tool_loop_events"] = list(loop_result.events)
    if loop_result.limits:
        context["tool_loop_limits"] = list(loop_result.limits)
    try:
        raw = await get_client().acomplete(
            _build_tool_result_prompt(arxiv_id, user_message, context, tool_result),
            task="agent_chat",
            variant="low",
        )
        reply = raw.strip()
        if reply:
            return reply
        return _tool_result_fallback(scope, tool_result, "模型返回为空")
    except Exception as e:
        return _tool_result_fallback(scope, tool_result, str(e))


async def _annotation_questions_data(arxiv_id: str, user_message: str, context: dict) -> dict:
    note_summary = files.build_paper_note_summary(arxiv_id)
    if not note_summary["annotation_count"] and not note_summary["has_paper_note"]:
        return _result_data(
            _annotation_summary(arxiv_id),
            limits=["当前论文没有可整理的用户笔记。"],
        )
    matches = await search_paper_notes(arxiv_id, user_message, limit=12)
    if not matches:
        matches = await search_paper_notes(arxiv_id, "", limit=12)
    note_lines: list[str] = []
    for item in matches:
        block_label = (
            f"，block #{item.get('block_index')}"
            if type(item.get("block_index")) is int
            else ""
        )
        note_lines.append(
            f"- 你的笔记「{item.get('heading') or '未命名'}」{block_label}: "
            f"{_short_text(item.get('snippet'), 600)}"
        )
    note_text = "\n".join(note_lines)
    messages = [
        {
            "role": "system",
            "content": (
                "你是论文阅读笔记整理助手。只基于给定的用户笔记整理问题清单；"
                "所有引用必须明确标注“你的笔记”，不得把用户判断写成论文事实。"
                "严格输出 JSON object，schema 为 "
                '{"summary":"一句话整理", "evidence":[{"claim":"笔记线索","source":"你的笔记",'
                '"location":{"block_index":N},"note_heading":"主笔记标题"}], '
                '"limits":["证据限制"], "next_questions":["可追问问题"]}。'
                "只有输入中真实存在 block_index 时才可填写 location；不得自行填写 page 或 region_id。"
                "引用主笔记章节时，note_heading 必须使用输入中的精确标题。"
            ),
        },
        {
            "role": "user",
            "content": f"用户请求：{user_message}\n\n检索到的用户笔记：\n{note_text or '无相关笔记'}",
        },
    ]
    try:
        raw = await get_client().acomplete(messages, task="agent_chat", variant="low")
        return _normalize_result_data(_extract_json_object(raw))
    except Exception as e:
        fallback_summary = (
            "我从相关的“你的笔记”中整理出这些追问线索：\n"
            + "\n".join(
                f"- {_short_text(item.get('snippet'), 240)}"
                for item in matches[:6]
            )
        ).strip()
        return _result_data(
            fallback_summary,
            evidence=[
                {
                    "claim": _short_text(item.get("snippet"), 500),
                    "source": f"你的笔记 · {item.get('heading') or '未命名'}",
                    "confidence": "medium",
                    **(
                        {"location": {"block_index": item["block_index"]}}
                        if type(item.get("block_index")) is int
                        else {"note_heading": item.get("heading")}
                    ),
                }
                for item in matches[:6]
            ],
            limits=[f"标注整理模型调用失败：{_short_text(e, 240)}"],
        )


async def _annotation_questions_result(arxiv_id: str, user_message: str, context: dict) -> str:
    return _format_result_data(await _annotation_questions_data(arxiv_id, user_message, context))


async def _collection_compare_data(arxiv_id: str, user_message: str, context: dict) -> dict:
    collection_id = context.get("collection_id")
    if not isinstance(collection_id, int):
        collections = await list_collections(arxiv_id=arxiv_id)
        selected = next((item for item in collections if item.get("contains_paper")), None)
        collection_id = selected.get("id") if selected else None
    collection = await get_collection(collection_id) if isinstance(collection_id, int) else None
    if collection is None:
        return _result_data(
            "专题比较需要至少一个包含当前论文的专题。",
            limits=["请先把论文加入专题，或在专题页指定比较对象。"],
        )

    from .routes_collections import _build_collection_agent_report

    report = _build_collection_agent_report(collection)
    files.save_collection_agent_report(int(collection_id), report)
    compact_report = _short_text(json.dumps(report, ensure_ascii=False), 12_000)
    messages = [
        {
            "role": "system",
            "content": (
                "你是论文专题比较助手。只基于专题报告进行横向比较，明确未分析论文造成的限制。"
                "报告中的 analysis 字段属于论文分析结果；note_preview、selection_note_count 和 "
                "has_paper_note 属于“你的笔记”元数据。引用后者时必须明确说“你的笔记”，"
                "不得把用户判断冒充论文事实。"
                "严格输出 JSON object，schema 为 "
                '{"summary":"比较结论", "evidence":[{"claim":"比较证据","source":"论文或专题"}], '
                '"limits":["限制"], "next_questions":["可追问问题"]}。'
            ),
        },
        {"role": "user", "content": f"用户请求：{user_message}\n\n专题报告：\n{compact_report}"},
    ]
    try:
        raw = await get_client().acomplete(messages, task="agent_summary", variant="low")
        return _normalize_result_data(_extract_json_object(raw))
    except Exception as e:
        return _result_data(
            f"专题「{collection.get('name', '')}」包含 {report.get('paper_count', 0)} 篇论文，"
            f"其中 {report.get('analyzed_count', 0)} 篇已有单篇分析。",
            evidence=[
                {"claim": item, "source": "专题报告", "confidence": "medium"}
                for item in report.get("synthesis", [])
            ],
            limits=[f"专题比较模型调用失败：{_short_text(e, 240)}"],
        )


async def _collection_compare_result(arxiv_id: str, user_message: str, context: dict) -> str:
    return _format_result_data(await _collection_compare_data(arxiv_id, user_message, context))


def _register_agent_task_tools(
    registry,
    arxiv_id: str,
    user_message: str,
    context: dict,
) -> None:
    """Add Pet's local professional tasks to this request-scoped registry."""

    task_handlers = {
        "selection_explanation": _selection_explanation_data,
        "method_explanation": _method_explanation_data,
        "reproducibility_deep_dive": _reproducibility_deep_dive_data,
        "four_agent_analysis": _four_agent_analysis_data,
        "annotation_questions": _annotation_questions_data,
        "collection_compare": _collection_compare_data,
    }
    scopes = {
        "selection_explanation": "",
        "annotation_questions": "",
        "method_explanation": "long_task",
        "reproducibility_deep_dive": "long_task",
        "four_agent_analysis": "long_task",
        "collection_compare": "long_task",
    }

    for task_type, handler in task_handlers.items():
        tool_name = f"local.{task_type}"
        if registry.get(tool_name) is not None:
            # A test/integration host may reuse a registry across permission
            # resume calls. The original request-scoped executor remains valid.
            continue

        async def execute(call: ToolCall, *, task_type=task_type, handler=handler) -> ToolResult:
            tool_context = dict(context)
            collection_id = call.arguments.get("collection_id")
            if isinstance(collection_id, int):
                tool_context["collection_id"] = collection_id
            result_data = _normalize_result_data(
                await handler(arxiv_id, str(call.arguments.get("query") or user_message), tool_context)
            )
            result_data = _enrich_result_data_for_paper(arxiv_id, result_data)
            return ToolResult(
                name=f"local.{task_type}",
                content=_format_result_data(result_data),
                evidence=tuple(result_data["evidence"]),
                metadata={
                    "source": "local_agent_task",
                    "task_type": task_type,
                    "result_data": result_data,
                },
            )

        registry.register(
            ToolSpec(
                name=tool_name,
                description=TASK_SUMMARIES[task_type],
                permission_scope=scopes[task_type],  # type: ignore[arg-type]
                source="local",
            ),
            execute,
        )


def _register_memory_tool(registry, arxiv_id: str) -> None:
    """Expose governed local memory writes to the resumable Agent Loop."""
    tool_name = "local.memory_save"
    if registry.get(tool_name) is not None:
        return

    async def execute(call: ToolCall) -> ToolResult:
        content = str(call.arguments.get("content") or "").strip()
        kind = str(call.arguments.get("kind") or "preference").strip() or "preference"
        memory = add_memory(
            content,
            kind=kind,
            arxiv_id=arxiv_id,
            source="agent",
        )
        return ToolResult(
            name=tool_name,
            content=f"已保存阅读记忆：{memory['content']}",
            metadata={"source": "local_memory", "memory": memory},
        )

    registry.register(
        ToolSpec(
            name=tool_name,
            description="保存用户确认的长期阅读偏好、纠正或判断标准。",
            permission_scope="memory_write",
            source="local",
            input_schema=LOCAL_MEMORY_TOOLS[0]["function"]["parameters"],
        ),
        execute,
    )


def _register_skill_tools(registry) -> None:
    for definition in LOCAL_SKILL_TOOLS:
        provider_name = definition["function"]["name"]
        tool_name = provider_name.replace("local_", "local.", 1)
        if registry.get(tool_name) is not None:
            continue

        async def execute(call: ToolCall, *, tool_name=tool_name) -> ToolResult:
            if tool_name == "local.skills_list":
                skills = load_skills()
                return ToolResult(name=tool_name, content=json.dumps(skills, ensure_ascii=False), metadata={"skills": skills})
            if tool_name == "local.skill_view":
                skill_id = str(call.arguments.get("skill_id") or "")
                skill = next((item for item in load_skills() if item.get("id") == skill_id), None)
                return ToolResult(
                    name=tool_name,
                    content=json.dumps(skill, ensure_ascii=False) if skill else f"没有找到 skill：{skill_id}",
                    metadata={"skill": skill} if skill else {"error": "skill_not_found"},
                )
            skill = {
                key: call.arguments.get(key)
                for key in ("id", "name", "description", "trigger", "task_type", "trigger_keywords", "steps")
            }
            try:
                proposal = create_skill_proposal(skill, str(call.arguments.get("action") or "create"))
            except ValueError as exc:
                return ToolResult(name=tool_name, content=f"Skill 提案无效：{exc}", metadata={"error": str(exc)})
            return ToolResult(
                name=tool_name,
                content=f"已创建待审核 Skill 提案：{proposal['skill']['name']}。批准前不会改变实际能力。",
                metadata={"proposal": proposal},
            )

        registry.register(
            ToolSpec(
                name=tool_name,
                description=definition["function"]["description"],
                permission_scope="",
                source="local",
                input_schema=definition["function"]["parameters"],
            ),
            execute,
        )


def _register_session_search_tool(registry, *, exclude_message_id: str | None = None) -> None:
    """Expose read-only local session history search without a permission pause."""
    tool_name = "local.session_search"
    if registry.get(tool_name) is not None:
        return

    async def execute(call: ToolCall) -> ToolResult:
        query = str(call.arguments.get("query") or "").strip()
        try:
            limit = min(20, max(1, int(call.arguments.get("limit") or 8)))
        except (TypeError, ValueError):
            limit = 8
        results = await search_agent_sessions(
            query,
            limit=limit,
            exclude_message_id=exclude_message_id,
        )
        if not results:
            return ToolResult(
                name=tool_name,
                content=f"没有找到与“{query}”匹配的历史讨论。",
                metadata={"source": "local_session_search", "query": query, "result_count": 0},
            )
        lines = [
            f"- {item['paper_title']}（{item['arxiv_id']}，{'你' if item['role'] == 'user' else 'Pet'}）：{item['snippet']}"
            for item in results
        ]
        evidence = tuple(
            {
                "kind": "agent_session_search_result",
                **item,
            }
            for item in results
        )
        return ToolResult(
            name=tool_name,
            content="找到这些历史讨论：\n" + "\n".join(lines),
            evidence=evidence,
            metadata={
                "source": "local_session_search",
                "query": query,
                "result_count": len(results),
            },
        )

    registry.register(
        ToolSpec(
            name=tool_name,
            description="只读搜索用户与 Pet 以前的论文会话；仅用于明确的历史讨论查询。",
            permission_scope="",
            source="local",
            input_schema=LOCAL_SESSION_TOOLS[0]["function"]["parameters"],
        ),
        execute,
    )


def _note_result_evidence(item: dict) -> dict:
    evidence = {
        "kind": "agent_note_search_result",
        "claim": _short_text(item.get("snippet") or item.get("markdown"), 500),
        "source": f"你的笔记 · {item.get('heading') or '未命名'}",
        "arxiv_id": item.get("arxiv_id"),
        "annotation_id": item.get("annotation_id"),
        "note_heading": item.get("heading") if item.get("source_type") == "paper_note" else None,
        "note_kind": item.get("kind"),
    }
    if type(item.get("block_index")) is int:
        evidence["location"] = {
            "block_index": item["block_index"],
            **({"page": item["page"]} if type(item.get("page")) is int else {}),
            **({"region_id": item["region_id"]} if item.get("region_id") else {}),
        }
    return evidence


async def _resolve_note_collection_id(arxiv_id: str, context: dict, requested: object) -> int | None:
    if type(requested) is int and requested > 0:
        collection = await get_collection(requested)
        if collection and any(
            paper.get("arxiv_id") == arxiv_id for paper in collection.get("papers", [])
        ):
            return requested
        return None
    context_id = context.get("collection_id")
    if type(context_id) is int and context_id > 0:
        collection = await get_collection(context_id)
        if collection and any(
            paper.get("arxiv_id") == arxiv_id for paper in collection.get("papers", [])
        ):
            return context_id
    collections = await list_collections(arxiv_id=arxiv_id)
    selected = next((item for item in collections if item.get("contains_paper")), None)
    return int(selected["id"]) if selected else None


def _register_note_tools(registry, arxiv_id: str, context: dict) -> None:
    """Expose current-paper note search/view as permission-free read-only tools."""
    search_name = "local.notes_search"
    if registry.get(search_name) is None:
        async def execute_search(call: ToolCall) -> ToolResult:
            query = str(call.arguments.get("query") or "").strip()
            scope = str(call.arguments.get("scope") or "current_paper")
            try:
                limit = min(10, max(1, int(call.arguments.get("limit") or 5)))
            except (TypeError, ValueError):
                limit = 5
            collection_id = None
            if scope == "current_collection":
                collection_id = await _resolve_note_collection_id(
                    arxiv_id,
                    context,
                    call.arguments.get("collection_id"),
                )
                if collection_id is None:
                    return ToolResult(
                        name=search_name,
                        content="当前论文没有可确认的专题上下文，无法搜索专题内笔记。",
                        metadata={
                            "source": "local_notes_search",
                            "scope": scope,
                            "error": "collection_context_missing",
                        },
                    )
                results = await search_collection_notes(collection_id, query, limit=limit)
            else:
                scope = "current_paper"
                results = await search_paper_notes(arxiv_id, query, limit=limit)
            if not results:
                return ToolResult(
                    name=search_name,
                    content=(
                        f"{'当前专题' if scope == 'current_collection' else '当前论文'}的"
                        f"“你的笔记”中没有找到与“{query}”相关的内容。"
                    ),
                    metadata={
                        "source": "local_notes_search",
                        "scope": scope,
                        "collection_id": collection_id,
                        "query": query,
                        "result_count": 0,
                    },
                )
            evidence = tuple(_note_result_evidence(item) for item in results)
            lines = [
                f"- {item.get('paper_title') or item['arxiv_id']}（{item['arxiv_id']}）"
                f"的你的笔记「{item['heading']}」：{_short_text(item['snippet'], 500)}"
                for item in results
            ]
            return ToolResult(
                name=search_name,
                content=(
                    "以下均来自用户保存的“你的笔记”，不是论文原文：\n"
                    + "\n".join(lines)
                ),
                evidence=evidence,
                metadata={
                    "source": "local_notes_search",
                    "scope": scope,
                    "collection_id": collection_id,
                    "query": query,
                    "result_count": len(results),
                },
            )

        registry.register(
            ToolSpec(
                name=search_name,
                description=LOCAL_NOTE_TOOLS[0]["function"]["description"],
                permission_scope="",
                source="local",
                input_schema=LOCAL_NOTE_TOOLS[0]["function"]["parameters"],
            ),
            execute_search,
        )

    view_name = "local.notes_view"
    if registry.get(view_name) is None:
        async def execute_view(call: ToolCall) -> ToolResult:
            annotation_id = str(call.arguments.get("annotation_id") or "").strip() or None
            heading = str(call.arguments.get("heading") or "").strip() or None
            target_arxiv_id = str(call.arguments.get("arxiv_id") or arxiv_id).strip()
            if bool(annotation_id) == bool(heading):
                return ToolResult(
                    name=view_name,
                    content="请且只请提供 annotation_id 或 heading 其中一个。",
                    metadata={"source": "local_notes_view", "error": "invalid_note_locator"},
                )
            if target_arxiv_id != arxiv_id:
                collection_id = await _resolve_note_collection_id(
                    arxiv_id,
                    context,
                    call.arguments.get("collection_id"),
                )
                collection = await get_collection(collection_id) if collection_id else None
                if not collection or not any(
                    paper.get("arxiv_id") == target_arxiv_id
                    for paper in collection.get("papers", [])
                ):
                    return ToolResult(
                        name=view_name,
                        content="指定笔记不属于当前论文或当前专题。",
                        metadata={"source": "local_notes_view", "error": "note_scope_mismatch"},
                    )
            item = view_paper_note(
                target_arxiv_id,
                annotation_id=annotation_id,
                heading=heading,
            )
            if item is None:
                return ToolResult(
                    name=view_name,
                    content="指定的用户笔记不存在或已经更新。",
                    metadata={"source": "local_notes_view", "error": "note_not_found"},
                )
            evidence = _note_result_evidence(
                {
                    **item,
                    "snippet": item.get("markdown") or item.get("quote"),
                }
            )
            body = str(item.get("markdown") or "").strip()
            quote = str(item.get("quote") or "").strip()
            content = (
                f"你的笔记（不是论文原文）：\n{body or '仅保存了语义高亮，未写备注。'}"
                + (f"\n\n对应原文：\n{quote}" if quote else "")
            )
            return ToolResult(
                name=view_name,
                content=content,
                evidence=(evidence,),
                metadata={"source": "local_notes_view", "item": item},
            )

        registry.register(
            ToolSpec(
                name=view_name,
                description=LOCAL_NOTE_TOOLS[1]["function"]["description"],
                permission_scope="",
                source="local",
                input_schema=LOCAL_NOTE_TOOLS[1]["function"]["parameters"],
            ),
            execute_view,
        )


# 子 Agent（后台 Run）执行器注册表：新增任务类型只需在此登记一个
# async (arxiv_id, user_message, context) -> str 的执行器
RUN_EXECUTORS = {
    "selection_explanation": _selection_explanation,
    "reproducibility_deep_dive": _reproducibility_deep_dive,
    "method_explanation": _method_explanation,
    "annotation_questions": _annotation_questions_result,
    "four_agent_analysis": _four_agent_analysis_result,
    "external_tool_request": _external_tool_request_result,
    "collection_compare": _collection_compare_result,
}


_RUN_TASKS: dict[str, asyncio.Task] = {}
_RUN_SEMAPHORE: asyncio.Semaphore | None = None
_RUN_SEMAPHORE_LIMIT: int | None = None
_RUN_SEMAPHORE_LOOP_ID: int | None = None


def _agent_run_concurrency() -> int:
    try:
        return max(1, int(get_config().agent_concurrency))
    except Exception:
        return 1


def _agent_run_semaphore() -> asyncio.Semaphore:
    global _RUN_SEMAPHORE, _RUN_SEMAPHORE_LIMIT, _RUN_SEMAPHORE_LOOP_ID
    limit = _agent_run_concurrency()
    loop_id = id(asyncio.get_running_loop())
    if (
        _RUN_SEMAPHORE is None
        or _RUN_SEMAPHORE_LIMIT != limit
        or _RUN_SEMAPHORE_LOOP_ID != loop_id
    ):
        _RUN_SEMAPHORE = asyncio.Semaphore(limit)
        _RUN_SEMAPHORE_LIMIT = limit
        _RUN_SEMAPHORE_LOOP_ID = loop_id
    return _RUN_SEMAPHORE


async def _mark_run_cancelled(arxiv_id: str, run_id: str, task_id: int) -> None:
    latest = get_run(arxiv_id, run_id)
    if latest is None:
        return
    if latest.get("status") == "running":
        latest = cancel_run(arxiv_id, run_id)
    if latest and latest.get("status") == "cancelled" and task_id:
        await update_agent_task(int(task_id), "cancelled", f"{latest.get('title', 'Agent Run')} 已取消")


def _cancel_registered_run_task(run_id: str) -> None:
    task = _RUN_TASKS.get(run_id)
    if task is not None and not task.done():
        task.cancel()


async def _build_run_result(
    arxiv_id: str,
    task_type: str,
    user_message: str,
    context: dict | None = None,
    on_tool_event=None,
) -> str:
    executor = RUN_EXECUTORS.get(task_type)
    if executor is None:
        return f"我已经记录这个任务，但还没有对应的执行器：{user_message}"
    if task_type == "external_tool_request":
        return await _external_tool_request_result_with_events(
            arxiv_id,
            user_message,
            context or {},
            on_tool_event=on_tool_event,
        )
    return await executor(arxiv_id, user_message, context or {})


async def _finish_agent_run(arxiv_id: str, run_id: str, task_id: int, on_tool_event=None) -> None:
    current_task = asyncio.current_task()
    if current_task is not None:
        _RUN_TASKS[run_id] = current_task
    try:
        async with _agent_run_semaphore():
            run = get_run(arxiv_id, run_id)
            if run is None or run.get("status") != "running":
                return
            run_context = run.get("context") if isinstance(run.get("context"), dict) else {}
            result = await _build_run_result(
                arxiv_id,
                run.get("task_type", ""),
                run.get("user_message", ""),
                run_context,
                on_tool_event=on_tool_event,
            )
            latest = get_run(arxiv_id, run_id)
            if latest is None or latest.get("status") != "running":
                return
            update_run(
                arxiv_id,
                run_id,
                status="done",
                result=result,
                result_data=_result_data(result),
            )
            await update_agent_task(task_id, "done", f"{run.get('title', 'Agent Run')} 完成")
            message_meta = {
                "kind": "agent_run_result",
                "run_id": run_id,
                "task_type": run.get("task_type"),
            }
            tool_trace = run_context.get("tool_trace")
            if isinstance(tool_trace, dict):
                message_meta["tool_trace"] = tool_trace
            append_message(
                arxiv_id,
                "assistant",
                f"{run.get('title', '后台任务')}完成：\n{result}",
                meta=message_meta,
            )
    except asyncio.CancelledError:
        await _mark_run_cancelled(arxiv_id, run_id, task_id)
    except Exception as e:
        # 只允许 running→error：取消/完成后才抛出的异常不能覆盖已有终态
        # （例如用户已取消，执行器随后因网络失败抛错，Run 必须保持 cancelled）
        latest = get_run(arxiv_id, run_id)
        if latest is None or latest.get("status") != "running":
            return
        update_run(arxiv_id, run_id, status="error", error=str(e))
        await update_agent_task(task_id, "error", "Agent Run 执行失败", str(e))
    finally:
        if current_task is not None and _RUN_TASKS.get(run_id) is current_task:
            _RUN_TASKS.pop(run_id, None)


AGENT_LOOP_SYSTEM_SUFFIX = (
    "\n\n你现在可以直接回答，也可以按需调用已提供的工具。"
    "只有确实需要外部证据时才调用工具；不要声称尚未执行的工具结果。"
    "当用户明确表达以后都要遵守的阅读偏好、纠正或判断标准时，调用 local_memory_save；"
    "临时问题、论文事实和一次性任务不得保存为长期记忆，未执行工具前不得声称已经记住。"
    "只有用户明确询问以前讨论过什么、在哪篇论文聊过某个主题时，才调用 local_session_search；"
    "普通论文事实和当前段落问题不得搜索历史会话。"
    "当用户询问自己对当前论文记过什么、有哪些疑问或要求根据笔记分析时，"
    "调用 local_notes_search，必要时再调用 local_notes_view；"
    "笔记搜索片段以“…”结尾表示内容已截断；需要回答该片段的具体内容时必须先调用 "
    "local_notes_view，不得补全或猜测缺失文字；"
    "用户明确要求比较当前专题中的多篇笔记时，local_notes_search 使用 current_collection，"
    "每条结果必须保留其论文 ID；"
    "必须把结果称为“你的笔记”，不得把用户判断冒充论文事实。"
    "用户要求帮忙记下内容时只能生成待确认草稿，不能直接修改笔记。"
    "当前论文的系统方法拆解优先调用 local_method_explanation；当前论文的复现条件深挖优先调用 "
    "local_reproducibility_deep_dive；完整四 Agent 报告必须调用 local_four_agent_analysis。"
    "这三个本地专业工具不得被 external_search 代替；只有用户明确要求最新网页、仓库状态或论文外部证据时"
    "才另外调用外部工具。"
    "工具失败后最多选择一个不同且合理的替代路径；替代仍失败时必须诚实结束并说明限制，"
    "不得继续轮换工具。"
)


def _uses_unified_agent_loop(message: str, context: dict) -> bool:
    """Route every non-management Pet message through the resumable Agent Loop."""
    if context.get("approved_permission"):
        # Compatibility for permission cards created before resumable agent-loop Runs.
        return False
    if _is_mcp_config_request(message) or _is_mcp_status_question(message):
        return False
    if should_save_memory(message) and not _intent_llm_enabled():
        return False
    if not _intent_llm_enabled():
        # Old installations can explicitly disable the intent/loop path while
        # they upgrade. Keep their deterministic local background tasks intact;
        # permissioned external work still uses the resumable loop.
        permission = _permission_request(message, context)
        if permission and permission.get("scope") in ("external_search", "mcp_tool"):
            return True
        intent = infer_agent_intent(message)
        task_type = _contextual_task_type(message, context, intent.get("task_type"))
        task_type = _prefer_selection_explanation(message, context, task_type)
        return task_type is None
    return True


def _agent_loop_messages(arxiv_id: str, user_message: str, context: dict) -> list[dict]:
    messages = _build_chat_prompt(arxiv_id, user_message, context)
    messages[0] = {
        **messages[0],
        "content": str(messages[0].get("content") or "") + AGENT_LOOP_SYSTEM_SUFFIX,
    }
    return messages


def _agent_loop_tool_name_map(registry, user_message: str, context: dict) -> dict[str, str]:
    mapping = {
        provider_name: registry_name
        for provider_name, registry_name in TOOL_PLAN_NATIVE_NAME_MAP.items()
        if registry.get(registry_name) is not None
    }
    mapping["mcp_tool"] = _tool_name_for_scope("mcp_tool", registry, user_message, context)
    for spec in registry.list():
        if spec.source == "mcp" and spec.name.startswith("mcp_"):
            mapping[spec.name] = spec.name
    return mapping


async def _execute_unified_agent_loop(
    arxiv_id: str,
    user_message: str,
    context: dict,
    *,
    state: AgentLoopState | None = None,
    approved_scope: str | None = None,
    run_id: str | None = None,
    session_exclude_message_id: str | None = None,
    on_event=None,
) -> AgentLoopResult:
    doc = files.load_document(arxiv_id)
    paper_title = str(context.get("paper_title") or (doc.title if doc else arxiv_id))
    if state is None:
        context["notes_context"] = await build_notes_context(
            arxiv_id,
            user_message,
            context.get("reader") if isinstance(context.get("reader"), dict) else None,
            snippet_budget=NOTES_CONTEXT_CHAR_BUDGET,
        )
    registry = build_agent_tool_registry()
    if _intent_llm_enabled():
        await register_mcp_tool_catalog(registry, get_config().mcp_servers)
    _register_agent_task_tools(registry, arxiv_id, user_message, context)
    _register_memory_tool(registry, arxiv_id)
    _register_skill_tools(registry)
    _register_session_search_tool(registry, exclude_message_id=session_exclude_message_id)
    _register_note_tools(registry, arxiv_id, context)
    current_task = asyncio.current_task()
    if run_id and current_task is not None:
        _RUN_TASKS[run_id] = current_task
    try:
        async with _agent_run_semaphore():
            return await run_iterative_agent_loop(
                get_client(),
                registry,
                messages=None if state is not None else _agent_loop_messages(arxiv_id, user_message, context),
                state=state,
                tools=[
                    *(
                        [tool for tool in TOOL_PLAN_NATIVE_TOOLS if tool["function"]["name"] != "mcp_tool"]
                        if any(spec.source == "mcp" and spec.name.startswith("mcp_") for spec in registry.list())
                        else TOOL_PLAN_NATIVE_TOOLS
                    ),
                    *LOCAL_AGENT_TASK_TOOLS,
                    *LOCAL_MEMORY_TOOLS,
                    *LOCAL_SESSION_TOOLS,
                    *LOCAL_NOTE_TOOLS,
                    *LOCAL_SKILL_TOOLS,
                    *(
                        LOCAL_BROWSER_TOOLS
                        if registry.get("local.browser_control") is not None
                        else []
                    ),
                    *[
                        {
                            "type": "function",
                            "function": {
                                "name": spec.name,
                                "description": spec.description,
                                "parameters": dict(spec.input_schema or {"type": "object", "properties": {}}),
                            },
                        }
                        for spec in registry.list()
                        if spec.source == "mcp" and spec.name.startswith("mcp_")
                    ],
                ],
                scope=approved_scope,
                base_arguments=_tool_arguments(arxiv_id, paper_title, user_message, context),
                tool_name_map=_agent_loop_tool_name_map(registry, user_message, context),
                task="agent_chat",
                variant="low",
                on_event=on_event,
            )
    finally:
        await registry.aclose()
        if run_id and current_task is not None and _RUN_TASKS.get(run_id) is current_task:
            _RUN_TASKS.pop(run_id, None)


def _agent_loop_context(client_context: dict, result: AgentLoopResult) -> dict:
    stored = dict(client_context)
    stored.pop("approved_permission", None)
    stored["agent_loop_state"] = result.state.to_dict()
    if result.pending_permission:
        stored["pending_permission"] = result.pending_permission
    else:
        stored.pop("pending_permission", None)
    return stored


def _agent_loop_client_context(stored_context: dict) -> dict:
    context = dict(stored_context)
    context.pop("agent_loop_state", None)
    context.pop("pending_permission", None)
    return context


def _agent_loop_final_text(
    arxiv_id: str,
    user_message: str,
    context: dict,
    result: AgentLoopResult,
) -> str:
    institution_reply = _institution_background_reply(arxiv_id, user_message, context)
    if institution_reply and _is_insufficient_reply(result.final_text):
        return institution_reply
    if (
        result.status == "error"
        and result.state.tool_calls == 0
        and "model_call_failed" in result.state.limits
    ):
        fallback = (
            institution_reply
            or _metadata_fallback_reply(arxiv_id, user_message, context)
            or _paper_context_reply(arxiv_id, context)
        )
        return fallback + DEGRADED_REPLY_NOTE
    return result.final_text


def _agent_evidence_source(item: dict, metadata: dict) -> dict:
    """Add a stable provenance label without replacing a tool's raw source."""

    enriched = dict(item)
    kind = str(enriched.get("kind") or "")
    tool_source = str(metadata.get("source") or "")
    raw_source = str(enriched.get("source") or "")
    if kind == "agent_note_search_result" or tool_source in {
        "local_notes_search",
        "local_notes_view",
    } or raw_source.startswith("你的笔记"):
        enriched["source_type"] = "user_note"
        enriched["source_label"] = "你的笔记"
    elif kind in {
        "web_search_result",
        "web_fetch_result",
        "external_paper_search_result",
        "semantic_scholar_author_result",
        "mcp_tool_result",
    }:
        enriched["source_type"] = "external_web"
        enriched["source_label"] = "外部网页"
    elif (
        type(enriched.get("block_index")) is int
        or (isinstance(enriched.get("location"), dict) and type(enriched["location"].get("block_index")) is int)
        or re.search(r"(?:block|段落)\s*#?\s*\d+", raw_source, re.I)
    ):
        enriched["source_type"] = "paper"
        enriched["source_label"] = "论文原文"
    else:
        enriched.setdefault("source_type", "tool_result")
        enriched.setdefault("source_label", "工具结果")
    return enriched


def _dedupe_agent_evidence(items: list[dict]) -> list[dict]:
    seen: set[str] = set()
    result: list[dict] = []
    for item in items:
        key = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result[:12]


def _agent_loop_result_data(result: AgentLoopResult, final_text: str) -> dict:
    """Recover structured results and source-labelled evidence from tool messages."""

    collected_evidence: list[dict] = []
    structured_data: dict | None = None
    for message in result.state.messages:
        if message.get("role") != "tool":
            continue
        try:
            payload = json.loads(str(message.get("content") or ""))
        except json.JSONDecodeError:
            continue
        metadata = payload.get("metadata") if isinstance(payload, dict) else None
        candidate = metadata.get("result_data") if isinstance(metadata, dict) else None
        if isinstance(candidate, dict):
            structured_data = _normalize_result_data(candidate, final_text)
        if isinstance(metadata, dict) and isinstance(payload.get("evidence"), list):
            collected_evidence.extend(
                _agent_evidence_source(item, metadata)
                for item in payload["evidence"]
                if isinstance(item, dict)
            )

    data = structured_data or _result_data(final_text)
    existing = [
        _agent_evidence_source(item, {})
        for item in data.get("evidence", [])
        if isinstance(item, dict)
    ]
    data["evidence"] = _dedupe_agent_evidence([*existing, *collected_evidence])
    if final_text:
        data["summary"] = _short_text(final_text, 2_000)
    if result.state.limits:
        data["limits"] = list(dict.fromkeys([*data["limits"], *result.state.limits]))[:8]
    return data


def _agent_loop_permission_request(
    user_message: str,
    run_id: str,
    result: AgentLoopResult,
) -> dict:
    scope = str(result.pending_permission or "")
    pending = result.state.pending_tool_calls[0] if result.state.pending_tool_calls else {}
    arguments = pending.get("arguments") if isinstance(pending.get("arguments"), dict) else {}
    request = {
        "scope": scope,
        "label": PERMISSION_LABELS.get(scope, scope),
        "description": PERMISSION_DESCRIPTIONS.get(scope, "需要确认本次工具调用权限。"),
        "original_message": user_message,
        "reason": _short_text(arguments.get("reason"), 160),
        "run_id": run_id,
    }
    if scope == "memory_write":
        request["memory_proposal"] = {
            "content": _short_text(arguments.get("content"), 1_000),
            "kind": _short_text(arguments.get("kind") or "preference", 80),
        }
    return request


def _record_agent_loop_result(
    arxiv_id: str,
    user_message: str,
    client_context: dict,
    result: AgentLoopResult,
    *,
    existing_run_id: str | None = None,
) -> tuple[dict | None, list[dict]]:
    if existing_run_id:
        latest = get_run(arxiv_id, existing_run_id)
        if latest is None or latest.get("status") == "cancelled":
            return None, []

    stored_context = _agent_loop_context(client_context, result)
    if result.status == "waiting_permission":
        if existing_run_id:
            run = update_run(
                arxiv_id,
                existing_run_id,
                status="waiting_permission",
                context=stored_context,
                error="",
            )
            created_runs: list[dict] = []
        else:
            run = create_run(
                arxiv_id,
                task_type="agent_loop",
                title="Pet 对话",
                user_message=user_message,
                inputs=["当前论文", "当前对话", "本地记忆", "Agent Loop state"],
                context=stored_context,
                status="waiting_permission",
            )
            created_runs = [run]
        if run is None:
            return None, []
        permission = _agent_loop_permission_request(user_message, str(run["id"]), result)
        assistant = append_message(
            arxiv_id,
            "assistant",
            _permission_confirmation_message(permission),
            meta={
                "kind": "permission_request",
                "intent": {"source": "iterative_agent_loop", "permission_scope": result.pending_permission},
                "client_context": client_context,
                "permission_request": permission,
                "created_tasks": [],
                "created_runs": [run] if not existing_run_id else [],
            },
        )
        return assistant, created_runs

    final_text = _agent_loop_final_text(arxiv_id, user_message, client_context, result)
    result_data = _enrich_result_data_for_paper(
        arxiv_id,
        _agent_loop_result_data(result, final_text),
    )
    if existing_run_id:
        terminal_status = "error" if result.status in ("error", "timeout") else "done"
        run = update_run(
            arxiv_id,
            existing_run_id,
            status=terminal_status,
            result=final_text if terminal_status == "done" else "",
            result_data=result_data if terminal_status == "done" else None,
            error=final_text if terminal_status == "error" else "",
            context=stored_context,
        )
        if run is None or run.get("status") == "cancelled":
            return None, []
    assistant = append_message(
        arxiv_id,
        "assistant",
        final_text,
        meta={
            "kind": "agent_loop",
            "run_id": existing_run_id,
            "intent": {"source": "iterative_agent_loop"},
            "client_context": client_context,
            "agent_loop_status": result.status,
            "agent_loop_trace": result.state.trace,
            "agent_loop_limits": result.state.limits,
            "result_data": result_data,
            "created_tasks": [],
            "created_runs": [],
        },
    )
    return assistant, []


async def _send_unified_agent_message(
    arxiv_id: str,
    payload: AgentChatRequest,
) -> AgentChatResponse:
    user_message = append_message(
        arxiv_id,
        "user",
        payload.message,
        meta={"client_context": payload.context} if payload.context else None,
    )
    result = await _execute_unified_agent_loop(
        arxiv_id,
        payload.message,
        payload.context,
        session_exclude_message_id=str(user_message["id"]),
    )
    assistant, created_run_dicts = _record_agent_loop_result(
        arxiv_id,
        payload.message,
        payload.context,
        result,
    )
    if assistant is None:
        raise HTTPException(status_code=409, detail="Agent Loop 已取消")
    state = _build_state(arxiv_id)
    return AgentChatResponse(
        **state.model_dump(),
        assistant_message=AgentMessage(**assistant),
        created_tasks=[],
        created_runs=[AgentRunItem(**run) for run in created_run_dicts],
        saved_memory=None,
    )


@router.get("/memories", response_model=list[AgentMemoryItem])
async def get_agent_memories(
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> list[AgentMemoryItem]:
    return [AgentMemoryItem(**item) for item in load_memories(limit=limit)]


@router.post("/memories", response_model=AgentMemoryItem)
async def create_agent_memory(payload: AgentMemoryCreateRequest) -> AgentMemoryItem:
    if payload.arxiv_id is not None and await get_paper(payload.arxiv_id) is None:
        raise HTTPException(status_code=404, detail=f"论文未找到: {payload.arxiv_id}")
    memory = add_memory(
        payload.content,
        kind=payload.kind,
        arxiv_id=payload.arxiv_id,
        source="manual",
    )
    return AgentMemoryItem(**memory)


@router.patch("/memories/{memory_id}", response_model=AgentMemoryItem)
async def edit_agent_memory(
    memory_id: str,
    payload: AgentMemoryUpdateRequest,
) -> AgentMemoryItem:
    try:
        memory = update_memory(memory_id, content=payload.content, kind=payload.kind)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if memory is None:
        raise HTTPException(status_code=404, detail=f"记忆未找到: {memory_id}")
    return AgentMemoryItem(**memory)


@router.delete("/memories/{memory_id}", response_model=AgentMemoryItem)
async def remove_agent_memory(memory_id: str) -> AgentMemoryItem:
    memory = delete_memory(memory_id)
    if memory is None:
        raise HTTPException(status_code=404, detail=f"记忆未找到: {memory_id}")
    return AgentMemoryItem(**memory)


@router.get("/skill-proposals", response_model=list[AgentSkillProposalItem])
async def get_skill_proposals(status: str | None = Query(default=None)) -> list[AgentSkillProposalItem]:
    return [AgentSkillProposalItem(**proposal) for proposal in load_skill_proposals(status=status)]


@router.get("/skill-proposals/{proposal_id}", response_model=AgentSkillProposalItem)
async def get_agent_skill_proposal(proposal_id: str) -> AgentSkillProposalItem:
    proposal = get_skill_proposal(proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail=f"Skill 提案未找到: {proposal_id}")
    return AgentSkillProposalItem(**proposal)


@router.post("/skill-proposals", response_model=AgentSkillProposalItem)
async def propose_agent_skill(payload: AgentSkillProposalRequest) -> AgentSkillProposalItem:
    try:
        proposal = create_skill_proposal(payload.skill.model_dump(), payload.action)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return AgentSkillProposalItem(**proposal)


@router.post("/skill-proposals/{proposal_id}/apply", response_model=AgentSkillProposalItem)
async def apply_agent_skill_proposal(proposal_id: str) -> AgentSkillProposalItem:
    proposal = apply_skill_proposal(proposal_id)
    if proposal is None:
        raise HTTPException(status_code=409, detail="Skill 提案不存在或已处理")
    return AgentSkillProposalItem(**proposal)


@router.post("/skill-proposals/{proposal_id}/reject", response_model=AgentSkillProposalItem)
async def reject_agent_skill_proposal(proposal_id: str) -> AgentSkillProposalItem:
    proposal = reject_skill_proposal(proposal_id)
    if proposal is None:
        raise HTTPException(status_code=409, detail="Skill 提案不存在或已处理")
    return AgentSkillProposalItem(**proposal)


@router.get("/chats", response_model=list[AgentChatSummary])
async def get_agent_chats(limit: int = 50) -> list[AgentChatSummary]:
    summaries = list_chat_summaries(limit=limit)
    result: list[AgentChatSummary] = []
    for item in summaries:
        paper = await get_paper(item["arxiv_id"])
        result.append(
            AgentChatSummary(
                arxiv_id=item["arxiv_id"],
                paper_title=paper.get("title") if paper else None,
                paper_exists=paper is not None,
                message_count=item["message_count"],
                last_role=item["last_role"],
                last_message=item["last_message"],
                updated_at=item.get("updated_at"),
            )
        )
    return result


@router.get("/sessions/search", response_model=list[AgentSessionSearchResult])
async def search_agent_session_history(
    q: Annotated[str, Query(min_length=1, max_length=200)],
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> list[AgentSessionSearchResult]:
    return [AgentSessionSearchResult(**item) for item in await search_agent_sessions(q, limit=limit)]


@router.get("/chat/{arxiv_id}", response_model=AgentChatState)
async def get_agent_chat(arxiv_id: str) -> AgentChatState:
    paper = await get_paper(arxiv_id)
    if paper is None:
        raise HTTPException(status_code=404, detail=f"论文未找到: {arxiv_id}")
    return _build_state(arxiv_id)


@router.delete("/chat/{arxiv_id}", response_model=AgentChatState)
async def clear_agent_chat(arxiv_id: str) -> AgentChatState:
    """清空当前论文对话；取消 running runs，保留 runs 历史与全局 memory。"""
    for run in load_runs(arxiv_id, limit=10_000):
        if run.get("status") not in ("running", "waiting_permission"):
            continue
        cancelled = cancel_run(arxiv_id, str(run.get("id") or ""))
        _cancel_registered_run_task(str(run.get("id") or ""))
        task_id = cancelled.get("task_id") if cancelled else None
        if task_id:
            await update_agent_task(int(task_id), "cancelled", f"{cancelled.get('title', 'Agent Run')} 已取消")
    clear_chat(arxiv_id)
    await sync_agent_session_index()
    return _build_state(arxiv_id)


@router.post("/chat/{arxiv_id}/messages", response_model=AgentChatResponse)
async def send_agent_message(
    arxiv_id: str,
    payload: AgentChatRequest,
    background_tasks: BackgroundTasks,
) -> AgentChatResponse:
    paper = await get_paper(arxiv_id)
    if paper is None:
        raise HTTPException(status_code=404, detail=f"论文未找到: {arxiv_id}")

    if _uses_unified_agent_loop(payload.message, payload.context):
        return await _send_unified_agent_message(arxiv_id, payload)

    approved_permission = payload.context.get("approved_permission")
    if not approved_permission:
        append_message(
            arxiv_id,
            "user",
            payload.message,
            meta={"client_context": payload.context} if payload.context else None,
        )
    tool_plan: dict | None = None
    if _should_use_tool_planner(payload.message, payload.context):
        tool_plan = await _plan_agent_action_llm(arxiv_id, payload.message, payload.context)
        if tool_plan is not None:
            payload.context["tool_plan"] = tool_plan
    llm_intent: dict | None = None
    if approved_permission in ("external_search", "mcp_tool"):
        # 已批准的外部检索 / MCP 重发必须确定性落到工具执行：不重新判定意图，
        # 否则"复现/方法/问题"等关键词会把批准的检索劫持成本地 Run，
        # 用户批准的外部查询永远不会执行
        intent = {
            "task_type": "external_tool_request",
            "confidence": "high",
            "source": "approved_permission",
        }
        task_type: str | None = "external_tool_request"
    elif tool_plan is not None and tool_plan.get("action") == "tool_request":
        intent = {
            "category": "tool_request",
            "task_type": None,
            "permission_scope": tool_plan.get("permission_scope"),
            "confidence": tool_plan.get("confidence", "medium"),
            "reason": tool_plan.get("user_facing_reason", ""),
            "source": "llm_tool_plan",
        }
        task_type = None
    else:
        # LLM 意图分类优先（approved 重发除外）；分类失败回退关键词规则管线
        if not approved_permission:
            llm_intent = await _classify_message_llm(arxiv_id, payload.message, payload.context)
        if llm_intent is not None:
            intent = llm_intent
            task_type = llm_intent.get("task_type")
        else:
            intent = infer_agent_intent(payload.message)
            task_type = intent["task_type"]
            if approved_permission and task_type is None:
                # 其他已确认权限（long_task）无明确任务时也落到外部工具请求
                task_type = "external_tool_request"
            else:
                task_type = _contextual_task_type(payload.message, payload.context, task_type)
                task_type = _prefer_selection_explanation(payload.message, payload.context, task_type)
            intent["task_type"] = task_type

    # MCP 配置向导：纯配置操作，不建任务、不触发权限（LLM 类别优先，关键词兜底）
    wizard_request = False
    if not approved_permission:
        if llm_intent is not None:
            wizard_request = llm_intent.get("category") == "mcp_config_wizard"
        else:
            wizard_request = _is_mcp_config_request(payload.message)
    if wizard_request:
        task_type = None
        intent = {**intent, "task_type": None, "category": "mcp_config_wizard"}

    saved_memory = None
    # 已批准权限的重发是同一条消息第二次入站，首轮已做过记忆判定，不重复存
    wants_memory = False
    if not approved_permission and _intent_llm_enabled():
        wants_memory = (
            bool(llm_intent.get("save_memory"))
            if llm_intent is not None
            else should_save_memory(payload.message)
        )
    if wants_memory:
        saved_memory = add_memory(
            payload.message,
            kind="preference",
            arxiv_id=arxiv_id,
            source="chat",
        )

    # 已批准权限的重发不再出任何权限卡：LLM 与关键词对 scope 判定不一致时，
    # 关键词规则会对已批准的消息误弹第二张卡；配置向导消息含 "mcp" 也会误触发
    if approved_permission or wizard_request:
        permission = None
    elif tool_plan is not None and tool_plan.get("action") == "tool_request":
        permission = _permission_from_tool_plan(tool_plan, payload.message, payload.context)
    elif llm_intent is not None:
        permission = _permission_from_llm_intent(llm_intent, payload.message, payload.context)
    else:
        permission = _permission_request(payload.message, payload.context)
    if permission:
        assistant = append_message(
            arxiv_id,
            "assistant",
            _permission_confirmation_message(permission),
            meta={
                "kind": "permission_request",
                "intent": intent,
                "client_context": payload.context,
                "permission_request": permission,
            },
        )
        state = _build_state(arxiv_id)
        return AgentChatResponse(
            **state.model_dump(),
            assistant_message=AgentMessage(**assistant),
            created_tasks=[],
            created_runs=[],
            saved_memory=AgentMemoryItem(**saved_memory) if saved_memory else None,
        )

    created_tasks: list[AgentCreatedTask] = []
    created_runs: list[AgentRunItem] = []
    if task_type:
        summary = TASK_SUMMARIES.get(task_type, f"子 Agent 计划：{task_type}")
        title = TASK_TITLES.get(task_type, task_type)
        task_id = await create_agent_task(
            arxiv_id=arxiv_id,
            task_type=task_type,
            summary=summary,
        )
        inputs = ["当前论文", "当前对话", "本地记忆", "相关缓存"]
        if payload.context.get("reader"):
            inputs.append("阅读页段落/选区上下文")
        if approved_permission:
            inputs.append(f"已确认权限：{PERMISSION_LABELS.get(str(approved_permission), str(approved_permission))}")
        run = create_run(
            arxiv_id=arxiv_id,
            task_type=task_type,
            title=title,
            user_message=payload.message,
            task_id=task_id,
            inputs=inputs,
            context=payload.context,
        )
        background_tasks.add_task(_finish_agent_run, arxiv_id, run["id"], task_id)
        created_tasks.append(
            AgentCreatedTask(
                id=task_id,
                task_type=task_type,
                summary=summary,
                status="running",
            )
        )
        created_runs.append(AgentRunItem(**run))

    mcp_config_draft: dict | None = None
    if wizard_request:
        # 配置向导：从内置目录出确定性草稿，等用户在卡片上确认写入
        draft = _build_mcp_config_draft(payload.message)
        mcp_config_draft = draft["server"]
        reply_text = draft["reply"]
    elif task_type:
        reply_text = _assistant_reply(arxiv_id, task_type, saved_memory is not None, payload.context)
    elif llm_intent is not None and llm_intent.get("category") == "mcp_status":
        # 配置状态自省：直接读配置回答，不调用 MCP、不再走 LLM
        reply_text = _mcp_status_reply_text()
    elif saved_memory is not None and llm_intent is None:
        # 关键词兜底路径没有"纯偏好陈述"的判定信号，保持既有确认模板
        reply_text = _assistant_reply(arxiv_id, task_type, True, payload.context)
    else:
        # 普通对话：走 LLM 带最近对话窗口/记忆/阅读上下文，失败回退规则回复
        reply_text = await _chat_reply(
            arxiv_id,
            payload.message,
            payload.context,
            llm_category=llm_intent.get("category") if llm_intent is not None else None,
        )
        if saved_memory is not None:
            # LLM 判定"偏好 + 提问"混合消息：记下偏好的同时仍要回答问题本身
            reply_text = f"我先把这条偏好记进阅读记忆。\n\n{reply_text}"

    assistant_meta: dict = {
        "intent": intent,
        "client_context": payload.context,
        "created_tasks": [task.model_dump() for task in created_tasks],
        "created_runs": [run.model_dump() for run in created_runs],
    }
    if tool_plan is not None:
        assistant_meta["tool_plan"] = tool_plan
    if mcp_config_draft is not None:
        assistant_meta["mcp_config_draft"] = mcp_config_draft
    assistant = append_message(
        arxiv_id,
        "assistant",
        reply_text,
        meta=assistant_meta,
    )
    state = _build_state(arxiv_id)
    return AgentChatResponse(
        **state.model_dump(),
        assistant_message=AgentMessage(**assistant),
        created_tasks=created_tasks,
        created_runs=created_runs,
        saved_memory=AgentMemoryItem(**saved_memory) if saved_memory else None,
    )


def _sse_event(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _agent_event_from_loop_event(event: dict) -> dict | None:
    event_type = str(event.get("type") or "")
    if event_type == "model_start" and int(event.get("iteration") or 1) > 1:
        return {"status": "finalizing", "message": "正在整理结论"}
    if event_type == "permission_required":
        return {"status": "waiting_permission", "message": "需要你确认后才能继续"}
    if event_type == "tool_start":
        tool_name = str(event.get("tool") or "")
        if "web_fetch" in tool_name:
            message = "正在阅读网页"
        elif "external_search" in tool_name or "web_search" in tool_name:
            message = "正在查找资料"
        elif tool_name.startswith("mcp:") or "mcp_tool" in tool_name:
            message = "正在使用已连接的工具"
        else:
            message = "正在处理资料"
        return {"status": "planning", "message": message}
    if event_type == "tool_error":
        return {"status": "finalizing", "message": "正在根据失败结果调整回答"}
    return None


def _is_technical_tool_event(event: dict) -> bool:
    return str(event.get("type") or "") in {
        "tool_start",
        "tool_done",
        "tool_error",
        "permission_required",
    }


async def _stream_plain_chat_events(arxiv_id: str, payload: AgentChatRequest, plan: dict):
    append_message(
        arxiv_id,
        "user",
        payload.message,
        meta={"client_context": payload.context} if payload.context else None,
    )
    reply_parts: list[str] = []
    async for delta in _stream_chat_reply_text(
        arxiv_id,
        payload.message,
        payload.context,
    ):
        reply_parts.append(delta)
        yield _sse_event("delta", {"text": delta})

    reply_text = "".join(reply_parts).strip()
    assistant_meta: dict = {
        "intent": plan["intent"],
        "client_context": payload.context,
        "created_tasks": [],
        "created_runs": [],
        "streamed": True,
    }
    assistant = append_message(
        arxiv_id,
        "assistant",
        reply_text,
        meta=assistant_meta,
    )
    yield _sse_event(
        "message",
        {
            "assistant_message": AgentMessage(**assistant).model_dump(),
            "created_tasks": [],
            "created_runs": [],
            "saved_memory": None,
        },
    )
    yield _sse_event("done", {"state": _build_state(arxiv_id).model_dump()})


async def _stream_unified_agent_events(arxiv_id: str, payload: AgentChatRequest):
    user_message = append_message(
        arxiv_id,
        "user",
        payload.message,
        meta={"client_context": payload.context} if payload.context else None,
    )
    queue: asyncio.Queue[dict] = asyncio.Queue()

    async def on_event(event: dict) -> None:
        await queue.put(event)

    task = asyncio.create_task(
        _execute_unified_agent_loop(
            arxiv_id,
            payload.message,
            payload.context,
            session_exclude_message_id=str(user_message["id"]),
            on_event=on_event,
        )
    )
    yield _sse_event("agent_event", {"status": "planning", "message": "正在理解你的问题"})
    streamed_iterations: set[int] = set()
    while not task.done() or not queue.empty():
        try:
            event = await asyncio.wait_for(queue.get(), timeout=0.1)
        except asyncio.TimeoutError:
            continue
        if event.get("type") == "model_delta":
            streamed_iterations.add(int(event.get("iteration") or 0))
            yield _sse_event("delta", {"text": str(event.get("text") or "")})
            continue
        agent_event = _agent_event_from_loop_event(event)
        if agent_event is not None:
            yield _sse_event("agent_event", agent_event)
        if _is_technical_tool_event(event):
            yield _sse_event("tool_event", event)
    result = await task
    if result.status != "waiting_permission":
        yield _sse_event("agent_event", {"status": "finalizing", "message": "正在整理结论"})
    assistant, created_run_dicts = _record_agent_loop_result(
        arxiv_id,
        payload.message,
        payload.context,
        result,
    )
    if assistant is not None:
        if result.status != "waiting_permission" and (
            result.status != "completed" or result.state.model_iterations not in streamed_iterations
        ):
            yield _sse_event("delta", {"text": assistant["content"]})
        yield _sse_event(
            "message",
            {
                "assistant_message": AgentMessage(**assistant).model_dump(),
                "created_tasks": [],
                "created_runs": [AgentRunItem(**run).model_dump() for run in created_run_dicts],
                "saved_memory": None,
            },
        )
    yield _sse_event("done", {"state": _build_state(arxiv_id).model_dump()})


@router.post("/chat/{arxiv_id}/messages/stream")
async def stream_agent_message(
    arxiv_id: str,
    payload: AgentChatRequest,
) -> StreamingResponse:
    paper = await get_paper(arxiv_id)
    if paper is None:
        raise HTTPException(status_code=404, detail=f"论文未找到: {arxiv_id}")

    async def event_generator():
        if _uses_unified_agent_loop(payload.message, payload.context):
            async for event in _stream_unified_agent_events(arxiv_id, payload):
                yield event
            return

        plain_plan = await _plain_chat_stream_plan(arxiv_id, payload)
        if plain_plan is not None:
            async for event in _stream_plain_chat_events(arxiv_id, payload, plain_plan):
                yield event
            return

        response = await send_agent_message(arxiv_id, payload, BackgroundTasks())
        yield _sse_event(
            "message",
            {
                "assistant_message": response.assistant_message.model_dump(),
                "created_tasks": [task.model_dump() for task in response.created_tasks],
                "created_runs": [run.model_dump() for run in response.created_runs],
                "saved_memory": response.saved_memory.model_dump() if response.saved_memory else None,
            },
        )
        run = next(
            (item for item in response.created_runs if item.task_type == "external_tool_request" and item.task_id),
            None,
        )
        if run is None:
            yield _sse_event("done", {"state": _build_state(arxiv_id).model_dump()})
            return

        queue: asyncio.Queue[dict] = asyncio.Queue()

        async def on_tool_event(event: dict) -> None:
            await queue.put(event)

        task = asyncio.create_task(
            _finish_agent_run(arxiv_id, run.id, int(run.task_id), on_tool_event=on_tool_event)
        )
        while not task.done() or not queue.empty():
            try:
                event = await asyncio.wait_for(queue.get(), timeout=0.1)
            except asyncio.TimeoutError:
                continue
            yield _sse_event("tool_event", event)
        try:
            await task
        except asyncio.CancelledError:
            pass
        yield _sse_event("done", {"state": _build_state(arxiv_id).model_dump()})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/chat/{arxiv_id}/runs/{run_id}/resume/stream")
async def resume_agent_run_stream(
    arxiv_id: str,
    run_id: str,
    payload: AgentRunResumeRequest,
) -> StreamingResponse:
    paper = await get_paper(arxiv_id)
    if paper is None:
        raise HTTPException(status_code=404, detail=f"论文未找到: {arxiv_id}")
    run = get_run(arxiv_id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Agent Run 未找到: {run_id}")
    if run.get("task_type") != "agent_loop" or run.get("status") != "waiting_permission":
        raise HTTPException(status_code=409, detail="Agent Run 当前不在等待权限状态")
    run_context = run.get("context") if isinstance(run.get("context"), dict) else {}
    client_context = _agent_loop_client_context(run_context)
    expected_scope = str(run_context.get("pending_permission") or "")
    if payload.approved_permission != expected_scope:
        raise HTTPException(status_code=409, detail=f"等待权限为 {expected_scope or 'unknown'}，不能批准其他 scope")
    raw_state = run_context.get("agent_loop_state")
    if not isinstance(raw_state, dict):
        raise HTTPException(status_code=409, detail="Agent Run 缺少可恢复的 loop state")
    loop_state = AgentLoopState.from_dict(raw_state)
    if not loop_state.pending_tool_calls:
        raise HTTPException(status_code=409, detail="Agent Run 没有待恢复的 tool call")
    updated = update_run(arxiv_id, run_id, status="running")
    if updated is None or updated.get("status") != "running":
        raise HTTPException(status_code=409, detail="Agent Run 无法进入恢复状态")

    async def event_generator():
        queue: asyncio.Queue[dict] = asyncio.Queue()

        async def on_event(event: dict) -> None:
            await queue.put(event)

        task = asyncio.create_task(
            _execute_unified_agent_loop(
                arxiv_id,
                str(run.get("user_message") or ""),
                client_context,
                state=loop_state,
                approved_scope=payload.approved_permission,
                run_id=run_id,
                on_event=on_event,
            )
        )
        yield _sse_event("agent_event", {"status": "resumed", "message": "已确认，继续处理"})
        streamed_iterations: set[int] = set()
        try:
            while not task.done() or not queue.empty():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    continue
                if event.get("type") == "model_delta":
                    streamed_iterations.add(int(event.get("iteration") or 0))
                    yield _sse_event("delta", {"text": str(event.get("text") or "")})
                    continue
                agent_event = _agent_event_from_loop_event(event)
                if agent_event is not None:
                    yield _sse_event("agent_event", agent_event)
                if _is_technical_tool_event(event):
                    yield _sse_event("tool_event", event)
            result = await task
        except asyncio.CancelledError:
            task.cancel()
            cancel_run(arxiv_id, run_id)
            yield _sse_event("done", {"state": _build_state(arxiv_id).model_dump()})
            return
        except Exception as exc:
            update_run(arxiv_id, run_id, status="error", error=str(exc))
            assistant = append_message(
                arxiv_id,
                "assistant",
                "这次恢复没有完成：Agent Loop 执行失败。请稍后重试。",
                meta={"kind": "agent_loop", "run_id": run_id, "agent_loop_status": "error"},
            )
            yield _sse_event("delta", {"text": assistant["content"]})
            yield _sse_event(
                "message",
                {
                    "assistant_message": AgentMessage(**assistant).model_dump(),
                    "created_tasks": [],
                    "created_runs": [],
                    "saved_memory": None,
                },
            )
            yield _sse_event("done", {"state": _build_state(arxiv_id).model_dump()})
            return

        if result.status != "waiting_permission":
            # The loop may finish immediately after the last tool event; emit a
            # stable human status even when the provider did not stream text.
            yield _sse_event("agent_event", {"status": "finalizing", "message": "正在整理结论"})
        assistant, _ = _record_agent_loop_result(
            arxiv_id,
            str(run.get("user_message") or ""),
            client_context,
            result,
            existing_run_id=run_id,
        )
        if assistant is not None:
            if result.status != "waiting_permission" and (
                result.status != "completed" or result.state.model_iterations not in streamed_iterations
            ):
                yield _sse_event("delta", {"text": assistant["content"]})
            yield _sse_event(
                "message",
                {
                    "assistant_message": AgentMessage(**assistant).model_dump(),
                    "created_tasks": [],
                    "created_runs": [],
                    "saved_memory": None,
                },
            )
        yield _sse_event("done", {"state": _build_state(arxiv_id).model_dump()})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/chat/{arxiv_id}/runs/{run_id}/cancel", response_model=AgentRunItem)
async def cancel_agent_run(arxiv_id: str, run_id: str) -> AgentRunItem:
    paper = await get_paper(arxiv_id)
    if paper is None:
        raise HTTPException(status_code=404, detail=f"论文未找到: {arxiv_id}")
    run = cancel_run(arxiv_id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Agent Run 未找到: {run_id}")
    _cancel_registered_run_task(run_id)
    task_id = run.get("task_id")
    if task_id and run.get("status") == "cancelled":
        await update_agent_task(int(task_id), "cancelled", f"{run.get('title', 'Agent Run')} 已取消")
    return AgentRunItem(**run)


class MCPConfigConfirmRequest(BaseModel):
    server: dict


@router.post("/chat/{arxiv_id}/mcp-config/confirm", response_model=AgentChatResponse)
async def confirm_mcp_config(
    arxiv_id: str,
    payload: MCPConfigConfirmRequest,
    _: Annotated[None, Depends(_require_admin)] = None,
) -> AgentChatResponse:
    """确认 Pet 配置向导草稿：校验后写入 config.yaml。

    受 PEINIDU_ADMIN_TOKEN 保护（与 /config 一致）；服务端强制 enabled=false 落盘，
    向导永不静默启用或执行命令——测试连接与启用都在设置页人工完成。
    """
    paper = await get_paper(arxiv_id)
    if paper is None:
        raise HTTPException(status_code=404, detail=f"论文未找到: {arxiv_id}")
    data = dict(payload.server)
    data["enabled"] = False
    try:
        server = MCPServerConfig(**data)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"MCP 配置无效: {e}") from e
    if not server.name.strip():
        raise HTTPException(status_code=422, detail="MCP server 需要名称")
    if server.transport == "stdio" and not server.command.strip():
        raise HTTPException(status_code=422, detail="stdio MCP 需要 command")
    if server.transport == "http" and not (server.url or "").strip():
        raise HTTPException(status_code=422, detail="http MCP 需要 url")

    config = get_config()
    existing = {item.name for item in config.mcp_servers}
    if server.name in existing:
        # 草稿生成时已去重；并发/重复确认时再兜一层
        base = server.name
        suffix = 2
        while f"{base}-{suffix}" in existing:
            suffix += 1
        server = server.model_copy(update={"name": f"{base}-{suffix}"})
    new_config = config.model_copy(update={"mcp_servers": [*config.mcp_servers, server]})
    try:
        save_config(new_config)
        reset_config()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"写入配置失败: {e}") from e

    assistant = append_message(
        arxiv_id,
        "assistant",
        (
            f"已把 MCP server `{server.name}` 写入配置，当前保持未启用。"
            "去设置页可以先「测试连接」，确认可用后再启用；"
            "启用后我调用它之前，仍会像现在一样先向你要权限确认。"
        ),
        meta={"kind": "mcp_config_written", "server_name": server.name},
    )
    state = _build_state(arxiv_id)
    return AgentChatResponse(
        **state.model_dump(),
        assistant_message=AgentMessage(**assistant),
        created_tasks=[],
        created_runs=[],
        saved_memory=None,
    )


@router.post("/chat/{arxiv_id}/memory", response_model=AgentMemoryItem)
async def save_agent_memory(arxiv_id: str, payload: AgentMemoryRequest) -> AgentMemoryItem:
    paper = await get_paper(arxiv_id)
    if paper is None:
        raise HTTPException(status_code=404, detail=f"论文未找到: {arxiv_id}")
    memory = add_memory(
        payload.content,
        kind=payload.kind,
        arxiv_id=arxiv_id,
        source="manual",
    )
    return AgentMemoryItem(**memory)
