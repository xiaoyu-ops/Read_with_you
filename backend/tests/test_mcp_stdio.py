from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from backend.llm.models import MCPServerConfig
from backend.tools.mcp import (
    MCPClientError,
    MCPClientSession,
    _remove_stale_playwright_profile_locks,
)


SERVER_CODE = textwrap.dedent(
    r"""
    import json
    import os
    import sys

    counter = 0
    for line in sys.stdin:
        message = json.loads(line)
        if "id" not in message:
            continue
        method = message.get("method")
        if method == "initialize":
            result = {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "stateful-test", "version": "1"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {"name": "browser_navigate", "inputSchema": {"type": "object"}},
                    {"name": "browser_snapshot", "inputSchema": {"type": "object"}},
                    {"name": "browser_evaluate", "inputSchema": {"type": "object"}},
                ]
            }
        elif method == "tools/call":
            counter += 1
            result = {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({"pid": os.getpid(), "counter": counter}),
                    }
                ]
            }
        else:
            result = {}
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
    """
)

EXIT_SERVER_CODE = textwrap.dedent(
    r"""
    import json
    import sys

    for line in sys.stdin:
        message = json.loads(line)
        if "id" not in message:
            continue
        if message.get("method") == "tools/call":
            sys.exit(7)
        result = {
            "protocolVersion": "2025-03-26",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "exit-test", "version": "1"},
        } if message.get("method") == "initialize" else {
            "tools": [{"name": "browser_navigate", "inputSchema": {"type": "object"}}]
        }
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
    """
)

CHILD_SERVER_CODE = textwrap.dedent(
    r"""
    import json
    import subprocess
    import sys

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    for line in sys.stdin:
        message = json.loads(line)
        if "id" not in message:
            continue
        method = message.get("method")
        if method == "initialize":
            result = {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "child-test", "version": "1"},
            }
        elif method == "tools/list":
            result = {"tools": [{"name": "child_pid", "inputSchema": {"type": "object"}}]}
        else:
            result = {"content": [{"type": "text", "text": str(child.pid)}]}
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
    """
)


class StatefulStdioMCPTest(unittest.TestCase):
    def test_stale_playwright_profile_locks_are_removed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp)
            for name, target in (
                ("SingletonLock", "old-container-999999"),
                ("SingletonCookie", "cookie"),
                ("SingletonSocket", "/tmp/stale-socket"),
            ):
                (profile / name).symlink_to(target)
            server = MCPServerConfig(
                name="playwright-test",
                command="npx",
                args=["--yes", "@playwright/mcp@latest", "--user-data-dir", str(profile)],
            )

            _remove_stale_playwright_profile_locks(server)

            assert not (profile / "SingletonLock").exists()
            assert not (profile / "SingletonLock").is_symlink()
            assert not (profile / "SingletonCookie").is_symlink()
            assert not (profile / "SingletonSocket").is_symlink()

    def test_one_session_reuses_process_and_filters_catalog(self) -> None:
        async def scenario() -> None:
            server = MCPServerConfig(
                name="playwright-test",
                command=sys.executable,
                args=["-u", "-c", SERVER_CODE],
                permission_scopes=["browser_control"],
                allowed_tools=["browser_navigate", "browser_snapshot"],
            )
            session = MCPClientSession()
            try:
                tools = await session.discover(server)
                assert [tool.name for tool in tools] == [
                    "browser_navigate",
                    "browser_snapshot",
                ]
                first = await session.call(server, "browser_navigate", {"url": "https://example.com"})
                second = await session.call(server, "browser_snapshot", {})
                first_payload = json.loads(first["content"][0]["text"])
                second_payload = json.loads(second["content"][0]["text"])
                assert first_payload["pid"] == second_payload["pid"]
                assert [first_payload["counter"], second_payload["counter"]] == [1, 2]
            finally:
                await session.aclose()
            assert session.active_count == 0

        asyncio.run(scenario())

    def test_process_exit_is_reported_and_session_closes(self) -> None:
        async def scenario() -> None:
            server = MCPServerConfig(
                name="exiting-test",
                command=sys.executable,
                args=["-u", "-c", EXIT_SERVER_CODE],
            )
            session = MCPClientSession()
            try:
                await session.discover(server)
                with self.assertRaisesRegex(MCPClientError, "进程已退出"):
                    await session.call(server, "browser_navigate", {})
            finally:
                await session.aclose()
            assert session.active_count == 0

        asyncio.run(scenario())

    def test_session_close_terminates_spawned_process_group(self) -> None:
        async def scenario() -> None:
            server = MCPServerConfig(
                name="child-test",
                command=sys.executable,
                args=["-u", "-c", CHILD_SERVER_CODE],
            )
            session = MCPClientSession()
            await session.discover(server)
            result = await session.call(server, "child_pid", {})
            child_pid = int(result["content"][0]["text"])
            os.kill(child_pid, 0)

            await session.aclose()
            for _ in range(20):
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break
                await asyncio.sleep(0.05)
            else:
                self.fail(f"spawned MCP child still running: {child_pid}")

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
