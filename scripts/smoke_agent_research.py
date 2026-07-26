#!/usr/bin/env python3
"""Public-data smoke for model -> external search -> evidence -> final answer."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from backend.agent.quality_gate import validate_evidence_item  # noqa: E402
from backend.agent.tool_loop import run_iterative_agent_loop  # noqa: E402
from backend.api.routes_agent_chat import (  # noqa: E402
    AGENT_LOOP_SYSTEM_SUFFIX,
    CHAT_SYSTEM_PROMPT,
    TOOL_PLAN_NATIVE_NAME_MAP,
    TOOL_PLAN_NATIVE_TOOLS,
    _agent_loop_result_data,
)
from backend.llm.client import get_client  # noqa: E402
from backend.tools import build_agent_tool_registry  # noqa: E402


async def main() -> int:
    registry = build_agent_tool_registry()
    try:
        result = await run_iterative_agent_loop(
            get_client(),
            registry,
            messages=[
                {
                    "role": "system",
                    "content": CHAT_SYSTEM_PROMPT + AGENT_LOOP_SYSTEM_SUFFIX,
                },
                {
                    "role": "user",
                    "content": (
                        "这是只含公开论文信息的真实链路冒烟，不含本地聊天、记忆或用户笔记。\n"
                        "论文原文线索：Attention Is All You Need 提出仅基于 attention 的 Transformer，"
                        "arXiv:1706.03762。\n"
                        "用户当前消息：请检索这篇论文的外部论文记录或相关研究，"
                        "再基于实际工具证据给出简短回答并说明限制。"
                    ),
                },
            ],
            tools=[
                tool
                for tool in TOOL_PLAN_NATIVE_TOOLS
                if tool["function"]["name"] != "mcp_tool"
            ],
            scope="external_search",
            base_arguments={
                "query": "Attention Is All You Need related research",
                "paper_title": "Attention Is All You Need",
                "exclude_arxiv_id": "1706.03762",
            },
            tool_name_map=TOOL_PLAN_NATIVE_NAME_MAP,
            task="agent_chat",
            variant="low",
        )
    finally:
        await registry.aclose()

    data = _agent_loop_result_data(result, result.final_text)
    external = [
        item
        for item in data["evidence"]
        if item.get("source_type") == "external_web"
    ]
    valid_external = [item for item in external if validate_evidence_item(item)]
    print(
        {
            "status": result.status,
            "tool_calls": result.state.tool_calls,
            "tool_events": [
                {
                    "tool": event.get("tool"),
                    "status": event.get("status"),
                    "evidence_count": event.get("evidence_count"),
                    "error": bool(event.get("error")),
                }
                for event in result.state.trace
                if event.get("type") in {"tool_done", "tool_error"}
            ],
            "external_evidence": len(external),
            "valid_external_evidence": len(valid_external),
            "answer_chars": len(result.final_text),
            "limits": result.state.limits,
        }
    )
    return 0 if (
        result.status in {"completed", "limited"}
        and result.state.tool_calls >= 1
        and valid_external
        and bool(result.final_text.strip())
    ) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
