"""MCP 测试连接接口（POST /config/mcp/test）单测。

成功路径用仓库内置的 stdio MCP server（backend.tools.mcp_search_server）：
initialize + tools/list 全程本地，不触网、不执行 tools/call。
"""

from __future__ import annotations

import asyncio
import sys
import unittest

from backend.api import routes_config
from backend.llm.models import MCPServerConfig


class MCPTestConnectionRouteTest(unittest.TestCase):
    def test_local_search_server_connects_and_lists_tools(self) -> None:
        server = MCPServerConfig(
            name="local-paper-search",
            transport="stdio",
            command=sys.executable,
            args=["-m", "backend.tools.mcp_search_server"],
            tool_name="paper_search",
        )
        response = asyncio.run(routes_config.test_mcp_server(server))
        assert response.ok, response.error
        assert any(tool.name == "paper_search" for tool in response.tools)
        assert response.chosen_tool == "paper_search"
        assert response.note == ""
        assert response.elapsed_ms >= 0

    def test_configured_tool_name_missing_returns_note(self) -> None:
        server = MCPServerConfig(
            name="local-paper-search",
            transport="stdio",
            command=sys.executable,
            args=["-m", "backend.tools.mcp_search_server"],
            tool_name="not_a_real_tool",
        )
        response = asyncio.run(routes_config.test_mcp_server(server))
        assert response.ok
        assert "not_a_real_tool" in response.note
        assert "paper_search" in response.note  # 自动选择的兜底工具

    def test_missing_command_reports_friendly_error(self) -> None:
        server = MCPServerConfig(
            name="broken",
            transport="stdio",
            command="definitely-not-a-real-binary-peinidu",
        )
        response = asyncio.run(routes_config.test_mcp_server(server))
        assert not response.ok
        assert response.error
        assert not response.tools

    def test_http_without_url_reports_protocol_error(self) -> None:
        server = MCPServerConfig(name="http-broken", transport="http", url=None)
        response = asyncio.run(routes_config.test_mcp_server(server))
        assert not response.ok
        assert "url" in response.error.lower() or "URL" in response.error

    def test_github_token_precheck_blocks_before_spawn(self) -> None:
        from unittest.mock import patch
        import os

        server = MCPServerConfig(
            name="github-official",
            transport="stdio",
            command="docker",
            args=["run", "-i", "--rm", "-e", "GITHUB_PERSONAL_ACCESS_TOKEN", "ghcr.io/github/github-mcp-server"],
        )
        env = {k: v for k, v in os.environ.items() if k != "GITHUB_PERSONAL_ACCESS_TOKEN"}
        with patch.dict(os.environ, env, clear=True):
            response = asyncio.run(routes_config.test_mcp_server(server))
        assert not response.ok
        assert "GITHUB_PERSONAL_ACCESS_TOKEN" in response.error


if __name__ == "__main__":
    unittest.main()
