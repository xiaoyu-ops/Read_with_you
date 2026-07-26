from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import patch

from backend.tools import build_agent_tool_registry, register_browser_control_tool
from backend.agent.tool_loop import AgentLoopState, run_iterative_agent_loop
from backend.api import routes_agent_chat
from backend.tools.browser_control import _wrapper_path
from backend.llm.models import MCPServerConfig
from backend.tools.mcp import (
    MCPClientError,
    MCPClientSession,
    MCPDiscoveredTool,
    register_mcp_tool_catalog,
)
from backend.tools.registry import ToolRegistry, ToolRegistryError, ToolResult, ToolSpec


class BrowserControlToolTest(unittest.TestCase):
    def test_browser_control_uses_bundled_wrapper_without_codex_home(self) -> None:
        previous = os.environ.pop("PEINIDU_PLAYWRIGHT_CLI", None)
        try:
            self.assertTrue(_wrapper_path().endswith("scripts/playwright_cli.sh"))
        finally:
            if previous is not None:
                os.environ["PEINIDU_PLAYWRIGHT_CLI"] = previous

    def test_browser_control_has_its_own_permission_scope(self) -> None:
        async def scenario() -> None:
            registry = ToolRegistry()
            register_browser_control_tool(registry)
            spec = registry.get("local.browser_control")
            assert spec is not None
            assert spec.permission_scope == "browser_control"
            with self.assertRaises(ToolRegistryError):
                await registry.execute(
                    "local.browser_control",
                    {"action": "snapshot"},
                    permission_scope="external_search",
                )

        asyncio.run(scenario())

    def test_local_browser_control_is_opt_in(self) -> None:
        with patch.dict(os.environ, {"PEINIDU_LOCAL_BROWSER_CONTROL": ""}):
            registry = build_agent_tool_registry()
            assert registry.get("local.browser_control") is None
            mapping = routes_agent_chat._agent_loop_tool_name_map(
                registry, "打开网页并点击", {}
            )
            assert "local_browser_control" not in mapping

        with patch.dict(os.environ, {"PEINIDU_LOCAL_BROWSER_CONTROL": "1"}):
            registry = build_agent_tool_registry()
            spec = registry.get("local.browser_control")
            assert spec is not None
            assert spec.permission_scope == "browser_control"
            mapping = routes_agent_chat._agent_loop_tool_name_map(
                registry, "打开网页并点击", {}
            )
            assert mapping["local_browser_control"] == "local.browser_control"

    def test_browser_control_pauses_then_resumes_the_original_call(self) -> None:
        async def scenario() -> None:
            executed: list[dict] = []

            async def executor(call):
                executed.append(dict(call.arguments))
                return ToolResult(name=call.name, content="浏览器 snapshot 完成")

            registry = ToolRegistry()
            registry.register(
                ToolSpec("local.browser_control", "browser", permission_scope="browser_control"),
                executor,
            )

            class Client:
                def __init__(self):
                    self.round = 0

                async def acomplete_with_tools(self, messages, **kwargs):
                    self.round += 1
                    if self.round == 1:
                        return {"content": "", "tool_calls": [{"id": "browser-1", "name": "browser", "arguments": {"action": "snapshot"}}]}
                    assert messages[-1]["role"] == "tool"
                    return {"content": "已读取浏览器页面。", "tool_calls": []}

                async def acomplete(self, messages, **kwargs):
                    return "限制总结"

            client = Client()
            tools = [{"type": "function", "function": {"name": "browser", "parameters": {"type": "object"}}}]
            paused = await run_iterative_agent_loop(client, registry, messages=[{"role": "user", "content": "打开浏览器"}], tools=tools, tool_name_map={"browser": "local.browser_control"})
            assert paused.status == "waiting_permission"
            assert paused.pending_permission == "browser_control"
            assert executed == []
            resumed = await run_iterative_agent_loop(client, registry, state=paused.state, tools=tools, tool_name_map={"browser": "local.browser_control"}, scope="browser_control")
            assert resumed.status == "completed"
            assert executed == [{"action": "snapshot"}]

        asyncio.run(scenario())

    def test_playwright_mcp_catalog_uses_browser_permission(self) -> None:
        async def scenario() -> None:
            registry = ToolRegistry()
            server = MCPServerConfig(
                name="playwright-official",
                command="npx",
                enabled=True,
                permission_scopes=["browser_control"],
                allowed_tools=["browser_snapshot"],
            )
            with patch(
                "backend.tools.mcp.discover_mcp_tools_cached",
                return_value=(
                    [MCPDiscoveredTool("browser_snapshot", input_schema={"type": "object"})],
                    None,
                ),
            ):
                await register_mcp_tool_catalog(registry, [server])
            spec = registry.get("mcp_playwright_official_browser_snapshot")
            assert spec is not None
            assert spec.permission_scope == "browser_control"
            assert spec.input_schema == {"type": "object"}
            await registry.aclose()

        asyncio.run(scenario())

    def test_unavailable_browser_worker_is_not_replaced_with_mock(self) -> None:
        async def scenario() -> None:
            registry = ToolRegistry()
            server = MCPServerConfig(
                name="playwright-official",
                transport="http",
                url="http://browser:8931/mcp",
                enabled=True,
                permission_scopes=["browser_control"],
            )
            with patch(
                "backend.tools.mcp.discover_mcp_tools_cached",
                return_value=([], "HTTP MCP 请求失败：连接被拒绝"),
            ):
                await register_mcp_tool_catalog(registry, [server])
            result = await registry.execute(
                "mcp_playwright_official_unavailable",
                {},
                permission_scope="browser_control",
            )
            assert result.metadata["mock"] is False
            assert "工具目录发现失败" in result.content
            assert "连接被拒绝" in result.content
            await registry.aclose()

        asyncio.run(scenario())

    def test_only_one_browser_mcp_session_is_active(self) -> None:
        async def scenario() -> None:
            class FakeHTTPClient:
                def __init__(self, server):
                    self.server = server

                async def __aenter__(self):
                    return self

                async def __aexit__(self, exc_type, exc, tb):
                    return None

            server = MCPServerConfig(
                name="playwright-official",
                transport="http",
                url="http://browser:8931/mcp",
                enabled=True,
                timeout_seconds=0.01,
                permission_scopes=["browser_control"],
            )
            first = MCPClientSession()
            second = MCPClientSession()
            with patch("backend.tools.mcp._HTTPMCPClient", FakeHTTPClient):
                await first._client(server)
                with self.assertRaisesRegex(MCPClientError, "另一个 Agent Run"):
                    await second._client(server)
                await first.aclose()
                await second._client(server)
                await second.aclose()

        asyncio.run(scenario())

    def test_unified_loop_cancellation_closes_registry_resources(self) -> None:
        async def scenario() -> None:
            registry = ToolRegistry()
            started = asyncio.Event()
            closed = asyncio.Event()

            async def close_resource() -> None:
                closed.set()

            async def blocking_loop(*args, **kwargs):
                started.set()
                await asyncio.sleep(30)

            registry.add_closer(close_resource)
            state = AgentLoopState(messages=[{"role": "user", "content": "打开浏览器"}])
            no_op = lambda *args, **kwargs: None
            with (
                patch.object(routes_agent_chat, "build_agent_tool_registry", return_value=registry),
                patch.object(routes_agent_chat, "_intent_llm_enabled", return_value=False),
                patch.object(routes_agent_chat, "_register_agent_task_tools", side_effect=no_op),
                patch.object(routes_agent_chat, "_register_memory_tool", side_effect=no_op),
                patch.object(routes_agent_chat, "_register_skill_tools", side_effect=no_op),
                patch.object(routes_agent_chat, "_register_session_search_tool", side_effect=no_op),
                patch.object(routes_agent_chat, "run_iterative_agent_loop", side_effect=blocking_loop),
            ):
                task = asyncio.create_task(
                    routes_agent_chat._execute_unified_agent_loop(
                        "1706.03762",
                        "打开浏览器",
                        {},
                        state=state,
                    )
                )
                await asyncio.wait_for(started.wait(), timeout=1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            assert closed.is_set()

        asyncio.run(scenario())
