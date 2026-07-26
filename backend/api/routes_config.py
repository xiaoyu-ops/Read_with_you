"""配置路由 — 交互式配置（config.yaml 唯一真相源，参考 ccswitch 的原子写 + 模型发现）。

GET  /config           读取当前配置（api_key 脱敏）
POST /config           保存配置 → 原子写回 config.yaml
POST /config/models    模型发现：base_url + key → GET /v1/models → 可用模型列表
POST /config/mcp/test  MCP 连通性测试：initialize + tools/list，不执行工具
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from ..llm.client import reset_client
from ..llm.config import (
    get_config,
    mask_config,
    reset_config,
    resolve_provider_api_key,
    save_config,
)
from ..llm.discover import discover_models
from ..llm.models import AppConfig, MCPServerConfig
from ..runtime import RuntimeMode
from ..security.credentials import (
    CredentialStoreError,
    SystemCredentialStore,
    get_system_credential_store,
    new_credential_ref,
)
from ..translation.deeplx import DeepLXError, translate_text as translate_with_deeplx
from ..tools.mcp import (
    MCPClientError,
    _choose_tool,
    _credential_error_for_server,
    discover_mcp_tools,
    invalidate_mcp_catalog,
)

router = APIRouter(prefix="/config", tags=["config"])
logger = logging.getLogger(__name__)

ADMIN_TOKEN_ENV = "PEINIDU_ADMIN_TOKEN"


class DiscoverRequest(BaseModel):
    """模型发现请求。"""

    base_url: str
    api_key: str = ""
    provider_name: str | None = None
    models_url: str | None = None  # 显式覆盖（可选）


class DiscoveredModelItem(BaseModel):
    id: str
    owned_by: str = ""


class DiscoverResponse(BaseModel):
    models: list[DiscoveredModelItem]
    count: int


def _require_admin(
    token: Annotated[str | None, Header(alias="X-Peinidu-Admin-Token")] = None,
) -> None:
    """保护配置接口。未设置 PEINIDU_ADMIN_TOKEN 时保持本地开发开放。"""
    expected = os.environ.get(ADMIN_TOKEN_ENV, "").strip()
    if not expected:
        return
    if not token or not secrets.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="需要管理员 token 才能访问配置接口")


@router.get("")
async def get_configuration(
    request: Request,
    _: Annotated[None, Depends(_require_admin)],
) -> dict:
    """返回当前 config.yaml 内容（api_key 脱敏）。"""
    cfg = get_config()
    return mask_config(cfg, request.app.state.runtime_mode)


def _is_local_core(request: Request) -> bool:
    return request.app.state.runtime_mode == RuntimeMode.LOCAL_CORE.value


def _is_secret_placeholder(value: object) -> bool:
    return isinstance(value, str) and ("***" in value or "•" in value)


def _prepare_system_secret(
    payload: dict,
    *,
    existing,
    secret_field: str,
    ref_field: str,
    kind: Literal["llm", "mineru", "deeplx"],
    store: SystemCredentialStore,
) -> str:
    payload.pop(ref_field, None)
    incoming = payload.get(secret_field, "")
    incoming = incoming if isinstance(incoming, str) else ""
    existing_ref = getattr(existing, ref_field, "") if existing is not None else ""
    existing_inline = (
        getattr(existing, secret_field, "") if existing is not None else ""
    )
    credential_ref = existing_ref
    if incoming.strip() and not _is_secret_placeholder(incoming):
        credential_ref = credential_ref or new_credential_ref(kind)
        store.set(credential_ref, incoming)
    elif not credential_ref and existing_inline:
        credential_ref = new_credential_ref(kind)
        store.set(credential_ref, existing_inline)
    payload[secret_field] = ""
    payload[ref_field] = credential_ref
    return credential_ref


def _secure_local_config_secrets(
    config_data: dict,
    existing: AppConfig,
    store: SystemCredentialStore,
) -> set[str]:
    retained_refs: set[str] = set()
    existing_by_name = {provider.name: provider for provider in existing.llm_providers}
    for index, provider in enumerate(config_data.get("llm_providers", [])):
        name = provider.get("name", "")
        previous = existing_by_name.get(name)
        if previous is None and index < len(existing.llm_providers):
            previous = existing.llm_providers[index]
        credential_ref = _prepare_system_secret(
            provider,
            existing=previous,
            secret_field="api_key",
            ref_field="api_key_ref",
            kind="llm",
            store=store,
        )
        if credential_ref:
            retained_refs.add(credential_ref)
        provider.pop("api_key_configured", None)

    mineru_data = config_data.get("mineru")
    if isinstance(mineru_data, dict):
        credential_ref = _prepare_system_secret(
            mineru_data,
            existing=existing.mineru,
            secret_field="api_token",
            ref_field="api_token_ref",
            kind="mineru",
            store=store,
        )
        if credential_ref:
            retained_refs.add(credential_ref)
        mineru_data.pop("api_token_configured", None)

    deeplx_data = config_data.setdefault("deeplx", existing.deeplx.model_dump())
    if isinstance(deeplx_data, dict):
        credential_ref = _prepare_system_secret(
            deeplx_data,
            existing=existing.deeplx,
            secret_field="api_key",
            ref_field="api_key_ref",
            kind="deeplx",
            store=store,
        )
        if credential_ref:
            retained_refs.add(credential_ref)
        deeplx_data.pop("api_key_configured", None)
    config_data.pop("credential_storage", None)
    return retained_refs


def _existing_credential_refs(config: AppConfig) -> set[str]:
    refs = {provider.api_key_ref for provider in config.llm_providers}
    refs.update({config.mineru.api_token_ref, config.deeplx.api_key_ref})
    return {ref for ref in refs if ref}


def _reset_config_consumers() -> None:
    invalidate_mcp_catalog()
    reset_config()
    reset_client()


@router.post("")
async def save_configuration(
    request: Request,
    config_data: dict,
    _: Annotated[None, Depends(_require_admin)],
) -> dict:
    """保存配置 → 原子写回 config.yaml。

    接收完整配置 JSON。api_key 若为脱敏值（含 ***）则保留原值不覆盖。
    """
    # 处理 api_key 脱敏值：前端回传的脱敏 key（含 ***）不覆盖真实 key
    cfg = get_config()
    existing_keys = {p.name: p.api_key for p in cfg.llm_providers}
    local_store: SystemCredentialStore | None = None
    retained_refs: set[str] = set()

    if _is_local_core(request):
        try:
            local_store = get_system_credential_store()
            retained_refs = _secure_local_config_secrets(config_data, cfg, local_store)
        except CredentialStoreError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
    else:
        for provider in config_data.get("llm_providers", []):
            name = provider.get("name", "")
            key = provider.get("api_key", "")
            if _is_secret_placeholder(key) and name in existing_keys:
                provider["api_key"] = existing_keys[name]
            provider.pop("api_key_configured", None)
            provider.pop("api_key_ref", None)

        mineru_data = config_data.get("mineru")
        if isinstance(mineru_data, dict):
            token = mineru_data.get("api_token", "")
            if _is_secret_placeholder(token):
                mineru_data["api_token"] = cfg.mineru.api_token
            mineru_data.pop("api_token_configured", None)
            mineru_data.pop("api_token_ref", None)

        deeplx_data = config_data.get("deeplx")
        if isinstance(deeplx_data, dict):
            key = deeplx_data.get("api_key", "")
            if _is_secret_placeholder(key):
                deeplx_data["api_key"] = cfg.deeplx.api_key
            deeplx_data.pop("api_key_configured", None)
            deeplx_data.pop("api_key_ref", None)
        config_data.pop("credential_storage", None)

    try:
        new_config = AppConfig(**config_data)
    except Exception as exc:
        raise HTTPException(status_code=422, detail="配置格式错误") from exc

    try:
        save_config(new_config)
        if local_store is not None:
            for credential_ref in _existing_credential_refs(cfg) - retained_refs:
                try:
                    local_store.delete(credential_ref)
                except CredentialStoreError:
                    logger.warning("orphaned system credential cleanup failed")
        _reset_config_consumers()
    except Exception as exc:
        logger.exception("配置保存失败")
        raise HTTPException(status_code=500, detail="保存失败") from exc

    message = (
        "配置已保存；凭据位于系统钥匙串"
        if local_store is not None
        else "配置已写入 config.yaml"
    )
    return {"status": "saved", "message": message}


class CredentialDeleteRequest(BaseModel):
    kind: Literal["llm_provider", "mineru", "deeplx"]
    provider_name: str | None = None


@router.post("/credentials/delete")
async def delete_local_credential(
    request: Request,
    body: CredentialDeleteRequest,
    _: Annotated[None, Depends(_require_admin)],
) -> dict:
    if not _is_local_core(request):
        raise HTTPException(status_code=409, detail="系统凭据仅供本地 Core 使用")
    config = get_config()
    data = config.model_dump()
    credential_ref = ""
    if body.kind == "llm_provider":
        if not body.provider_name:
            raise HTTPException(status_code=422, detail="缺少 provider_name")
        for provider in data["llm_providers"]:
            if provider["name"] == body.provider_name:
                credential_ref = provider.get("api_key_ref", "")
                provider["api_key"] = ""
                provider["api_key_ref"] = ""
                break
        else:
            raise HTTPException(status_code=404, detail="Provider 不存在")
    elif body.kind == "mineru":
        credential_ref = data["mineru"].get("api_token_ref", "")
        data["mineru"]["api_token"] = ""
        data["mineru"]["api_token_ref"] = ""
    else:
        credential_ref = data["deeplx"].get("api_key_ref", "")
        data["deeplx"]["api_key"] = ""
        data["deeplx"]["api_key_ref"] = ""

    try:
        save_config(AppConfig(**data))
        if credential_ref:
            get_system_credential_store().delete(credential_ref)
        _reset_config_consumers()
    except CredentialStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("系统凭据删除失败")
        raise HTTPException(status_code=500, detail="删除失败") from exc
    return {"status": "deleted"}


@router.post("/models", response_model=DiscoverResponse)
async def discover_available_models(
    request: Request,
    req: DiscoverRequest,
    _: Annotated[None, Depends(_require_admin)],
) -> DiscoverResponse:
    """模型发现：用 base_url + api_key 调 /v1/models，返回可用模型列表。"""
    api_key = req.api_key.strip()
    if not api_key and _is_local_core(request) and req.provider_name:
        provider = next(
            (p for p in get_config().llm_providers if p.name == req.provider_name),
            None,
        )
        if provider is None:
            raise HTTPException(status_code=404, detail="Provider 不存在")
        try:
            api_key = resolve_provider_api_key(provider)
        except CredentialStoreError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
    try:
        models = await discover_models(req.base_url, api_key, req.models_url)
    except PermissionError as e:
        raise HTTPException(status_code=401, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        logger.exception("模型发现异常")
        raise HTTPException(status_code=500, detail=f"发现失败: {e}") from e

    return DiscoverResponse(
        models=[DiscoveredModelItem(id=m.id, owned_by=m.owned_by) for m in models],
        count=len(models),
    )


@router.post("/deeplx/test")
async def test_deeplx_configuration(
    request: Request,
    _: Annotated[None, Depends(_require_admin)],
) -> dict:
    if not _is_local_core(request):
        raise HTTPException(status_code=409, detail="该测试仅供本地 Core 使用")
    try:
        await translate_with_deeplx("This is a connection test.")
    except DeepLXError as exc:
        raise HTTPException(status_code=400, detail=exc.code) from exc
    return {"ok": True}


class MCPToolInfo(BaseModel):
    name: str
    description: str = ""


class MCPTestResponse(BaseModel):
    ok: bool
    error: str = ""
    note: str = ""
    tools: list[MCPToolInfo] = Field(default_factory=list)
    chosen_tool: str = ""
    elapsed_ms: int = 0


@router.post("/mcp/test", response_model=MCPTestResponse)
async def test_mcp_server(
    server: MCPServerConfig,
    _: Annotated[None, Depends(_require_admin)] = None,
) -> MCPTestResponse:
    """MCP 连通性测试：对表单里的 server 草稿做 initialize + tools/list。

    不需要先保存配置，也绝不执行 tools/call；stdio 会拉起一次短生命周期子进程。
    """
    started = time.monotonic()

    def _elapsed() -> int:
        return int((time.monotonic() - started) * 1000)

    credential_error = _credential_error_for_server(server)
    if credential_error:
        return MCPTestResponse(ok=False, error=credential_error, elapsed_ms=_elapsed())

    # discover 的超时是按单次读消息算的，这里再罩一个总预算，防止子进程持续输出拖死请求
    overall_timeout = min(max(server.timeout_seconds or 12.0, 1.0) + 8.0, 30.0)
    try:
        tools = await asyncio.wait_for(discover_mcp_tools(server), timeout=overall_timeout)
    except asyncio.TimeoutError:
        return MCPTestResponse(
            ok=False,
            error=f"连接超时（>{overall_timeout:.0f}s）：server 没有在预算内完成 initialize/tools/list，检查 command/url 与网络。",
            elapsed_ms=_elapsed(),
        )
    except FileNotFoundError as e:
        return MCPTestResponse(
            ok=False,
            error=f"命令不存在：{e}。检查 command 是否已安装并在后端进程的 PATH 中。",
            elapsed_ms=_elapsed(),
        )
    except MCPClientError as e:
        return MCPTestResponse(ok=False, error=f"MCP 握手或协议失败：{e}", elapsed_ms=_elapsed())
    except Exception as e:
        logger.warning("MCP 测试连接失败 %s: %s", server.name, e)
        return MCPTestResponse(ok=False, error=f"连接失败：{e}", elapsed_ms=_elapsed())

    chosen = _choose_tool(server, tools, "")
    configured = (server.tool_name or "").strip()
    note = ""
    if configured and (chosen is None or chosen.name != configured):
        note = (
            f"配置的 tool_name={configured} 不在 tools/list 里，"
            f"实际调用时会自动选择 {chosen.name if chosen else '（无可用工具）'}。"
        )
    elif not tools:
        note = "server 可连接，但没有暴露任何工具。"
    return MCPTestResponse(
        ok=True,
        note=note,
        tools=[MCPToolInfo(name=t.name, description=t.description or "") for t in tools],
        chosen_tool=chosen.name if chosen else "",
        elapsed_ms=_elapsed(),
    )
