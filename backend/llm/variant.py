"""variant → temperature 映射（模仿 oh-my-opencode-slim）。

variant 表示推理强度等级：
- low    (0.1)  确定性提取 / 事实性任务（亮点提炼）
- medium (0.4)  平衡（改进点分析）
- high   (0.7)  需要推理 / 深度分析（摘要、可复现性判断）
- max    (1.0)  最大创造性（暂未使用）
"""

from __future__ import annotations

VARIANT_TEMPERATURE: dict[str, float] = {
    "low": 0.1,
    "medium": 0.4,
    "high": 0.7,
    "max": 1.0,
}


def variant_to_temperature(variant: str) -> float:
    """把 variant 名转成 temperature 值。未知 variant 回退到 medium。"""
    return VARIANT_TEMPERATURE.get(variant, VARIANT_TEMPERATURE["medium"])
