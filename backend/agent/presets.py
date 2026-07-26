"""Agent preset 系统（模仿 oh-my-opencode-slim）。

每个 preset 给每个 agent 分配 model + variant。
从 config.yaml 的 presets.{name}.{agent}.{model,variant} 读取。
default_preset = openai。
换模型改配置不改代码。
"""

from __future__ import annotations

from ..llm.config import get_config
from ..llm.models import AppConfig, Preset
from .base import AgentDefinition

# preset 中支持的 agent 名
AGENT_NAMES = ("orchestrator", "summary", "reproducibility", "improvement", "highlights")


def get_active_preset(config: AppConfig | None = None) -> Preset:
    """获取当前激活的 preset。"""
    cfg = config or get_config()
    name = cfg.default_preset
    preset = cfg.presets.get(name)
    if preset is None:
        # 回退：用 Preset 默认值
        preset = Preset()
    return preset


def resolve_agent_model_variant(
    agent_name: str,
    config: AppConfig | None = None,
) -> tuple[str | None, str]:
    """从激活 preset 解析某 agent 的 model + variant。"""
    preset = get_active_preset(config)
    override = getattr(preset, agent_name, None)
    if override is None:
        return None, "medium"
    return override.model, override.variant


def apply_preset_to_agent(
    agent: AgentDefinition,
    config: AppConfig | None = None,
) -> AgentDefinition:
    """把 preset 的 model + variant 应用到 agent 定义上。返回新实例。"""
    model, variant = resolve_agent_model_variant(agent.name, config)
    return AgentDefinition(
        name=agent.name,
        description=agent.description,
        prompt=agent.prompt,
        model=model or agent.model,
        variant=variant,
        temperature=agent.temperature,
    )
