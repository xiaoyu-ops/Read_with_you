from __future__ import annotations

import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from backend.extraction.pdf_layout import (
    PdfInfoPage,
    PdfLayoutError,
    extract_pdf_layout,
    parse_bbox_layout,
    parse_pdfinfo,
    parse_tsv_layout,
)


FIXTURES = Path(__file__).parent / "fixtures" / "pdf_layout"


class PdfLayoutTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.xml = (FIXTURES / "two_column_rotated.xhtml").read_bytes()
        cls.pdfinfo_text = (FIXTURES / "two_column_rotated.pdfinfo").read_text(encoding="utf-8")

    def test_parse_preserves_flow_order_headers_footers_and_cross_page_order(self) -> None:
        document = parse_bbox_layout(self.xml, parse_pdfinfo(self.pdfinfo_text))

        self.assertEqual(document.page_count, 2)
        self.assertEqual([page.rotation for page in document.pages], [0, 90])
        self.assertEqual((document.pages[1].width, document.pages[1].height), (800, 600))

        first_page = document.pages[0]
        self.assertEqual(
            [block.text for block in first_page.blocks],
            ["Header One", "Left column first", "Left lower", "Right column", "Footer 1"],
        )
        self.assertEqual([block.flow_index for block in first_page.blocks], [0, 1, 1, 2, 3])
        self.assertEqual(document.pages[1].blocks[0].text, "Continued paragraph")

        blocks = [block for page in document.pages for block in page.blocks]
        lines = [line for block in blocks for line in block.lines]
        words = [word for line in lines for word in line.words]
        self.assertEqual([block.reading_order for block in blocks], list(range(len(blocks))))
        self.assertEqual([line.reading_order for line in lines], list(range(len(lines))))
        self.assertEqual([word.reading_order for word in words], list(range(len(words))))
        self.assertEqual(first_page.blocks[0].bbox.normalized(600, 800), (0.066667, 0.025, 0.933333, 0.05))

    def test_parse_rejects_invalid_or_out_of_page_bbox(self) -> None:
        info = parse_pdfinfo(self.pdfinfo_text)
        inverted = self.xml.replace(b'xMax="92"', b'xMax="20"', 1)
        overflow = self.xml.replace(b'xMax="92"', b'xMax="700"', 1)
        not_finite = self.xml.replace(b'width="600"', b'width="nan"', 1)

        for payload in (inverted, overflow, not_finite):
            with self.subTest(payload=payload[:40]):
                with self.assertRaises(PdfLayoutError):
                    parse_bbox_layout(payload, info)

    def test_parse_rejects_page_count_and_page_number_mismatches(self) -> None:
        info = parse_pdfinfo(self.pdfinfo_text)
        extra_info = replace(
            info,
            page_count=3,
            pages=info.pages + (PdfInfoPage(page=3, width=600, height=800, rotation=0),),
        )
        duplicate_number = self.xml.replace(b'<page number="2"', b'<page number="1"', 1)

        with self.assertRaisesRegex(PdfLayoutError, "页数"):
            parse_bbox_layout(self.xml, extra_info)
        with self.assertRaisesRegex(PdfLayoutError, "页面编号"):
            parse_bbox_layout(duplicate_number, info)

    def test_parser_rejects_entity_declarations(self) -> None:
        payload = b'<!ENTITY leak SYSTEM "file:///etc/passwd">' + self.xml

        with self.assertRaisesRegex(PdfLayoutError, "实体声明"):
            parse_bbox_layout(payload, parse_pdfinfo(self.pdfinfo_text))

    def test_parser_removes_forbidden_xml_controls_without_dropping_text(self) -> None:
        payload = self.xml.replace(b"Header", b"He\x0bader", 1)

        document = parse_bbox_layout(payload, parse_pdfinfo(self.pdfinfo_text))

        self.assertEqual(document.pages[0].blocks[0].text, "Header One")

    def test_tsv_fallback_preserves_page_line_word_and_flow_order(self) -> None:
        tsv = (
            "level\tpage_num\tpar_num\tblock_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
            "5\t1\t0\t0\t0\t0\t40\t20\t50\t12\t99\tHeader\n"
            "5\t1\t0\t0\t0\t1\t95\t20\t30\t12\t99\tOne\n"
            "5\t1\t1\t0\t0\t0\t40\t100\t60\t12\t99\tLeft\n"
            "5\t2\t0\t0\t0\t0\t50\t60\t75\t12\t99\tContinued\n"
        )

        document = parse_tsv_layout(tsv, parse_pdfinfo(self.pdfinfo_text))

        self.assertEqual(document.extraction_mode, "tsv")
        self.assertEqual(document.warnings, ("bbox_layout_failed_tsv_fallback",))
        self.assertEqual(
            [block.text for block in document.pages[0].blocks],
            ["Header One", "Left"],
        )
        self.assertEqual(document.pages[1].blocks[0].text, "Continued")
        self.assertEqual(
            [block.reading_order for page in document.pages for block in page.blocks],
            [0, 1, 2],
        )

    def test_pdfinfo_requires_continuous_pages_and_valid_rotation(self) -> None:
        missing_page = self.pdfinfo_text.replace("Page    2 size:  600 x 800 pts\n", "")
        invalid_rotations = [
            self.pdfinfo_text.replace("Page    2 rot:   90", f"Page    2 rot:   {value}")
            for value in (45, 450)
        ]

        with self.assertRaisesRegex(PdfLayoutError, "不连续"):
            parse_pdfinfo(missing_page)
        for payload in invalid_rotations:
            with self.subTest(payload=payload[-30:]):
                with self.assertRaisesRegex(PdfLayoutError, "旋转不合法"):
                    parse_pdfinfo(payload)

    def test_extract_invokes_bbox_layout_without_a_shell(self) -> None:
        calls: list[list[str]] = []

        def fake_run(command, **kwargs):
            calls.append(command)
            self.assertNotIn("shell", kwargs)
            if command[0] == "pdfinfo":
                return subprocess.CompletedProcess(command, 0, stdout=self.pdfinfo_text, stderr="")
            Path(command[-1]).write_bytes(self.xml)
            return subprocess.CompletedProcess(command, 0, stdout=None, stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "sample.pdf"
            pdf_path.write_bytes(b"%PDF-test")
            with (
                patch("backend.extraction.pdf_layout.shutil.which", return_value="/usr/bin/poppler"),
                patch("backend.extraction.pdf_layout.subprocess.run", side_effect=fake_run),
            ):
                document = extract_pdf_layout(pdf_path)

        self.assertEqual(document.page_count, 2)
        self.assertEqual([call[0] for call in calls], ["pdfinfo", "pdftotext"])
        self.assertIn("-bbox-layout", calls[1])
        self.assertEqual(calls[1][calls[1].index("-l") + 1], "2")

    def test_extract_falls_back_to_tsv_when_bbox_layout_is_invalid(self) -> None:
        calls: list[list[str]] = []
        tsv = (
            "level\tpage_num\tpar_num\tblock_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
            "5\t1\t0\t0\t0\t0\t40\t20\t50\t12\t99\tFallback\n"
        )

        def fake_run(command, **kwargs):
            calls.append(command)
            if command[0] == "pdfinfo":
                return subprocess.CompletedProcess(command, 0, stdout=self.pdfinfo_text, stderr="")
            if "-bbox-layout" in command:
                Path(command[-1]).write_text("<not-valid", encoding="utf-8")
            else:
                Path(command[-1]).write_text(tsv, encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout=None, stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "sample.pdf"
            pdf_path.write_bytes(b"%PDF-test")
            with (
                patch("backend.extraction.pdf_layout.shutil.which", return_value="/usr/bin/poppler"),
                patch("backend.extraction.pdf_layout.subprocess.run", side_effect=fake_run),
            ):
                document = extract_pdf_layout(pdf_path)

        self.assertEqual(len(calls), 3)
        self.assertIn("-bbox-layout", calls[1])
        self.assertIn("-tsv", calls[2])
        self.assertEqual(document.extraction_mode, "tsv")
        self.assertEqual(document.pages[0].blocks[0].text, "Fallback")

    def test_extract_reports_poppler_failure_without_leaking_full_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "sample.pdf"
            pdf_path.write_bytes(b"%PDF-test")
            error = subprocess.CalledProcessError(
                1,
                ["pdfinfo"],
                stderr="first useful line\nsecond line that should not be copied",
            )
            with (
                patch("backend.extraction.pdf_layout.shutil.which", return_value="/usr/bin/poppler"),
                patch("backend.extraction.pdf_layout.subprocess.run", side_effect=error),
            ):
                with self.assertRaises(PdfLayoutError) as raised:
                    extract_pdf_layout(pdf_path)

        self.assertIn("first useful line", str(raised.exception))
        self.assertNotIn("second line", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
