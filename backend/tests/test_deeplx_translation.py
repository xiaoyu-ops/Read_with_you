from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from backend.extraction.blocks import Block, PaperDocument
from backend.storage import files as storage_files
from backend.storage.files import load_document, save_document
from backend.translation.deeplx import DeepLXError, translate_text
from backend.translation.translate import translate_paper_sse, translate_single_block


_TEST_ENV = {
    "DEEPLX_API_KEY": "test-token-abcdefghijklmnopqrstuvwxyz",
    "DEEPLX_API_BASE": "https://api.deeplx.test",
    "DEEPLX_TIMEOUT_SECONDS": "5",
}


class DeepLXClientTest(unittest.TestCase):
    def test_success_validates_payload_and_returns_translation(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.method, "POST")
            self.assertEqual(
                request.url.path,
                "/test-token-abcdefghijklmnopqrstuvwxyz/translate",
            )
            payload = json.loads(request.content)
            self.assertEqual(
                payload,
                {"text": "Academic text.", "source_lang": "EN", "target_lang": "ZH"},
            )
            return httpx.Response(200, json={"code": 200, "data": "学术文本。"})

        async def run() -> str:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await translate_text("Academic text.", client=client)

        with patch.dict(os.environ, _TEST_ENV):
            self.assertEqual(asyncio.run(run()), "学术文本。")

    def test_http_and_provider_errors_are_stable(self) -> None:
        cases = (
            (401, {"code": 401}, "deeplx_authentication_failed"),
            (429, {"code": 429}, "deeplx_rate_limited"),
            (503, {"code": 503}, "deeplx_http_error"),
            (200, {"code": 500}, "deeplx_provider_error"),
        )
        for status_code, body, expected in cases:
            with self.subTest(status_code=status_code, body=body):
                async def run() -> None:
                    transport = httpx.MockTransport(
                        lambda _request: httpx.Response(status_code, json=body)
                    )
                    async with httpx.AsyncClient(transport=transport) as client:
                        await translate_text("Academic text.", client=client)

                with patch.dict(os.environ, _TEST_ENV):
                    with self.assertRaises(DeepLXError) as caught:
                        asyncio.run(run())
                self.assertEqual(caught.exception.code, expected)
                self.assertNotIn(_TEST_ENV["DEEPLX_API_KEY"], str(caught.exception))

    def test_invalid_empty_and_notice_responses_are_rejected(self) -> None:
        cases = (
            (httpx.Response(200, content=b"not-json"), "deeplx_invalid_response"),
            (httpx.Response(200, json={"code": 200, "data": ""}), "deeplx_empty_translation"),
            (
                httpx.Response(200, json={"code": 200, "data": "https://service.example/notice"}),
                "deeplx_invalid_translation",
            ),
        )
        for response, expected in cases:
            with self.subTest(expected=expected):
                async def run() -> None:
                    async with httpx.AsyncClient(
                        transport=httpx.MockTransport(lambda _request: response)
                    ) as client:
                        await translate_text("Academic text.", client=client)

                with patch.dict(os.environ, _TEST_ENV):
                    with self.assertRaises(DeepLXError) as caught:
                        asyncio.run(run())
                self.assertEqual(caught.exception.code, expected)

    def test_timeout_is_redacted_and_cancellation_propagates(self) -> None:
        def timeout_handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=request)

        async def timeout_run() -> None:
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(timeout_handler)
            ) as client:
                await translate_text("Academic text.", client=client)

        with patch.dict(os.environ, _TEST_ENV):
            with self.assertRaises(DeepLXError) as caught:
                asyncio.run(timeout_run())
        self.assertEqual(caught.exception.code, "deeplx_timeout")
        self.assertNotIn(_TEST_ENV["DEEPLX_API_KEY"], str(caught.exception))

        def cancel_handler(_request: httpx.Request) -> httpx.Response:
            raise asyncio.CancelledError

        async def cancel_run() -> None:
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(cancel_handler)
            ) as client:
                await translate_text("Academic text.", client=client)

        with patch.dict(os.environ, _TEST_ENV):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(cancel_run())

    def test_missing_or_invalid_configuration_is_rejected(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(DeepLXError) as caught:
                asyncio.run(translate_text("Academic text."))
        self.assertEqual(caught.exception.code, "deeplx_not_configured")

        invalid = dict(_TEST_ENV, DEEPLX_API_BASE="http://api.deeplx.test")
        with patch.dict(os.environ, invalid):
            with self.assertRaises(DeepLXError) as caught:
                asyncio.run(translate_text("Academic text."))
        self.assertEqual(caught.exception.code, "deeplx_invalid_base_url")


class DeepLXTranslationIntegrationTest(unittest.TestCase):
    def test_block_translation_uses_deeplx_and_restores_immutables(self) -> None:
        doc = PaperDocument(
            paper_id="deeplx-test",
            title="DeepLX Test",
            source="ar5iv",
            extracted_at="2026-07-22T00:00:00Z",
            blocks=[
                Block(
                    index=0,
                    type="paragraph",
                    original="Loss $L(x)$ follows prior work [12].",
                    status="pending",
                )
            ],
        )
        received: list[str] = []

        async def fake_deeplx(text: str) -> str:
            received.append(text)
            placeholders = re.findall(
                r"⟦PET_IMMUTABLE_[A-F0-9]+_\d{4}⟧",
                text,
            )
            return f"损失 {placeholders[0]} 遵循已有工作 {placeholders[1]}。"

        with (
            patch.dict(os.environ, {"PEINIDU_TRANSLATION_PROVIDER": "deeplx"}),
            patch(
                "backend.translation.translate.translate_with_deeplx",
                side_effect=fake_deeplx,
            ),
            patch(
                "backend.translation.translate.get_client",
                side_effect=AssertionError("DeepLX path must not call LiteLLM"),
            ),
        ):
            result = asyncio.run(translate_single_block(doc, 0))

        self.assertEqual(result, (0, "损失 $L(x)$ 遵循已有工作 [12]。", "done"))
        self.assertEqual(len(received), 1)
        self.assertNotIn("【上一段】", received[0])

    def test_deeplx_error_keeps_block_retryable(self) -> None:
        doc = PaperDocument(
            paper_id="deeplx-error",
            title="DeepLX Error",
            source="ar5iv",
            extracted_at="2026-07-22T00:00:00Z",
            blocks=[Block(index=0, type="paragraph", original="Text.", status="pending")],
        )

        async def fail(_text: str) -> str:
            raise DeepLXError("deeplx_rate_limited")

        with (
            patch.dict(os.environ, {"PEINIDU_TRANSLATION_PROVIDER": "deeplx"}),
            patch("backend.translation.translate.translate_with_deeplx", side_effect=fail),
        ):
            result = asyncio.run(translate_single_block(doc, 0))

        self.assertEqual(result, (0, None, "error"))

    def test_deeplx_result_flows_through_sse_and_persists(self) -> None:
        async def fake_deeplx(text: str) -> str:
            self.assertEqual(text, "A long academic paragraph.")
            return "一段较长的学术文本。"

        async def collect() -> list[str]:
            return [event async for event in translate_paper_sse("deeplx-sse")]

        with tempfile.TemporaryDirectory() as tmp:
            papers_dir = Path(tmp) / "papers"
            doc = PaperDocument(
                paper_id="deeplx-sse",
                title="DeepLX SSE",
                source="ar5iv",
                extracted_at="2026-07-22T00:00:00Z",
                blocks=[
                    Block(
                        index=0,
                        type="paragraph",
                        original="A long academic paragraph.",
                        status="pending",
                    )
                ],
            )
            with (
                patch.object(storage_files, "PAPERS_DIR", papers_dir),
                patch.dict(os.environ, {"PEINIDU_TRANSLATION_PROVIDER": "deeplx"}),
                patch(
                    "backend.translation.translate.translate_with_deeplx",
                    side_effect=fake_deeplx,
                ),
                patch(
                    "backend.translation.translate.get_config",
                    return_value=SimpleNamespace(translation_concurrency=1),
                ),
            ):
                save_document(doc)
                events = asyncio.run(collect())
                reloaded = load_document("deeplx-sse")

        self.assertTrue(any("event: block_done" in event for event in events))
        self.assertTrue(any("event: complete" in event for event in events))
        self.assertIsNotNone(reloaded)
        self.assertEqual(reloaded.blocks[0].status, "done")
        self.assertEqual(reloaded.blocks[0].translation, "一段较长的学术文本。")


if __name__ == "__main__":
    unittest.main()
