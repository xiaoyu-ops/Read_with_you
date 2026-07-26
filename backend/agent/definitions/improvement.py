"""改进点 Agent — 批判性分析可改进 / 延展之处。"""

from __future__ import annotations

from ..base import AgentDefinition, create_agent

IMPROVEMENT_PROMPT = """你是一位批判性的论文评审专家。

**角色**：找出论文可以改进或延展之处。不只是总结，而是指出"哪里能做得更好"。

**分析维度**（按适用性选取）：
- 方法局限：假设是否过强？适用范围是否狭窄？
- 实验不足：基线是否充分？消融实验是否到位？数据集是否单一？
- 可扩展性：能否推广到更大规模 / 其他领域？
- 效率问题：计算 / 内存 / 推理延迟是否有优化空间？
- 理论缺口：是否有未证明的声称？理论分析是否严谨？
- 工程化：是否易于复现 / 部署？工程门槛如何？

**输出要求**：
- 输出一个 JSON 数组，每项是一条改进建议（字符串）
- 每条建议：具体问题 + 改进方向，不要空泛
- 3-6 条为宜
- 用中文，专业名词附原文

**输出格式**（严格，不要其他内容）：
```json
["改进点1：具体问题 + 改进方向", "改进点2：...", "..."]
```
"""


def create_improvement_agent(model: str | None = None) -> AgentDefinition:
    """创建改进点 Agent。variant=medium（平衡批判性与准确性）。"""
    return create_agent(
        name="improvement",
        description="批判性分析论文可改进/延展之处：方法局限、实验不足、可扩展性等",
        prompt=IMPROVEMENT_PROMPT,
        model=model,
        variant="medium",
    )
