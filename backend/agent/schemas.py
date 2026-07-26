"""Agent 输出 schema — Pydantic 模型（严格对齐立项文档第 14 节）。

可复现性判断必须结构化输出：verdict + confidence + evidence（D5）。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_serializer


class EvidenceLocation(BaseModel):
    """证据在当前 TranslationLayout 中的可选定位信息。"""

    block_index: int = Field(ge=0)
    page: int | None = Field(default=None, ge=1)
    region_id: str | None = Field(default=None, min_length=1)

    @model_serializer(mode="wrap")
    def _serialize_without_empty_coordinates(self, handler):
        return {key: value for key, value in handler(self).items() if value is not None}


class Evidence(BaseModel):
    """单个证据项（数据集/代码/超参数/硬件 四个 aspect 各一个）。"""

    aspect: str
    status: str
    detail: str
    citation: str = ""
    location: EvidenceLocation | None = None

    @model_serializer(mode="wrap")
    def _serialize_without_empty_location(self, handler):
        data = handler(self)
        if data.get("location") is None:
            data.pop("location", None)
        return data


class ReproducibilityReport(BaseModel):
    """可复现性判断结构化输出（第 14 节）。"""

    verdict: Literal[
        "likely_reproducible",
        "partially_reproducible",
        "not_reproducible",
        "insufficient_info",
    ]
    confidence: Literal["high", "medium", "low"]
    evidence: list[Evidence] = Field(default_factory=list)
    summary: str = ""


class AnalysisResult(BaseModel):
    """四项分析的汇总结果。"""

    summary: str = ""
    reproducibility: ReproducibilityReport | None = None
    improvements: list[str] = Field(default_factory=list)
    highlights: list[str] = Field(default_factory=list)
