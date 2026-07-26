from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import patch

import httpx

from backend.llm.models import MCPServerConfig
from backend.tools.mcp import MCPClientError, _HTTPMCPClient


class HTTPMCPClientTest(unittest.TestCase):
    def _client_patch(self, transport: httpx.MockTransport):
        real_client = httpx.AsyncClient
        return patch(
            "backend.tools.mcp.httpx.AsyncClient",
            side_effect=lambda **kwargs: real_client(transport=transport, **kwargs),
        )

    def test_streamable_http_session_handles_json_and_sse(self) -> None:
        seen: list[tuple[str, str, str | None]] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "DELETE":
                seen.append(("DELETE", "", request.headers.get("mcp-session-id")))
                return httpx.Response(204)
            payload = json.loads(request.content)
            method = payload.get("method", "")
            seen.append((request.method, method, request.headers.get("mcp-session-id")))
            assert "Peinidu" in request.headers["user-agent"]
            assert "text/event-stream" in request.headers["accept"]
            if method == "initialize":
                body = {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "test", "version": "1"},
                    },
                }
                return httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream", "mcp-session-id": "session-1"},
                    text=f"event: message\ndata: {json.dumps(body)}\n\n",
                )
            assert request.headers.get("mcp-session-id") == "session-1"
            if method == "notifications/initialized":
                return httpx.Response(202)
            if method == "tools/list":
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": payload["id"],
                        "result": {
                            "tools": [
                                {
                                    "name": "search_generic_code",
                                    "description": "Search code",
                                    "inputSchema": {"type": "object"},
                                }
                            ]
                        },
                    },
                )
            if method == "tools/call":
                body = {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {"content": [{"type": "text", "text": "result"}]},
                }
                return httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    text=f": keepalive\n\nevent: message\ndata: {json.dumps(body)}\n\n",
                )
            raise AssertionError(method)

        async def scenario() -> None:
            server = MCPServerConfig(
                name="gitmcp-test",
                transport="http",
                url="https://gitmcp.test/owner/repo",
            )
            async with _HTTPMCPClient(server) as client:
                listed = await client.request("tools/list", {})
                assert listed["tools"][0]["name"] == "search_generic_code"
                called = await client.request(
                    "tools/call",
                    {"name": "search_generic_code", "arguments": {"query": "agent loop"}},
                )
                assert called["content"][0]["text"] == "result"

        with self._client_patch(httpx.MockTransport(handler)):
            asyncio.run(scenario())

        assert [item[1] for item in seen] == [
            "initialize",
            "notifications/initialized",
            "tools/list",
            "tools/call",
            "",
        ]
        assert seen[-1] == ("DELETE", "", "session-1")

    def test_malformed_sse_returns_protocol_error(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "DELETE":
                return httpx.Response(204)
            payload = json.loads(request.content)
            if payload.get("method") == "initialize":
                return httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    text='event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{}}\n\n',
                )
            if payload.get("method") == "notifications/initialized":
                return httpx.Response(202)
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text="event: message\ndata: not-json\n\n",
            )

        async def scenario() -> None:
            server = MCPServerConfig(name="broken", transport="http", url="https://mcp.test")
            async with _HTTPMCPClient(server) as client:
                with self.assertRaisesRegex(MCPClientError, "SSE"):
                    await client.request("tools/list", {})

        with self._client_patch(httpx.MockTransport(handler)):
            asyncio.run(scenario())

    def test_http_status_is_actionable(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"detail": "blocked"})

        async def scenario() -> None:
            server = MCPServerConfig(name="blocked", transport="http", url="https://mcp.test")
            with self.assertRaisesRegex(MCPClientError, "HTTP 403"):
                async with _HTTPMCPClient(server):
                    pass

        with self._client_patch(httpx.MockTransport(handler)):
            asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
