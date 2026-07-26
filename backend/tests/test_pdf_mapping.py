from __future__ import annotations

import csv
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.extraction.blocks import Block
from backend.extraction.pdf_mapping import (
    PdfWord,
    _match_block,
    _normalize_tokens,
    _parse_pdftotext_tsv,
    _words_to_boxes,
    build_block_pdf_map,
)


class PdfMappingTest(unittest.TestCase):
    def test_match_block_finds_contiguous_pdf_words(self) -> None:
        words = [
            _word("unrelated", 1, 10),
            _word("the", 1, 20),
            _word("transformer", 1, 20),
            _word("uses", 1, 20),
            _word("multi", 1, 20),
            _word("head", 1, 20),
            _word("attention", 1, 20),
            _word("elsewhere", 1, 40),
        ]

        match = _match_block(_normalize_tokens("The Transformer uses multi-head attention."), words, 0)

        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match[:2], (1, 7))
        self.assertGreater(match[2], 0.9)

    def test_match_block_does_not_start_from_previous_paragraph_tail(self) -> None:
        words = [
            _word("outputs", 1, 10),
            _word("of", 1, 10),
            _word("dimension", 1, 10),
            _word("d", 1, 10),
            _word("model", 1, 10),
            _word("512", 1, 10),
            _word("decoder", 1, 20),
            _word("the", 1, 20),
            _word("decoder", 1, 20),
            _word("is", 1, 20),
            _word("also", 1, 20),
            _word("composed", 1, 20),
            _word("of", 1, 20),
            _word("a", 1, 20),
            _word("stack", 1, 20),
            _word("decoder", 1, 40),
            _word("the", 1, 40),
            _word("decoder", 1, 40),
            _word("is", 1, 40),
            _word("also", 1, 40),
            _word("composed", 1, 40),
            _word("of", 1, 40),
            _word("a", 1, 40),
            _word("stack", 1, 40),
        ]

        match = _match_block(
            _normalize_tokens("The decoder is also composed of a stack"),
            words,
            0,
        )

        self.assertIsNotNone(match)
        assert match is not None
        self.assertNotEqual(match[0], 0)
        self.assertGreaterEqual(match[0], 6)

    def test_words_to_boxes_merges_same_line(self) -> None:
        words = [_word("a", 2, 100), _word("b", 2, 101), _word("c", 2, 130)]

        boxes = _words_to_boxes(words)

        self.assertEqual(len(boxes), 2)
        self.assertEqual(boxes[0].page, 2)
        self.assertEqual(boxes[0].y0, 100)
        self.assertEqual(boxes[1].y0, 130)

    def test_parse_pdftotext_tsv_uses_word_boxes(self) -> None:
        text = "\n".join(
            [
                "level\tpage_num\tpar_num\tblock_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext",
                "1\t1\t0\t0\t0\t0\t0\t0\t612\t792\t-1\t###PAGE###",
                "5\t1\t0\t0\t0\t0\t124.67\t73.86\t42.99\t10.69\t100\tMulti-head",
                "5\t1\t0\t0\t0\t1\t170.65\t73.86\t31.20\t10.69\t100\tattention",
            ]
        )

        words = _parse_pdftotext_tsv(text)

        self.assertEqual([word.norm for word in words], ["multi", "head", "attention"])
        self.assertEqual(words[0].x0, 124.67)
        self.assertLess(words[0].x1, words[1].x0)
        self.assertEqual(words[1].y0, words[0].y0)
        self.assertEqual(words[2].x0, 170.65)
        self.assertEqual(words[-1].page_width, 612)

    def test_parse_pdftotext_tsv_handles_large_fields(self) -> None:
        old_limit = csv.field_size_limit()
        long_word = "a" * 2048
        text = "\n".join(
            [
                "level\tpage_num\tpar_num\tblock_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext",
                "1\t1\t0\t0\t0\t0\t0\t0\t612\t792\t-1\t###PAGE###",
                f"5\t1\t0\t0\t0\t0\t10\t20\t100\t10\t100\t{long_word}",
            ]
        )
        try:
            csv.field_size_limit(1024)
            words = _parse_pdftotext_tsv(text)
        finally:
            csv.field_size_limit(old_limit)

        self.assertEqual(len(words), 1)
        self.assertEqual(words[0].norm, long_word)

    def test_build_map_uses_pdf_url_without_page_images(self) -> None:
        words = [
            _word("the", 1, 20),
            _word("transformer", 1, 20),
            _word("uses", 1, 20),
            _word("attention", 1, 20),
        ]
        blocks = [
            Block(
                index=7,
                type="paragraph",
                original="The Transformer uses attention.",
            )
        ]

        with (
            patch("backend.extraction.pdf_mapping.extract_pdf_words", return_value=words),
            patch("backend.extraction.pdf_mapping._pdf_page_count", return_value=12),
        ):
            data = build_block_pdf_map(blocks, Path("/tmp/1706.03762/original.pdf"))

        self.assertEqual(data["pdf_url"], "/assets/1706.03762/original.pdf")
        self.assertEqual(data["page_count"], 12)
        self.assertNotIn("page_image_url_template", data)
        self.assertEqual(data["mapping_count"], 1)


def _word(text: str, page: int, y: float) -> PdfWord:
    return PdfWord(
        text=text,
        norm=text,
        page=page,
        x0=10,
        y0=y,
        x1=50,
        y1=y + 10,
        page_width=612,
        page_height=792,
    )


if __name__ == "__main__":
    unittest.main()
