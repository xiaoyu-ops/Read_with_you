"""blocks 数据结构（立项文档第 10 节）。

每篇论文的中间数据存为 translation.json，核心是 blocks 数组。
index 做高亮映射，type 区分块类型，status 支持断点续翻。
"""

from __future__ import annotations

from dataclasses import dataclass, asdict, field
from typing import Any, Literal

BlockType = Literal["heading", "paragraph", "table", "code", "formula", "figure"]
BlockStatus = Literal["pending", "translating", "done", "error", "skip"]


@dataclass
class Block:
    """单个内容块。"""

    index: int
    type: BlockType
    original: str
    translation: str | None = None
    status: BlockStatus = "pending"
    level: int | None = None  # 仅 heading 有，1-6

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if self.level is None:
            d.pop("level")
        return d


@dataclass
class PaperDocument:
    """一篇论文的完整 blocks 容器（对应 translation.json）。"""

    paper_id: str
    title: str
    source: str  # ar5iv | latex | mineru | local_pdf | ocr
    extracted_at: str
    blocks: list[Block] = field(default_factory=list)
    source_page_range: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = {
            "paper_id": self.paper_id,
            "title": self.title,
            "source": self.source,
            "extracted_at": self.extracted_at,
            "blocks": [b.to_dict() for b in self.blocks],
        }
        if self.source_page_range is not None:
            data["source_page_range"] = self.source_page_range
        return data

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PaperDocument:
        blocks = [
            Block(
                index=b["index"],
                type=b["type"],
                original=b["original"],
                translation=b.get("translation"),
                status=b.get("status", "pending"),
                level=b.get("level"),
            )
            for b in d.get("blocks", [])
        ]
        return cls(
            paper_id=d["paper_id"],
            title=d["title"],
            source=d["source"],
            extracted_at=d.get("extracted_at", ""),
            blocks=blocks,
            source_page_range=d.get("source_page_range"),
        )


def reconstruct_text(blocks: list[Block]) -> str:
    """把 blocks 重构为可读文本，供 Agent 分析用。"""
    parts: list[str] = []
    for b in blocks:
        prefix = ""
        if b.type == "heading":
            prefix = "#" * (b.level or 1) + " "
        elif b.type == "formula":
            prefix = "$$"
        elif b.type == "figure":
            prefix = "[Figure] "
        parts.append(f"{prefix}{b.original}" + ("$$" if b.type == "formula" else ""))
    return "\n\n".join(parts)
