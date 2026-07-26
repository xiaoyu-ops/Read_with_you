"""Fixed-sample quality gate for the Pet Agent's first decision and evidence."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse


@dataclass(frozen=True)
class AgentQualityCase:
    id: str
    category: str
    message: str
    expected_action: str
    expected_tools: tuple[str, ...] = ()
    forbidden_tools: tuple[str, ...] = ()
    required_text: tuple[str, ...] = ()
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentQualityResult:
    case_id: str
    passed: bool
    action_correct: bool
    forbidden_tool_used: bool
    missing_text: tuple[str, ...]
    observed_action: str
    observed_tools: tuple[str, ...]


def load_quality_cases(path: str | Path) -> list[AgentQualityCase]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list) or len(raw) < 24:
        raise ValueError("Agent quality fixture must contain at least 24 cases")
    cases: list[AgentQualityCase] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("Agent quality case must be an object")
        case_id = str(item.get("id") or "").strip()
        expected_action = str(item.get("expected_action") or "").strip()
        if not case_id or case_id in seen:
            raise ValueError("Agent quality case ids must be non-empty and unique")
        if expected_action not in {"direct", "clarify", "tool"}:
            raise ValueError(f"Unsupported expected_action for {case_id}")
        seen.add(case_id)
        cases.append(
            AgentQualityCase(
                id=case_id,
                category=str(item.get("category") or "").strip(),
                message=str(item.get("message") or "").strip(),
                expected_action=expected_action,
                expected_tools=tuple(_string_list(item.get("expected_tools"))),
                forbidden_tools=tuple(_string_list(item.get("forbidden_tools"))),
                required_text=tuple(_string_list(item.get("required_text"))),
                context=dict(item.get("context")) if isinstance(item.get("context"), dict) else {},
            )
        )
    return cases


def evaluate_quality_response(
    case: AgentQualityCase,
    response: Mapping[str, Any],
) -> AgentQualityResult:
    content = str(response.get("content") or "").strip()
    raw_calls = response.get("tool_calls")
    calls = raw_calls if isinstance(raw_calls, list) else []
    tools = tuple(
        str(call.get("name") or "").strip()
        for call in calls
        if isinstance(call, Mapping) and str(call.get("name") or "").strip()
    )
    refusal_markers = (
        "不能编造",
        "无法确定",
        "无法确认",
        "未披露",
        "没有证据",
        "证据不足",
        "不确定",
        "不能确定",
        "不应猜",
    )
    clarification_markers = (
        "请明确",
        "请提供",
        "请说明",
        "请指定",
        "需要你",
        "你想比较",
        "想比较什么",
        "比较哪些",
        "比较哪",
        "想对比",
        "具体是",
        "具体指",
        "指的是",
        "具体内容",
        "请告诉",
        "请补充",
        "期望的产物",
    )
    if tools:
        action = "tool"
    elif (
        any(
            marker in content[:100] or (len(content) <= 160 and marker in content)
            for marker in clarification_markers
        )
        and not any(marker in content for marker in refusal_markers)
    ):
        action = "clarify"
    else:
        action = "direct"

    action_correct = action == case.expected_action
    if case.expected_action == "tool":
        action_correct = action_correct and bool(tools) and tools[0] in case.expected_tools
    forbidden_tool_used = any(tool in case.forbidden_tools for tool in tools)
    missing_text = tuple(text for text in case.required_text if text not in content)
    return AgentQualityResult(
        case_id=case.id,
        passed=action_correct and not forbidden_tool_used and not missing_text,
        action_correct=action_correct,
        forbidden_tool_used=forbidden_tool_used,
        missing_text=missing_text,
        observed_action=action,
        observed_tools=tools,
    )


def quality_summary(results: list[AgentQualityResult]) -> dict[str, Any]:
    total = len(results)
    correct = sum(1 for result in results if result.action_correct)
    forbidden = sum(1 for result in results if result.forbidden_tool_used)
    passed = sum(1 for result in results if result.passed)
    return {
        "total": total,
        "passed": passed,
        "tool_selection_accuracy": correct / total if total else 0.0,
        "forbidden_tool_violations": forbidden,
        "all_required_text_present": all(not result.missing_text for result in results),
    }


def validate_evidence_item(item: Mapping[str, Any]) -> bool:
    """Validate source-specific anchors before a quality run counts evidence."""

    source_type = str(item.get("source_type") or "")
    if source_type == "paper":
        location = item.get("location")
        if not isinstance(location, Mapping):
            return False
        block_index = location.get("block_index")
        if not isinstance(block_index, int) or isinstance(block_index, bool) or block_index < 0:
            return False
        page = location.get("page")
        region_id = location.get("region_id")
        return (page is None and region_id is None) or (
            isinstance(page, int)
            and not isinstance(page, bool)
            and page > 0
            and isinstance(region_id, str)
            and bool(region_id.strip())
        )
    if source_type == "external_web":
        url = str(item.get("url") or "")
        parsed = urlparse(url)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    if source_type == "user_note":
        return bool(str(item.get("arxiv_id") or "").strip()) and bool(
            str(item.get("annotation_id") or item.get("note_heading") or "").strip()
        )
    return False


def _string_list(value: object) -> list[str]:
    return [
        str(item).strip()
        for item in value
        if str(item).strip()
    ] if isinstance(value, list) else []
