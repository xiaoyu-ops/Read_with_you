"""Lightweight tool registry for MCP / external tool execution.

This module defines the stable interface first. Real MCP connection,
tool discovery and execution are wired in later tasks.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from ..llm.models import MCPServerConfig

ToolPermissionScope = Literal["", "mcp_tool", "external_search", "long_task", "memory_write", "browser_control"]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    permission_scope: ToolPermissionScope = "mcp_tool"
    source: Literal["mcp", "local", "mock"] = "local"
    server_name: str | None = None
    input_schema: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    permission_scope: ToolPermissionScope = "mcp_tool"


@dataclass(frozen=True)
class ToolResult:
    name: str
    content: str
    evidence: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    metadata: Mapping[str, Any] = field(default_factory=dict)


ToolExecutor = Callable[[ToolCall], Awaitable[ToolResult]]
ToolCloser = Callable[[], Awaitable[None]]


class ToolRegistryError(RuntimeError):
    pass


class ToolRegistry:
    """Register tools once, then execute them through a uniform interface."""

    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._executors: dict[str, ToolExecutor] = {}
        self._closers: list[ToolCloser] = []

    def register(self, spec: ToolSpec, executor: ToolExecutor) -> None:
        if spec.name in self._specs:
            raise ToolRegistryError(f"工具已注册: {spec.name}")
        self._specs[spec.name] = spec
        self._executors[spec.name] = executor

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def list(self) -> list[ToolSpec]:
        return list(self._specs.values())

    def add_closer(self, closer: ToolCloser) -> None:
        self._closers.append(closer)

    async def aclose(self) -> None:
        while self._closers:
            closer = self._closers.pop()
            try:
                await closer()
            except Exception:
                # 资源清理不能覆盖已经生成的 Agent 结果。
                pass

    async def execute(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        permission_scope: ToolPermissionScope | None = None,
    ) -> ToolResult:
        spec = self._specs.get(name)
        executor = self._executors.get(name)
        if spec is None or executor is None:
            raise ToolRegistryError(f"工具未注册: {name}")
        resolved_scope = permission_scope or spec.permission_scope
        if resolved_scope != spec.permission_scope:
            raise ToolRegistryError(
                f"工具权限不匹配: {name} 需要 {spec.permission_scope}，收到 {resolved_scope}"
            )
        call = ToolCall(
            name=name,
            arguments=arguments or {},
            permission_scope=resolved_scope,
        )
        return await executor(call)


def build_mcp_server_specs(servers: Iterable[MCPServerConfig]) -> list[ToolSpec]:
    specs: list[ToolSpec] = []
    for server in servers:
        if not server.enabled:
            continue
        scope = server.permission_scopes[0] if server.permission_scopes else "mcp_tool"
        location = server.url or server.command or "未配置 command/url"
        specs.append(
            ToolSpec(
                name=f"mcp:{server.name}",
                description=f"MCP server {server.name} ({server.transport}: {location})",
                permission_scope=scope,
                source="mcp",
                server_name=server.name,
            )
        )
    return specs
