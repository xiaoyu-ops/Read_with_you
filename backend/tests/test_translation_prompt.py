from __future__ import annotations

import unittest

from backend.translation.prompts import (
    TRANSLATION_SYSTEM_PROMPT,
    build_translation_messages,
    compact_heading_translation,
)


class TranslationPromptTest(unittest.TestCase):
    def test_custom_system_prompt_is_used(self) -> None:
        messages = build_translation_messages(
            "Previous paragraph.",
            "Current paragraph.",
            "Next paragraph.",
            "用更严格的学术中文翻译。",
        )

        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[0]["content"], "用更严格的学术中文翻译。")
        self.assertIn("【当前段】\nCurrent paragraph.", messages[1]["content"])

    def test_blank_custom_prompt_falls_back_to_default(self) -> None:
        messages = build_translation_messages(
            "",
            "Current paragraph.",
            "",
            "  ",
        )

        self.assertEqual(messages[0]["content"], TRANSLATION_SYSTEM_PROMPT)
        self.assertIn("（本文首段，无上文）", messages[1]["content"])
        self.assertIn("（本文末段，无下文）", messages[1]["content"])

    def test_heading_constraint_applies_with_custom_prompt(self) -> None:
        messages = build_translation_messages(
            "",
            "5.1 Training Data and Batching",
            "",
            "自定义翻译要求。",
            "heading",
        )

        self.assertIn("当前段是标题", messages[1]["content"])
        self.assertIn("不要在括号中重复英文标题", messages[1]["content"])
        self.assertIn("<pet-heading>", messages[1]["content"])

    def test_compact_heading_translation_only_removes_exact_source_suffix(self) -> None:
        self.assertEqual(
            compact_heading_translation(
                "5.1 Training Data and Batching",
                "5.1 训练数据与批处理 (Training Data and Batching)",
            ),
            "5.1 训练数据与批处理",
        )
        self.assertEqual(
            compact_heading_translation("Attention", "注意力（核心机制）"),
            "注意力（核心机制）",
        )
        self.assertEqual(
            compact_heading_translation(
                "5.1 Training Data",
                "训练数据 (5.1 Training Data)",
            ),
            "训练数据 (5.1 Training Data)",
        )
        self.assertEqual(
            compact_heading_translation(
                "Attention [3]",
                "注意力 (Attention [3])",
            ),
            "注意力 (Attention [3])",
        )

    def test_compact_heading_translation_discards_following_paragraph(self) -> None:
        self.assertEqual(
            compact_heading_translation(
                "Defining and identifying semantic duplicates",
                """
                ### 定义并识别语义重复项

                虽然识别感知重复项可以在输入空间中轻松完成，但这是下一段的内容。
                """,
            ),
            "定义并识别语义重复项",
        )

    def test_compact_heading_translation_accepts_tagged_or_labelled_output(self) -> None:
        cases = (
            (
                "<pet-heading>A.1 SemDeDup 的 k-means 聚类数量</pet-heading>\n\n多余正文。",
                "A.1 SemDeDup 的 k-means 聚类数量",
            ),
            ("【标题译文】\n## 训练数据与批处理\n多余正文。", "训练数据与批处理"),
            ("译文：注意力机制\n多余正文。", "注意力机制"),
        )
        for response, expected in cases:
            with self.subTest(response=response):
                self.assertEqual(
                    compact_heading_translation("Training Data", response),
                    expected,
                )

        self.assertEqual(
            compact_heading_translation("Training Data", "<pet-heading></pet-heading>"),
            "",
        )


if __name__ == "__main__":
    unittest.main()
