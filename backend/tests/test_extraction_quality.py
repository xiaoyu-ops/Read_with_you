from __future__ import annotations

import unittest

from backend.extraction.blocks import Block
from backend.extraction.quality import assess_extraction_quality


class ExtractionQualityTest(unittest.TestCase):
    def test_all_figures_missing_images_is_fatal(self) -> None:
        report = assess_extraction_quality(
            [
                Block(index=0, type="heading", original="Title"),
                Block(index=1, type="figure", original='{"images": [], "caption": "Figure 1: A"}'),
                Block(index=2, type="figure", original='{"images": [], "caption": "Figure 2: B"}'),
            ],
            "ar5iv",
        )

        self.assertFalse(report.acceptable)
        self.assertTrue(any(item.code == "all_figures_missing_images" for item in report.findings))

    def test_equation_number_table_is_fatal(self) -> None:
        report = assess_extraction_quality(
            [
                Block(index=0, type="heading", original="Title"),
                Block(index=1, type="paragraph", original="Before."),
                Block(index=2, type="table", original="|  |  | (3) |\n| --- | --- | --- |"),
            ],
            "ar5iv",
        )

        self.assertFalse(report.acceptable)
        self.assertTrue(any(item.code == "equation_number_table" for item in report.findings))

    def test_repeated_numeric_marker_is_warning_only(self) -> None:
        report = assess_extraction_quality(
            [
                Block(index=0, type="heading", original="Title"),
                Block(index=1, type="paragraph", original="We used GPUs 2 2 2 for training."),
                Block(index=2, type="paragraph", original="After."),
            ],
            "ar5iv",
        )

        self.assertTrue(report.acceptable)
        self.assertTrue(any(item.code == "repeated_numeric_marker" for item in report.findings))

    def test_distinct_years_metrics_and_model_sizes_are_not_numeric_markers(self) -> None:
        report = assess_extraction_quality(
            [
                Block(index=0, type="heading", original="Title"),
                Block(
                    index=1,
                    type="paragraph",
                    original="Results for 2019 2020 2021 use model sizes 7 13 70 and scores 82 91 95.",
                ),
                Block(index=2, type="paragraph", original="After."),
            ],
            "ar5iv",
        )

        self.assertFalse(any(item.code == "repeated_numeric_marker" for item in report.findings))

    def test_structured_table_is_acceptable(self) -> None:
        report = assess_extraction_quality(
            [
                Block(index=0, type="heading", original="Title"),
                Block(index=1, type="paragraph", original="Before."),
                Block(
                    index=2,
                    type="table",
                    original='{"kind":"table","rows":[[{"text":"Model","header":true}],[{"text":"A"}]]}',
                ),
            ],
            "ar5iv",
        )

        self.assertTrue(report.acceptable)
        self.assertFalse(any(item.code == "legacy_markdown_table" for item in report.findings))

    def test_malformed_structured_table_is_fatal(self) -> None:
        report = assess_extraction_quality(
            [
                Block(index=0, type="heading", original="Title"),
                Block(index=1, type="paragraph", original="Before."),
                Block(index=2, type="table", original='{"kind":"table","rows":'),
            ],
            "ar5iv",
        )

        self.assertFalse(report.acceptable)
        self.assertTrue(any(item.code == "invalid_table_json" for item in report.findings))

    def test_legacy_latex_table_starting_with_brace_is_warning_only(self) -> None:
        report = assess_extraction_quality(
            [
                Block(index=0, type="heading", original="Title"),
                Block(index=1, type="paragraph", original="Before."),
                Block(index=2, type="table", original=r"{c}% \centering \begin{tabular}{cc} A & B \\ \end{tabular}"),
            ],
            "latex",
        )

        self.assertTrue(report.acceptable)
        self.assertTrue(any(item.code == "legacy_latex_table" for item in report.findings))
        self.assertFalse(any(item.code == "invalid_table_json" for item in report.findings))


if __name__ == "__main__":
    unittest.main()
