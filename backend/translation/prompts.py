"""翻译 prompt 设计（立项文档第 13 节）。

原则：上下文窗口 + 只输出当前段，保证译文连贯且不越界。
喂入"上一段 + 当前段 + 下一段"，只翻译当前段。
边界：首段 prev 为空，末段 next 为空。
"""

from __future__ import annotations

import re
import unicodedata

from .immutables import extract_immutable_fragments

TRANSLATION_SYSTEM_PROMPT = """你是一位严谨的学术论文翻译。我会给你三段原文：上一段、当前段、下一段。
请只翻译【当前段】，不要翻译上一段和下一段。
上一段和下一段仅作为上下文，帮助你理解指代、术语和行文逻辑。

要求：
1. 保持学术术语准确，专业名词首次出现时附原文（如：注意力机制 (attention mechanism)）。
2. 保留原文中的公式、图表引用编号、引用标记 [n] 不翻译。
3. 不要添加解释性内容，不要意译扩写，忠实于当前段原文。
4. 输出仅为当前段的中文译文，不要输出其他任何内容。
5. 如果当前段是标题，保留章节编号，使用简洁中文；不要在括号中重复英文标题。"""


def build_translation_messages(
    prev: str,
    current: str,
    next_: str,
    system_prompt: str | None = None,
    block_type: str | None = None,
) -> list[dict]:
    """组装上下文窗口翻译的 messages。

    prev / next_ 为空时填占位说明（首段/末段边界处理）。
    """
    prev_block = prev if prev.strip() else "（本文首段，无上文）"
    next_block = next_ if next_.strip() else "（本文末段，无下文）"
    immutable_instruction = ""
    if "⟦PET_IMMUTABLE_" in current:
        immutable_instruction = (
            "【不可变标记】\n"
            "当前段中形如 ⟦PET_IMMUTABLE_...⟧ 的标记代表公式或引用。"
            "必须原字符、原数量、原顺序保留，不得删除、复制、改写或调换。\n\n"
        )
    layout_instruction = ""
    if block_type == "heading":
        layout_instruction = (
            "【版面约束】\n"
            "当前段是标题：保留章节编号，只输出一行简洁中文标题，"
            "不要在括号中重复英文标题。"
            "将标题放在 <pet-heading> 与 </pet-heading> 之间，"
            "标签外不得输出上下文、说明或 Markdown。\n\n"
        )
    user_prompt = (
        f"{immutable_instruction}"
        f"{layout_instruction}"
        f"【上一段】\n{prev_block}\n\n"
        f"【当前段】\n{current}\n\n"
        f"【下一段】\n{next_block}"
    )
    prompt = system_prompt.strip() if system_prompt and system_prompt.strip() else TRANSLATION_SYSTEM_PROMPT
    return [
        {"role": "system", "content": prompt},
        {"role": "user", "content": user_prompt},
    ]


def compact_heading_translation(original: str, translation: str) -> str:
    """Keep one heading payload, then remove an exact redundant source suffix."""
    stripped = _extract_heading_payload(translation)
    if extract_immutable_fragments(original):
        return stripped
    match = re.search(r"\s*[（(]([^()（）]+)[）)]\s*$", stripped)
    if match is None:
        return stripped
    source = _normalize_heading_comparison(original)
    source_without_number = _normalize_heading_comparison(
        re.sub(r"^\s*\d+(?:\.\d+)*\.?\s+", "", original)
    )
    repeated = _normalize_heading_comparison(match.group(1))
    if repeated not in {source, source_without_number}:
        return stripped
    compacted = stripped[: match.start()].rstrip()
    section_number = re.match(r"^\s*(\d+(?:\.\d+)*\.?)\s+", original)
    if section_number is not None and re.match(
        rf"^\s*{re.escape(section_number.group(1))}(?:\s|$)", compacted
    ) is None:
        return stripped
    return compacted or stripped


def _extract_heading_payload(translation: str) -> str:
    """Extract the first model-produced heading without touching prose blocks."""
    stripped = translation.strip()
    tagged = re.search(
        r"<pet-heading>\s*(.*?)\s*</pet-heading>",
        stripped,
        flags=re.IGNORECASE | re.DOTALL,
    )
    payload = tagged.group(1) if tagged is not None else stripped

    for raw_line in payload.splitlines():
        line = raw_line.strip()
        if not line or line.casefold() in {"```", "```markdown"}:
            continue
        if re.fullmatch(r"【(?:标题)?译文】", line):
            continue
        line = re.sub(r"^(?:<(?:pet-heading|heading|translation)>\s*)", "", line, flags=re.I)
        line = re.sub(r"\s*</(?:pet-heading|heading|translation)>$", "", line, flags=re.I)
        line = re.sub(r"^#{1,6}\s+", "", line)
        line = re.sub(r"^(?:(?:标题)?译文|翻译)\s*[:：]\s*", "", line)
        if line:
            return line.strip()
    return ""


def _normalize_heading_comparison(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return " ".join(normalized.casefold().split())
