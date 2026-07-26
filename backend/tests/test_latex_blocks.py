from __future__ import annotations

import io
import json
import tarfile
import unittest
from unittest.mock import patch

from backend.extraction.latex import _find_main_tex, latex_to_blocks
from backend.extraction.quality import assess_extraction_quality


def _source_tar(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, content in files.items():
            payload = content.encode()
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
    return buffer.getvalue()


class LatexBlocksTest(unittest.TestCase):
    def test_latex_envs_become_typed_skip_blocks(self) -> None:
        blocks = latex_to_blocks(
            r"""
\section{Introduction}
This paper introduces a method.

\begin{equation}
E = mc^2
\end{equation}

\begin{table}
\begin{tabular}{cc}
A & B \\
1 & 2
\end{tabular}
\end{table}

\begin{verbatim}
print("hello")
\end{verbatim}

Final paragraph.
"""
        )

        self.assertEqual([b.type for b in blocks], ["heading", "paragraph", "formula", "table", "code", "paragraph"])
        self.assertEqual([b.index for b in blocks], list(range(len(blocks))))
        for block in blocks[2:5]:
            self.assertEqual(block.status, "skip")

    def test_display_math_delimiters_become_formula(self) -> None:
        blocks = latex_to_blocks(
            r"""
Before math.

\[
a^2 + b^2 = c^2
\]

After math.
"""
        )

        self.assertEqual([b.type for b in blocks], ["paragraph", "formula", "paragraph"])
        self.assertEqual(blocks[1].original, "a^2 + b^2 = c^2")
        self.assertEqual(blocks[1].status, "skip")

    def test_preamble_before_document_is_skipped(self) -> None:
        blocks = latex_to_blocks(
            r"""
\documentclass{article}
\usepackage{xcolor}
\iclrfinalcopy
\input{math_commands.tex}
\newcommand{\model}{STAgent}
\definecolor{mycolor}{HTML}{eff6fd}
\title{AMAP Agentic Planning Technical Report}
\author{AMAP AI Agent LLM Team}

\begin{document}
\maketitle

\begin{abstract}
We introduce an agentic planning system.
\end{abstract}

\section{Introduction}
The system plans routes with tool use.
\end{document}
"""
        )

        originals = "\n".join(block.original for block in blocks)
        self.assertNotIn("newcommand", originals)
        self.assertNotIn("maketitle", originals)
        self.assertNotIn("iclrfinalcopy", originals)
        self.assertEqual(blocks[0].type, "heading")
        self.assertEqual(blocks[0].level, 1)
        self.assertEqual(blocks[0].original, "AMAP Agentic Planning Technical Report")
        self.assertEqual(blocks[1].type, "paragraph")
        self.assertIn("We introduce an agentic planning system", blocks[1].original)
        self.assertEqual(blocks[2].type, "heading")
        self.assertEqual(blocks[2].original, "Introduction")

    def test_source_bundle_expands_nested_inputs_before_block_numbering(self) -> None:
        source = _source_tar(
            {
                "main.tex": r"""
\documentclass{article}
\begin{document}
\input{sections/method}
\end{document}
""",
                "sections/method.tex": r"""
\section{Method}
Opening paragraph.
\include{details}
""",
                "sections/details.tex": "Nested details remain in reading order.",
            }
        )

        expanded = _find_main_tex(source)

        self.assertIsNotNone(expanded)
        assert expanded is not None
        self.assertNotIn(r"\input", expanded)
        self.assertNotIn(r"\include{details}", expanded)
        blocks = latex_to_blocks(expanded)
        self.assertEqual([block.index for block in blocks], list(range(len(blocks))))
        self.assertEqual([block.type for block in blocks], ["heading", "paragraph"])
        self.assertIn("Opening paragraph", blocks[1].original)
        self.assertIn("Nested details", blocks[1].original)

    def test_source_bundle_rejects_missing_traversal_and_cyclic_inputs(self) -> None:
        missing = _source_tar(
            {
                "main.tex": (
                    r"\documentclass{article}"
                    "\n"
                    r"\begin{document}\input{missing}\end{document}"
                )
            }
        )
        traversal = _source_tar(
            {
                "main.tex": (
                    r"\documentclass{article}"
                    "\n"
                    r"\begin{document}\input{../secret}\end{document}"
                )
            }
        )
        cyclic = _source_tar(
            {
                "main.tex": (
                    r"\documentclass{article}"
                    "\n"
                    r"\begin{document}\input{part}\end{document}"
                ),
                "part.tex": r"\input{main}",
            }
        )

        self.assertIsNone(_find_main_tex(missing))
        self.assertIsNone(_find_main_tex(traversal))
        self.assertIsNone(_find_main_tex(cyclic))

    def test_source_bundle_ignores_commented_inputs(self) -> None:
        source = _source_tar(
            {
                "main.tex": (
                    r"\documentclass{article}"
                    "\n"
                    r"\begin{document}"
                    "\n"
                    r"% \input{removed-draft}"
                    "\nVisible body.\n"
                    r"\end{document}"
                )
            }
        )

        expanded = _find_main_tex(source)

        self.assertIsNotNone(expanded)
        assert expanded is not None
        self.assertIn("Visible body.", expanded)

    def test_source_bundle_rejects_include_count_and_expansion_amplification(self) -> None:
        repeated = _source_tar(
            {
                "main.tex": (
                    r"\documentclass{article}"
                    "\n"
                    r"\begin{document}\input{part}\input{part}\input{part}\end{document}"
                ),
                "part.tex": "Repeated body.",
            }
        )
        expanded = _source_tar(
            {
                "main.tex": (
                    r"\documentclass{article}"
                    "\n"
                    r"\begin{document}\input{part}\input{part}\end{document}"
                ),
                "part.tex": "12345678",
            }
        )

        with patch("backend.extraction.latex._MAX_TEX_INCLUDE_COUNT", 2):
            self.assertIsNone(_find_main_tex(repeated))
        with patch("backend.extraction.latex._MAX_TEX_EXPANDED_BYTES", 32):
            self.assertIsNone(_find_main_tex(expanded))

    def test_fallback_converter_keeps_prose_but_drops_control_blocks(self) -> None:
        source = r"""
\documentclass{article}
\newcommand{\model}{STAgent}
\begin{document}
\subsubsection{Training Details} Visible \textbf{body} for \model.

\begin{figure}[h]
\centering
\includegraphics{model.pdf}
\caption{Architecture of \model.}
\end{figure}

\appendix
\label{app:hidden}
\end{document}
"""
        with patch("backend.extraction.latex.LatexNodes2Text", None):
            blocks = latex_to_blocks(source)

        self.assertEqual(
            [block.type for block in blocks],
            ["heading", "paragraph", "figure", "paragraph"],
        )
        self.assertEqual(blocks[0].original, "Training Details")
        self.assertEqual(blocks[1].original, "Visible body for STAgent.")
        self.assertEqual(blocks[3].original, "Architecture of STAgent.")
        figure = json.loads(blocks[2].original)
        self.assertEqual(figure["images"], [])
        self.assertEqual(figure["caption"], "Architecture of STAgent.")
        self.assertEqual(figure["source_files"], ["model.pdf"])
        self.assertTrue(figure["preserved_in_pdf"])
        self.assertTrue(assess_extraction_quality(blocks, "latex").acceptable)
        prose = "\n".join(
            block.original
            for block in blocks
            if block.type in {"heading", "paragraph"}
        )
        self.assertNotIn("\\", prose)
        self.assertNotIn("appendix", prose.lower())


if __name__ == "__main__":
    unittest.main()
