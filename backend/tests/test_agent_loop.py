from __future__ import annotations

import asyncio
import json
import unittest

from backend.agent.tool_loop import AgentLoopState, _agent_loop_tool_content, run_iterative_agent_loop
from backend.tools.registry import ToolCall, ToolRegistry, ToolResult, ToolSpec


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": "search",
            "parameters": {"type": "object"},
        },
    }
]


class ScriptedClient:
    def __init__(self, responses: list[dict], final_text: str = "最终回答") -> None:
        self.responses = list(responses)
        self.final_text = final_text
        self.model_messages: list[list[dict]] = []
        self.final_messages: list[dict] | None = None

    async def acomplete_with_tools(self, messages, tools, **kwargs):
        self.model_messages.append([dict(message) for message in messages])
        return self.responses.pop(0)

    async def acomplete(self, messages, **kwargs):
        self.final_messages = [dict(message) for message in messages]
        return self.final_text


def tool_call(call_id: str = "call-1", query: str = "paper") -> dict:
    return {
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "name": "search",
                "arguments": {"query": query},
            }
        ],
    }


class IterativeAgentLoopTest(unittest.TestCase):
    def test_large_tool_result_remains_valid_json_with_evidence(self) -> None:
        result = ToolResult(
            name="local.search",
            content="result " * 4_000,
            evidence=(
                {
                    "kind": "web_fetch_result",
                    "url": "https://example.com/paper",
                    "title": "source " * 1_000,
                },
            ),
            metadata={"source": "agent_tool_loop", "large": "x" * 20_000},
        )

        payload = json.loads(_agent_loop_tool_content(result))

        assert payload["evidence"][0]["kind"] == "web_fetch_result"
        assert payload["evidence"][0]["url"] == "https://example.com/paper"

    def test_provider_failures_have_actionable_messages(self) -> None:
        class ProviderError(RuntimeError):
            def __init__(self, message: str, status_code: int | None = None) -> None:
                super().__init__(message)
                self.status_code = status_code

        cases = [
            (ProviderError("bad credential", 401), "model_authentication_failed", "鉴权失败"),
            (ProviderError("Model missing-one is not supported", 401), "model_not_found", "模型不可用"),
            (ProviderError("5-hour usage limit reached", 429), "model_rate_limited", "额度已用完"),
            (ProviderError("model not found", 404), "model_not_found", "模型不可用"),
            (ProviderError("request timed out"), "model_request_timeout", "响应超时"),
            (ProviderError("unsupported parameter: tools"), "model_unsupported_request", "不支持"),
        ]

        for error, expected_limit, expected_text in cases:
            async def scenario() -> None:
                class FailingClient:
                    async def acomplete_with_tools(self, messages, tools, **kwargs):
                        raise error

                result = await run_iterative_agent_loop(
                    FailingClient(),
                    ToolRegistry(),
                    messages=[{"role": "user", "content": "test"}],
                    tools=TOOLS,
                )
                assert result.status == "error"
                assert expected_limit in result.state.limits
                assert expected_text in result.final_text

            with self.subTest(limit=expected_limit):
                asyncio.run(scenario())

    def test_state_round_trip_is_json_safe(self) -> None:
        state = AgentLoopState(
            messages=[{"role": "user", "content": "hi"}],
            granted_scopes=["external_search"],
            pending_tool_calls=[
                {
                    "call_id": "call-1",
                    "provider_name": "search",
                    "tool_name": "local.search",
                    "arguments": {"query": "paper"},
                }
            ],
            model_iterations=2,
            tool_calls=1,
            last_call_signature='{"tool":"search"}',
            last_result_fingerprint="abc",
            repeated_no_progress=1,
            failed_tool_calls=1,
            failure_fallback_attempted=True,
            trace=[{"type": "tool_done"}],
            limits=["limit"],
        )

        restored = AgentLoopState.from_dict(state.to_dict())

        assert restored.to_dict() == state.to_dict()

    def test_ungranted_scope_pauses_without_executing_tool(self) -> None:
        calls: list[ToolCall] = []

        async def executor(call: ToolCall) -> ToolResult:
            calls.append(call)
            return ToolResult(name=call.name, content="should not run")

        async def scenario() -> None:
            registry = ToolRegistry()
            registry.register(ToolSpec("local.search", "search", permission_scope="external_search"), executor)
            client = ScriptedClient([tool_call()])

            result = await run_iterative_agent_loop(
                client,
                registry,
                messages=[{"role": "user", "content": "查找论文仓库"}],
                tools=TOOLS,
                tool_name_map={"search": "local.search"},
            )

            assert result.status == "waiting_permission"
            assert result.pending_permission == "external_search"
            assert calls == []
            assert result.state.tool_calls == 0
            assert result.state.pending_tool_calls[0]["tool_name"] == "local.search"
            assert [message["role"] for message in result.state.messages] == ["user", "assistant"]

        asyncio.run(scenario())

    def test_resume_executes_original_pending_call_before_model_continues(self) -> None:
        calls: list[ToolCall] = []

        async def executor(call: ToolCall) -> ToolResult:
            calls.append(call)
            return ToolResult(name=call.name, content="原 pending call 的结果")

        async def scenario() -> None:
            registry = ToolRegistry()
            registry.register(ToolSpec("local.search", "search", permission_scope="external_search"), executor)
            client = ScriptedClient(
                [tool_call(query="exact query"), {"content": "已根据原结果回答。", "tool_calls": []}]
            )
            paused = await run_iterative_agent_loop(
                client,
                registry,
                messages=[{"role": "user", "content": "查找"}],
                tools=TOOLS,
                base_arguments={"paper_title": "Paper"},
                tool_name_map={"search": "local.search"},
            )
            persisted = paused.state.to_dict()

            resumed = await run_iterative_agent_loop(
                client,
                registry,
                state=AgentLoopState.from_dict(persisted),
                tools=TOOLS,
                scope="external_search",
                tool_name_map={"search": "local.search"},
            )

            assert resumed.status == "completed"
            assert resumed.final_text == "已根据原结果回答。"
            assert len(client.model_messages) == 2
            assert calls[0].arguments == {"paper_title": "Paper", "query": "exact query"}
            assert client.model_messages[1][-1]["role"] == "tool"
            assert client.model_messages[1][-1]["tool_call_id"] == "call-1"
            assert resumed.state.pending_tool_calls == []
            assert resumed.state.granted_scopes == ["external_search"]

        asyncio.run(scenario())

    def test_mcp_catalog_call_does_not_receive_local_base_arguments(self) -> None:
        calls: list[ToolCall] = []

        async def executor(call: ToolCall) -> ToolResult:
            calls.append(call)
            return ToolResult(name=call.name, content="clicked")

        async def scenario() -> None:
            tool_name = "mcp_playwright_official_browser_click"
            registry = ToolRegistry()
            registry.register(
                ToolSpec(
                    tool_name,
                    "click",
                    permission_scope="browser_control",
                    source="mcp",
                    input_schema={
                        "type": "object",
                        "properties": {"target": {"type": "string"}},
                        "required": ["target"],
                    },
                ),
                executor,
            )
            tools = [
                {
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "description": "click",
                        "parameters": {"type": "object"},
                    },
                }
            ]
            client = ScriptedClient(
                [
                    {
                        "content": "",
                        "tool_calls": [
                            {"id": "call-1", "name": tool_name, "arguments": {"target": "e8"}}
                        ],
                    },
                    {"content": "完成。", "tool_calls": []},
                ]
            )

            result = await run_iterative_agent_loop(
                client,
                registry,
                messages=[{"role": "user", "content": "点击文献库"}],
                tools=tools,
                scope="browser_control",
                base_arguments={"query": "点击文献库", "exclude_arxiv_id": "2104.08691"},
                tool_name_map={tool_name: tool_name},
            )

            assert result.status == "completed"
            assert calls[0].arguments == {"target": "e8"}

        asyncio.run(scenario())

    def test_multiple_scopes_pause_sequentially_in_original_call_order(self) -> None:
        calls: list[str] = []

        async def executor(call: ToolCall) -> ToolResult:
            calls.append(call.name)
            return ToolResult(name=call.name, content=f"{call.name} result")

        async def scenario() -> None:
            registry = ToolRegistry()
            registry.register(ToolSpec("local.search", "search", permission_scope="external_search"), executor)
            registry.register(ToolSpec("mcp:reader", "reader", permission_scope="mcp_tool"), executor)
            response = tool_call("call-1")
            response["tool_calls"].append(
                {"id": "call-2", "name": "reader", "arguments": {"document": "paper"}}
            )
            tools = [
                *TOOLS,
                {
                    "type": "function",
                    "function": {"name": "reader", "description": "reader", "parameters": {"type": "object"}},
                },
            ]
            client = ScriptedClient([response, {"content": "两项工具都完成。", "tool_calls": []}])

            first = await run_iterative_agent_loop(
                client,
                registry,
                messages=[{"role": "user", "content": "查找并读取"}],
                tools=tools,
                tool_name_map={"search": "local.search", "reader": "mcp:reader"},
            )
            assert first.pending_permission == "external_search"

            second = await run_iterative_agent_loop(
                client,
                registry,
                state=first.state,
                tools=tools,
                scope="external_search",
                tool_name_map={"search": "local.search", "reader": "mcp:reader"},
            )
            assert second.status == "waiting_permission"
            assert second.pending_permission == "mcp_tool"
            assert calls == ["local.search"]
            assert len(client.model_messages) == 1

            third = await run_iterative_agent_loop(
                client,
                registry,
                state=second.state,
                tools=tools,
                scope="mcp_tool",
                tool_name_map={"search": "local.search", "reader": "mcp:reader"},
            )
            assert third.status == "completed"
            assert calls == ["local.search", "mcp:reader"]
            assert len(client.model_messages) == 2

        asyncio.run(scenario())

    def test_tool_result_is_returned_to_model_before_final_answer(self) -> None:
        calls: list[ToolCall] = []

        async def executor(call: ToolCall) -> ToolResult:
            calls.append(call)
            return ToolResult(
                name=call.name,
                content="找到论文仓库",
                evidence=({"url": "https://example.com/repo"},),
                metadata={"mock": False},
            )

        async def scenario() -> None:
            registry = ToolRegistry()
            registry.register(ToolSpec("local.search", "search", permission_scope="external_search"), executor)
            client = ScriptedClient([tool_call(), {"content": "根据检索结果，仓库在 example.com。", "tool_calls": []}])

            result = await run_iterative_agent_loop(
                client,
                registry,
                messages=[{"role": "user", "content": "查找论文仓库"}],
                tools=TOOLS,
                scope="external_search",
                base_arguments={"paper_title": "Paper"},
                tool_name_map={"search": "local.search"},
            )

            assert result.status == "completed"
            assert result.final_text == "根据检索结果，仓库在 example.com。"
            assert result.state.model_iterations == 2
            assert result.state.tool_calls == 1
            assert calls[0].arguments["paper_title"] == "Paper"
            assert client.model_messages[1][-1]["role"] == "tool"
            assert "找到论文仓库" in client.model_messages[1][-1]["content"]
            tool_start = next(event for event in result.state.trace if event["type"] == "tool_start")
            assert tool_start["arguments"] == {"query": "paper"}
            assert [message["role"] for message in result.state.messages] == [
                "user",
                "assistant",
                "tool",
                "assistant",
            ]

        asyncio.run(scenario())

    def test_tool_capable_stream_forwards_model_deltas_without_storing_them_in_trace(self) -> None:
        class StreamingClient:
            async def astream_with_tools(self, messages, tools, **kwargs):
                yield {"type": "content_delta", "content": "逐"}
                yield {"type": "content_delta", "content": "字"}
                yield {"type": "response", "content": "逐字", "tool_calls": []}

            async def acomplete(self, messages, **kwargs):
                return "unused"

        async def scenario() -> None:
            events: list[dict] = []

            async def on_event(event: dict) -> None:
                events.append(event)

            result = await run_iterative_agent_loop(
                StreamingClient(),
                ToolRegistry(),
                messages=[{"role": "user", "content": "直接回答"}],
                tools=TOOLS,
                on_event=on_event,
            )

            assert result.status == "completed"
            assert result.final_text == "逐字"
            assert [event["text"] for event in events if event["type"] == "model_delta"] == ["逐", "字"]
            assert all(event["type"] != "model_delta" for event in result.state.trace)

        asyncio.run(scenario())

    def test_tool_error_is_fed_back_and_model_can_recover(self) -> None:
        async def failing_executor(call: ToolCall) -> ToolResult:
            raise RuntimeError("network down")

        async def scenario() -> None:
            registry = ToolRegistry()
            registry.register(
                ToolSpec("local.search", "search", permission_scope="external_search"),
                failing_executor,
            )
            client = ScriptedClient([tool_call(), {"content": "外部检索失败，我暂时无法确认。", "tool_calls": []}])

            result = await run_iterative_agent_loop(
                client,
                registry,
                messages=[{"role": "user", "content": "查一下"}],
                tools=TOOLS,
                scope="external_search",
                tool_name_map={"search": "local.search"},
            )

            assert result.status == "completed"
            assert "无法确认" in result.final_text
            assert "network down" in client.model_messages[1][-1]["content"]
            assert any(event["type"] == "tool_error" for event in result.state.trace)

        asyncio.run(scenario())

    def test_repeated_no_progress_stops_and_forces_final_answer(self) -> None:
        calls: list[str] = []

        async def executor(call: ToolCall) -> ToolResult:
            calls.append(call.name)
            return ToolResult(name=call.name, content="same result", metadata={"mock": False})

        async def scenario() -> None:
            registry = ToolRegistry()
            registry.register(ToolSpec("local.search", "search", permission_scope="external_search"), executor)
            client = ScriptedClient([tool_call("call-1"), tool_call("call-2")], final_text="没有更多进展。")

            result = await run_iterative_agent_loop(
                client,
                registry,
                messages=[{"role": "user", "content": "重复查找"}],
                tools=TOOLS,
                scope="external_search",
                tool_name_map={"search": "local.search"},
            )

            assert result.status == "limited"
            assert result.final_text == "没有更多进展。"
            assert calls == ["local.search", "local.search"]
            assert "repeated_tool_call_no_progress" in result.state.limits
            assert client.final_messages is not None

        asyncio.run(scenario())

    def test_tool_failure_allows_only_one_model_selected_fallback(self) -> None:
        calls: list[str] = []

        async def executor(call: ToolCall) -> ToolResult:
            calls.append(call.name)
            if call.name == "local.search":
                raise RuntimeError("search unavailable")
            return ToolResult(name=call.name, content="fallback evidence", metadata={"mock": False})

        async def scenario() -> None:
            registry = ToolRegistry()
            registry.register(
                ToolSpec("local.search", "search", permission_scope="external_search"),
                executor,
            )
            registry.register(
                ToolSpec("local.fetch", "fetch", permission_scope="external_search"),
                executor,
            )
            tools = [
                *TOOLS,
                {
                    "type": "function",
                    "function": {
                        "name": "fetch",
                        "description": "fetch",
                        "parameters": {"type": "object"},
                    },
                },
            ]
            fallback_response = {
                "content": "",
                "tool_calls": [
                    {"id": "call-2", "name": "fetch", "arguments": {"url": "https://example.com"}},
                    {"id": "call-3", "name": "search", "arguments": {"query": "try again"}},
                ],
            }
            client = ScriptedClient(
                [tool_call("call-1"), fallback_response],
                final_text="已使用唯一替代证据作答。",
            )

            result = await run_iterative_agent_loop(
                client,
                registry,
                messages=[{"role": "user", "content": "查找后读取"}],
                tools=tools,
                scope="external_search",
                tool_name_map={"search": "local.search", "fetch": "local.fetch"},
            )

            assert calls == ["local.search", "local.fetch"]
            assert result.final_text == "已使用唯一替代证据作答。"
            assert result.state.failed_tool_calls == 1
            assert result.state.failure_fallback_attempted is True
            assert "tool_failure_fallback_limited" in result.state.limits

        asyncio.run(scenario())

    def test_second_tool_failure_ends_without_third_attempt(self) -> None:
        calls: list[str] = []

        async def executor(call: ToolCall) -> ToolResult:
            calls.append(call.name)
            raise RuntimeError(f"{call.name} unavailable")

        async def scenario() -> None:
            registry = ToolRegistry()
            registry.register(
                ToolSpec("local.search", "search", permission_scope="external_search"),
                executor,
            )
            registry.register(
                ToolSpec("local.fetch", "fetch", permission_scope="external_search"),
                executor,
            )
            client = ScriptedClient(
                [
                    tool_call("call-1"),
                    {
                        "content": "",
                        "tool_calls": [
                            {"id": "call-2", "name": "fetch", "arguments": {"url": "https://example.com"}}
                        ],
                    },
                    tool_call("call-3"),
                ],
                final_text="两个工具都失败，无法确认。",
            )

            result = await run_iterative_agent_loop(
                client,
                registry,
                messages=[{"role": "user", "content": "查找后读取"}],
                tools=TOOLS,
                scope="external_search",
                tool_name_map={"search": "local.search", "fetch": "local.fetch"},
            )

            assert calls == ["local.search", "local.fetch"]
            assert result.final_text == "两个工具都失败，无法确认。"
            assert result.state.failed_tool_calls == 2
            assert "tool_failure_fallback_exhausted" in result.state.limits
            assert len(client.responses) == 1

        asyncio.run(scenario())

    def test_tool_budget_stops_remaining_calls(self) -> None:
        calls: list[str] = []

        async def executor(call: ToolCall) -> ToolResult:
            calls.append(call.name)
            return ToolResult(name=call.name, content="ok", metadata={"mock": False})

        async def scenario() -> None:
            registry = ToolRegistry()
            registry.register(ToolSpec("local.search", "search", permission_scope="external_search"), executor)
            response = tool_call("call-1")
            response["tool_calls"].append(
                {"id": "call-2", "name": "search", "arguments": {"query": "second"}}
            )
            client = ScriptedClient([response], final_text="只使用了预算内结果。")

            result = await run_iterative_agent_loop(
                client,
                registry,
                messages=[{"role": "user", "content": "查两次"}],
                tools=TOOLS,
                scope="external_search",
                tool_name_map={"search": "local.search"},
                max_tool_calls=1,
            )

            assert calls == ["local.search"]
            assert result.status == "limited"
            assert "tool_call_budget_exhausted" in result.state.limits
            assistant_call_message = next(
                message for message in result.state.messages if message.get("tool_calls")
            )
            assert len(assistant_call_message["tool_calls"]) == 1

        asyncio.run(scenario())

    def test_exact_tool_budget_finalizes_without_false_exhaustion(self) -> None:
        async def executor(call: ToolCall) -> ToolResult:
            return ToolResult(name=call.name, content="task completed", metadata={"mock": False})

        async def scenario() -> None:
            registry = ToolRegistry()
            registry.register(
                ToolSpec("local.search", "search", permission_scope="external_search"),
                executor,
            )
            client = ScriptedClient([tool_call("call-1")], final_text="任务已经完成。")

            result = await run_iterative_agent_loop(
                client,
                registry,
                messages=[{"role": "user", "content": "只查一次"}],
                tools=TOOLS,
                scope="external_search",
                tool_name_map={"search": "local.search"},
                max_tool_calls=1,
            )

            assert result.status == "completed"
            assert result.final_text == "任务已经完成。"
            assert "tool_call_budget_exhausted" not in result.state.limits
            assert result.state.tool_calls == 1

        asyncio.run(scenario())

    def test_cancellation_propagates_to_model_call(self) -> None:
        started = asyncio.Event()

        class BlockingClient:
            async def acomplete_with_tools(self, messages, tools, **kwargs):
                started.set()
                await asyncio.sleep(30)

            async def acomplete(self, messages, **kwargs):
                return "unused"

        async def scenario() -> None:
            registry = ToolRegistry()
            worker = asyncio.create_task(
                run_iterative_agent_loop(
                    BlockingClient(),
                    registry,
                    messages=[{"role": "user", "content": "wait"}],
                    tools=TOOLS,
                    scope="external_search",
                )
            )
            await asyncio.wait_for(started.wait(), timeout=1)
            worker.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await worker

        asyncio.run(scenario())

    def test_timeout_returns_deterministic_result(self) -> None:
        class BlockingClient:
            async def acomplete_with_tools(self, messages, tools, **kwargs):
                await asyncio.sleep(30)

            async def acomplete(self, messages, **kwargs):
                return "unused"

        async def scenario() -> None:
            result = await run_iterative_agent_loop(
                BlockingClient(),
                ToolRegistry(),
                messages=[{"role": "user", "content": "wait"}],
                tools=TOOLS,
                scope="external_search",
                timeout_seconds=0.01,
            )

            assert result.status == "timeout"
            assert "安全时限" in result.final_text
            assert "agent_loop_timeout" in result.state.limits

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
