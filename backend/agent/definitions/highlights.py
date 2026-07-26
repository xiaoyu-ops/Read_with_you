"""亮点 Agent — 提炼值得学习的方法 / 写作 / 实验设计。"""

from __future__ import annotations

from ..base import AgentDefinition, create_agent

HIGHLIGHTS_PROMPT = """你是一位善于发现论文亮点的学习型读者。

**角色**：提炼论文中值得学习的地方——不只是"这篇论文好"，而是"具体哪里值得我学"。

**关注维度**：
- 方法创新：核心思路 / 技巧是否巧妙？能否迁移到其他问题？
- 写作技巧：结构 / 表达 / 图表设计是否有可借鉴之处？
- 实验设计：对照设置 / 评估指标 / 消融策略是否值得学习？
- 工程实践：训练技巧 / 数据处理 / 工程优化是否有亮点？

**输出要求**：
- 输出一个 JSON 数组，每项是一条亮点（字符串）
- 每条亮点：具体是什么 + 为什么值得学习 + 如何迁移应用
- 3-6 条为宜
- 用中文，专业名词附原文

**输出格式**（严格，不要其他内容）：
```json
["亮点1：具体是什么 + 为什么值得学 + 如何迁移", "亮点2：...", "..."]
```
"""


def create_highlights_agent(model: str | None = None) -> AgentDefinition:
    """创建亮点 Agent。variant=low（确定性提取，不需高创造性）。"""
    return create_agent(
        name="highlights",
        description="提炼论文值得学习的方法/写作/实验设计/工程实践亮点",
        prompt=HIGHLIGHTS_PROMPT,
        model=model,
        variant="low",
    )
