"""Orchestrator — 多 Agent 编排核心（模仿 oh-my-opencode-slim）。

职责：
1. 持有 AGENT_DESCRIPTIONS（4 个 specialist 的 lane/role/when-to-delegate，嵌入 Orchestrator prompt）
2. 用 asyncio.gather 并行扇出 4 个 specialist
3. reconcile（汇总）各 specialist 输出成 AnalysisResult

MVP 简化（对齐文档 8.3）：
- 4 specialist 不调工具、不互相通信，纯 LLM 调用并行跑完
- agent 间通信 / 子 agent 调用 / permission 矩阵留 v2
- Agent 定义用声明式结构，v2 升级编排层时 Agent 定义不动
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from ..extraction.blocks import Block, reconstruct_text
from ..llm.config import get_config
from .base import AgentDefinition
from .definitions.highlights import create_highlights_agent
from .definitions.improvement import create_improvement_agent
from .definitions.reproducibility import create_reproducibility_agent
from .definitions.summary import create_summary_agent
from .presets import apply_preset_to_agent
from .schemas import AnalysisResult, Evidence, ReproducibilityReport

logger = logging.getLogger(__name__)

# Agent descriptions — 嵌入 Orchestrator 的调度规则（模仿 oh-my-opencode-slim AGENT_DESCRIPTIONS）
AGENT_DESCRIPTIONS: dict[str, str] = {
    "summary": """@summary
- Lane: 通读全文 → 核心摘要
- variant: high（需要深度理解）
- 输出: 短文本（200-400 字）
- 调度: 总是执行，独立于其他 agent""",

    "reproducibility": """@reproducibility
- Lane: 按 aspect 逐项检查可复现性
- variant: high（需要细致证据检查）
- 输出: 结构化 JSON（verdict + confidence + 4 evidence + summary）
- 调度: 总是执行，结构化输出不可省略""",

    "improvement": """@improvement
- Lane: 批判性分析可改进/延展点
- variant: medium（平衡批判性与准确性）
- 输出: JSON 数组（3-6 条改进建议）
- 调度: 总是执行""",

    "highlights": """@highlights
- Lane: 提炼值得学习的方法/写作/实验设计
- variant: low（确定性提取）
- 输出: JSON 数组（3-6 条亮点）
- 调度: 总是执行""",
}

ORCHESTRATOR_PROMPT = """你是「陪你读」的多 Agent 协调器（Orchestrator）。

你的团队有四个 specialist Agent，各自负责一项分析：

{descriptions}

**编排规则**：
1. 四个 specialist 并行执行（asyncio.gather），互不依赖
2. 每个 specialist 输入同一份全文文本
3. 汇总四个结果成 AnalysisResult

**MVP 阶段**：你本身不调用 LLM，仅作为编排逻辑存在。四个 specialist 独立调用 LLM 后由代码汇总。
""".format(
    descriptions="\n\n".join(AGENT_DESCRIPTIONS.values())
)

REPRODUCIBILITY_ASPECTS = ("数据集", "代码", "超参数", "硬件环境")
_AGENT_SEMAPHORE: asyncio.Semaphore | None = None
_AGENT_SEMAPHORE_LIMIT: int | None = None


def _get_agent_semaphore(limit: int) -> asyncio.Semaphore:
    """返回进程内 Agent LLM 调用限流器。"""
    global _AGENT_SEMAPHORE, _AGENT_SEMAPHORE_LIMIT
    safe_limit = max(1, int(limit or 1))
    if _AGENT_SEMAPHORE is None or _AGENT_SEMAPHORE_LIMIT != safe_limit:
        _AGENT_SEMAPHORE = asyncio.Semaphore(safe_limit)
        _AGENT_SEMAPHORE_LIMIT = safe_limit
    return _AGENT_SEMAPHORE


async def _with_agent_limit(limit: int, coro):
    semaphore = _get_agent_semaphore(limit)
    async with semaphore:
        return await coro


def _build_specialists(config=None) -> dict[str, AgentDefinition]:
    """构建 4 个 specialist agent，应用 preset 的 model + variant。"""
    specialists = {
        "summary": create_summary_agent(),
        "reproducibility": create_reproducibility_agent(),
        "improvement": create_improvement_agent(),
        "highlights": create_highlights_agent(),
    }
    # 应用 preset
    return {name: apply_preset_to_agent(agent, config) for name, agent in specialists.items()}


def _extract_json(text: str) -> Any:
    """从 LLM 输出中提取 JSON（兼容 ```json 代码块包裹）。"""
    # 去掉 ```json ... ``` 包裹
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    raw = m.group(1).strip() if m else text.strip()
    return json.loads(raw)


def _reconstruct_evidence_text(blocks: list[Block]) -> str:
    """保留原文结构，并为证据 Agent 增加稳定、可校验的 block 标记。"""

    return "\n\n".join(
        f"[block #{block.index}]\n{reconstruct_text([block])}"
        for block in blocks
    )


def _sanitize_model_evidence_locations(
    data: Any,
    valid_block_indexes: set[int] | None,
) -> Any:
    """模型只能选择真实 block；page/region 坐标必须由版面层补齐。"""

    if not isinstance(data, dict) or not isinstance(data.get("evidence"), list):
        return data
    for item in data["evidence"]:
        if not isinstance(item, dict) or "location" not in item:
            continue
        location = item.get("location")
        if location is None:
            continue
        block_index = location.get("block_index") if isinstance(location, dict) else None
        valid_index = (
            isinstance(block_index, int)
            and not isinstance(block_index, bool)
            and block_index >= 0
            and (valid_block_indexes is None or block_index in valid_block_indexes)
        )
        if valid_index:
            item["location"] = {"block_index": block_index}
        else:
            item.pop("location", None)
    return data


def _missing_evidence(aspect: str, detail: str = "模型输出未提供该维度的可追溯证据。") -> Evidence:
    """生成可复现性兜底证据，保持四个 aspect 的结构稳定。"""
    return Evidence(aspect=aspect, status="未提", detail=detail, citation="")


def _ensure_reproducibility_evidence(report: ReproducibilityReport) -> ReproducibilityReport:
    """确保可复现性报告至少包含文档要求的四个 evidence aspect。"""
    existing = {item.aspect for item in report.evidence}
    for aspect in REPRODUCIBILITY_ASPECTS:
        if aspect not in existing:
            report.evidence.append(_missing_evidence(aspect))
    return report


async def _run_summary(agent: AgentDefinition, full_text: str) -> str:
    """摘要 agent：直接返回文本。"""
    return await agent.run(full_text)


async def _run_reproducibility(
    agent: AgentDefinition,
    full_text: str,
    valid_block_indexes: set[int] | None = None,
) -> ReproducibilityReport:
    """可复现性 agent：解析结构化 JSON。"""
    raw = await agent.run(full_text)
    try:
        data = _sanitize_model_evidence_locations(
            _extract_json(raw),
            valid_block_indexes,
        )
        return _ensure_reproducibility_evidence(ReproducibilityReport(**data))
    except Exception as e:
        logger.warning("可复现性输出解析失败: %s", e)
        # 兜底：返回 insufficient_info，并保留四个固定 aspect，避免裸结论
        report = ReproducibilityReport(
            verdict="insufficient_info",
            confidence="low",
            evidence=[
                _missing_evidence(aspect, f"可复现性输出解析失败，无法确认该维度：{e}")
                for aspect in REPRODUCIBILITY_ASPECTS
            ],
            summary=f"解析失败: {e}",
        )
        return report


async def _run_list_agent(agent: AgentDefinition, full_text: str) -> list[str]:
    """改进点 / 亮点 agent：解析 JSON 数组。"""
    raw = await agent.run(full_text)
    try:
        data = _extract_json(raw)
        if isinstance(data, list):
            return [str(x) for x in data]
        return [str(data)]
    except Exception as e:
        logger.warning("%s 输出解析失败: %s", agent.name, e)
        # 兜底：把原文按行切
        return [line.strip() for line in raw.split("\n") if line.strip()][:6]


async def analyze_paper(blocks: list[Block]) -> AnalysisResult:
    """编排四 Agent 并行分析论文。返回 AnalysisResult。

    MVP：asyncio.gather 并行扇出 4 个 specialist，汇总结果。
    """
    cfg = get_config()
    specialists = _build_specialists(cfg)
    full_text = reconstruct_text(blocks)
    evidence_text = _reconstruct_evidence_text(blocks)
    valid_block_indexes = {block.index for block in blocks}

    logger.info("开始四 Agent 并行分析（全文 %d 字符）", len(full_text))

    # 并行扇出（模仿 oh-my-opencode-slim 的并行 specialist 调度）
    agent_concurrency = getattr(cfg, "agent_concurrency", 2)
    summary_text, reproducibility, improvements, highlights = await asyncio.gather(
        _with_agent_limit(agent_concurrency, _run_summary(specialists["summary"], full_text)),
        _with_agent_limit(
            agent_concurrency,
            _run_reproducibility(
                specialists["reproducibility"],
                evidence_text,
                valid_block_indexes,
            ),
        ),
        _with_agent_limit(
            agent_concurrency,
            _run_list_agent(specialists["improvement"], full_text),
        ),
        _with_agent_limit(
            agent_concurrency,
            _run_list_agent(specialists["highlights"], full_text),
        ),
    )

    logger.info(
        "四 Agent 分析完成: summary=%d字, repro=%s, improvements=%d条, highlights=%d条",
        len(summary_text),
        reproducibility.verdict,
        len(improvements),
        len(highlights),
    )

    return AnalysisResult(
        summary=summary_text,
        reproducibility=reproducibility,
        improvements=improvements,
        highlights=highlights,
    )
