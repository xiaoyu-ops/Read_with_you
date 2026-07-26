from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from backend.llm.client import LLMClient
from backend.llm.models import AppConfig, Provider


class LLMToolCallsTest(unittest.TestCase):
    def test_acomplete_with_tools_normalizes_litellm_tool_calls(self) -> None:
        async def fake_acompletion(**params):
            assert params["tools"][0]["function"]["name"] == "local_web_search"
            assert params["tool_choice"] == "auto"
            message = SimpleNamespace(
                content="",
                tool_calls=[
                    SimpleNamespace(
                        id="call-1",
                        type="function",
                        function=SimpleNamespace(
                            name="local_web_search",
                            arguments='{"search_query":"paper repo","reason":"需要搜索"}',
                        ),
                    )
                ],
            )
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

        config = AppConfig(
            llm_providers=[Provider(name="openai-compatible", type="openai", models=["gpt-test"])],
            default_provider="openai-compatible",
            default_model="gpt-test",
        )
        client = LLMClient(config)

        async def run() -> None:
            with patch("backend.llm.client.litellm.acompletion", fake_acompletion):
                result = await client.acomplete_with_tools(
                    [{"role": "user", "content": "hi"}],
                    tools=[
                        {
                            "type": "function",
                            "function": {"name": "local_web_search", "parameters": {"type": "object"}},
                        }
                    ],
                    task="agent_intent",
                    variant="low",
                )

            assert result["content"] == ""
            assert result["tool_calls"] == [
                {
                    "id": "call-1",
                    "type": "function",
                    "name": "local_web_search",
                    "arguments": {"search_query": "paper repo", "reason": "需要搜索"},
                }
            ]

        asyncio.run(run())

    def test_astream_with_tools_emits_text_and_assembles_split_tool_call(self) -> None:
        async def fake_stream():
            yield SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content="正在", tool_calls=[]))]
            )
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content=None,
                            tool_calls=[
                                SimpleNamespace(
                                    index=0,
                                    id="call-1",
                                    type="function",
                                    function=SimpleNamespace(name="local_web_", arguments='{"search_query":"paper'),
                                )
                            ],
                        )
                    )
                ]
            )
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content=None,
                            tool_calls=[
                                SimpleNamespace(
                                    index=0,
                                    id=None,
                                    type=None,
                                    function=SimpleNamespace(name="search", arguments=' repo"}'),
                                )
                            ],
                        )
                    )
                ]
            )

        async def fake_acompletion(**params):
            assert params["stream"] is True
            return fake_stream()

        config = AppConfig(
            llm_providers=[Provider(name="openai-compatible", type="openai", models=["gpt-test"])],
            default_provider="openai-compatible",
            default_model="gpt-test",
        )
        client = LLMClient(config)

        async def run() -> None:
            with patch("backend.llm.client.litellm.acompletion", fake_acompletion):
                events = [
                    event
                    async for event in client.astream_with_tools(
                        [{"role": "user", "content": "hi"}],
                        tools=[{"type": "function", "function": {"name": "local_web_search"}}],
                    )
                ]
            assert events[0] == {"type": "content_delta", "content": "正在"}
            assert events[-1] == {
                "type": "response",
                "content": "正在",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "name": "local_web_search",
                        "arguments": {"search_query": "paper repo"},
                    }
                ],
            }

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
