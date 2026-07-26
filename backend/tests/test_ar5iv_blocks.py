from __future__ import annotations

import unittest
import json

from backend.extraction.ar5iv import parse_ar5iv_html


class Ar5ivBlocksTest(unittest.TestCase):
    def test_pure_contour_layout_paragraph_is_removed_before_indexing(self) -> None:
        blocks = parse_ar5iv_html(
            r"""
            <html><body><main>
              <p>0.1pt \contournumber 10</p>
              <h2>Introduction</h2>
              <p>We use \contournumber as a literal command in this explanation.</p>
            </main></body></html>
            """
        )

        self.assertEqual([block.index for block in blocks], [0, 1])
        self.assertEqual(blocks[0].original, "Introduction")
        self.assertIn("literal command", blocks[1].original)

    def test_inline_math_uses_latex_annotation_without_mathml_noise(self) -> None:
        blocks = parse_ar5iv_html(
            """
            <html><body><main>
              <h2>1 Introduction</h2>
              <p>
                They generate hidden states
                <math display="inline">
                  <mi>h</mi><msub><mi>h</mi><mi>t</mi></msub>
                  <annotation encoding="application/x-tex">h_{t}</annotation>
                </math>
                as a function of previous states
                <math display="inline">
                  <annotation encoding="application/x-tex">h_{t-1}</annotation>
                </math>
                and inputs <math display="inline"><annotation encoding="application/x-tex">t</annotation></math>.
                Recent work [ 21 ] and conditional computation [ 32 ] helped.
              </p>
            </main></body></html>
            """
        )

        self.assertEqual(blocks[0].original, "1 Introduction")
        self.assertEqual(blocks[1].type, "paragraph")
        self.assertIn("hidden states $h_{t}$ as a function", blocks[1].original)
        self.assertIn("previous states $h_{t-1}$ and inputs $t$.", blocks[1].original)
        self.assertIn("[21] and conditional computation [32]", blocks[1].original)
        self.assertNotIn("subscript", blocks[1].original)
        self.assertEqual(len(blocks), 2)

    def test_figure_block_keeps_image_and_caption(self) -> None:
        blocks = parse_ar5iv_html(
            """
            <html><body><main>
              <figure class="ltx_figure">
                <span class="ltx_picture">\\includegraphics{decomposition.png}</span>
                <figcaption>Figure 2: Decomposition with <math display="inline">
                  <annotation encoding="application/x-tex">1\\times 1</annotation>
                </math> Conv.</figcaption>
              </figure>
            </main></body></html>
            """,
            {"decomposition.png": "/assets/2202.09741/assets/decomposition.png"},
        )

        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0].type, "figure")
        data = json.loads(blocks[0].original)
        self.assertEqual(data["images"], ["/assets/2202.09741/assets/decomposition.png"])
        self.assertIn("Figure 2: Decomposition with $1\\times 1$ Conv.", data["caption"])
        self.assertEqual(blocks[1].type, "paragraph")
        self.assertEqual(blocks[1].original, data["caption"])

    def test_figure_block_reads_ar5iv_img_src(self) -> None:
        blocks = parse_ar5iv_html(
            """
            <html><body><main>
              <figure class="ltx_figure">
                <img src="/html/1706.03762/assets/Figures/ModalNet-21.png">
                <figcaption>Figure 1: The Transformer.</figcaption>
              </figure>
            </main></body></html>
            """,
            {"ModalNet-21.png": "/assets/1706.03762/assets/ModalNet-21.png"},
        )

        self.assertEqual(len(blocks), 2)
        data = json.loads(blocks[0].original)
        self.assertEqual(data["images"], ["/assets/1706.03762/assets/ModalNet-21.png"])
        self.assertEqual(data["caption"], "Figure 1: The Transformer.")
        self.assertEqual(blocks[1].original, "Figure 1: The Transformer.")

    def test_figure_block_keeps_ar5iv_generated_image_url(self) -> None:
        blocks = parse_ar5iv_html(
            """
            <html><body><main>
              <figure class="ltx_figure">
                <img src="/html/1706.03762/assets/x1.png">
                <figcaption>Figure 3: Attention example.</figcaption>
              </figure>
            </main></body></html>
            """
        )

        data = json.loads(blocks[0].original)
        self.assertEqual(
            data["images"],
            ["https://ar5iv.labs.arxiv.org/html/1706.03762/assets/x1.png"],
        )
        self.assertEqual(blocks[1].original, "Figure 3: Attention example.")

    def test_isolated_subfigure_label_is_not_a_translation_block(self) -> None:
        blocks = parse_ar5iv_html(
            """
            <html><body><main>
              <figure class="ltx_figure">
                <img src="/html/1234.56789/assets/a.png">
                <figcaption>(a)</figcaption>
              </figure>
            </main></body></html>
            """
        )

        self.assertEqual([block.type for block in blocks], ["figure"])

    def test_equation_layout_table_is_not_rendered_as_table(self) -> None:
        blocks = parse_ar5iv_html(
            """
            <html><body><main>
              <table class="ltx_equation ltx_eqn_table" id="S5.E3">
                <tbody><tr>
                  <td></td>
                  <td><math display="block">
                    <annotation encoding="application/x-tex">lrate=d_{model}^{-0.5}</annotation>
                  </math></td>
                  <td>(3)</td>
                </tr></tbody>
              </table>
            </main></body></html>
            """
        )

        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].type, "formula")
        self.assertEqual(blocks[0].original, "lrate=d_{model}^{-0.5}")

    def test_table_preserves_colspan_and_rowspan(self) -> None:
        blocks = parse_ar5iv_html(
            """
            <html><body><main>
              <table>
                <tr>
                  <th rowspan="2">Model</th>
                  <th colspan="2">BLEU</th>
                  <th colspan="2">Training Cost</th>
                </tr>
                <tr><th>EN-DE</th><th>EN-FR</th><th>EN-DE</th><th>EN-FR</th></tr>
                <tr><td>Transformer</td><td>27.3</td><td>38.1</td><td>3.3e18</td><td></td></tr>
              </table>
            </main></body></html>
            """
        )

        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].type, "table")
        data = json.loads(blocks[0].original)
        self.assertEqual(data["kind"], "table")
        self.assertEqual(data["rows"][0][0]["rowspan"], 2)
        self.assertEqual(data["rows"][0][1]["colspan"], 2)
        self.assertEqual(data["rows"][0][2]["colspan"], 2)

    def test_table_drops_ar5iv_visual_separator_column(self) -> None:
        blocks = parse_ar5iv_html(
            """
            <html><body><main>
              <table>
                <tr>
                  <th rowspan="2">Model</th>
                  <td colspan="2">BLEU</td>
                  <td></td>
                  <td colspan="2">Training Cost</td>
                </tr>
                <tr><td>EN-DE</td><td>EN-FR</td><td></td><td>EN-DE</td><td>EN-FR</td></tr>
                <tr><th>ByteNet</th><td>23.75</td><td></td><td></td><td></td><td></td></tr>
              </table>
            </main></body></html>
            """
        )

        data = json.loads(blocks[0].original)
        self.assertEqual([cell["text"] for cell in data["rows"][0]], ["Model", "BLEU", "Training Cost"])
        self.assertEqual([cell["text"] for cell in data["rows"][1]], ["EN-DE", "EN-FR", "EN-DE", "EN-FR"])
        self.assertEqual(len(data["rows"][2]), 5)

    def test_table_caption_becomes_translatable_paragraph(self) -> None:
        blocks = parse_ar5iv_html(
            """
            <html><body><main>
              <table>
                <caption>Table 1: Main evaluation results.</caption>
                <tr><th>Model</th><th>Score</th></tr>
                <tr><td>Pet</td><td>0.98</td></tr>
              </table>
            </main></body></html>
            """
        )

        self.assertEqual([block.type for block in blocks], ["table", "paragraph"])
        self.assertEqual(blocks[1].original, "Table 1: Main evaluation results.")

    def test_ar5iv_table_figure_keeps_body_and_caption_once(self) -> None:
        blocks = parse_ar5iv_html(
            """
            <html><body><main>
              <figure class="ltx_table">
                <table>
                  <tr><th>Model</th><th>Score</th></tr>
                  <tr><td>Pet</td><td>0.98</td></tr>
                </table>
                <figcaption>Table 2: Full evaluation results.</figcaption>
              </figure>
            </main></body></html>
            """
        )

        self.assertEqual([block.type for block in blocks], ["table", "paragraph"])
        self.assertEqual(blocks[1].original, "Table 2: Full evaluation results.")


if __name__ == "__main__":
    unittest.main()
