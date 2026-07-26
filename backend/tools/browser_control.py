"""Permissioned Playwright CLI bridge for state-changing browser actions."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from .registry import ToolCall, ToolRegistry, ToolResult, ToolSpec

ALLOWED_ACTIONS = {"open", "snapshot", "click", "fill", "press"}
MAX_OUTPUT_CHARS = 8_000


def _wrapper_path() -> str:
    configured = os.environ.get("PEINIDU_PLAYWRIGHT_CLI")
    if configured:
        return configured
    bundled = Path(__file__).resolve().parents[2] / "scripts" / "playwright_cli.sh"
    if bundled.is_file():
        return str(bundled)
    return str(Path.home() / ".codex" / "skills" / "playwright" / "scripts" / "playwright_cli.sh")


async def browser_control(call: ToolCall) -> ToolResult:
    action = str(call.arguments.get("action") or "").strip()
    if action not in ALLOWED_ACTIONS:
        return ToolResult(name=call.name, content="浏览器动作无效。", metadata={"error": "invalid_browser_action"})
    wrapper = _wrapper_path()
    if not os.path.isfile(wrapper):
        return ToolResult(name=call.name, content="浏览器控制不可用：未找到 Playwright CLI。", metadata={"error": "playwright_cli_missing"})
    args = [wrapper, action]
    if action == "open":
        url = str(call.arguments.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            return ToolResult(name=call.name, content="打开浏览器需要 http/https URL。", metadata={"error": "invalid_url"})
        args.append(url)
    elif action in {"click", "press"}:
        target = str(call.arguments.get("target") or "").strip()
        if not target:
            return ToolResult(name=call.name, content="浏览器动作缺少最新 snapshot 中的目标引用。", metadata={"error": "missing_target"})
        args.append(target)
    elif action == "fill":
        target = str(call.arguments.get("target") or "").strip()
        text = str(call.arguments.get("text") or "")
        if not target:
            return ToolResult(name=call.name, content="输入动作缺少目标引用。", metadata={"error": "missing_target"})
        args.extend([target, text])
    try:
        process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=45)
    except Exception as exc:
        return ToolResult(name=call.name, content=f"浏览器动作失败：{exc}", metadata={"error": str(exc)})
    output = (stdout.decode(errors="replace") + stderr.decode(errors="replace")).strip()[:MAX_OUTPUT_CHARS]
    if process.returncode:
        return ToolResult(name=call.name, content=f"浏览器动作失败：{output}", metadata={"error": output})
    return ToolResult(name=call.name, content=output or "浏览器动作完成。", metadata={"action": action, "source": "playwright_cli"})


def register_browser_control_tool(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            name="local.browser_control",
            description="通过 Playwright 控制浏览器。只在需要打开交互页面、点击、输入或按键时使用。",
            permission_scope="browser_control",
            source="local",
        ),
        browser_control,
    )
