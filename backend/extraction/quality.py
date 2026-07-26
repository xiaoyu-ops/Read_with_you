"""提取质量门禁。

目标不是证明提取 100% 正确，而是及时发现会破坏阅读可信度的结构问题。
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Literal

from .blocks import Block

Severity = Literal["fatal", "warning"]


@dataclass
class ExtractionFinding:
    code: str
    severity: Severity
    detail: str
    block_index: int | None = None


@dataclass
class ExtractionQualityReport:
    source: str
    block_count: int
    type_counts: dict[str, int]
    score: float
    acceptable: bool
    findings: list[ExtractionFinding] = field(default_factory=list)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["findings"] = [asdict(item) for item in self.findings]
        return data


def assess_extraction_quality(blocks: list[Block], source: str) -> ExtractionQualityReport:
    """对结构化 blocks 做通用质量评估。"""
    findings: list[ExtractionFinding] = []
    type_counts: dict[str, int] = {}
    for block in blocks:
        type_counts[block.type] = type_counts.get(block.type, 0) + 1

    if len(blocks) < 3:
        findings.append(
            ExtractionFinding(
                code="too_few_blocks",
                severity="fatal",
                detail="提取出的 block 数过少，正文很可能不完整。",
            )
        )

    _check_figures(blocks, findings)
    _check_tables(blocks, findings)
    _check_layout_artifacts(blocks, findings)
    _check_text_noise(blocks, findings)

    fatal_count = sum(1 for item in findings if item.severity == "fatal")
    warning_count = sum(1 for item in findings if item.severity == "warning")
    score = max(0.0, 1.0 - fatal_count * 0.35 - warning_count * 0.05)

    return ExtractionQualityReport(
        source=source,
        block_count=len(blocks),
        type_counts=type_counts,
        score=round(score, 3),
        acceptable=fatal_count == 0,
        findings=findings,
    )


def _check_figures(blocks: list[Block], findings: list[ExtractionFinding]) -> None:
    figure_blocks = [block for block in blocks if block.type == "figure"]
    empty_image_count = 0
    for block in figure_blocks:
        try:
            data = json.loads(block.original)
        except json.JSONDecodeError:
            findings.append(
                ExtractionFinding(
                    code="invalid_figure_json",
                    severity="fatal",
                    detail="figure block 不是合法 JSON，前端无法可靠渲染。",
                    block_index=block.index,
                )
            )
            continue
        images = data.get("images") or []
        caption = str(data.get("caption") or "")
        preserved_in_pdf = data.get("preserved_in_pdf") is True
        if caption.lower().startswith("figure") and not images and not preserved_in_pdf:
            empty_image_count += 1
            findings.append(
                ExtractionFinding(
                    code="figure_missing_image",
                    severity="warning",
                    detail="检测到 Figure caption 但没有图片资源。",
                    block_index=block.index,
                )
            )

    if len(figure_blocks) >= 2 and empty_image_count == len(figure_blocks):
        findings.append(
            ExtractionFinding(
                code="all_figures_missing_images",
                severity="fatal",
                detail="所有 figure 都缺少图片，说明图片抽取路径失效。",
            )
        )


def _check_tables(blocks: list[Block], findings: list[ExtractionFinding]) -> None:
    for block in blocks:
        if block.type != "table":
            continue
        stripped = block.original.lstrip()
        if re.match(r'^\{\s*"(?:kind|rows)"\s*:', stripped):
            try:
                data = json.loads(block.original)
            except json.JSONDecodeError:
                findings.append(
                    ExtractionFinding(
                        code="invalid_table_json",
                        severity="fatal",
                        detail="结构化表格不是合法 JSON。",
                        block_index=block.index,
                    )
                )
                continue
            rows = data.get("rows")
            if not isinstance(rows, list) or not rows:
                findings.append(
                    ExtractionFinding(
                        code="empty_table",
                        severity="warning",
                        detail="表格没有可渲染行。",
                        block_index=block.index,
                    )
                )
            continue

        is_latex = bool(
            re.search(
                r"\\(?:begin|end|centering|caption|toprule|midrule|bottomrule|resizebox|multirow)\b",
                stripped,
            )
        )
        findings.append(
            ExtractionFinding(
                code="legacy_latex_table" if is_latex else "legacy_markdown_table",
                severity="warning",
                detail=(
                    "表格仍是旧 LaTeX 格式，按兼容内容保留。"
                    if is_latex
                    else "表格仍是 Markdown 格式，可能丢失 colspan/rowspan。"
                ),
                block_index=block.index,
            )
        )


def _check_layout_artifacts(blocks: list[Block], findings: list[ExtractionFinding]) -> None:
    equation_number_table = re.compile(r"^\|\s*\|\s*(?:\|\s*)*\(\d+\)\s*\|", re.MULTILINE)
    for block in blocks:
        if block.type == "table" and equation_number_table.search(block.original):
            findings.append(
                ExtractionFinding(
                    code="equation_number_table",
                    severity="fatal",
                    detail="公式编号布局表被误提取为普通表格。",
                    block_index=block.index,
                )
            )


def _check_text_noise(blocks: list[Block], findings: list[ExtractionFinding]) -> None:
    repeated_marker = re.compile(r"\b(\d{1,3})\b(?:\s+\1\b){2,}")
    for block in blocks:
        if block.type != "paragraph":
            continue
        if repeated_marker.search(block.original):
            findings.append(
                ExtractionFinding(
                    code="repeated_numeric_marker",
                    severity="warning",
                    detail="段落中出现连续数字标记，可能是脚注或引用清洗不完整。",
                    block_index=block.index,
                )
            )
