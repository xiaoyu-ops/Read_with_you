"""Tool registry primitives for Pet / MCP integration."""

import os

from .external_search import register_external_search_tool
from .mcp import register_mcp_servers
from .web_search import register_web_search_tool
from .browser_control import register_browser_control_tool
from .registry import (
    ToolCall,
    ToolExecutor,
    ToolRegistry,
    ToolRegistryError,
    ToolResult,
    ToolSpec,
    build_mcp_server_specs,
)
from .mock import build_mock_tool_registry


def local_browser_control_enabled() -> bool:
    """Keep the legacy local CLI bridge opt-in for non-container development."""

    return os.environ.get("PEINIDU_LOCAL_BROWSER_CONTROL", "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }


def build_agent_tool_registry() -> ToolRegistry:
    """Tools available to Agent chat after permission confirmation."""
    from ..llm.config import get_config

    registry = build_mock_tool_registry()
    register_external_search_tool(registry)
    register_web_search_tool(registry)
    if local_browser_control_enabled():
        register_browser_control_tool(registry)
    register_mcp_servers(registry, get_config().mcp_servers)
    return registry


__all__ = [
    "ToolCall",
    "ToolExecutor",
    "ToolRegistry",
    "ToolRegistryError",
    "ToolResult",
    "ToolSpec",
    "build_agent_tool_registry",
    "build_mock_tool_registry",
    "build_mcp_server_specs",
    "local_browser_control_enabled",
    "register_external_search_tool",
    "register_mcp_servers",
    "register_web_search_tool",
    "register_browser_control_tool",
]
