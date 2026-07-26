"""Local mock tools for validating MCP tool-call wiring."""

from __future__ import annotations

from .registry import ToolCall, ToolRegistry, ToolResult, ToolSpec


async def mock_mcp_tool(call: ToolCall) -> ToolResult:
    query = str(call.arguments.get("query") or "").strip()
    arxiv_id = str(call.arguments.get("arxiv_id") or "").strip()
    return ToolResult(
        name=call.name,
        content=(
            "本地 mock MCP 工具结果：尚未连接真实 MCP server。"
            f"已记录工具请求：{query or '无'}；论文：{arxiv_id or '未知'}。"
        ),
        evidence=(
            {
                "kind": "mock_mcp_tool",
                "query": query,
                "arxiv_id": arxiv_id,
                "note": "占位结果，用于验证 MCP 工具调用接口。",
            },
        ),
        metadata={"mock": True, "permission_scope": call.permission_scope},
    )


def build_mock_tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="mock.mcp_tool",
            description="本地 mock MCP 工具，不连接真实 server，只返回占位证据。",
            permission_scope="mcp_tool",
            source="mock",
        ),
        mock_mcp_tool,
    )
    return registry
