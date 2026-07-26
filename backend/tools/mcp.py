"""Minimal MCP client and registry adapter.

Supports the subset Pet needs over stdio and Streamable HTTP: initialize,
notifications/initialized, tools/list and tools/call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import shlex
import socket
import time
import weakref
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from ..llm.models import MCPServerConfig
from .registry import ToolCall, ToolRegistry, ToolResult, ToolSpec

logger = logging.getLogger(__name__)

MCP_PROTOCOL_VERSION = "2025-03-26"
DEFAULT_MCP_TIMEOUT_SECONDS = 12.0
MCP_CATALOG_TTL_SECONDS = 300.0
MCP_HTTP_USER_AGENT = (
    "Mozilla/5.0 (compatible; Peinidu/0.1; +https://readwithyou.xiaoyu666.cyou)"
)
_DISCOVERY_CACHE: dict[str, tuple[float, list["MCPDiscoveredTool"], str | None]] = {}
_BROWSER_SESSION_LOCKS: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, asyncio.Lock
] = weakref.WeakKeyDictionary()


class MCPClientError(RuntimeError):
    pass


def _browser_session_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _BROWSER_SESSION_LOCKS.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _BROWSER_SESSION_LOCKS[loop] = lock
    return lock


def _requires_browser_slot(server: MCPServerConfig) -> bool:
    return "browser_control" in server.permission_scopes


@dataclass(frozen=True)
class MCPDiscoveredTool:
    name: str
    description: str = ""
    input_schema: Mapping[str, Any] | None = None


def register_mcp_servers(
    registry: ToolRegistry,
    servers: list[MCPServerConfig],
) -> None:
    """Register enabled MCP servers as executable registry tools."""

    for server in servers:
        if not server.enabled:
            continue
        for spec in _server_specs(server):
            registry.register(spec, _build_server_executor(server, spec))


def invalidate_mcp_catalog() -> None:
    _DISCOVERY_CACHE.clear()


async def register_mcp_tool_catalog(registry: ToolRegistry, servers: list[MCPServerConfig]) -> None:
    """Expose each enabled MCP tool to the model instead of keyword-routing it."""

    session = MCPClientSession()
    registry.add_closer(session.aclose)
    for server in (item for item in servers if item.enabled):
        tools, error = await discover_mcp_tools_cached(server, session=session)
        scope = (server.permission_scopes or ["mcp_tool"])[0]
        if error:
            name = _catalog_tool_name(server.name, "unavailable")
            if registry.get(name) is not None:
                continue
            registry.register(
                ToolSpec(name=name, description=f"MCP {server.name} 不可用：{error}", permission_scope=scope, source="mcp", server_name=server.name),
                _build_discovery_error_executor(server, error),
            )
            continue
        for tool in tools:
            name = _catalog_tool_name(server.name, tool.name)
            if registry.get(name) is not None:
                continue
            registry.register(
                ToolSpec(name=name, description=tool.description or f"MCP {server.name} 的 {tool.name}", permission_scope=scope, source="mcp", server_name=server.name, input_schema=tool.input_schema),
                _build_discovered_executor(server, tool, session=session),
            )


def _catalog_tool_name(server_name: str, tool_name: str) -> str:
    safe = lambda value: "".join(char if char.isalnum() else "_" for char in value)
    return f"mcp_{safe(server_name)}_{safe(tool_name)}"[:120]


async def discover_mcp_tools_cached(
    server: MCPServerConfig,
    session: "MCPClientSession | None" = None,
) -> tuple[list[MCPDiscoveredTool], str | None]:
    key = json.dumps(server.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
    cached = _DISCOVERY_CACHE.get(key)
    if cached and time.monotonic() - cached[0] < MCP_CATALOG_TTL_SECONDS:
        return cached[1], cached[2]
    try:
        tools = await (session.discover(server) if session else discover_mcp_tools(server))
        error = None
    except Exception as exc:
        tools, error = [], str(exc)
    _DISCOVERY_CACHE[key] = (time.monotonic(), tools, error)
    return tools, error


def _server_specs(server: MCPServerConfig) -> list[ToolSpec]:
    scopes = server.permission_scopes or ["mcp_tool"]
    specs: list[ToolSpec] = []
    for scope in scopes:
        location = server.url or server.command or "未配置 command/url"
        specs.append(
            ToolSpec(
                name=f"mcp:{server.name}:{scope}",
                description=f"MCP server {server.name} ({server.transport}: {location})",
                permission_scope=scope,
                source="mcp",
                server_name=server.name,
            )
        )
    return specs


def _build_server_executor(server: MCPServerConfig, spec: ToolSpec):
    async def execute(call: ToolCall) -> ToolResult:
        query = str(call.arguments.get("query") or "").strip()
        session = MCPClientSession()
        try:
            credential_error = _credential_error_for_server(server)
            if credential_error:
                return _error_result(call, server, credential_error)
            tools = await session.discover(server)
            tool = _choose_tool(server, tools, query)
            if tool is None:
                return _error_result(
                    call,
                    server,
                    "MCP server 没有暴露可调用工具。",
                    discovered_tools=[],
                )
            tool_arguments = _arguments_for_tool(tool, call.arguments)
            result = await session.call(server, tool.name, tool_arguments)
            result_text = _extract_text_content(result)
            evidence = (
                {
                    "kind": "mcp_tool_result",
                    "server_name": server.name,
                    "tool_name": tool.name,
                    "query": query,
                    "arguments": tool_arguments,
                    "result": result,
                },
            )
            return ToolResult(
                name=call.name,
                content=result_text or "MCP 工具执行完成，但没有返回文本内容。",
                evidence=evidence,
                metadata={
                    "mock": False,
                    "source": "mcp",
                    "server_name": server.name,
                    "tool_name": tool.name,
                    "discovered_tools": [item.name for item in tools],
                    "permission_scope": call.permission_scope,
                },
            )
        except Exception as e:
            logger.warning("MCP tool failed for %s: %s", server.name, e)
            return _error_result(call, server, str(e))
        finally:
            await session.aclose()

    return execute


def _build_discovery_error_executor(server: MCPServerConfig, error: str):
    async def execute(call: ToolCall) -> ToolResult:
        return _error_result(call, server, f"工具目录发现失败：{error}", discovered_tools=[])
    return execute


def _build_discovered_executor(
    server: MCPServerConfig,
    tool: MCPDiscoveredTool,
    session: "MCPClientSession | None" = None,
):
    async def execute(call: ToolCall) -> ToolResult:
        try:
            tool_arguments = _arguments_for_tool(tool, call.arguments)
            result = await (
                session.call(server, tool.name, tool_arguments)
                if session
                else call_mcp_tool(server, tool.name, tool_arguments)
            )
            return ToolResult(
                name=call.name,
                content=_extract_text_content(result) or "MCP 工具执行完成，但没有返回文本内容。",
                evidence=({"kind": "mcp_tool_result", "server_name": server.name, "tool_name": tool.name, "arguments": tool_arguments, "result": result},),
                metadata={"mock": False, "source": "mcp", "server_name": server.name, "tool_name": tool.name, "permission_scope": call.permission_scope},
            )
        except Exception as exc:
            return _error_result(call, server, str(exc))
    return execute


def _credential_error_for_server(server: MCPServerConfig) -> str | None:
    command_text = " ".join([server.command, *server.args])
    if "GITHUB_PERSONAL_ACCESS_TOKEN" not in command_text:
        return None
    if os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN"):
        return None
    return (
        "GitHub MCP 需要环境变量 GITHUB_PERSONAL_ACCESS_TOKEN。"
        "请在后端进程环境或 .env 中设置后重启后端；不要把真实 token 写入仓库配置。"
    )


def _choose_tool(
    server: MCPServerConfig,
    tools: list[MCPDiscoveredTool],
    query: str,
) -> MCPDiscoveredTool | None:
    if not tools:
        return None
    configured = (server.tool_name or "").strip()
    if configured:
        for tool in tools:
            if tool.name == configured:
                return tool
    preferred_terms = ("search", "query", "paper", "arxiv", "scholar", "web")
    for tool in tools:
        haystack = f"{tool.name} {tool.description}".casefold()
        if any(term in haystack for term in preferred_terms):
            return tool
    return tools[0]


def _arguments_for_tool(
    tool: MCPDiscoveredTool,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    """Filter project context down to the selected MCP tool schema."""

    raw_query = str(arguments.get("query") or "").strip()
    paper_title = str(arguments.get("paper_title") or "").strip()
    query = _query_for_tool(tool.name, raw_query, paper_title)

    schema = tool.input_schema or {}
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        result = dict(arguments)
        result["query"] = query
        return result

    allowed = set(properties)
    result: dict[str, Any] = {}
    if "query" in allowed:
        result["query"] = query
    if "perPage" in allowed:
        result["perPage"] = 5
    if "minimal_output" in allowed:
        result["minimal_output"] = True
    for key, value in arguments.items():
        if key in allowed and key not in result:
            result[key] = _normalize_playwright_argument(tool.name, key, value)
    return result


def _normalize_playwright_argument(tool_name: str, key: str, value: Any) -> Any:
    """Accept snapshot refs copied as ``[ref=e8]`` or ``ref=e8``."""

    if not tool_name.startswith("browser_") or key != "target" or not isinstance(value, str):
        return value
    target = value.strip()
    if target.startswith("[ref=") and target.endswith("]"):
        return target[5:-1].strip()
    if target.startswith("ref="):
        return target[4:].strip()
    return value


def _query_for_tool(tool_name: str, raw_query: str, paper_title: str) -> str:
    if tool_name == "search_repositories" and paper_title:
        return f"{paper_title} in:name,description,readme"
    return raw_query or paper_title


def _error_result(
    call: ToolCall,
    server: MCPServerConfig,
    error: str,
    discovered_tools: list[str] | None = None,
) -> ToolResult:
    return ToolResult(
        name=call.name,
        content=f"MCP 工具调用失败：{server.name}。原因：{error}",
        evidence=(
            {
                "kind": "mcp_tool_error",
                "server_name": server.name,
                "query": str(call.arguments.get("query") or ""),
                "error": error,
                "discovered_tools": discovered_tools,
            },
        ),
        metadata={
            "mock": False,
            "source": "mcp",
            "server_name": server.name,
            "error": error,
            "permission_scope": call.permission_scope,
        },
    )


class MCPClientSession:
    """Request-scoped MCP clients so stateful tools reuse one transport session."""

    def __init__(self) -> None:
        self._clients: dict[str, Any] = {}
        self._browser_lock: asyncio.Lock | None = None

    @property
    def active_count(self) -> int:
        return len(self._clients)

    async def _client(self, server: MCPServerConfig):
        key = json.dumps(server.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
        existing = self._clients.get(key)
        if existing is not None:
            return existing
        acquired_browser_slot = False
        if _requires_browser_slot(server) and self._browser_lock is None:
            lock = _browser_session_lock()
            try:
                await asyncio.wait_for(
                    lock.acquire(),
                    timeout=server.timeout_seconds or DEFAULT_MCP_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError as exc:
                raise MCPClientError(
                    "浏览器服务正由另一个 Agent Run 使用，请稍后重试。"
                ) from exc
            self._browser_lock = lock
            acquired_browser_slot = True
        client = _HTTPMCPClient(server) if server.transport == "http" else _StdioMCPClient(server)
        try:
            await client.__aenter__()
        except BaseException:
            if acquired_browser_slot and self._browser_lock is not None:
                self._browser_lock.release()
                self._browser_lock = None
            raise
        self._clients[key] = client
        return client

    async def discover(self, server: MCPServerConfig) -> list[MCPDiscoveredTool]:
        client = await self._client(server)
        result = await client.request("tools/list", {})
        return _discovered_tools_from_result(server, result)

    async def call(
        self,
        server: MCPServerConfig,
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        client = await self._client(server)
        return await client.request(
            "tools/call",
            {"name": tool_name, "arguments": dict(arguments)},
        )

    async def aclose(self) -> None:
        clients = list(self._clients.values())
        self._clients.clear()
        for client in reversed(clients):
            with suppress(Exception):
                await client.__aexit__(None, None, None)
        if self._browser_lock is not None:
            self._browser_lock.release()
            self._browser_lock = None


async def discover_mcp_tools(server: MCPServerConfig) -> list[MCPDiscoveredTool]:
    if server.transport == "http":
        async with _HTTPMCPClient(server) as client:
            result = await client.request("tools/list", {})
    else:
        async with _StdioMCPClient(server) as client:
            result = await client.request("tools/list", {})
    return _discovered_tools_from_result(server, result)


def _discovered_tools_from_result(
    server: MCPServerConfig,
    result: Mapping[str, Any],
) -> list[MCPDiscoveredTool]:
    tools = result.get("tools") if isinstance(result, dict) else None
    if not isinstance(tools, list):
        return []
    discovered: list[MCPDiscoveredTool] = []
    for item in tools:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        discovered.append(
            MCPDiscoveredTool(
                name=str(item["name"]),
                description=str(item.get("description") or ""),
                input_schema=item.get("inputSchema") if isinstance(item.get("inputSchema"), dict) else None,
            )
        )
    if not server.allowed_tools:
        return discovered
    allowed = set(server.allowed_tools)
    return [tool for tool in discovered if tool.name in allowed]


async def call_mcp_tool(
    server: MCPServerConfig,
    tool_name: str,
    arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    params = {"name": tool_name, "arguments": dict(arguments)}
    if server.transport == "http":
        async with _HTTPMCPClient(server) as client:
            return await client.request("tools/call", params)
    async with _StdioMCPClient(server) as client:
        return await client.request("tools/call", params)


def _extract_text_content(result: Mapping[str, Any]) -> str:
    content = result.get("content")
    if isinstance(content, list):
        texts = [
            str(item.get("text") or "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        return "\n".join(text for text in texts if text).strip()
    structured = result.get("structuredContent")
    if structured is not None:
        return json.dumps(structured, ensure_ascii=False)
    if result:
        return json.dumps(result, ensure_ascii=False)
    return ""


class _HTTPMCPClient:
    """Short-lived Streamable HTTP session for one MCP operation group."""

    def __init__(self, server: MCPServerConfig) -> None:
        self.server = server
        self.timeout = server.timeout_seconds or DEFAULT_MCP_TIMEOUT_SECONDS
        self.client: httpx.AsyncClient | None = None
        self.session_id: str | None = None
        self._next_id = 1

    async def __aenter__(self) -> "_HTTPMCPClient":
        if not self.server.url:
            raise MCPClientError("HTTP MCP server 缺少 url")
        self.client = httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "User-Agent": MCP_HTTP_USER_AGENT,
            },
        )
        try:
            await self.request(
                "initialize",
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "peinidu", "version": "0.1"},
                },
            )
            await self.notify("notifications/initialized", {})
        except BaseException:
            await self.client.aclose()
            self.client = None
            raise
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        client = self.client
        if client is None:
            return
        if self.session_id and self.server.url:
            with suppress(Exception):
                await client.delete(
                    self.server.url,
                    headers={"Mcp-Session-Id": self.session_id},
                )
        await client.aclose()
        self.client = None

    async def notify(self, method: str, params: Mapping[str, Any]) -> None:
        await self._post(
            {"jsonrpc": "2.0", "method": method, "params": dict(params)}
        )

    async def request(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        response = await self._post(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": dict(params),
            }
        )
        messages = _http_response_messages(response)
        for message in messages:
            if message.get("id") != request_id:
                continue
            if message.get("error"):
                raise MCPClientError(str(message["error"]))
            result = message.get("result")
            if not isinstance(result, dict):
                raise MCPClientError(f"MCP HTTP 响应缺少 result: {message!r}")
            return result
        raise MCPClientError(f"MCP HTTP 响应缺少请求 id={request_id} 的结果")

    async def _post(self, payload: Mapping[str, Any]) -> httpx.Response:
        client = self.client
        if client is None or not self.server.url:
            raise MCPClientError("HTTP MCP client 未启动")
        headers = {"Mcp-Session-Id": self.session_id} if self.session_id else None
        try:
            response = await client.post(self.server.url, json=dict(payload), headers=headers)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text.strip().replace("\n", " ")[:500]
            suffix = f"：{detail}" if detail else ""
            raise MCPClientError(f"HTTP {exc.response.status_code}{suffix}") from exc
        except httpx.HTTPError as exc:
            raise MCPClientError(f"HTTP MCP 请求失败：{exc}") from exc
        session_id = response.headers.get("mcp-session-id")
        if session_id:
            self.session_id = session_id
        return response


def _http_response_messages(response: httpx.Response) -> list[Mapping[str, Any]]:
    content_type = response.headers.get("content-type", "").casefold()
    if "text/event-stream" not in content_type:
        try:
            data = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise MCPClientError("MCP HTTP 响应不是合法 JSON") from exc
        if not isinstance(data, dict):
            raise MCPClientError(f"MCP HTTP 响应必须是对象: {data!r}")
        return [data]

    messages: list[Mapping[str, Any]] = []
    data_lines: list[str] = []
    for line in response.text.splitlines() + [""]:
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
            continue
        if line or not data_lines:
            continue
        raw = "\n".join(data_lines)
        data_lines = []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MCPClientError("MCP SSE data 不是合法 JSON") from exc
        if isinstance(data, dict):
            messages.append(data)
    if not messages:
        raise MCPClientError("MCP SSE 响应没有 message data")
    return messages


class _StdioMCPClient:
    def __init__(self, server: MCPServerConfig) -> None:
        self.server = server
        self.timeout = server.timeout_seconds or DEFAULT_MCP_TIMEOUT_SECONDS
        self.process: asyncio.subprocess.Process | None = None
        self._process_group_id: int | None = None
        self._next_id = 1
        self._framing = "jsonl"

    async def __aenter__(self) -> "_StdioMCPClient":
        await self._start_process()
        try:
            await self._initialize()
        except asyncio.TimeoutError:
            # 旧配置可能仍指向项目早期使用 Content-Length framing 的 server。
            await self._stop_process()
            self._framing = "content-length"
            self._next_id = 1
            try:
                await self._start_process()
                await self._initialize()
            except BaseException:
                await self._stop_process()
                raise
        except BaseException:
            await self._stop_process()
            raise
        return self

    async def _start_process(self) -> None:
        command = _stdio_command(self.server)
        if not command:
            raise MCPClientError("stdio MCP server 缺少 command")
        _remove_stale_playwright_profile_locks(self.server)
        self.process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(Path(__file__).resolve().parents[2]),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        self._process_group_id = self.process.pid

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self._stop_process()

    async def _stop_process(self) -> None:
        process = self.process
        if process is None:
            return
        process_group_id = self._process_group_id
        if process.stdin is not None and not process.stdin.is_closing():
            process.stdin.close()
            with suppress(Exception):
                await process.stdin.wait_closed()
        if process.returncode is None:
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=1)
        if process_group_id is not None and _process_group_exists(process_group_id):
            with suppress(ProcessLookupError):
                os.killpg(process_group_id, signal.SIGTERM)
            await asyncio.sleep(0.25)
        elif process.returncode is None:
            process.terminate()
        if process.returncode is None:
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=1)
        if process_group_id is not None and _process_group_exists(process_group_id):
            with suppress(ProcessLookupError):
                os.killpg(process_group_id, signal.SIGKILL)
        elif process.returncode is None:
            process.kill()
        if process.returncode is None:
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=1)
        self.process = None
        self._process_group_id = None

    async def _initialize(self) -> None:
        await self.request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "peinidu", "version": "0.1"},
            },
        )
        await self.notify("notifications/initialized", {})

    async def notify(self, method: str, params: Mapping[str, Any]) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": dict(params)})

    async def request(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        await self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": dict(params),
            }
        )
        while True:
            message = await asyncio.wait_for(self._read(), timeout=self.timeout)
            if message.get("id") != request_id:
                continue
            if message.get("error"):
                raise MCPClientError(str(message["error"]))
            result = message.get("result")
            if not isinstance(result, dict):
                raise MCPClientError(f"MCP 响应缺少 result: {message!r}")
            return result

    async def _send(self, payload: Mapping[str, Any]) -> None:
        process = self.process
        if process is None or process.stdin is None:
            raise MCPClientError("MCP stdio 进程未启动")
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if self._framing == "content-length":
            process.stdin.write(
                f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
            )
        else:
            process.stdin.write(body + b"\n")
        await process.stdin.drain()

    async def _read(self) -> Mapping[str, Any]:
        process = self.process
        if process is None or process.stdout is None:
            raise MCPClientError("MCP stdio 进程未启动")
        while True:
            line = await process.stdout.readline()
            if not line:
                stderr = await self._read_stderr()
                raise MCPClientError(f"MCP stdio 进程已退出。stderr: {stderr}")
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith(b"{"):
                data = json.loads(stripped.decode("utf-8"))
                if isinstance(data, dict):
                    return data
                continue
            if stripped.lower().startswith(b"content-length:"):
                length = int(stripped.split(b":", 1)[1].strip())
                while True:
                    header = await process.stdout.readline()
                    if header in (b"\r\n", b"\n", b""):
                        break
                body = await process.stdout.readexactly(length)
                data = json.loads(body.decode("utf-8"))
                if isinstance(data, dict):
                    return data

    async def _read_stderr(self) -> str:
        process = self.process
        if process is None or process.stderr is None:
            return ""
        with suppress(Exception):
            data = await asyncio.wait_for(process.stderr.read(), timeout=0.2)
            return data.decode("utf-8", errors="replace").strip()
        return ""


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _remove_stale_playwright_profile_locks(server: MCPServerConfig) -> None:
    command = " ".join([server.command, *server.args]).casefold()
    if "playwright" not in command or "--user-data-dir" not in server.args:
        return
    flag_index = server.args.index("--user-data-dir")
    if flag_index + 1 >= len(server.args):
        return
    profile_dir = Path(server.args[flag_index + 1])
    if not profile_dir.is_absolute():
        profile_dir = Path(__file__).resolve().parents[2] / profile_dir
    lock_path = profile_dir / "SingletonLock"
    if not lock_path.is_symlink():
        return
    try:
        owner, raw_pid = os.readlink(lock_path).rsplit("-", 1)
        lock_pid = int(raw_pid)
    except (OSError, TypeError, ValueError):
        return
    if owner == socket.gethostname():
        try:
            os.kill(lock_pid, 0)
        except ProcessLookupError:
            pass
        except PermissionError:
            return
        else:
            return
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        with suppress(OSError):
            (profile_dir / name).unlink()


def _stdio_command(server: MCPServerConfig) -> list[str]:
    command = server.command.strip()
    if not command:
        return []
    if server.args:
        return [command, *server.args]
    return shlex.split(command)
