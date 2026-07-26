"""Agent 基础抽象 — 声明式 AgentDefinition（模仿 oh-my-opencode-slim）。

每个 Agent = {name, description, model, variant, temperature, prompt}。
variant → temperature（复用 llm/variant.py）。
agent 不直接调 LLM SDK，全部走 llm/client.py（D8）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..llm.client import get_client
from ..llm.variant import variant_to_temperature


@dataclass
class AgentDefinition:
    """声明式 Agent 定义（对应 oh-my-opencode-slim 的 AgentDefinition interface）。"""

    name: str
    description: str  # whenToUse，嵌入 Orchestrator prompt
    prompt: str  # 系统提示词 / 角色定义
    model: str | None = None  # None 则用 default_model
    variant: str = "medium"  # low/medium/high/max → temperature
    temperature: float | None = None  # 显式覆盖 variant

    def get_temperature(self) -> float:
        return self.temperature if self.temperature is not None else variant_to_temperature(self.variant)

    async def run(self, user_input: str, model: str | None = None) -> str:
        """执行一次 LLM 调用，返回完整文本。"""
        client = get_client()
        messages = [
            {"role": "system", "content": self.prompt},
            {"role": "user", "content": user_input},
        ]
        return await client.acomplete(
            messages,
            model=model or self.model,
            variant=self.variant,
            temperature=self.temperature,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "model": self.model,
            "variant": self.variant,
            "temperature": self.temperature,
        }


def create_agent(
    name: str,
    description: str,
    prompt: str,
    model: str | None = None,
    variant: str = "medium",
) -> AgentDefinition:
    """工厂函数（对应 oh-my-opencode-slim 的 createXxxAgent）。"""
    return AgentDefinition(
        name=name,
        description=description,
        prompt=prompt,
        model=model,
        variant=variant,
    )
