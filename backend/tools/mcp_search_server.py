"""Local paper-search MCP server.

Run with:
    python -m backend.tools.mcp_search_server
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from ..retrieval.arxiv import search_arxiv
from ..retrieval.match import merge_and_rank
from ..retrieval.semantic_scholar import search_s2_combined

PROTOCOL_VERSION = "2024-11-05"
SEARCH_TIMEOUT_SECONDS = 8.0


def main() -> None:
    while True:
        message = _read_message()
        if message is None:
            break
        if "id" not in message:
            continue
        response = _handle_request(message)
        _write_message(response)


def _handle_request(message: dict[str, Any]) -> dict[str, Any]:
    request_id = message.get("id")
    method = str(message.get("method") or "")
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    try:
        if method == "initialize":
            result = {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "peinidu-paper-search", "version": "0.1"},
            }
        elif method == "tools/list":
            result = {"tools": [_paper_search_tool()]}
        elif method == "tools/call":
            result = asyncio.run(_call_tool(params))
        else:
            raise ValueError(f"unsupported method: {method}")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    except Exception as e:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32000, "message": str(e)},
        }


def _paper_search_tool() -> dict[str, Any]:
    return {
        "name": "paper_search",
        "description": "Search papers through arXiv and Semantic Scholar.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query."},
                "paper_title": {"type": "string", "description": "Current paper title."},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            "required": ["query"],
        },
    }


async def _call_tool(params: dict[str, Any]) -> dict[str, Any]:
    name = str(params.get("name") or "")
    if name != "paper_search":
        raise ValueError(f"unknown tool: {name}")
    arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
    query = str(arguments.get("query") or arguments.get("paper_title") or "").strip()
    if not query:
        raise ValueError("query 不能为空")
    max_results = _coerce_max_results(arguments.get("max_results"))
    arxiv_task = asyncio.create_task(_safe_arxiv(query, max_results))
    s2_task = asyncio.create_task(_safe_s2(query, max_results))
    arxiv_results, s2_results = await asyncio.gather(arxiv_task, s2_task)
    ranked = merge_and_rank(query, arxiv_results, s2_results)
    payload = {
        "query": query,
        "count": len(ranked),
        "candidates": [candidate.to_dict() for candidate in ranked[:max_results]],
    }
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(payload, ensure_ascii=False, indent=2),
            }
        ]
    }


async def _safe_arxiv(query: str, max_results: int):
    try:
        return await search_arxiv(query, max_results=max_results, timeout=SEARCH_TIMEOUT_SECONDS)
    except Exception:
        return []


async def _safe_s2(query: str, max_results: int):
    try:
        return await search_s2_combined(
            query,
            max_results=max_results,
            timeout=SEARCH_TIMEOUT_SECONDS,
        )
    except Exception:
        return []


def _coerce_max_results(value: Any) -> int:
    try:
        return max(1, min(int(value), 10))
    except (TypeError, ValueError):
        return 5


def _read_message() -> dict[str, Any] | None:
    length: int | None = None
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        stripped = line.strip()
        if not stripped:
            if length is not None:
                break
            continue
        if stripped.startswith(b"{"):
            data = json.loads(stripped.decode("utf-8"))
            return data if isinstance(data, dict) else None
        if stripped.lower().startswith(b"content-length:"):
            length = int(stripped.split(b":", 1)[1].strip())
    if length is None:
        return None
    body = sys.stdin.buffer.read(length)
    data = json.loads(body.decode("utf-8"))
    return data if isinstance(data, dict) else None


def _write_message(message: dict[str, Any]) -> None:
    body = json.dumps(message, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(body + b"\n")
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
