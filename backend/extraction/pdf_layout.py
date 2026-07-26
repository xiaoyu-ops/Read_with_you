"""Neutral PDF layout extraction backed by Poppler ``pdftotext -bbox-layout``.

This module deliberately stops at page/block/line/word geometry.  Matching the
geometry to Pet blocks and deciding whether translated text may replace the PDF
content belong to the translation-layout layer.
"""

from __future__ import annotations

import csv
import math
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from lxml import etree


POPPLER_LAYOUT_ADAPTER = "poppler_bbox_layout"
POPPLER_LAYOUT_ADAPTER_VERSION = "14"
MAX_LAYOUT_XML_BYTES = 128 * 1024 * 1024
MAX_LAYOUT_TSV_BYTES = 128 * 1024 * 1024
MAX_PDF_PAGES = 100_000
_BBOX_TOLERANCE = 1.0


class PdfLayoutError(RuntimeError):
    """Poppler layout extraction or validation failed."""


@dataclass(frozen=True)
class PdfLayoutBox:
    x0: float
    y0: float
    x1: float
    y1: float

    def normalized(self, page_width: float, page_height: float) -> tuple[float, float, float, float]:
        """Return stable ``[0, 1]`` coordinates for downstream adapters."""
        return (
            round(self.x0 / page_width, 6),
            round(self.y0 / page_height, 6),
            round(self.x1 / page_width, 6),
            round(self.y1 / page_height, 6),
        )


@dataclass(frozen=True)
class PdfLayoutWord:
    text: str
    bbox: PdfLayoutBox
    reading_order: int


@dataclass(frozen=True)
class PdfLayoutLine:
    bbox: PdfLayoutBox
    words: tuple[PdfLayoutWord, ...]
    reading_order: int

    @property
    def text(self) -> str:
        return " ".join(word.text for word in self.words)


@dataclass(frozen=True)
class PdfLayoutBlock:
    bbox: PdfLayoutBox
    lines: tuple[PdfLayoutLine, ...]
    flow_index: int
    reading_order: int

    @property
    def text(self) -> str:
        return " ".join(line.text for line in self.lines)


@dataclass(frozen=True)
class PdfLayoutPage:
    page: int
    width: float
    height: float
    rotation: int
    blocks: tuple[PdfLayoutBlock, ...]


@dataclass(frozen=True)
class PdfLayoutDocument:
    pages: tuple[PdfLayoutPage, ...]
    adapter: str = POPPLER_LAYOUT_ADAPTER
    adapter_version: str = POPPLER_LAYOUT_ADAPTER_VERSION
    extraction_mode: str = "bbox_layout"
    warnings: tuple[str, ...] = ()

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class PdfInfoPage:
    page: int
    width: float
    height: float
    rotation: int


@dataclass(frozen=True)
class PdfInfoDocument:
    page_count: int
    pages: tuple[PdfInfoPage, ...]


def extract_pdf_layout(pdf_path: Path, *, timeout: float = 90.0) -> PdfLayoutDocument:
    """Extract and validate neutral layout geometry from a PDF.

    Both commands are invoked without a shell.  ``pdfinfo`` supplies the
    authoritative page count and rotations, while ``pdftotext -bbox-layout``
    supplies the page/block/line/word hierarchy.
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.is_file() or pdf_path.stat().st_size <= 0:
        raise PdfLayoutError(f"PDF 文件不存在或为空: {pdf_path.name}")
    if shutil.which("pdftotext") is None:
        raise PdfLayoutError("缺少 Poppler pdftotext，无法提取精准 PDF 版面。")
    if shutil.which("pdfinfo") is None:
        raise PdfLayoutError("缺少 Poppler pdfinfo，无法校验 PDF 页数与旋转。")

    info_result = _run_poppler(
        ["pdfinfo", "-f", "1", "-l", str(MAX_PDF_PAGES), "-box", str(pdf_path)],
        timeout=timeout,
        capture_stdout=True,
    )
    pdf_info = parse_pdfinfo(info_result.stdout or "")

    with tempfile.TemporaryDirectory() as tmp:
        output_path = Path(tmp) / "layout.xhtml"
        try:
            _run_poppler(
                [
                    "pdftotext",
                    "-q",
                    "-enc",
                    "UTF-8",
                    "-f",
                    "1",
                    "-l",
                    str(pdf_info.page_count),
                    "-bbox-layout",
                    str(pdf_path),
                    str(output_path),
                ],
                timeout=timeout,
                capture_stdout=False,
            )
            if not output_path.is_file() or output_path.stat().st_size <= 0:
                raise PdfLayoutError("pdftotext 未生成 bbox-layout 输出。")
            if output_path.stat().st_size > MAX_LAYOUT_XML_BYTES:
                raise PdfLayoutError("pdftotext bbox-layout 输出过大，已拒绝解析。")
            return parse_bbox_layout(output_path.read_bytes(), pdf_info)
        except PdfLayoutError as bbox_error:
            tsv_path = Path(tmp) / "layout.tsv"
            try:
                _run_poppler(
                    [
                        "pdftotext",
                        "-q",
                        "-enc",
                        "UTF-8",
                        "-f",
                        "1",
                        "-l",
                        str(pdf_info.page_count),
                        "-tsv",
                        str(pdf_path),
                        str(tsv_path),
                    ],
                    timeout=timeout,
                    capture_stdout=False,
                )
                if not tsv_path.is_file() or tsv_path.stat().st_size <= 0:
                    raise PdfLayoutError("pdftotext 未生成 TSV 坐标输出。")
                if tsv_path.stat().st_size > MAX_LAYOUT_TSV_BYTES:
                    raise PdfLayoutError("pdftotext TSV 输出过大，已拒绝解析。")
                document = parse_tsv_layout(
                    tsv_path.read_text(encoding="utf-8", errors="replace"),
                    pdf_info,
                )
            except PdfLayoutError as tsv_error:
                raise PdfLayoutError(
                    f"bbox-layout 与 TSV 坐标提取均失败: {bbox_error}; {tsv_error}"
                ) from tsv_error
            return PdfLayoutDocument(
                pages=document.pages,
                extraction_mode="tsv",
                warnings=("bbox_layout_failed_tsv_fallback",),
            )


def parse_pdfinfo(text: str) -> PdfInfoDocument:
    """Parse the deterministic per-page subset emitted by ``pdfinfo -box``."""
    page_count_match = re.search(r"^Pages:\s+(\d+)\s*$", text, flags=re.M)
    if page_count_match is None:
        raise PdfLayoutError("pdfinfo 输出缺少页数。")
    page_count = int(page_count_match.group(1))
    if page_count < 1 or page_count > MAX_PDF_PAGES:
        raise PdfLayoutError(f"PDF 页数不合法: {page_count}")

    sizes: dict[int, tuple[float, float]] = {}
    rotations: dict[int, int] = {}
    size_pattern = re.compile(
        r"^Page\s+(\d+)\s+size:\s+([-+0-9.eE]+)\s+x\s+([-+0-9.eE]+)\s+pts\b",
        flags=re.M,
    )
    rotation_pattern = re.compile(r"^Page\s+(\d+)\s+rot:\s+(-?\d+)\s*$", flags=re.M)
    for match in size_pattern.finditer(text):
        page = int(match.group(1))
        if page in sizes:
            raise PdfLayoutError(f"pdfinfo 包含重复页面尺寸: {page}")
        width = _positive_finite(match.group(2), "page width")
        height = _positive_finite(match.group(3), "page height")
        sizes[page] = (width, height)
    for match in rotation_pattern.finditer(text):
        page = int(match.group(1))
        if page in rotations:
            raise PdfLayoutError(f"pdfinfo 包含重复页面旋转: {page}")
        raw_rotation = int(match.group(2))
        if raw_rotation not in (0, 90, 180, 270, -90, -180, -270):
            raise PdfLayoutError(f"PDF 页面旋转不合法: page={page}, rotation={raw_rotation}")
        rotations[page] = raw_rotation % 360

    # A one-page pdfinfo output can use the unnumbered form even with older Poppler.
    if page_count == 1 and 1 not in sizes:
        size = re.search(
            r"^Page size:\s+([-+0-9.eE]+)\s+x\s+([-+0-9.eE]+)\s+pts\b",
            text,
            flags=re.M,
        )
        rotation = re.search(r"^Page rot:\s+(-?\d+)\s*$", text, flags=re.M)
        if size is not None:
            sizes[1] = (
                _positive_finite(size.group(1), "page width"),
                _positive_finite(size.group(2), "page height"),
            )
        if rotation is not None:
            raw_value = int(rotation.group(1))
            if raw_value not in (0, 90, 180, 270, -90, -180, -270):
                raise PdfLayoutError(f"PDF 页面旋转不合法: page=1, rotation={raw_value}")
            rotations[1] = raw_value % 360

    expected_pages = set(range(1, page_count + 1))
    if set(sizes) != expected_pages or set(rotations) != expected_pages:
        raise PdfLayoutError("pdfinfo 页面尺寸或旋转记录不连续。")
    return PdfInfoDocument(
        page_count=page_count,
        pages=tuple(
            PdfInfoPage(
                page=page,
                width=sizes[page][0],
                height=sizes[page][1],
                rotation=rotations[page],
            )
            for page in range(1, page_count + 1)
        ),
    )


def parse_bbox_layout(xml_data: bytes | str, pdf_info: PdfInfoDocument) -> PdfLayoutDocument:
    """Safely parse Poppler XHTML while preserving its flow reading order."""
    raw = xml_data.encode("utf-8") if isinstance(xml_data, str) else xml_data
    if not raw or len(raw) > MAX_LAYOUT_XML_BYTES:
        raise PdfLayoutError("bbox-layout XML 为空或过大。")
    if re.search(br"<!ENTITY\b", raw, flags=re.I):
        raise PdfLayoutError("bbox-layout XML 包含不允许的实体声明。")
    raw = _strip_invalid_xml_controls(raw)

    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        recover=False,
        huge_tree=False,
    )
    try:
        root = etree.fromstring(raw, parser=parser)
    except (etree.XMLSyntaxError, ValueError) as exc:
        raise PdfLayoutError("bbox-layout XML 格式无效。") from exc

    docs = root.xpath("//*[local-name()='doc']")
    if _local_name(root) == "doc":
        docs = [root]
    if len(docs) != 1:
        raise PdfLayoutError("bbox-layout XML 必须包含一个 doc 节点。")
    page_elements = [child for child in docs[0] if _local_name(child) == "page"]
    if len(page_elements) != pdf_info.page_count:
        raise PdfLayoutError(
            f"bbox-layout 页数与 pdfinfo 不一致: {len(page_elements)} != {pdf_info.page_count}"
        )

    pages: list[PdfLayoutPage] = []
    block_order = 0
    line_order = 0
    word_order = 0
    for page_number, (page_element, info_page) in enumerate(
        zip(page_elements, pdf_info.pages, strict=True), start=1
    ):
        declared_number = page_element.get("number")
        if declared_number is not None:
            try:
                parsed_number = int(declared_number)
            except ValueError as exc:
                raise PdfLayoutError("bbox-layout 页面编号不是整数。") from exc
            if parsed_number != page_number:
                raise PdfLayoutError("bbox-layout 页面编号不连续。")

        width = _positive_finite(page_element.get("width"), "page width")
        height = _positive_finite(page_element.get("height"), "page height")
        if not _page_dimensions_match(width, height, info_page):
            raise PdfLayoutError(f"bbox-layout 页面尺寸与 pdfinfo 不一致: page={page_number}")

        blocks: list[PdfLayoutBlock] = []
        flow_index = 0
        for child in page_element:
            name = _local_name(child)
            if name == "flow":
                block_elements = [item for item in child if _local_name(item) == "block"]
            elif name == "block":
                block_elements = [child]
            else:
                continue
            for block_element in block_elements:
                block_bbox = _parse_box(block_element, width, height, "block")
                lines: list[PdfLayoutLine] = []
                for line_element in block_element:
                    if _local_name(line_element) != "line":
                        continue
                    line_bbox = _parse_box(line_element, width, height, "line")
                    if not _contains(block_bbox, line_bbox):
                        raise PdfLayoutError("line bbox 超出所属 block。")
                    words: list[PdfLayoutWord] = []
                    for word_element in line_element:
                        if _local_name(word_element) != "word":
                            continue
                        text = "".join(word_element.itertext()).strip()
                        if not text:
                            continue
                        word_bbox = _parse_box(word_element, width, height, "word")
                        if not _contains(line_bbox, word_bbox):
                            raise PdfLayoutError("word bbox 超出所属 line。")
                        words.append(
                            PdfLayoutWord(
                                text=text,
                                bbox=word_bbox,
                                reading_order=word_order,
                            )
                        )
                        word_order += 1
                    if words:
                        lines.append(
                            PdfLayoutLine(
                                bbox=line_bbox,
                                words=tuple(words),
                                reading_order=line_order,
                            )
                        )
                        line_order += 1
                if lines:
                    blocks.append(
                        PdfLayoutBlock(
                            bbox=block_bbox,
                            lines=tuple(lines),
                            flow_index=flow_index,
                            reading_order=block_order,
                        )
                    )
                    block_order += 1
            flow_index += 1
        pages.append(
            PdfLayoutPage(
                page=page_number,
                width=width,
                height=height,
                rotation=info_page.rotation,
                blocks=tuple(blocks),
            )
        )

    return PdfLayoutDocument(pages=tuple(pages))


def parse_tsv_layout(tsv_text: str, pdf_info: PdfInfoDocument) -> PdfLayoutDocument:
    """Parse Poppler TSV as a compatibility fallback with the same hierarchy."""
    if not tsv_text or len(tsv_text.encode("utf-8")) > MAX_LAYOUT_TSV_BYTES:
        raise PdfLayoutError("TSV 坐标输出为空或过大。")
    reader = csv.DictReader(tsv_text.splitlines(), delimiter="\t")
    required = {
        "level",
        "page_num",
        "par_num",
        "block_num",
        "line_num",
        "word_num",
        "left",
        "top",
        "width",
        "height",
        "text",
    }
    if reader.fieldnames is None or not required.issubset(reader.fieldnames):
        raise PdfLayoutError("TSV 坐标输出缺少必需字段。")

    info_by_page = {page.page: page for page in pdf_info.pages}
    groups: dict[
        int,
        dict[tuple[int, int], dict[int, list[PdfLayoutWord]]],
    ] = {page.page: {} for page in pdf_info.pages}
    word_order = 0
    for row_number, row in enumerate(reader, start=2):
        try:
            level = int(row.get("level") or 0)
        except ValueError as exc:
            raise PdfLayoutError(f"TSV 第 {row_number} 行 level 不合法。") from exc
        if level != 5:
            continue
        try:
            page_number = int(row.get("page_num") or 0)
            paragraph_number = int(row.get("par_num") or 0)
            block_number = int(row.get("block_num") or 0)
            line_number = int(row.get("line_num") or 0)
            left = float(row.get("left") or math.nan)
            top = float(row.get("top") or math.nan)
            width = float(row.get("width") or math.nan)
            height = float(row.get("height") or math.nan)
        except ValueError as exc:
            raise PdfLayoutError(f"TSV 第 {row_number} 行坐标不合法。") from exc
        info_page = info_by_page.get(page_number)
        if info_page is None:
            raise PdfLayoutError(f"TSV 第 {row_number} 行引用未知页面。")
        text = str(row.get("text") or "").strip()
        if not text or text.startswith("###"):
            continue
        word_box = _box_from_coordinates(
            left,
            top,
            left + width,
            top + height,
            info_page.width,
            info_page.height,
            f"TSV 第 {row_number} 行 word",
        )
        word = PdfLayoutWord(text=text, bbox=word_box, reading_order=word_order)
        word_order += 1
        block_key = (paragraph_number, block_number)
        lines = groups[page_number].setdefault(block_key, {})
        lines.setdefault(line_number, []).append(word)

    pages: list[PdfLayoutPage] = []
    block_order = 0
    line_order = 0
    for info_page in pdf_info.pages:
        blocks: list[PdfLayoutBlock] = []
        for (flow_index, _), raw_lines in groups[info_page.page].items():
            lines: list[PdfLayoutLine] = []
            for words in raw_lines.values():
                if not words:
                    continue
                line_box = _union_pdf_boxes([word.bbox for word in words])
                lines.append(
                    PdfLayoutLine(
                        bbox=line_box,
                        words=tuple(words),
                        reading_order=line_order,
                    )
                )
                line_order += 1
            if not lines:
                continue
            blocks.append(
                PdfLayoutBlock(
                    bbox=_union_pdf_boxes([line.bbox for line in lines]),
                    lines=tuple(lines),
                    flow_index=flow_index,
                    reading_order=block_order,
                )
            )
            block_order += 1
        pages.append(
            PdfLayoutPage(
                page=info_page.page,
                width=info_page.width,
                height=info_page.height,
                rotation=info_page.rotation,
                blocks=tuple(blocks),
            )
        )
    return PdfLayoutDocument(
        pages=tuple(pages),
        extraction_mode="tsv",
        warnings=("bbox_layout_failed_tsv_fallback",),
    )


def _run_poppler(
    command: list[str],
    *,
    timeout: float,
    capture_stdout: bool,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            check=True,
            stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise PdfLayoutError(f"Poppler 命令超时: {command[0]}") from exc
    except (OSError, subprocess.CalledProcessError) as exc:
        stderr = getattr(exc, "stderr", "") or ""
        detail = str(stderr).strip().splitlines()[:1]
        suffix = f"：{detail[0][:300]}" if detail else ""
        raise PdfLayoutError(f"Poppler 命令失败: {command[0]}{suffix}") from exc


def _strip_invalid_xml_controls(raw: bytes) -> bytes:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PdfLayoutError("bbox-layout XML 不是有效 UTF-8。") from exc
    cleaned = "".join(
        character
        for character in text
        if character in "\t\n\r"
        or 0x20 <= ord(character) <= 0xD7FF
        or 0xE000 <= ord(character) <= 0xFFFD
        or 0x10000 <= ord(character) <= 0x10FFFF
    )
    return cleaned.encode("utf-8")


def _box_from_coordinates(
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    page_width: float,
    page_height: float,
    label: str,
) -> PdfLayoutBox:
    if any(not math.isfinite(value) for value in (x0, y0, x1, y1)):
        raise PdfLayoutError(f"{label} 坐标不是有限数字。")
    if x0 >= x1 or y0 >= y1:
        raise PdfLayoutError(f"{label} bbox 面积不合法。")
    if (
        x0 < -_BBOX_TOLERANCE
        or y0 < -_BBOX_TOLERANCE
        or x1 > page_width + _BBOX_TOLERANCE
        or y1 > page_height + _BBOX_TOLERANCE
    ):
        raise PdfLayoutError(f"{label} bbox 超出页面。")
    return PdfLayoutBox(
        x0=max(0.0, x0),
        y0=max(0.0, y0),
        x1=min(page_width, x1),
        y1=min(page_height, y1),
    )


def _union_pdf_boxes(boxes: list[PdfLayoutBox]) -> PdfLayoutBox:
    if not boxes:
        raise PdfLayoutError("不能合并空 bbox。")
    return PdfLayoutBox(
        x0=min(box.x0 for box in boxes),
        y0=min(box.y0 for box in boxes),
        x1=max(box.x1 for box in boxes),
        y1=max(box.y1 for box in boxes),
    )


def _parse_box(element, page_width: float, page_height: float, label: str) -> PdfLayoutBox:
    x0 = _finite(element.get("xMin"), f"{label}.xMin")
    y0 = _finite(element.get("yMin"), f"{label}.yMin")
    x1 = _finite(element.get("xMax"), f"{label}.xMax")
    y1 = _finite(element.get("yMax"), f"{label}.yMax")
    return _box_from_coordinates(
        x0,
        y0,
        x1,
        y1,
        page_width,
        page_height,
        label,
    )


def _finite(value: str | None, label: str) -> float:
    try:
        parsed = float(value) if value is not None else math.nan
    except ValueError as exc:
        raise PdfLayoutError(f"{label} 不是数字。") from exc
    if not math.isfinite(parsed):
        raise PdfLayoutError(f"{label} 不是有限数字。")
    return parsed


def _positive_finite(value: str | None, label: str) -> float:
    parsed = _finite(value, label)
    if parsed <= 0:
        raise PdfLayoutError(f"{label} 必须大于 0。")
    return parsed


def _contains(parent: PdfLayoutBox, child: PdfLayoutBox) -> bool:
    return (
        child.x0 >= parent.x0 - _BBOX_TOLERANCE
        and child.y0 >= parent.y0 - _BBOX_TOLERANCE
        and child.x1 <= parent.x1 + _BBOX_TOLERANCE
        and child.y1 <= parent.y1 + _BBOX_TOLERANCE
    )


def _page_dimensions_match(width: float, height: float, page: PdfInfoPage) -> bool:
    def close(left: float, right: float) -> bool:
        return abs(left - right) <= max(1.0, max(left, right) * 0.002)

    same = close(width, page.width) and close(height, page.height)
    swapped = page.rotation in (90, 270) and close(width, page.height) and close(height, page.width)
    return same or swapped


def _local_name(element) -> str:
    try:
        return etree.QName(element).localname
    except (TypeError, ValueError):
        return ""
