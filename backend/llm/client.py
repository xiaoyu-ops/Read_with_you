"""LLM 客户端 — LiteLLM 封装（D8：所有 LLM 调用走 LiteLLM，不直接依赖单一 provider SDK）。

提供两个核心方法：
- acomplete(...)  普通补全，返回完整字符串
- astream(...)    流式补全，async yield 文本片段

按 task 解析模型（task_models），或直接指定 model。
"""

from __future__ import annotations

import importlib
import json
from types import ModuleType
from typing import Any, AsyncIterator

from .config import get_config, get_model_provider_map, resolve_provider_api_key
from .models import AppConfig, Provider
from .variant import variant_to_temperature


class _LazyLiteLLM:
    """Delay LiteLLM's heavy import until the first real model request."""

    _module: ModuleType | None = None

    def _load(self) -> ModuleType:
        if self._module is None:
            module = importlib.import_module("litellm")
            module.suppress_debug_debugger_logging = True
            self._module = module
        return self._module

    def __getattr__(self, name: str) -> Any:
        return getattr(self._load(), name)


litellm = _LazyLiteLLM()


class LLMClient:
    """统一的 LLM 调用入口。"""

    def __init__(self, config: AppConfig | None = None) -> None:
        self.config = config or get_config()
        self._model_provider = get_model_provider_map(self.config)

    # ── 解析 ──────────────────────────────────────────────

    def _resolve_model(self, task: str | None, model: str | None) -> str:
        """确定用哪个 model：显式 model > task_models[task] > default_model。

        task_models 里的空字符串视为未配置（agent_chat / agent_intent 默认跟随
        default_model），回退 default_model。
        """
        if model:
            return model
        if task:
            tm = self.config.task_models.model_dump()
            if tm.get(task):
                return tm[task]
        return self.config.default_model

    def _resolve_provider(self, model: str) -> Provider | None:
        """根据 model 名找它所属的 provider；找不到则回退 default_provider。"""
        provider = self._model_provider.get(model)
        if provider:
            return provider
        return next(
            (p for p in self.config.llm_providers if p.name == self.config.default_provider),
            None,
        )

    def _resolve_temperature(
        self,
        temperature: float | None,
        variant: str | None,
    ) -> float | None:
        if temperature is not None:
            return temperature
        if variant:
            return variant_to_temperature(variant)
        return None

    # 不支持 temperature 参数的模型（reasoner 类模型，传了会报错）
    _NO_TEMPERATURE_MODELS = frozenset({
        "deepseek-reasoner",  # DeepSeek R1
        "deepseek-v4-pro",    # DeepSeek V4 Pro（推理增强版，可能不支持 temperature）
        "o1", "o1-preview", "o1-mini",  # OpenAI o1 系列
        "o3", "o3-mini",
    })

    def _supports_temperature(self, model: str) -> bool:
        """判断该模型是否支持 temperature 参数。"""
        # 精确匹配 + 后缀匹配（如 o1-2024-12-17）
        if model in self._NO_TEMPERATURE_MODELS:
            return False
        for m in self._NO_TEMPERATURE_MODELS:
            if model.startswith(m + "-"):
                return False
        return True

    def _litellm_params(
        self,
        model: str,
        provider: Provider | None,
        messages: list[dict],
        temperature: float | None,
        stream: bool,
    ) -> dict:
        """组装 litellm.acompletion 的参数。"""
        # litellm 约定 "provider_type/model" 前缀格式，明确路由
        litellm_model = (
            f"{provider.type}/{model}" if provider and provider.type else model
        )
        params: dict = {
            "model": litellm_model,
            "messages": messages,
            "stream": stream,
            "timeout": self.config.request_timeout,
        }
        # reasoner 类模型（deepseek-reasoner / o1 / o3）不支持 temperature，传了会报错
        if temperature is not None and self._supports_temperature(model):
            params["temperature"] = temperature
        if provider:
            api_key = resolve_provider_api_key(provider)
            if api_key:
                params["api_key"] = api_key
            if provider.api_base:
                # 中转站：OpenAI 兼容格式，只需设 api_base（D18）
                params["api_base"] = provider.api_base
        return params

    # ── 公开接口 ──────────────────────────────────────────

    async def acomplete(
        self,
        messages: list[dict],
        task: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        variant: str | None = None,
    ) -> str:
        """普通补全，返回完整文本。"""
        mdl = self._resolve_model(task, model)
        provider = self._resolve_provider(mdl)
        temp = self._resolve_temperature(temperature, variant)
        params = self._litellm_params(mdl, provider, messages, temp, stream=False)
        resp = await litellm.acompletion(**params)
        return resp.choices[0].message.content or ""

    async def acomplete_openai_response(
        self,
        messages: list[dict],
        *,
        task: str = "translation",
        response_format: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Return a non-streaming LiteLLM response in OpenAI-compatible form.

        This narrow adapter is used by the isolated PDF export sidecar.  Model
        and provider selection still come exclusively from ``AppConfig``; the
        caller cannot supply an upstream model name or provider credential.
        """
        mdl = self._resolve_model(task, None)
        provider = self._resolve_provider(mdl)
        params = self._litellm_params(mdl, provider, messages, None, stream=False)
        if response_format is not None:
            params["response_format"] = response_format
        response = await litellm.acompletion(**params)
        if isinstance(response, dict):
            return dict(response)
        model_dump = getattr(response, "model_dump", None)
        if callable(model_dump):
            dumped = model_dump(exclude_none=True)
            if isinstance(dumped, dict):
                return dumped
        raise TypeError("LiteLLM returned an unsupported response type")

    async def acomplete_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        task: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        variant: str | None = None,
        tool_choice: str | dict | None = "auto",
    ) -> dict[str, Any]:
        """补全并允许 provider 原生 tool calls，返回规范化结果。

        返回结构：
        {
            "content": str,
            "tool_calls": [
                {"name": str, "arguments": dict, "id": str, "type": str}
            ]
        }
        """
        mdl = self._resolve_model(task, model)
        provider = self._resolve_provider(mdl)
        temp = self._resolve_temperature(temperature, variant)
        params = self._litellm_params(mdl, provider, messages, temp, stream=False)
        params["tools"] = tools
        if tool_choice is not None:
            params["tool_choice"] = tool_choice
        resp = await litellm.acompletion(**params)
        message = resp.choices[0].message
        return {
            "content": _message_get(message, "content") or "",
            "tool_calls": _normalize_tool_calls(_message_get(message, "tool_calls") or []),
        }

    async def astream_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        task: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        variant: str | None = None,
        tool_choice: str | dict | None = "auto",
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream content deltas while assembling provider tool-call deltas.

        Content chunks are emitted as ``content_delta`` events. The final
        ``response`` event uses the same normalized shape as
        :meth:`acomplete_with_tools` so the Agent Loop has one execution path.
        """
        mdl = self._resolve_model(task, model)
        provider = self._resolve_provider(mdl)
        temp = self._resolve_temperature(temperature, variant)
        params = self._litellm_params(mdl, provider, messages, temp, stream=True)
        params["tools"] = tools
        if tool_choice is not None:
            params["tool_choice"] = tool_choice
        stream = await litellm.acompletion(**params)
        content_parts: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        async for chunk in stream:
            choices = _message_get(chunk, "choices") or []
            if not choices:
                continue
            delta = _message_get(choices[0], "delta") or {}
            content = str(_message_get(delta, "content") or "")
            if content:
                content_parts.append(content)
                yield {"type": "content_delta", "content": content}
            raw_tool_calls = _message_get(delta, "tool_calls") or []
            if not isinstance(raw_tool_calls, list):
                continue
            for fallback_index, raw_call in enumerate(raw_tool_calls):
                raw_index = _message_get(raw_call, "index")
                index = int(raw_index) if isinstance(raw_index, int) else fallback_index
                current = calls.setdefault(
                    index,
                    {"id": "", "type": "function", "name": "", "arguments": ""},
                )
                call_id = _message_get(raw_call, "id")
                call_type = _message_get(raw_call, "type")
                function = _message_get(raw_call, "function") or {}
                name = _message_get(function, "name")
                arguments = _message_get(function, "arguments")
                if call_id:
                    current["id"] += str(call_id)
                if call_type:
                    current["type"] = str(call_type)
                if name:
                    current["name"] += str(name)
                if isinstance(arguments, str):
                    current["arguments"] += arguments
                elif isinstance(arguments, dict):
                    current["arguments"] += json.dumps(arguments, ensure_ascii=False)
        normalized_calls: list[dict[str, Any]] = []
        for index in sorted(calls):
            call = calls[index]
            name = call["name"].strip()
            if not name:
                continue
            arguments: dict[str, Any] = {}
            if call["arguments"].strip():
                try:
                    parsed = json.loads(call["arguments"])
                    if isinstance(parsed, dict):
                        arguments = parsed
                except json.JSONDecodeError:
                    arguments = {}
            normalized_calls.append(
                {
                    "id": call["id"],
                    "type": call["type"],
                    "name": name,
                    "arguments": arguments,
                }
            )
        yield {
            "type": "response",
            "content": "".join(content_parts),
            "tool_calls": normalized_calls,
        }

    async def astream(
        self,
        messages: list[dict],
        task: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        variant: str | None = None,
    ) -> AsyncIterator[str]:
        """流式补全，逐片段 yield 文本（用于 SSE 推送）。"""
        mdl = self._resolve_model(task, model)
        provider = self._resolve_provider(mdl)
        temp = self._resolve_temperature(temperature, variant)
        params = self._litellm_params(mdl, provider, messages, temp, stream=True)
        stream = await litellm.acompletion(**params)
        async for chunk in stream:
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta


# 模块级单例
_client: LLMClient | None = None


def get_client() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client


def reset_client() -> None:
    """重置 LLM client 单例，让配置保存后立即生效。"""
    global _client
    _client = None


def _message_get(message: Any, key: str) -> Any:
    if isinstance(message, dict):
        return message.get(key)
    return getattr(message, key, None)


def _normalize_tool_calls(raw_tool_calls: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_tool_calls, list):
        return []
    calls: list[dict[str, Any]] = []
    for raw_call in raw_tool_calls:
        call_type = _message_get(raw_call, "type") or "function"
        call_id = _message_get(raw_call, "id") or ""
        function = _message_get(raw_call, "function") or {}
        name = str(_message_get(function, "name") or "").strip()
        arguments = _message_get(function, "arguments")
        parsed_arguments: dict[str, Any] = {}
        if isinstance(arguments, str) and arguments.strip():
            try:
                data = json.loads(arguments)
                if isinstance(data, dict):
                    parsed_arguments = data
            except json.JSONDecodeError:
                parsed_arguments = {}
        elif isinstance(arguments, dict):
            parsed_arguments = arguments
        if not name:
            continue
        calls.append(
            {
                "id": str(call_id),
                "type": str(call_type),
                "name": name,
                "arguments": parsed_arguments,
            }
        )
    return calls
