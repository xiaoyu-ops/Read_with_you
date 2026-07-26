"""LaTeX 源码提取（备路径，D14）。

arXiv e-print（.tar.gz）→ 解压 → 找主 .tex 文件 → pylatexenc 转 blocks。
ar5iv 不可用或渲染不全时回退到此路径。
"""

from __future__ import annotations

import io
import json
import posixpath
import re
import tarfile

import httpx

try:
    from pylatexenc.latex2text import LatexNodes2Text
except ImportError:
    LatexNodes2Text = None  # type: ignore[assignment]

from .blocks import Block

ARXIV_EPRINT_URL = "https://arxiv.org/e-print/{arxiv_id}"
_MAX_TEX_MEMBER_BYTES = 4 * 1024 * 1024
_MAX_TEX_TOTAL_BYTES = 32 * 1024 * 1024
_MAX_TEX_FILES = 512
_MAX_TEX_INCLUDE_DEPTH = 32
_MAX_TEX_INCLUDE_COUNT = 1024
_MAX_TEX_EXPANDED_BYTES = 32 * 1024 * 1024
_INPUT_RE = re.compile(r"\\(?:input|include)\s*\{([^{}]+)\}")


async def fetch_latex_source(arxiv_id: str, timeout: float = 60.0) -> bytes | None:
    """下载 arXiv e-print（.tar.gz 或单个 .tex）。"""
    url = ARXIV_EPRINT_URL.format(arxiv_id=arxiv_id)
    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, headers={"User-Agent": "PeiNiDu/0.1"}
        ) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return None
            return resp.content
    except httpx.HTTPError:
        return None


def _find_main_tex(tar_bytes: bytes) -> str | None:
    """Find and safely expand the main TeX file from an arXiv source bundle."""
    try:
        tar = tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:*")
    except (tarfile.TarError, EOFError):
        # 可能是单个 .tex 文件（非 tar.gz）
        try:
            text = tar_bytes.decode("utf-8", errors="ignore")
        except Exception:
            return None
        return None if _INPUT_RE.search(text) else text

    sources: dict[str, str] = {}
    total_bytes = 0
    with tar:
        for member in tar.getmembers():
            if not member.isfile() or not member.name.lower().endswith(".tex"):
                continue
            normalized_name = _safe_tex_member_name(member.name)
            if normalized_name is None or normalized_name in sources:
                return None
            if member.size < 0 or member.size > _MAX_TEX_MEMBER_BYTES:
                return None
            total_bytes += member.size
            if len(sources) >= _MAX_TEX_FILES or total_bytes > _MAX_TEX_TOTAL_BYTES:
                return None
            content = tar.extractfile(member)
            if content is None:
                return None
            sources[normalized_name] = content.read().decode("utf-8", errors="ignore")

    if not sources:
        return None
    main_name = next(
        (name for name, text in sources.items() if "\\documentclass" in text),
        max(sources, key=lambda name: len(sources[name])),
    )
    try:
        return _expand_tex_inputs(main_name, sources, stack=())
    except ValueError:
        return None


def _safe_tex_member_name(name: str) -> str | None:
    candidate = name.replace("\\", "/")
    normalized = posixpath.normpath(candidate).removeprefix("./")
    if (
        not normalized
        or normalized == "."
        or posixpath.isabs(candidate)
        or normalized == ".."
        or normalized.startswith("../")
    ):
        return None
    return normalized


def _expand_tex_inputs(
    name: str,
    sources: dict[str, str],
    *,
    stack: tuple[str, ...],
    include_count: list[int] | None = None,
) -> str:
    if include_count is None:
        include_count = [0]
    if name in stack or len(stack) >= _MAX_TEX_INCLUDE_DEPTH:
        raise ValueError("latex_include_cycle")
    text = sources.get(name)
    if text is None:
        raise ValueError("latex_include_missing")

    def expand_match(match: re.Match[str]) -> str:
        include_count[0] += 1
        if include_count[0] > _MAX_TEX_INCLUDE_COUNT:
            raise ValueError("latex_include_count_exceeded")
        raw_target = match.group(1).strip().replace("\\", "/")
        if not raw_target:
            raise ValueError("latex_include_missing")
        target = raw_target if posixpath.splitext(raw_target)[1] else f"{raw_target}.tex"
        resolved = _safe_tex_member_name(posixpath.join(posixpath.dirname(name), target))
        if resolved is None or resolved not in sources:
            raise ValueError("latex_include_missing")
        return _expand_tex_inputs(
            resolved,
            sources,
            stack=(*stack, name),
            include_count=include_count,
        )

    expanded_lines: list[str] = []
    expanded_bytes = 0

    def append(piece: str) -> None:
        nonlocal expanded_bytes
        expanded_bytes += len(piece.encode("utf-8"))
        if expanded_bytes > _MAX_TEX_EXPANDED_BYTES:
            raise ValueError("latex_expanded_bytes_exceeded")
        expanded_lines.append(piece)

    for line in text.splitlines(keepends=True):
        comment = re.search(r"(?<!\\)%", line)
        code = line if comment is None else line[: comment.start()]
        cursor = 0
        for match in _INPUT_RE.finditer(code):
            append(code[cursor : match.start()])
            append(expand_match(match))
            cursor = match.end()
        append(code[cursor:])
        if comment is not None:
            append(line[comment.start() :])
    return "".join(expanded_lines)


# 简单的 LaTeX 结构识别正则
_HEADING_RE = re.compile(
    r"^\\(section|subsection|subsubsection|paragraph|subparagraph)\*?"
    r"\{([^{}]*)\}(.*)$"
)
_ABSTRACT_RE = re.compile(r"\\begin\{abstract\}(.*?)\\end\{abstract\}", re.DOTALL)
_EQUATION_RE = re.compile(r"\\begin\{equation\}(.*?)\\end\{equation\}", re.DOTALL)
_BEGIN_ENV_RE = re.compile(r"\\begin\{([^}]+)\}")
_SIMPLE_MACRO_RE = re.compile(
    r"\\(?:newcommand|renewcommand|providecommand)\s*"
    r"\{\\([A-Za-z@]+)\}\s*\{([^{}]*)\}"
)
_DROP_ARGUMENT_COMMAND_RE = re.compile(
    r"\\(?:cite|citep|citet|citealp|citeauthor|ref|eqref|autoref|label)\*?"
    r"(?:\[[^\]\n]*\])*\{[^{}\n]*\}"
)
_GENERIC_COMMAND_RE = re.compile(r"\\[A-Za-z@]+\*?")

_ENV_BLOCK_TYPES = {
    "equation": "formula",
    "equation*": "formula",
    "align": "formula",
    "align*": "formula",
    "gather": "formula",
    "gather*": "formula",
    "displaymath": "formula",
    "table": "table",
    "table*": "table",
    "tabular": "table",
    "tabular*": "table",
    "verbatim": "code",
    "lstlisting": "code",
    "minted": "code",
    "figure": "figure",
    "figure*": "figure",
}

_SKIP_COMMAND_PREFIXES = (
    "\\documentclass",
    "\\usepackage",
    "\\input",
    "\\newcommand",
    "\\renewcommand",
    "\\providecommand",
    "\\definecolor",
    "\\title",
    "\\author",
    "\\date",
    "\\makeatletter",
    "\\makeatother",
    "\\maketitle",
    "\\iclrfinalcopy",
    "\\appendix",
    "\\label",
    "\\bibliography",
    "\\bibliographystyle",
    "\\centering",
    "\\vspace",
    "\\hspace",
    "\\vskip",
    "\\small",
    "\\scriptsize",
    "\\footnotesize",
    "\\normalsize",
    "\\par",
    "\\noindent",
    "\\rule",
    "\\tcblower",
)


def _strip_latex_env(text: str, env_name: str) -> str:
    """去掉环境外壳，保留环境内部原始内容。"""
    text = re.sub(rf"\\begin\{{{re.escape(env_name)}\}}(?:\[[^\]]*\])?", "", text, count=1)
    text = re.sub(rf"\\end\{{{re.escape(env_name)}\}}", "", text, count=1)
    return text.strip()


def _simple_latex_macros(text: str) -> dict[str, str]:
    return {
        match.group(1): match.group(2).strip()
        for match in _SIMPLE_MACRO_RE.finditer(text)
    }


def _replace_simple_macros(text: str, macros: dict[str, str]) -> str:
    for name in sorted(macros, key=len, reverse=True):
        text = re.sub(
            rf"\\{re.escape(name)}(?![A-Za-z@])",
            lambda _match, value=macros[name]: value,
            text,
        )
    return text


def _fallback_latex_to_text(text: str) -> str:
    text = _DROP_ARGUMENT_COMMAND_RE.sub(" ", text)
    text = (
        text.replace(r"\&", "&")
        .replace(r"\%", "%")
        .replace(r"\_", "_")
        .replace(r"\#", "#")
        .replace("~", " ")
        .replace("\x60\x60", '"')
        .replace("''", '"')
    )
    text = re.sub(r"\\begin\{[^{}]+\}(?:\[[^\]]*\])?", " ", text)
    text = re.sub(r"\\end\{[^{}]+\}", " ", text)
    text = _GENERIC_COMMAND_RE.sub(" ", text)
    text = text.replace("{", " ").replace("}", " ").replace("\\\\", " ")
    return re.sub(r"\s+", " ", text).strip()


def _readable_latex(text: str, converter, macros: dict[str, str]) -> str:
    expanded = _replace_simple_macros(text, macros)
    if converter is not None:
        try:
            return converter.latex_to_text(expanded).strip()
        except Exception:
            pass
    return _fallback_latex_to_text(expanded)


def _command_arguments(text: str, command: str) -> list[str]:
    arguments: list[str] = []
    marker = re.compile(rf"\\{re.escape(command)}\*?(?:\[[^\]]*\])?\s*\{{")
    cursor = 0
    while match := marker.search(text, cursor):
        start = match.end()
        depth = 1
        index = start
        while index < len(text) and depth:
            if text[index] == "{" and (index == 0 or text[index - 1] != "\\"):
                depth += 1
            elif text[index] == "}" and (index == 0 or text[index - 1] != "\\"):
                depth -= 1
            index += 1
        if depth:
            break
        arguments.append(text[start : index - 1])
        cursor = index
    return arguments


def _figure_to_json(content: str, converter, macros: dict[str, str]) -> str:
    """Describe a LaTeX figure without pretending its source files are web assets."""
    captions = [
        _readable_latex(value, converter, macros)
        for value in _command_arguments(content, "caption")
    ]
    source_files = [
        value.strip()
        for value in _command_arguments(content, "includegraphics")
        if value.strip()
    ]
    return json.dumps(
        {
            "images": [],
            "caption": " ".join(value for value in captions if value),
            "preserved_in_pdf": True,
            "source_files": source_files,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _strip_latex_comment(line: str) -> str:
    return re.sub(r"(?<!\\)%.*$", "", line)


def latex_to_blocks(latex_text: str) -> list[Block]:
    """把 LaTeX 源码粗切为 blocks（比 ar5iv 粗糙，但兜底够用）。"""
    l2t = LatexNodes2Text() if LatexNodes2Text is not None else None
    macros = _simple_latex_macros(latex_text)
    title_arguments = _command_arguments(latex_text, "title")
    document_title = (
        _readable_latex(title_arguments[0], l2t, macros)
        if title_arguments
        else ""
    )
    title_emitted = False
    blocks: list[Block] = []
    idx = 0

    # 切分：按 \section / \subsection / 段落空行
    # 这是一个简化的切分；复杂宏包兼容留 v2 优化
    lines = latex_text.split("\n")
    has_document_env = "\\begin{document}" in latex_text
    in_document = not has_document_env
    current_text: list[str] = []
    env_name: str | None = None
    env_type: str | None = None
    env_lines: list[str] = []
    skip_unknown_options = False

    def add_block(
        type_: str,
        original: str,
        *,
        level: int | None = None,
        status: str = "pending",
    ) -> None:
        nonlocal idx
        text = original.strip()
        if not text:
            return
        blocks.append(
            Block(
                index=idx,
                type=type_,  # type: ignore[arg-type]
                original=text,
                level=level,
                status=status,  # type: ignore[arg-type]
            )
        )
        idx += 1

    def flush_paragraph() -> None:
        text = " ".join(current_text).strip()
        current_text.clear()
        if not text:
            return
        readable = _readable_latex(text, l2t, macros)
        if readable:
            add_block("paragraph", readable)

    def flush_env() -> None:
        nonlocal env_name, env_type
        if env_name is None or env_type is None:
            return
        raw = "\n".join(env_lines)
        content = _strip_latex_env(raw, env_name)
        env_lines.clear()
        if content:
            block_content = (
                _figure_to_json(content, l2t, macros)
                if env_type == "figure"
                else content
            )
            add_block(env_type, block_content, status="skip")
            if env_type in {"figure", "table"}:
                for caption in _command_arguments(content, "caption"):
                    readable = _readable_latex(caption, l2t, macros)
                    if readable:
                        add_block("paragraph", readable)
        env_name = None
        env_type = None

    def flush_display_math() -> None:
        nonlocal env_name, env_type
        raw = "\n".join(env_lines).strip()
        env_lines.clear()
        content = raw.removeprefix("\\[").removesuffix("\\]").strip()
        content = content.removeprefix("$$").removesuffix("$$").strip()
        if content:
            add_block("formula", content, status="skip")
        env_name = None
        env_type = None

    for line in lines:
        stripped = _strip_latex_comment(line).strip()
        if skip_unknown_options:
            if "]" in stripped:
                skip_unknown_options = False
            continue
        if has_document_env and not in_document:
            if stripped.startswith("\\begin{document}"):
                in_document = True
            continue
        if stripped.startswith("\\begin{document}") or stripped.startswith("\\end{document}"):
            flush_paragraph()
            continue
        if stripped.startswith("\\maketitle"):
            flush_paragraph()
            if document_title and not title_emitted:
                add_block("heading", document_title, level=1)
                title_emitted = True
            continue
        if env_name is not None:
            env_lines.append(stripped)
            if env_name == "__display_math__":
                if stripped.endswith("\\]") or stripped.endswith("$$"):
                    flush_display_math()
                continue
            if re.search(rf"\\end\{{{re.escape(env_name)}\}}", stripped):
                flush_env()
            continue

        env_match = _BEGIN_ENV_RE.search(stripped)
        if env_match and env_match.group(1) in _ENV_BLOCK_TYPES:
            flush_paragraph()
            env_name = env_match.group(1)
            env_type = _ENV_BLOCK_TYPES[env_name]
            env_lines.append(stripped)
            if re.search(rf"\\end\{{{re.escape(env_name)}\}}", stripped):
                flush_env()
            continue

        if stripped.startswith("\\[") or stripped.startswith("$$"):
            flush_paragraph()
            env_name = "__display_math__"
            env_type = "formula"
            env_lines.append(stripped)
            if stripped.endswith("\\]") or (stripped.endswith("$$") and len(stripped) > 2):
                flush_display_math()
            continue

        heading = _HEADING_RE.match(stripped)
        if heading:
            flush_paragraph()
            levels = {
                "section": 2,
                "subsection": 3,
                "subsubsection": 4,
                "paragraph": 5,
                "subparagraph": 6,
            }
            add_block(
                "heading",
                _readable_latex(heading.group(2), l2t, macros),
                level=levels[heading.group(1)],
            )
            stripped = heading.group(3).strip()
            if not stripped:
                continue
        # equation
        m = _EQUATION_RE.match(stripped)
        if m:
            flush_paragraph()
            add_block("formula", m.group(1), status="skip")
            continue
        # 段落空行 → flush
        if not stripped:
            flush_paragraph()
            continue
        # 跳过宏定义、注释、documentclass 等非正文行
        if stripped.startswith(_SKIP_COMMAND_PREFIXES):
            continue
        if stripped.startswith("\\begin{") and "document" not in stripped:
            if "[" in stripped and "]" not in stripped:
                skip_unknown_options = True
            continue
        if stripped.startswith("\\end{"):
            continue
        current_text.append(stripped)

    flush_paragraph()
    return blocks


async def extract_from_latex(arxiv_id: str, timeout: float = 60.0) -> list[Block] | None:
    """从 LaTeX e-print 提取 blocks。"""
    raw = await fetch_latex_source(arxiv_id, timeout)
    if raw is None:
        return None
    main_tex = _find_main_tex(raw)
    if main_tex is None:
        return None
    blocks = latex_to_blocks(main_tex)
    return blocks if blocks else None
