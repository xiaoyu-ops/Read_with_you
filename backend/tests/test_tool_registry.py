from __future__ import annotations

import asyncio
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.llm.config import load_config, reset_config, save_config
from backend.llm.models import MCPServerConfig
from backend.retrieval.arxiv import PaperCandidate
from backend.tools import build_agent_tool_registry, build_mock_tool_registry
from backend.api import routes_agent_chat
from backend.tools.mcp import MCPDiscoveredTool, _arguments_for_tool, register_mcp_servers
from backend.tools.web_search import register_web_search_tool
from backend.tools.registry import (
    ToolCall,
    ToolRegistry,
    ToolRegistryError,
    ToolResult,
    ToolSpec,
    build_mcp_server_specs,
)


class ToolRegistryTest(unittest.TestCase):
    def tearDown(self) -> None:
        reset_config()

    def test_mcp_server_config_loads_and_saves(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.yaml"
            config_path.write_text(
                """
mcp_servers:
  - name: search
    transport: http
    url: http://127.0.0.1:9000/mcp
    enabled: true
    permission_scopes: [external_search]
""".strip(),
                encoding="utf-8",
            )

            config = load_config(config_path)
            assert len(config.mcp_servers) == 1
            assert config.mcp_servers[0].name == "search"
            assert config.mcp_servers[0].permission_scopes == ["external_search"]
            assert config.mcp_servers[0].allowed_tools == []

            save_config(config, config_path)
            reloaded = load_config(config_path)
            assert reloaded.mcp_servers[0].url == "http://127.0.0.1:9000/mcp"
            assert reloaded.mcp_servers[0].enabled is True

    def test_browser_mcp_scope_and_allowed_tools_are_compatible(self) -> None:
        server = MCPServerConfig.model_validate(
            {
                "name": "playwright",
                "command": "npx",
                "permission_scopes": ["browser_control"],
                "allowed_tools": ["browser_navigate", "browser_snapshot"],
            }
        )
        assert server.permission_scopes == ["browser_control"]
        assert server.allowed_tools == ["browser_navigate", "browser_snapshot"]

    def test_build_mcp_server_specs_uses_enabled_servers_only(self) -> None:
        specs = build_mcp_server_specs(
            [
                MCPServerConfig(
                    name="search",
                    transport="http",
                    url="http://127.0.0.1:9000/mcp",
                    enabled=True,
                    permission_scopes=["external_search"],
                ),
                MCPServerConfig(
                    name="disabled",
                    command="python disabled.py",
                    enabled=False,
                ),
            ]
        )

        assert len(specs) == 1
        assert specs[0].name == "mcp:search"
        assert specs[0].source == "mcp"
        assert specs[0].permission_scope == "external_search"
        assert "http://127.0.0.1:9000/mcp" in specs[0].description

    def test_tool_registry_executes_registered_executor(self) -> None:
        async def fake_executor(call: ToolCall) -> ToolResult:
            assert call.name == "mock.search"
            assert call.arguments["query"] == "paper code"
            assert call.permission_scope == "external_search"
            return ToolResult(
                name=call.name,
                content="found repository",
                evidence=({"url": "https://example.com/repo"},),
            )

        async def run() -> None:
            registry = ToolRegistry()
            registry.register(
                ToolSpec(
                    name="mock.search",
                    description="Mock search",
                    permission_scope="external_search",
                    source="mock",
                ),
                fake_executor,
            )
            result = await registry.execute("mock.search", {"query": "paper code"})
            assert result.content == "found repository"
            assert result.evidence[0]["url"] == "https://example.com/repo"

            with self.assertRaisesRegex(ToolRegistryError, "工具权限不匹配"):
                await registry.execute(
                    "mock.search",
                    {"query": "paper code"},
                    permission_scope="mcp_tool",
                )

            with self.assertRaises(ToolRegistryError):
                registry.register(
                    ToolSpec(name="mock.search", description="Duplicate"),
                    fake_executor,
                )
            with self.assertRaises(ToolRegistryError):
                await registry.execute("missing")

        asyncio.run(run())

    def test_mock_tool_registry_only_keeps_mcp_placeholder(self) -> None:
        async def run() -> None:
            registry = build_mock_tool_registry()
            assert registry.get("mock.external_search") is None
            assert registry.get("mock.mcp_tool") is not None

            result = await registry.execute(
                "mock.mcp_tool",
                {"query": "paper code", "arxiv_id": "1706.03762"},
            )
            assert "本地 mock MCP 工具结果" in result.content
            assert result.evidence[0]["kind"] == "mock_mcp_tool"
            assert result.metadata["mock"] is True

        asyncio.run(run())

    def test_agent_tool_registry_runs_real_external_search_tool(self) -> None:
        async def fake_arxiv(query: str, max_results: int = 10, timeout: float = 20.0):
            assert query == "Attention Is All You Need"
            return [
                PaperCandidate(
                    arxiv_id="1706.03762",
                    title="Attention Is All You Need",
                    authors=["A. Vaswani"],
                    abstract="Transformer paper.",
                    year="2017",
                    citation_count=None,
                )
            ]

        async def fake_s2(query: str, max_results: int = 10, timeout: float = 20.0):
            assert query == "Attention Is All You Need"
            return []

        async def run() -> None:
            registry = build_agent_tool_registry()
            assert registry.get("local.external_search") is not None
            assert registry.get("mock.mcp_tool") is not None

            result = await registry.execute(
                "local.external_search",
                {
                    "query": "查一下这篇论文",
                    "paper_title": "Attention Is All You Need",
                    "arxiv_id": "1706.03762",
                },
                permission_scope="external_search",
            )
            assert "真实外部检索结果" in result.content
            assert "本地 mock" not in result.content
            assert result.evidence[0]["kind"] == "external_paper_search_result"
            assert result.evidence[0]["arxiv_id"] == "1706.03762"
            assert result.metadata["mock"] is False

        with (
            patch("backend.tools.external_search.search_arxiv", fake_arxiv),
            patch("backend.tools.external_search.search_s2_combined", fake_s2),
        ):
            asyncio.run(run())

    def test_related_paper_search_filters_current_paper(self) -> None:
        async def fake_arxiv(query: str, max_results: int = 10, timeout: float = 20.0):
            assert query == "data efficient learning web scale semantic deduplication"
            return [
                PaperCandidate(
                    arxiv_id="2303.09540",
                    title="SemDeDup: Data-efficient learning at web-scale through semantic deduplication",
                    authors=["Amro Abbas"],
                    abstract="Current paper.",
                    year="2023",
                ),
                PaperCandidate(
                    arxiv_id="2401.00001",
                    title="Data Deduplication for Efficient Training",
                    authors=["Ada Reader"],
                    abstract="Related data deduplication work.",
                    year="2024",
                    citation_count=12,
                ),
            ]

        async def fake_s2(query: str, max_results: int = 10, timeout: float = 20.0):
            assert query == "data efficient learning web scale semantic deduplication"
            return [
                PaperCandidate(
                    arxiv_id="",
                    paper_id="s2-current",
                    title="SemDeDup: Data-efficient learning at web-scale through semantic deduplication",
                    authors=["Amro Abbas"],
                    abstract="Current paper from S2.",
                    year="2023",
                ),
                PaperCandidate(
                    arxiv_id="",
                    paper_id="s2-related",
                    title="Learning with Dataset Deduplication",
                    authors=["Bo Builder"],
                    abstract="Another related work.",
                    year="2022",
                    citation_count=34,
                ),
            ]

        async def run() -> None:
            registry = build_agent_tool_registry()
            result = await registry.execute(
                "local.external_search",
                {
                    "query": "帮我查一下和这个文章相似的文章有哪些可以做到吗",
                    "query_mode": "related_papers",
                    "search_query": "data efficient learning web scale semantic deduplication",
                    "paper_title": "SemDeDup: Data-efficient learning at web-scale through semantic deduplication",
                    "exclude_title": "SemDeDup: Data-efficient learning at web-scale through semantic deduplication",
                    "arxiv_id": "2303.09540",
                    "exclude_arxiv_id": "2303.09540",
                },
                permission_scope="external_search",
            )

            titles = [item["title"] for item in result.evidence if item["kind"] == "external_paper_search_result"]
            assert "SemDeDup: Data-efficient learning at web-scale through semantic deduplication" not in titles
            assert "Data Deduplication for Efficient Training" in titles
            assert "Learning with Dataset Deduplication" in titles
            assert result.metadata["query_mode"] == "related_papers"
            assert result.metadata["search_query"] == "data efficient learning web scale semantic deduplication"
            assert result.metadata["excluded_current_paper_count"] >= 1

        with (
            patch("backend.tools.external_search.search_arxiv", fake_arxiv),
            patch("backend.tools.external_search.search_s2_combined", fake_s2),
        ):
            asyncio.run(run())

    def test_agent_tool_registry_executes_stdio_mcp_server(self) -> None:
        server_code = r'''
import json
import sys

def read_message():
    length = None
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        stripped = line.strip()
        if not stripped:
            if length is not None:
                break
            continue
        if stripped.lower().startswith(b"content-length:"):
            length = int(stripped.split(b":", 1)[1].strip())
    return json.loads(sys.stdin.buffer.read(length).decode("utf-8"))

def write_message(message):
    body = json.dumps(message).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
    sys.stdout.buffer.flush()

while True:
    message = read_message()
    if message is None:
        break
    if "id" not in message:
        continue
    method = message.get("method")
    if method == "initialize":
        result = {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "test-search", "version": "0.1"},
        }
    elif method == "tools/list":
        result = {
            "tools": [
                {
                    "name": "paper_search",
                    "description": "Search papers.",
                    "inputSchema": {"type": "object"},
                }
            ]
        }
    elif method == "tools/call":
        query = message.get("params", {}).get("arguments", {}).get("query", "")
        result = {"content": [{"type": "text", "text": f"mcp result for {query}"}]}
    else:
        result = {}
    write_message({"jsonrpc": "2.0", "id": message["id"], "result": result})
'''

        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "server.py"
            script.write_text(textwrap.dedent(server_code), encoding="utf-8")
            config_path = Path(tmp) / "config.yaml"
            config_path.write_text(
                f"""
mcp_servers:
  - name: search
    transport: stdio
    command: {sys.executable}
    args: [{script}]
    enabled: true
    tool_name: paper_search
    timeout_seconds: 3
    permission_scopes: [mcp_tool]
""".strip(),
                encoding="utf-8",
            )
            load_config(config_path)

            async def run() -> None:
                registry = build_agent_tool_registry()
                tool_name = routes_agent_chat._tool_name_for_scope("mcp_tool", registry)
                assert tool_name == "mcp:search:mcp_tool"
                result = await registry.execute(
                    tool_name,
                    {"query": "visual attention network"},
                    permission_scope="mcp_tool",
                )
                assert "mcp result for visual attention network" in result.content
                assert result.metadata["mock"] is False
                assert result.metadata["source"] == "mcp"
                assert result.evidence[0]["kind"] == "mcp_tool_result"

            asyncio.run(run())

    def test_github_search_repository_arguments_are_schema_filtered(self) -> None:
        args = _arguments_for_tool(
            MCPDiscoveredTool(
                name="search_repositories",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "perPage": {"type": "number"},
                        "minimal_output": {"type": "boolean"},
                    },
                },
            ),
            {
                "query": "帮我找这篇论文的复现代码",
                "paper_title": "Visual Attention Network",
                "arxiv_id": "2202.09741",
                "paper_metadata": {"source": "ar5iv"},
            },
        )

        assert args == {
            "query": "Visual Attention Network in:name,description,readme",
            "perPage": 5,
            "minimal_output": True,
        }

    def test_playwright_snapshot_ref_is_normalized_for_click(self) -> None:
        args = _arguments_for_tool(
            MCPDiscoveredTool(
                name="browser_click",
                input_schema={
                    "type": "object",
                    "properties": {
                        "target": {"type": "string"},
                        "element": {"type": "string"},
                    },
                    "required": ["target"],
                },
            ),
            {
                "target": "[ref=e8]",
                "element": "文献库链接",
                "query": "不应传给浏览器工具",
            },
        )

        assert args == {"target": "e8", "element": "文献库链接"}

    def test_github_mcp_missing_pat_returns_actionable_error(self) -> None:
        async def run() -> None:
            registry = ToolRegistry()
            register_mcp_servers(
                registry,
                [
                    MCPServerConfig(
                        name="github-official",
                        command="docker",
                        args=[
                            "run",
                            "-i",
                            "--rm",
                            "-e",
                            "GITHUB_PERSONAL_ACCESS_TOKEN",
                            "ghcr.io/github/github-mcp-server",
                        ],
                        enabled=True,
                        tool_name="search_repositories",
                        permission_scopes=["mcp_tool"],
                    )
                ],
            )

            with (
                patch.dict("os.environ", {}, clear=True),
                patch(
                    "backend.tools.mcp.discover_mcp_tools",
                    side_effect=AssertionError("credential preflight should stop before discovery"),
                ),
            ):
                result = await registry.execute(
                    "mcp:github-official:mcp_tool",
                    {
                        "query": "找复现代码",
                        "paper_title": "Visual Attention Network",
                    },
                    permission_scope="mcp_tool",
                )

            assert "GITHUB_PERSONAL_ACCESS_TOKEN" in result.content
            assert "重启后端" in result.content
            assert result.metadata["mock"] is False
            assert result.metadata["source"] == "mcp"
            assert result.evidence[0]["kind"] == "mcp_tool_error"

        asyncio.run(run())

    def test_web_search_tool_returns_structured_results(self) -> None:
        html = """
<html><body>
  <div class="result">
    <a class="result__a" href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpaper-code">Example code</a>
    <a class="result__snippet">Official reproduction repository.</a>
  </div>
  <div class="result">
    <a class="result__a" href="https://example.org/blog">Blog writeup</a>
    <a class="result__snippet">A readable implementation note.</a>
  </div>
</body></html>
"""

        class FakeResponse:
            text = html

            def raise_for_status(self) -> None:
                return None

        class FakeClient:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def get(self, url, params):
                assert params["q"] == "Visual Attention Network 找复现代码"
                return FakeResponse()

        async def run() -> None:
            registry = ToolRegistry()
            register_web_search_tool(registry)
            with patch("backend.tools.web_search.httpx.AsyncClient", FakeClient):
                result = await registry.execute(
                    "local.web_search",
                    {
                        "query": "找复现代码",
                        "paper_title": "Visual Attention Network",
                    },
                    permission_scope="external_search",
                )

            assert "通用网页搜索结果" in result.content
            assert result.metadata["mock"] is False
            assert result.metadata["provider"] == "duckduckgo_html"
            assert result.evidence[0]["kind"] == "web_search_result"
            assert result.evidence[0]["url"] == "https://example.com/paper-code"
            assert result.evidence[0]["source"] == "example.com"

        asyncio.run(run())

    def test_web_fetch_tool_extracts_page_content(self) -> None:
        html = """
<html>
  <head>
    <title>Example Article</title>
    <meta name="description" content="Short page description.">
  </head>
  <body>
    <nav>Navigation noise</nav>
    <main>
      <h1>Example Article</h1>
      <p>This page explains the implementation details.</p>
      <p>It includes code links and benchmark notes.</p>
    </main>
  </body>
</html>
"""

        class FakeResponse:
            text = html
            url = "https://example.com/article"

            def raise_for_status(self) -> None:
                return None

        class FakeClient:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def get(self, url, *args, **kwargs):
                assert url == "https://example.com/article"
                return FakeResponse()

        async def run() -> None:
            registry = ToolRegistry()
            register_web_search_tool(registry)
            with patch("backend.tools.web_search.httpx.AsyncClient", FakeClient):
                result = await registry.execute(
                    "local.web_fetch",
                    {"query": "总结这个网页 https://example.com/article"},
                    permission_scope="external_search",
                )

            assert "网页读取结果" in result.content
            assert "Example Article" in result.content
            assert "Navigation noise" not in result.content
            assert result.metadata["provider"] == "http_fetch"
            assert result.evidence[0]["kind"] == "web_fetch_result"
            assert result.evidence[0]["url"] == "https://example.com/article"
            assert "implementation details" in result.evidence[0]["text_excerpt"]

        asyncio.run(run())

    def test_external_search_tool_returns_author_metric_evidence(self) -> None:
        async def fake_arxiv(query: str, max_results: int = 10, timeout: float = 20.0):
            return []

        async def fake_s2(query: str, max_results: int = 10, timeout: float = 20.0):
            assert query == "Visual Attention Network"
            return [
                PaperCandidate(
                    arxiv_id="2202.09741",
                    title="Visual Attention Network",
                    authors=["Meng-Hao Guo", "Cheng-Ze Lu"],
                    abstract="Attention for vision.",
                    year="2022",
                    citation_count=1234,
                    venue="Computational Visual Media",
                    paper_id="s2-paper-id",
                )
            ]

        async def fake_authors(query: str, max_results: int = 3, timeout: float = 8.0):
            assert query == "Meng-Hao Guo"
            return [
                {
                    "author_id": "author-1",
                    "name": "Meng-Hao Guo",
                    "url": "https://www.semanticscholar.org/author/author-1",
                    "affiliations": ["Peking University"],
                    "paper_count": 42,
                    "citation_count": 5678,
                    "h_index": 31,
                }
            ]

        async def run() -> None:
            registry = build_agent_tool_registry()
            result = await registry.execute(
                "local.external_search",
                {
                    "query": "哪个机构做的一作作者的引用数多少",
                    "paper_title": "Visual Attention Network",
                    "paper_authors": ["Meng-Hao Guo", "Cheng-Ze Lu"],
                    "arxiv_id": "2202.09741",
                    "lookup_targets": ["papers", "authors", "citation_metrics"],
                    "author_scope": "first_author",
                },
                permission_scope="external_search",
            )

            assert "真实外部检索结果" in result.content
            assert "作者信息（Semantic Scholar）" in result.content
            assert "Peking University" in result.content
            assert "5678 citations" in result.content
            assert result.metadata["semantic_scholar_author_result_count"] == 1
            author_evidence = [
                item for item in result.evidence if item["kind"] == "semantic_scholar_author_result"
            ]
            assert author_evidence
            assert author_evidence[0]["queried_author"] == "Meng-Hao Guo"
            assert author_evidence[0]["citation_count"] == 5678
            assert author_evidence[0]["affiliations"] == ["Peking University"]
            assert result.metadata["lookup_targets"] == ["authors", "citation_metrics", "papers"]

            no_author_result = await registry.execute(
                "local.external_search",
                {
                    "query": "作者机构和引用数",
                    "paper_title": "Visual Attention Network",
                    "paper_authors": ["Meng-Hao Guo", "Cheng-Ze Lu"],
                    "arxiv_id": "2202.09741",
                    "lookup_targets": ["papers"],
                    "author_scope": "none",
                },
                permission_scope="external_search",
            )
            assert no_author_result.metadata["semantic_scholar_author_result_count"] == 0

        with (
            patch("backend.tools.external_search.search_arxiv", fake_arxiv),
            patch("backend.tools.external_search.search_s2_combined", fake_s2),
            patch("backend.tools.external_search.search_semantic_scholar_authors", fake_authors),
        ):
            asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
