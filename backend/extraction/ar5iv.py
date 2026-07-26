"""ar5iv HTML 提取（主路径，D1, D14）。

https://ar5iv.labs.arxiv.org/html/{arxiv_id}
用 BeautifulSoup 解析 HTML 段落结构 → blocks JSON。

段落切分规则（立项文档第 9.3 节）：
- <h1>-<h6> → heading（记录 level）
- <p>       → paragraph
- <table>   → table（保留 HTML/Markdown 格式）
- <pre>/<code> → code
- <math>/MathML/$...$ → formula（保留原始 LaTeX）
"""

from __future__ import annotations

import re
import json

import httpx
from bs4 import BeautifulSoup, NavigableString, Tag

from .blocks import Block

AR5IV_URL = "https://ar5iv.labs.arxiv.org/html/{arxiv_id}"
AR5IV_ORIGIN = "https://ar5iv.labs.arxiv.org"
_PURE_LAYOUT_COMMAND = re.compile(
    r"^(?:\d*\.?\d+(?:pt|mm|cm|em|ex)\s+)?"
    r"\\(?:contournumber|contourlength)(?:\s+\d*\.?\d+(?:pt|mm|cm|em|ex)?)?$"
)


async def fetch_ar5iv_html(arxiv_id: str, timeout: float = 30.0) -> str | None:
    """拉取 ar5iv HTML，失败返回 None（由调用方回退 LaTeX）。"""
    url = AR5IV_URL.format(arxiv_id=arxiv_id)
    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, headers={"User-Agent": "PeiNiDu/0.1"}
        ) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return None
            return resp.text
    except httpx.HTTPError:
        return None


def _extract_formula_latex(el: Tag) -> str:
    """从 <math> 元素提取 LaTeX 源。ar5iv 通常在 annotation 里放 LaTeX。"""
    # 优先找 annotation（ar5iv 的 MathML 注解）
    ann = el.find("annotation", attrs={"encoding": "application/x-tex"})
    if ann:
        return ann.get_text(strip=True)
    # 回退：取 textcontent
    return el.get_text(strip=True)


def _clean_text_spacing(text: str) -> str:
    """清理 HTML 抽取文本里的多余空格。"""
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(
        r"\[\s*([0-9,\s]+)\s*\]",
        lambda m: "[" + re.sub(r"\s+", "", m.group(1)) + "]",
        text,
    )
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"([\(\[])\s+", r"\1", text)
    text = re.sub(r"\s+([\)\]])", r"\1", text)
    return text


def _text_with_inline_math(el: Tag) -> str:
    """提取文本，并把 inline MathML 替换成可读 LaTeX。"""
    clone = BeautifulSoup(str(el), "lxml").find(el.name)
    if clone is None:
        return _clean_text_spacing(el.get_text(" ", strip=True))

    for math in clone.find_all("math"):
        latex = _extract_formula_latex(math)
        if math.get("display") == "inline":
            math.replace_with(NavigableString(f" ${latex}$ "))
        else:
            math.decompose()
    return _clean_text_spacing(clone.get_text(" ", strip=True))


def _is_pure_layout_command(text: str) -> bool:
    """识别 ar5iv 泄漏出的独立排版命令，不匹配包含正文的 LaTeX。"""
    return bool(_PURE_LAYOUT_COMMAND.fullmatch(text.strip()))


def _span_value(cell: Tag, name: str) -> int:
    raw = cell.get(name)
    try:
        value = int(str(raw)) if raw else 1
    except ValueError:
        return 1
    return max(value, 1)


def _row_cell_positions(rows: list[list[dict]]) -> list[list[tuple[int, dict]]]:
    positioned: list[list[tuple[int, dict]]] = []
    rowspans: dict[int, int] = {}

    for row in rows:
        col = 0
        placed: list[tuple[int, dict]] = []
        new_rowspans: dict[int, int] = {}
        for cell in row:
            while rowspans.get(col, 0) > 0:
                col += 1
            placed.append((col, cell))
            colspan = int(cell.get("colspan") or 1)
            rowspan = int(cell.get("rowspan") or 1)
            if rowspan > 1:
                for span_col in range(col, col + colspan):
                    new_rowspans[span_col] = max(new_rowspans.get(span_col, 0), rowspan - 1)
            col += colspan
        positioned.append(placed)
        rowspans = {
            span_col: remaining - 1
            for span_col, remaining in rowspans.items()
            if remaining > 1
        }
        for span_col, remaining in new_rowspans.items():
            rowspans[span_col] = max(rowspans.get(span_col, 0), remaining)

    return positioned


def _drop_separator_columns(rows: list[list[dict]]) -> list[list[dict]]:
    """删除 ar5iv 为表格分组插入的纯视觉空白列。"""
    if not rows:
        return rows

    positioned = _row_cell_positions(rows)
    separator_cols: set[int] = set()
    for row in positioned[:2]:
        for index, (col, cell) in enumerate(row):
            previous_cell = row[index - 1][1] if index > 0 else None
            next_cell = row[index + 1][1] if index + 1 < len(row) else None
            if (
                not cell.get("text")
                and int(cell.get("colspan") or 1) == 1
                and int(cell.get("rowspan") or 1) == 1
                and previous_cell is not None
                and next_cell is not None
                and (
                    int(previous_cell.get("colspan") or 1) > 1
                    or bool(next_cell.get("text"))
                )
            ):
                separator_cols.add(col)

    if not separator_cols:
        return rows

    cleaned: list[list[dict]] = []
    for row in positioned:
        cleaned.append([cell for col, cell in row if col not in separator_cols])
    return cleaned


def _table_to_json(table: Tag) -> str:
    """把 <table> 转成结构化 JSON，保留 colspan/rowspan。"""
    rows: list[list[dict]] = []
    for tr in table.find_all("tr"):
        cells: list[dict] = []
        for cell in tr.find_all(["td", "th"], recursive=False):
            cells.append(
                {
                    "text": _text_with_inline_math(cell),
                    "header": cell.name == "th",
                    "colspan": _span_value(cell, "colspan"),
                    "rowspan": _span_value(cell, "rowspan"),
                }
            )
        if cells:
            rows.append(cells)
    if not rows:
        return table.get_text(strip=True)
    rows = _drop_separator_columns(rows)
    return json.dumps({"kind": "table", "rows": rows}, ensure_ascii=False)


def _is_ltx_figure(el: Tag) -> bool:
    classes = set(el.get("class") or [])
    return el.name == "figure" and "ltx_figure" in classes


def _is_ltx_table_figure(el: Tag) -> bool:
    classes = set(el.get("class") or [])
    return el.name == "figure" and "ltx_table" in classes


def _inside_ltx_figure(el: Tag) -> bool:
    parent = el.parent
    while isinstance(parent, Tag):
        if _is_ltx_figure(parent):
            return True
        parent = parent.parent
    return False


def _inside_ltx_table_figure(el: Tag) -> bool:
    parent = el.parent
    while isinstance(parent, Tag):
        if _is_ltx_table_figure(parent):
            return True
        parent = parent.parent
    return False


def _is_ltx_equation_table(el: Tag) -> bool:
    classes = set(el.get("class") or [])
    return el.name == "table" and "ltx_equation" in classes and "ltx_eqn_table" in classes


def _inside_table(el: Tag) -> Tag | None:
    parent = el.parent
    while isinstance(parent, Tag):
        if parent.name == "table":
            return parent
        parent = parent.parent
    return None


def _figure_to_json(fig: Tag, asset_map: dict[str, str]) -> str:
    """把 ar5iv figure 转成前端可渲染 JSON。"""
    caption_el = fig.find("figcaption")
    caption = _text_with_inline_math(caption_el) if isinstance(caption_el, Tag) else ""
    image_urls: list[str] = []
    seen: set[str] = set()

    for img in fig.find_all("img"):
        src = img.get("src")
        if not src:
            continue
        basename = str(src).split("?", 1)[0].rsplit("/", 1)[-1]
        url = asset_map.get(basename)
        if not url and str(src).startswith("/html/"):
            url = f"{AR5IV_ORIGIN}{src}"
        elif not url and str(src).startswith("http"):
            url = str(src)
        if url and url not in seen:
            image_urls.append(url)
            seen.add(url)

    for ref in re.findall(r"\{([^{}]+\.(?:png|jpg|jpeg|pdf))\}", str(fig), flags=re.I):
        basename = ref.rsplit("/", 1)[-1]
        url = asset_map.get(basename)
        if url and url not in seen:
            image_urls.append(url)
            seen.add(url)
    return json.dumps({"images": image_urls, "caption": caption}, ensure_ascii=False)


def _translatable_caption(text: str) -> str:
    """Return meaningful caption prose, excluding isolated subfigure labels."""
    normalized = re.sub(r"\s+", " ", text).strip()
    if re.fullmatch(r"\(?[a-z0-9]+\)?[.:]?", normalized, flags=re.I):
        return ""
    return normalized


def parse_ar5iv_html(html: str, asset_map: dict[str, str] | None = None) -> list[Block]:
    """把 ar5iv HTML 解析为 blocks。"""
    asset_map = asset_map or {}
    soup = BeautifulSoup(html, "lxml")

    # ar5iv 正文在 <main> 或 <article> 里，fallback 到整个 body
    content = soup.find("main") or soup.find("article") or soup.body
    if content is None:
        return []

    blocks: list[Block] = []
    idx = 0

    for el in content.descendants:
        if isinstance(el, NavigableString):
            continue
        if not isinstance(el, Tag):
            continue

        tag = el.name

        if _inside_ltx_figure(el) or _inside_ltx_table_figure(el):
            continue

        if _is_ltx_figure(el):
            figure_json = _figure_to_json(el, asset_map)
            data = json.loads(figure_json)
            if data.get("images") or data.get("caption"):
                blocks.append(Block(index=idx, type="figure", original=figure_json, status="skip"))
                idx += 1
            caption = _translatable_caption(str(data.get("caption") or ""))
            if caption:
                blocks.append(
                    Block(index=idx, type="paragraph", original=caption, status="pending")
                )
                idx += 1
            continue

        if _is_ltx_table_figure(el):
            table = el.find("table")
            if isinstance(table, Tag) and not _is_ltx_equation_table(table):
                table_data = _table_to_json(table)
                if table_data.strip():
                    blocks.append(
                        Block(index=idx, type="table", original=table_data, status="skip")
                    )
                    idx += 1
            caption_el = el.find("figcaption")
            caption = _translatable_caption(
                _text_with_inline_math(caption_el)
                if isinstance(caption_el, Tag)
                else ""
            )
            if caption:
                blocks.append(
                    Block(index=idx, type="paragraph", original=caption, status="pending")
                )
                idx += 1
            continue

        # 标题
        if tag and tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            # 跳过 nav/LDTD 等非正文标题
            text = _text_with_inline_math(el)
            if not text:
                continue
            blocks.append(Block(index=idx, type="heading", original=text, level=int(tag[1]), status="pending"))
            idx += 1
            continue

        # 段落
        if tag == "p":
            text = _text_with_inline_math(el)
            if not text or _is_pure_layout_command(text):
                continue
            blocks.append(Block(index=idx, type="paragraph", original=text, status="pending"))
            idx += 1
            continue

        # 表格
        if tag == "table":
            if _is_ltx_equation_table(el):
                continue
            table_data = _table_to_json(el)
            if table_data.strip():
                blocks.append(Block(index=idx, type="table", original=table_data, status="skip"))
                idx += 1
            caption_el = el.find(["caption", "figcaption"])
            caption = _translatable_caption(
                _text_with_inline_math(caption_el)
                if isinstance(caption_el, Tag)
                else ""
            )
            if caption:
                blocks.append(
                    Block(index=idx, type="paragraph", original=caption, status="pending")
                )
                idx += 1
            continue

        # 代码块
        if tag in ("pre",):
            code = el.get_text(strip=True)
            if code:
                blocks.append(Block(index=idx, type="code", original=code, status="skip"))
                idx += 1
            continue

        # 公式（MathML）
        if tag == "math":
            parent_table = _inside_table(el)
            if parent_table is not None and not _is_ltx_equation_table(parent_table):
                continue
            if el.get("display") == "inline":
                continue
            latex = _extract_formula_latex(el)
            if latex:
                blocks.append(Block(index=idx, type="formula", original=latex, status="skip"))
                idx += 1
            continue

    # 合并连续空块、去重（理论上不会产生空 index，因为都检查了 text）
    return blocks


async def extract_from_ar5iv(arxiv_id: str, timeout: float = 30.0) -> list[Block] | None:
    """从 ar5iv 提取 blocks。失败返回 None（触发 LaTeX 回退）。"""
    html = await fetch_ar5iv_html(arxiv_id, timeout)
    if html is None:
        return None
    from .assets import extract_source_assets

    asset_map = await extract_source_assets(arxiv_id)
    blocks = parse_ar5iv_html(html, asset_map)
    # 渲染不全的判断：blocks 太少视为失败
    if len(blocks) < 3:
        return None
    return blocks
