"""摘要 Agent — 通读全文 → 核心摘要。

照 oh-my-opencode-slim explorer.ts 模式：PROMPT 常量 + 工厂函数。
"""

from __future__ import annotations

from ..base import AgentDefinition, create_agent

SUMMARY_PROMPT = """你是一位资深的学术论文摘要专家。

**角色**：通读论文全文，提炼核心内容。

**输出要求**：
1. 论文做了什么（一句话）
2. 方法（2-3 句，关键技术点）
3. 主要结论（1-2 句，含关键数据/指标）
4. 适用场景 / 局限（1 句）

**约束**：
- 总长度 200-400 字，不要罗列细节
- 忠实于原文，不臆测
- 用中文，专业名词附原文
- 不要输出标题、编号，直接输出摘要正文
"""


def create_summary_agent(model: str | None = None) -> AgentDefinition:
    """创建摘要 Agent。variant=high（需要深度理解）。"""
    return create_agent(
        name="summary",
        description="通读论文全文，提炼核心摘要：做了什么 / 方法 / 结论 / 局限",
        prompt=SUMMARY_PROMPT,
        model=model,
        variant="high",
    )
