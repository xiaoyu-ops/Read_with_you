"""Local PDF extraction helpers.

This path is intentionally lightweight: uploaded PDFs are converted to readable
blocks with local Poppler text extraction. Complex scanned PDFs should later go
through MinerU/OCR instead of pretending this path can solve them all.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .blocks import Block


class LocalPdfExtractionError(RuntimeError):
    """Local PDF extraction failed."""


_PDF_TEXT_TIMEOUT_SECONDS = 90


def extract_blocks_from_local_pdf(pdf_path: Path) -> list[Block]:
    """Extract readable text blocks from a local PDF file."""
    text = _extract_text_with_pdftotext(pdf_path)
    return text_to_blocks(text)


def text_to_blocks(text: str) -> list[Block]:
    """Convert plain PDF text into conservative heading/paragraph blocks."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    pages = normalized.split("\f")
    blocks: list[Block] = []

    def add_block(type_: str, original: str, *, level: int | None = None) -> None:
        clean = _clean_text(original)
        if not clean:
            return
        blocks.append(
            Block(
                index=len(blocks),
                type=type_,  # type: ignore[arg-type]
                original=clean,
                level=level,
            )
        )

    for page in pages:
        chunks = re.split(r"\n\s*\n+", page)
        for chunk in chunks:
            lines = [re.sub(r"\s+", " ", line).strip() for line in chunk.split("\n")]
            lines = [line for line in lines if line]
            if not lines:
                continue
            if len(lines) > 1 and _looks_like_heading(lines[0]):
                add_block("heading", lines[0], level=2)
                add_block("paragraph", " ".join(lines[1:]))
                continue
            clean = " ".join(lines).strip()
            if _looks_like_heading(clean):
                add_block("heading", clean, level=2)
            else:
                add_block("paragraph", clean)

    return blocks


def _extract_text_with_pdftotext(pdf_path: Path) -> str:
    if shutil.which("pdftotext") is None:
        raise LocalPdfExtractionError("缺少 Poppler pdftotext，无法解析本地 PDF。")

    with tempfile.TemporaryDirectory() as tmp:
        out_path = Path(tmp) / "plain.txt"
        try:
            subprocess.run(
                ["pdftotext", "-layout", str(pdf_path), str(out_path)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_PDF_TEXT_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise LocalPdfExtractionError("本地 PDF 文本抽取超时。") from exc
        except subprocess.CalledProcessError as exc:
            raise LocalPdfExtractionError("本地 PDF 文本抽取失败。") from exc
        text = out_path.read_text(encoding="utf-8", errors="ignore")

    if not text.strip():
        raise LocalPdfExtractionError("本地 PDF 未抽取到文本，可能是扫描件。")
    return text


def _clean_text(text: str) -> str:
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.split("\n")]
    return " ".join(line for line in lines if line).strip()


def _looks_like_heading(text: str) -> bool:
    if len(text) > 120 or text.endswith("."):
        return False
    heading_words = (
        r"abstract|introduction|related work|method|methods|experiments?|"
        r"results?|discussion|conclusion|references"
    )
    if re.match(rf"^({heading_words})\b", text, re.I):
        return True
    words = text.split()
    title_case_words = sum(1 for word in words if word[:1].isupper())
    if 1 <= len(words) <= 10 and title_case_words >= max(1, len(words) // 2):
        return True
    return bool(re.match(r"^\d+(?:\.\d+)*\s+[A-Z][A-Za-z0-9 ,:;()/-]{2,}$", text))
