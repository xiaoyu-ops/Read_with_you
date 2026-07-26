from __future__ import annotations

import json
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts import audit_real_papers


class RealPaperAuditTest(unittest.TestCase):
    def test_gate_passes_complete_sample(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            papers_dir = Path(tmp)
            self._write_sample(papers_dir, with_mapping=True)
            gate = {"source": "ar5iv", "min_blocks": 3, "min_mapped_ratio": 0.8}

            result = audit_real_papers.audit_paper(papers_dir, "sample", gate)

        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["block_types"], {"heading": 1, "paragraph": 2})
        self.assertEqual(result["pdf_pages"], 2)

    def test_missing_pdf_mapping_is_skipped_and_main_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            papers_dir = Path(tmp)
            self._write_sample(papers_dir, with_mapping=False)
            gates = {"sample": {"source": "ar5iv", "min_blocks": 3, "min_mapped_ratio": 0.8}}

            with patch.object(audit_real_papers, "SAMPLE_GATES", gates):
                result = audit_real_papers.audit_paper(papers_dir, "sample", gates["sample"])
                with redirect_stdout(io.StringIO()):
                    exit_code = audit_real_papers.main(["--papers-dir", str(papers_dir), "--json"])

        self.assertEqual(result["status"], "skipped")
        self.assertIn("pdf_mapping_missing", result["reasons"])
        self.assertEqual(exit_code, 1)

    def test_threshold_failure_returns_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            papers_dir = Path(tmp)
            self._write_sample(papers_dir, with_mapping=True, mapped_ratio=0.7)
            gate = {"source": "ar5iv", "min_blocks": 3, "min_mapped_ratio": 0.8}

            result = audit_real_papers.audit_paper(papers_dir, "sample", gate)

        self.assertEqual(result["status"], "fail")
        self.assertIn("mapped_ratio:0.700<0.800", result["reasons"])

    @staticmethod
    def _write_sample(papers_dir: Path, *, with_mapping: bool, mapped_ratio: float = 0.9) -> None:
        paper_dir = papers_dir / "sample"
        paper_dir.mkdir(parents=True)
        document = {
            "paper_id": "sample",
            "title": "Sample",
            "source": "ar5iv",
            "extracted_at": "2026-07-16T00:00:00Z",
            "blocks": [
                {"index": 0, "type": "heading", "original": "Title", "status": "pending", "level": 1},
                {"index": 1, "type": "paragraph", "original": "First paragraph.", "status": "pending"},
                {"index": 2, "type": "paragraph", "original": "Second paragraph.", "status": "pending"},
            ],
        }
        (paper_dir / "translation.json").write_text(json.dumps(document), encoding="utf-8")
        if with_mapping:
            mapping = {
                "page_count": 2,
                "mapped_ratio": mapped_ratio,
                "average_confidence": 0.9,
                "low_confidence_count": 0,
            }
            (paper_dir / "block_to_pdf_map.json").write_text(json.dumps(mapping), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
