import asyncio
import unittest

from backend.agent.tool_loop import choose_initial_tool, run_agent_tool_loop
from backend.tools.registry import ToolCall, ToolRegistry, ToolResult, ToolSpec


class AgentToolLoopTest(unittest.TestCase):
    def test_web_search_continues_with_fetch_steps(self) -> None:
        calls: list[tuple[str, dict]] = []

        async def web_search(call: ToolCall) -> ToolResult:
            calls.append((call.name, dict(call.arguments)))
            return ToolResult(
                name=call.name,
                content="search result",
                evidence=(
                    {"kind": "web_search_result", "title": "Repo", "url": "https://example.com/repo"},
                    {"kind": "web_search_result", "title": "Blog", "url": "https://example.com/blog"},
                ),
                metadata={"mock": False},
            )

        async def web_fetch(call: ToolCall) -> ToolResult:
            calls.append((call.name, dict(call.arguments)))
            return ToolResult(
                name=call.name,
                content=f"fetched {call.arguments['url']}",
                evidence=(
                    {
                        "kind": "web_fetch_result",
                        "url": call.arguments["url"],
                        "title": "Fetched",
                    },
                ),
                metadata={"mock": False},
            )

        async def run() -> None:
            registry = ToolRegistry()
            registry.register(
                ToolSpec("local.web_search", "search", permission_scope="external_search"),
                web_search,
            )
            registry.register(
                ToolSpec("local.web_fetch", "fetch", permission_scope="external_search"),
                web_fetch,
            )
            result = await run_agent_tool_loop(
                registry,
                scope="external_search",
                user_message="帮我网上搜一下复现博客",
                base_arguments={"query": "Visual Attention Network repo"},
                initial_tool="local.web_search",
            )

            assert result.tool_result.name == "local.web_research"
            assert result.tool_result.metadata["tool_sequence"] == [
                "local.web_search",
                "local.web_fetch",
                "local.web_fetch",
            ]
            assert result.trace["sequence"] == [
                "local.web_search",
                "local.web_fetch",
                "local.web_fetch",
            ]
            assert result.trace["events"][0]["type"] == "tool_start"
            assert [name for name, _ in calls] == [
                "local.web_search",
                "local.web_fetch",
                "local.web_fetch",
            ]
            assert calls[1][1]["url"] == "https://example.com/repo"
            assert calls[2][1]["url"] == "https://example.com/blog"

        asyncio.run(run())

    def test_tool_loop_returns_error_result_without_raising(self) -> None:
        async def broken_tool(call: ToolCall) -> ToolResult:
            raise RuntimeError("network down")

        async def run() -> None:
            registry = ToolRegistry()
            registry.register(
                ToolSpec("local.external_search", "search", permission_scope="external_search"),
                broken_tool,
            )
            result = await run_agent_tool_loop(
                registry,
                scope="external_search",
                user_message="查相关论文",
                base_arguments={"query": "semantic deduplication"},
                initial_tool="local.external_search",
            )

            assert result.tool_result.name == "local.external_search"
            assert "network down" in result.tool_result.content
            assert result.trace["steps"][0]["status"] == "error"
            assert result.trace["events"][-1]["type"] == "tool_error"

        asyncio.run(run())

    def test_choose_initial_tool_honors_validated_plan(self) -> None:
        async def noop(call: ToolCall) -> ToolResult:
            return ToolResult(name=call.name, content="ok")

        registry = ToolRegistry()
        registry.register(
            ToolSpec("local.external_search", "search", permission_scope="external_search"),
            noop,
        )
        registry.register(
            ToolSpec("local.web_search", "web", permission_scope="external_search"),
            noop,
        )

        tool = choose_initial_tool(
            registry,
            scope="external_search",
            user_message="帮我查相关论文",
            context={"tool_plan": {"tool_name": "local.external_search"}},
        )

        assert tool == "local.external_search"

    def test_planned_tool_calls_drive_execution_order(self) -> None:
        calls: list[tuple[str, dict]] = []

        async def executor(call: ToolCall) -> ToolResult:
            calls.append((call.name, dict(call.arguments)))
            return ToolResult(
                name=call.name,
                content=f"ok {call.name}",
                evidence=({"kind": "web_fetch_result" if call.name == "local.web_fetch" else "web_search_result"},),
                metadata={"mock": False},
            )

        async def run() -> None:
            registry = ToolRegistry()
            registry.register(ToolSpec("local.web_search", "search", permission_scope="external_search"), executor)
            registry.register(ToolSpec("local.web_fetch", "fetch", permission_scope="external_search"), executor)

            result = await run_agent_tool_loop(
                registry,
                scope="external_search",
                user_message="搜索并读取官网",
                base_arguments={"query": "paper repo"},
                context={
                    "tool_plan": {
                        "tool_calls": [
                            {
                                "tool_name": "local.web_search",
                                "arguments": {"query": "planned query"},
                                "reason": "先搜索",
                            },
                            {
                                "tool_name": "local.web_fetch",
                                "arguments": {"url": "https://example.com"},
                                "reason": "再读取",
                            },
                        ]
                    }
                },
            )

            assert [name for name, _ in calls] == ["local.web_search", "local.web_fetch"]
            assert calls[0][1]["query"] == "planned query"
            assert calls[1][1]["query"] == "paper repo"
            assert calls[1][1]["url"] == "https://example.com"
            assert result.trace["events"][0]["type"] == "tool_plan_accepted"
            assert result.trace["events"][1]["type"] == "tool_plan_accepted"

        asyncio.run(run())

    def test_planned_tool_calls_reject_scope_mismatch_and_fallback(self) -> None:
        calls: list[str] = []

        async def executor(call: ToolCall) -> ToolResult:
            calls.append(call.name)
            return ToolResult(name=call.name, content="ok", metadata={"mock": False})

        async def run() -> None:
            registry = ToolRegistry()
            registry.register(ToolSpec("local.external_search", "search", permission_scope="external_search"), executor)
            registry.register(ToolSpec("mcp:github:mcp_tool", "github", permission_scope="mcp_tool", source="mcp"), executor)
            context = {
                "tool_plan": {
                    "tool_name": "mcp:github:mcp_tool",
                    "tool_calls": [
                        {"tool_name": "mcp:github:mcp_tool", "arguments": {}, "reason": "越权工具"}
                    ],
                }
            }
            initial_tool = choose_initial_tool(
                registry,
                scope="external_search",
                user_message="查相关论文",
                context=context,
            )

            result = await run_agent_tool_loop(
                registry,
                scope="external_search",
                user_message="查相关论文",
                base_arguments={"query": "semantic deduplication"},
                context=context,
                initial_tool=initial_tool,
            )

            assert initial_tool == "local.external_search"
            assert calls == ["local.external_search"]
            assert result.tool_result.name == "local.external_search"
            assert result.trace["events"][0]["type"] == "tool_plan_rejected"
            assert "planned_tool_calls_rejected" in result.limits

        asyncio.run(run())

    def test_planned_tool_calls_respect_step_budget(self) -> None:
        calls: list[str] = []

        async def executor(call: ToolCall) -> ToolResult:
            calls.append(call.name)
            return ToolResult(name=call.name, content="ok", metadata={"mock": False})

        async def run() -> None:
            registry = ToolRegistry()
            registry.register(ToolSpec("local.web_fetch", "fetch", permission_scope="external_search"), executor)

            result = await run_agent_tool_loop(
                registry,
                scope="external_search",
                user_message="读取多个链接",
                base_arguments={"query": "links"},
                context={
                    "tool_plan": {
                        "tool_calls": [
                            {"tool_name": "local.web_fetch", "arguments": {"url": "https://example.com/1"}},
                            {"tool_name": "local.web_fetch", "arguments": {"url": "https://example.com/2"}},
                            {"tool_name": "local.web_fetch", "arguments": {"url": "https://example.com/3"}},
                        ]
                    }
                },
                max_steps=2,
            )

            assert calls == ["local.web_fetch", "local.web_fetch"]
            assert result.trace["sequence"] == ["local.web_fetch", "local.web_fetch"]
            assert "planned_tool_step_budget_exhausted" in result.limits

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
