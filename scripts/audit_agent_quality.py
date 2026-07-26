#!/usr/bin/env python3
"""Run the fixed Pet Agent decision-quality set against the configured provider."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from backend.agent.quality_gate import (  # noqa: E402
    evaluate_quality_response,
    load_quality_cases,
    quality_summary,
)
from backend.api.routes_agent_chat import (  # noqa: E402
    LOCAL_AGENT_TASK_TOOLS,
    LOCAL_BROWSER_TOOLS,
    LOCAL_MEMORY_TOOLS,
    LOCAL_NOTE_TOOLS,
    LOCAL_SESSION_TOOLS,
    TOOL_PLAN_NATIVE_TOOLS,
    AGENT_LOOP_SYSTEM_SUFFIX,
    CHAT_SYSTEM_PROMPT,
)
from backend.llm.client import get_client  # noqa: E402
from backend.tools import local_browser_control_enabled  # noqa: E402


DEFAULT_FIXTURE = ROOT / "backend/tests/fixtures/agent_quality_cases.json"


async def audit(fixture: Path, arxiv_id: str) -> int:
    cases = load_quality_cases(fixture)
    tools = [
        *[tool for tool in TOOL_PLAN_NATIVE_TOOLS if tool["function"]["name"] != "mcp_tool"],
        *LOCAL_AGENT_TASK_TOOLS,
        *LOCAL_MEMORY_TOOLS,
        *LOCAL_SESSION_TOOLS,
        *LOCAL_NOTE_TOOLS,
        *(LOCAL_BROWSER_TOOLS if local_browser_control_enabled() else []),
    ]
    client = get_client()
    results = []
    for case in cases:
        # The live gate intentionally sends only this checked-in synthetic
        # fixture. It never loads local chat history, memories or real notes.
        messages = [
            {
                "role": "system",
                "content": CHAT_SYSTEM_PROMPT + AGENT_LOOP_SYSTEM_SUFFIX,
            },
            {
                "role": "user",
                "content": (
                    "固定质量样例；论文标题：Synthetic Research Paper；"
                    f"论文 ID：{arxiv_id}。\n"
                    "以下 JSON 只包含测试夹具中的合成阅读上下文：\n"
                    f"{json.dumps(case.context, ensure_ascii=False, sort_keys=True)}\n\n"
                    f"用户当前消息：{case.message}"
                ),
            },
        ]
        response = await client.acomplete_with_tools(
            messages,
            tools=tools,
            task="agent_chat",
            variant="low",
            tool_choice="auto",
        )
        result = evaluate_quality_response(case, response)
        results.append(result)
        observed = ",".join(result.observed_tools) or result.observed_action
        print(f"[{'PASS' if result.passed else 'FAIL'}] {case.id}: {observed}")

    summary = quality_summary(results)
    print(
        "[summary] "
        f"passed={summary['passed']}/{summary['total']} "
        f"tool_selection_accuracy={summary['tool_selection_accuracy']:.1%} "
        f"forbidden_tool_violations={summary['forbidden_tool_violations']}"
    )
    return 0 if (
        summary["tool_selection_accuracy"] >= 0.90
        and summary["forbidden_tool_violations"] == 0
        and summary["all_required_text_present"]
    ) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Audit Pet Agent decision quality")
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--paper", default="2202.09741")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(audit(args.fixture, args.paper)))
