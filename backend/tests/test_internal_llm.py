from __future__ import annotations

import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routes_internal_llm import router
from backend.llm.client import LLMClient
from backend.llm.models import AppConfig, Provider, TaskModels


class _StatusError(Exception):
    def __init__(self, status_code: int, retry_after: str | None = None) -> None:
        super().__init__("sensitive provider detail")
        self.status_code = status_code
        self.response = SimpleNamespace(headers={"retry-after": retry_after} if retry_after else {})


class InternalLLMRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)
        self.url = "/internal/llm/v1/chat/completions"
        self.payload = {
            "model": "pdf-translation",
            "messages": [{"role": "user", "content": "Translate this sentence."}],
            "stream": False,
        }

    def tearDown(self) -> None:
        self.client.close()

    def test_missing_server_token_disables_endpoint(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            response = self.client.post(self.url, json=self.payload)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "internal_llm_not_configured")

    def test_invalid_bearer_token_is_rejected(self) -> None:
        with patch.dict(os.environ, {"PEINIDU_PDF_EXPORT_INTERNAL_TOKEN": "expected"}, clear=True):
            response = self.client.post(
                self.url,
                json=self.payload,
                headers={"Authorization": "Bearer wrong"},
            )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["www-authenticate"], "Bearer")

    def test_alias_and_non_streaming_contract_are_strict(self) -> None:
        headers = {"Authorization": "Bearer expected"}
        with patch.dict(os.environ, {"PEINIDU_PDF_EXPORT_INTERNAL_TOKEN": "expected"}, clear=True):
            wrong_alias = self.client.post(
                self.url,
                json={**self.payload, "model": "real-provider-model"},
                headers=headers,
            )
            streaming = self.client.post(
                self.url,
                json={**self.payload, "stream": True},
                headers=headers,
            )
        self.assertEqual(wrong_alias.status_code, 422)
        self.assertEqual(streaming.status_code, 422)

    def test_success_uses_translation_task_and_hides_real_model(self) -> None:
        upstream = {
            "id": "chatcmpl-test",
            "model": "secret-real-model",
            "provider_specific_fields": {"credential": "must-not-leak"},
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "译文"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        }
        fake_client = SimpleNamespace(
            acomplete_openai_response=AsyncMock(return_value=upstream)
        )
        payload = {**self.payload, "response_format": {"type": "text"}}
        with (
            patch.dict(os.environ, {"PEINIDU_PDF_EXPORT_INTERNAL_TOKEN": "expected"}, clear=True),
            patch("backend.api.routes_internal_llm.get_client", return_value=fake_client),
        ):
            response = self.client.post(
                self.url,
                json=payload,
                headers={"Authorization": "Bearer expected"},
            )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["model"], "pdf-translation")
        self.assertEqual(body["choices"][0]["message"]["content"], "译文")
        self.assertNotIn("provider_specific_fields", body)
        fake_client.acomplete_openai_response.assert_awaited_once_with(
            [{"role": "user", "content": "Translate this sentence."}],
            task="translation",
            response_format={"type": "text"},
        )

    def test_rate_limit_status_and_retry_after_are_preserved(self) -> None:
        fake_client = SimpleNamespace(
            acomplete_openai_response=AsyncMock(side_effect=_StatusError(429, "17"))
        )
        with (
            patch.dict(os.environ, {"PEINIDU_PDF_EXPORT_INTERNAL_TOKEN": "expected"}, clear=True),
            patch("backend.api.routes_internal_llm.get_client", return_value=fake_client),
        ):
            response = self.client.post(
                self.url,
                json=self.payload,
                headers={"Authorization": "Bearer expected"},
            )
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["retry-after"], "17")
        self.assertEqual(response.json()["error"]["code"], "provider_rate_limited")
        self.assertNotIn("sensitive provider detail", response.text)

    def test_timeout_is_mapped_without_exception_detail(self) -> None:
        fake_client = SimpleNamespace(
            acomplete_openai_response=AsyncMock(side_effect=asyncio.TimeoutError("secret"))
        )
        with (
            patch.dict(os.environ, {"PEINIDU_PDF_EXPORT_INTERNAL_TOKEN": "expected"}, clear=True),
            patch("backend.api.routes_internal_llm.get_client", return_value=fake_client),
        ):
            response = self.client.post(
                self.url,
                json=self.payload,
                headers={"Authorization": "Bearer expected"},
            )
        self.assertEqual(response.status_code, 504)
        self.assertEqual(response.json()["error"]["code"], "provider_timeout")
        self.assertNotIn("secret", response.text)

    def test_provider_http_timeout_is_normalized(self) -> None:
        fake_client = SimpleNamespace(
            acomplete_openai_response=AsyncMock(side_effect=_StatusError(408))
        )
        with (
            patch.dict(os.environ, {"PEINIDU_PDF_EXPORT_INTERNAL_TOKEN": "expected"}, clear=True),
            patch("backend.api.routes_internal_llm.get_client", return_value=fake_client),
        ):
            response = self.client.post(
                self.url,
                json=self.payload,
                headers={"Authorization": "Bearer expected"},
            )
        self.assertEqual(response.status_code, 504)
        self.assertEqual(response.json()["error"]["code"], "provider_timeout")


class InternalLLMClientTests(unittest.TestCase):
    def test_openai_response_uses_configured_translation_model(self) -> None:
        captured: dict = {}

        class _Response:
            def model_dump(self, *, exclude_none: bool):
                self.exclude_none = exclude_none
                return {
                    "id": "chatcmpl-configured",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "译文"},
                            "finish_reason": "stop",
                        }
                    ],
                }

        async def fake_acompletion(**params):
            captured.update(params)
            return _Response()

        config = AppConfig(
            llm_providers=[
                Provider(
                    name="translation-provider",
                    type="openai",
                    api_key="configured-only-in-backend",
                    api_base="https://provider.example/v1",
                    models=["translation-model"],
                )
            ],
            default_provider="translation-provider",
            default_model="default-model",
            task_models=TaskModels(translation="translation-model"),
        )
        client = LLMClient(config)

        async def run():
            with patch("backend.llm.client.litellm.acompletion", fake_acompletion):
                return await client.acomplete_openai_response(
                    [{"role": "user", "content": "hello"}],
                    response_format={"type": "text"},
                )

        result = asyncio.run(run())
        self.assertEqual(result["id"], "chatcmpl-configured")
        self.assertEqual(captured["model"], "openai/translation-model")
        self.assertEqual(captured["api_base"], "https://provider.example/v1")
        self.assertEqual(captured["response_format"], {"type": "text"})
        self.assertFalse(captured["stream"])


if __name__ == "__main__":
    unittest.main()
