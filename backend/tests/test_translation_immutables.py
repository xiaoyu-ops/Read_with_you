from __future__ import annotations

import asyncio
import os
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.extraction.blocks import Block, PaperDocument
from backend.storage import files as storage_files
from backend.storage.files import load_document, save_document
from backend.translation.immutables import (
    ImmutablePlaceholderError,
    audit_immutable_translation,
    extract_immutable_fragments,
    protect_immutable_fragments,
    restore_immutable_fragments,
)
from backend.translation.translate import (
    retry_single_block,
    translate_paper_sse,
    translate_single_block,
)


class TranslationImmutableTest(unittest.TestCase):
    def setUp(self) -> None:
        self.provider_patch = patch.dict(
            os.environ,
            {"PEINIDU_TRANSLATION_PROVIDER": "litellm"},
        )
        self.provider_patch.start()
        self.addCleanup(self.provider_patch.stop)

    def test_extracts_non_overlapping_math_and_citations_in_source_order(self) -> None:
        source = r"Loss $L(x)$ follows \eqref{eq:loss} and [3, 5] before \(y = 1\)."

        fragments = extract_immutable_fragments(source)

        self.assertEqual(
            [fragment.value for fragment in fragments],
            ["$L(x)$", r"\eqref{eq:loss}", "[3, 5]", r"\(y = 1\)"],
        )

    def test_exact_placeholder_round_trip_restores_duplicate_fragments(self) -> None:
        source = "Compare $x$ with $x$ [2]."
        protected = protect_immutable_fragments(source)
        placeholders = protected.placeholders

        restored = restore_immutable_fragments(
            f"比较 {placeholders[0]} 与 {placeholders[1]} {placeholders[2]}。",
            protected,
        )

        self.assertEqual(restored, "比较 $x$ 与 $x$ [2]。")
        self.assertEqual(len(set(placeholders)), 3)

    def test_currency_range_is_not_mistaken_for_inline_math(self) -> None:
        source = "Funding rose from $5 million in 2020 to $10 million, while $x$ stayed fixed."

        fragments = extract_immutable_fragments(source)

        self.assertEqual([fragment.value for fragment in fragments], ["$x$"])
        self.assertEqual(
            [fragment.value for fragment in extract_immutable_fragments("Use $5$ and $10^6$. ")],
            ["$5$", "$10^6$"],
        )
        self.assertEqual(
            [
                fragment.value
                for fragment in extract_immutable_fragments(
                    "The budget was $5 million; objective $L$ stayed fixed."
                )
            ],
            ["$L$"],
        )
        self.assertEqual(
            [
                fragment.value
                for fragment in extract_immutable_fragments(
                    "Budget $5million to $10million while $x$ stayed fixed."
                )
            ],
            ["$x$"],
        )

    def test_rejects_missing_duplicate_reordered_and_unknown_placeholders(self) -> None:
        protected = protect_immutable_fragments("$x$ [2]")
        first, second = protected.placeholders
        cases = (
            (first, "immutable_placeholder_missing"),
            (f"{first} {second} {second}", "immutable_placeholder_duplicate"),
            (f"{second} {first}", "immutable_placeholder_reordered"),
            (f"{first} ⟦PET_IMMUTABLE_UNKNOWN_0001⟧", "immutable_placeholder_unknown"),
        )
        for value, reason in cases:
            with self.subTest(reason=reason):
                with self.assertRaises(ImmutablePlaceholderError) as caught:
                    restore_immutable_fragments(value, protected)
                self.assertEqual(caught.exception.reason, reason)

    def test_reserved_source_literal_is_itself_protected_without_collision(self) -> None:
        literal = "⟦PET_IMMUTABLE_EXISTING_0000⟧"
        protected = protect_immutable_fragments(f"Keep {literal} and $x$.")

        self.assertNotIn(protected.placeholder_prefix, f"Keep {literal} and $x$.")
        self.assertEqual(len(protected.fragments), 2)
        self.assertEqual(
            restore_immutable_fragments(" ".join(protected.placeholders), protected),
            f"{literal} $x$",
        )

    def test_restore_rejects_formula_inserted_outside_valid_placeholders(self) -> None:
        protected = protect_immutable_fragments("Keep $x$ [2].")

        with self.assertRaises(ImmutablePlaceholderError) as caught:
            restore_immutable_fragments(
                f"保留 {protected.placeholders[0]} {protected.placeholders[1]}，另有 $z$。",
                protected,
            )

        self.assertEqual(caught.exception.reason, "immutable_changed")

    def test_restore_rejects_inserted_formula_when_source_has_no_fragments(self) -> None:
        protected = protect_immutable_fragments("Plain prose only.")

        with self.assertRaises(ImmutablePlaceholderError) as caught:
            restore_immutable_fragments("只有普通文本，但新增 $z$。", protected)

        self.assertEqual(caught.exception.reason, "immutable_changed")

    def test_persisted_translation_audit_is_strict_and_reusable(self) -> None:
        source = "Use $x$ [3] then $y$."
        cases = (
            ("使用 $x$ [3] 然后 $y$。", True, None),
            ("使用 $x$ [3]。", False, "immutable_missing"),
            ("使用 $x$ $x$ [3] 然后 $y$。", False, "immutable_duplicate"),
            ("使用 $y$ [3] 然后 $x$。", False, "immutable_reordered"),
            ("使用 $x$ [3] 然后 $z$。", False, "immutable_changed"),
            ("使用 $x$ [3] 然后 $y$，并新增 \\eqref{extra}。", False, "immutable_changed"),
        )

        for translation, safe, reason in cases:
            with self.subTest(reason=reason, translation=translation):
                audit = audit_immutable_translation(source, translation)
                self.assertEqual(audit.safe, safe)
                self.assertEqual(audit.reason, reason)

    def test_translate_single_block_restores_exact_model_placeholders(self) -> None:
        doc = _document("Loss $L(x)$ is reported in [3].")

        class Client:
            async def acomplete(self, messages: list[dict], *, task: str) -> str:
                self.messages = messages
                placeholders = re.findall(r"⟦PET_IMMUTABLE_[A-F0-9]+_\d{4}⟧", messages[1]["content"])
                return f"损失 {placeholders[0]} 报告于 {placeholders[1]}。"

        client = Client()
        with (
            patch(
                "backend.translation.translate.get_config",
                return_value=SimpleNamespace(translation_prompt=""),
            ),
            patch("backend.translation.translate.get_client", return_value=client),
        ):
            index, translation, status = asyncio.run(translate_single_block(doc, 0))

        self.assertEqual((index, status), (0, "done"))
        self.assertEqual(translation, "损失 $L(x)$ 报告于 [3]。")
        self.assertIn("【不可变标记】", client.messages[1]["content"])

    def test_invalid_model_placeholder_sequence_becomes_error(self) -> None:
        doc = _document("Loss $L(x)$ is reported in [3].")

        class Client:
            async def acomplete(self, messages: list[dict], *, task: str) -> str:
                placeholders = re.findall(r"⟦PET_IMMUTABLE_[A-F0-9]+_\d{4}⟧", messages[1]["content"])
                return f"顺序错误 {placeholders[1]} {placeholders[0]}"

        with (
            patch(
                "backend.translation.translate.get_config",
                return_value=SimpleNamespace(translation_prompt=""),
            ),
            patch("backend.translation.translate.get_client", return_value=Client()),
        ):
            index, translation, status = asyncio.run(translate_single_block(doc, 0))

        self.assertEqual((index, translation, status), (0, None, "error"))

    def test_translate_single_block_rejects_model_inserted_formula(self) -> None:
        doc = _document("Loss $L(x)$ is reported in [3].")

        class Client:
            async def acomplete(self, messages: list[dict], *, task: str) -> str:
                placeholders = re.findall(r"⟦PET_IMMUTABLE_[A-F0-9]+_\d{4}⟧", messages[1]["content"])
                return f"损失 {placeholders[0]} 报告于 {placeholders[1]}，另见 $z$。"

        with (
            patch(
                "backend.translation.translate.get_config",
                return_value=SimpleNamespace(translation_prompt=""),
            ),
            patch("backend.translation.translate.get_client", return_value=Client()),
        ):
            index, translation, status = asyncio.run(translate_single_block(doc, 0))

        self.assertEqual((index, translation, status), (0, None, "error"))

    def test_heading_compaction_preserves_immutable_fragments_and_section_number(self) -> None:
        cases = (
            (
                "Attention [3]",
                "注意力 (Attention {placeholder})",
                "注意力 (Attention [3])",
            ),
            ("Energy $E$", "能量 (Energy {placeholder})", "能量 (Energy $E$)"),
            (
                "5.1 Training Data",
                "训练数据 (5.1 Training Data)",
                "训练数据 (5.1 Training Data)",
            ),
        )
        for original, response, expected in cases:
            with self.subTest(original=original):
                doc = _document(original)
                doc.blocks[0].type = "heading"

                class Client:
                    async def acomplete(self, messages: list[dict], *, task: str) -> str:
                        placeholders = re.findall(
                            r"⟦PET_IMMUTABLE_[A-F0-9]+_\d{4}⟧",
                            messages[1]["content"],
                        )
                        value = response
                        if "{placeholder}" in value:
                            value = value.format(placeholder=placeholders[0])
                        return value

                with (
                    patch(
                        "backend.translation.translate.get_config",
                        return_value=SimpleNamespace(translation_prompt=""),
                    ),
                    patch("backend.translation.translate.get_client", return_value=Client()),
                ):
                    index, translation, status = asyncio.run(
                        translate_single_block(doc, 0)
                    )

                self.assertEqual((index, status), (0, "done"))
                self.assertEqual(translation, expected)

    def test_heading_compaction_runs_before_placeholder_restore(self) -> None:
        doc = _document("Energy $E$")
        doc.blocks[0].type = "heading"

        class Client:
            async def acomplete(self, messages: list[dict], *, task: str) -> str:
                placeholder = re.findall(
                    r"⟦PET_IMMUTABLE_[A-F0-9]+_\d{4}⟧",
                    messages[1]["content"],
                )[0]
                return (
                    f"<pet-heading>## 能量 {placeholder}</pet-heading>\n\n"
                    "这是下一段，并且含有不属于标题的 $z$。"
                )

        with (
            patch(
                "backend.translation.translate.get_config",
                return_value=SimpleNamespace(translation_prompt=""),
            ),
            patch("backend.translation.translate.get_client", return_value=Client()),
        ):
            index, translation, status = asyncio.run(translate_single_block(doc, 0))

        self.assertEqual((index, status), (0, "done"))
        self.assertEqual(translation, "能量 $E$")

    def test_paragraph_translation_keeps_multiple_paragraphs_from_model(self) -> None:
        doc = _document("First paragraph. Second paragraph.")

        class Client:
            async def acomplete(self, messages: list[dict], *, task: str) -> str:
                return "第一段。\n\n第二段。"

        with (
            patch(
                "backend.translation.translate.get_config",
                return_value=SimpleNamespace(translation_prompt=""),
            ),
            patch("backend.translation.translate.get_client", return_value=Client()),
        ):
            index, translation, status = asyncio.run(translate_single_block(doc, 0))

        self.assertEqual((index, status), (0, "done"))
        self.assertEqual(translation, "第一段。\n\n第二段。")

    def test_retry_forces_done_block_through_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            papers_dir = Path(tmp) / "papers"
            doc = _document(
                "Defining and identifying semantic duplicates",
                paper_id="force-retry",
            )
            doc.blocks[0].type = "heading"
            doc.blocks[0].status = "done"
            doc.blocks[0].translation = "旧标题\n\n误吸入的下一段。"

            class Client:
                calls = 0

                async def acomplete(self, messages: list[dict], *, task: str) -> str:
                    self.calls += 1
                    return "<pet-heading>定义并识别语义重复项</pet-heading>"

            client = Client()
            with (
                patch.object(storage_files, "PAPERS_DIR", papers_dir),
                patch(
                    "backend.translation.translate.get_config",
                    return_value=SimpleNamespace(translation_prompt=""),
                ),
                patch("backend.translation.translate.get_client", return_value=client),
            ):
                save_document(doc)
                result = asyncio.run(retry_single_block("force-retry", 0))
                reloaded = load_document("force-retry")

        self.assertEqual(client.calls, 1)
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["translation"], "定义并识别语义重复项")
        self.assertIsNotNone(reloaded)
        self.assertEqual(reloaded.blocks[0].status, "done")
        self.assertEqual(reloaded.blocks[0].translation, "定义并识别语义重复项")

    def test_normal_translation_keeps_done_block_cache(self) -> None:
        doc = _document("A heading")
        doc.blocks[0].type = "heading"
        doc.blocks[0].status = "done"
        doc.blocks[0].translation = "已缓存标题"

        class Client:
            async def acomplete(self, messages: list[dict], *, task: str) -> str:
                raise AssertionError("done block must not call the model")

        with patch("backend.translation.translate.get_client", return_value=Client()):
            result = asyncio.run(translate_single_block(doc, 0))

        self.assertEqual(result, (0, "已缓存标题", "done"))

    def test_failed_retry_clears_previous_done_translation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            papers_dir = Path(tmp) / "papers"
            doc = _document("A heading", paper_id="failed-force-retry")
            doc.blocks[0].type = "heading"
            doc.blocks[0].status = "done"
            doc.blocks[0].translation = "已知错误的旧译文"

            class Client:
                async def acomplete(self, messages: list[dict], *, task: str) -> str:
                    return "<pet-heading></pet-heading>"

            with (
                patch.object(storage_files, "PAPERS_DIR", papers_dir),
                patch(
                    "backend.translation.translate.get_config",
                    return_value=SimpleNamespace(translation_prompt=""),
                ),
                patch("backend.translation.translate.get_client", return_value=Client()),
            ):
                save_document(doc)
                result = asyncio.run(retry_single_block("failed-force-retry", 0))
                reloaded = load_document("failed-force-retry")

        self.assertEqual(result["status"], "error")
        self.assertIsNone(result["translation"])
        self.assertIsNotNone(reloaded)
        self.assertEqual(reloaded.blocks[0].status, "error")
        self.assertEqual(reloaded.blocks[0].translation, "")

    def test_sse_does_not_persist_block_done_when_placeholder_validation_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            papers_dir = Path(tmp) / "papers"
            doc = _document("Loss $L(x)$ is reported in [3].", paper_id="immutable-sse")

            class Client:
                async def acomplete(self, messages: list[dict], *, task: str) -> str:
                    placeholders = re.findall(
                        r"⟦PET_IMMUTABLE_[A-F0-9]+_\d{4}⟧",
                        messages[1]["content"],
                    )
                    return placeholders[0]

            with (
                patch.object(storage_files, "PAPERS_DIR", papers_dir),
                patch(
                    "backend.translation.translate.get_config",
                    return_value=SimpleNamespace(
                        translation_prompt="",
                        translation_concurrency=1,
                    ),
                ),
                patch("backend.translation.translate.get_client", return_value=Client()),
            ):
                save_document(doc)
                events = asyncio.run(_collect_events("immutable-sse"))
                reloaded = load_document("immutable-sse")

        self.assertTrue(any("event: block_error" in event for event in events))
        self.assertFalse(any("event: block_done" in event for event in events))
        self.assertIsNotNone(reloaded)
        self.assertEqual(reloaded.blocks[0].status, "error")
        self.assertIsNone(reloaded.blocks[0].translation)


def _document(source: str, *, paper_id: str = "immutable") -> PaperDocument:
    return PaperDocument(
        paper_id=paper_id,
        title="Immutable Translation",
        source="ar5iv",
        extracted_at="2026-07-21T00:00:00Z",
        blocks=[Block(index=0, type="paragraph", original=source, status="pending")],
    )


async def _collect_events(paper_id: str) -> list[str]:
    return [event async for event in translate_paper_sse(paper_id)]


if __name__ == "__main__":
    unittest.main()
