"""Private OpenAI-compatible completion endpoint for the PDF export sidecar.

The route is intentionally not exposed through nginx.  It accepts one fixed
model alias, then resolves the real translation model/provider through the
normal LiteLLM client so no upstream credential crosses the sidecar boundary.
"""

from __future__ import annotations

import asyncio
import hmac
import os
import secrets
import time
from typing import Any, Literal

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ..llm.client import get_client


router = APIRouter(prefix="/internal/llm/v1", tags=["internal"])
MODEL_ALIAS = "pdf-translation"


class _ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=200_000)


class _ResponseFormat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["text", "json_object"]


class _ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: Literal[MODEL_ALIAS]
    messages: list[_ChatMessage] = Field(min_length=1, max_length=32)
    stream: Literal[False] = False
    response_format: _ResponseFormat | None = None


def _error_response(
    status_code: int,
    message: str,
    code: str,
    *,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "message": message,
                "type": "pdf_export_upstream_error",
                "code": code,
            }
        },
        headers=headers,
    )


def _authorize(authorization: str | None) -> JSONResponse | None:
    expected = os.environ.get("PEINIDU_PDF_EXPORT_INTERNAL_TOKEN", "").strip()
    if not expected:
        return _error_response(
            503,
            "PDF export internal LLM access is not configured.",
            "internal_llm_not_configured",
        )
    scheme, _, supplied = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not supplied or not hmac.compare_digest(supplied, expected):
        return _error_response(
            401,
            "Invalid internal bearer token.",
            "invalid_internal_token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return None


def _upstream_status(error: Exception) -> int:
    if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
        return 504
    raw_status = getattr(error, "status_code", None)
    if raw_status in {408, 504}:
        return 504
    if isinstance(raw_status, int) and raw_status in {
        400,
        401,
        403,
        404,
        409,
        429,
        500,
        502,
        503,
        504,
    }:
        return raw_status
    return 502


def _retry_after(error: Exception) -> str | None:
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if headers is not None:
        value = headers.get("retry-after")
        if value:
            return str(value)
    value = getattr(error, "retry_after", None)
    return str(value) if value is not None else None


def _public_response(raw: dict[str, Any]) -> dict[str, Any]:
    """Whitelist OpenAI response fields and hide the real provider/model."""
    choices: list[dict[str, Any]] = []
    for fallback_index, item in enumerate(raw.get("choices") or []):
        if not isinstance(item, dict):
            continue
        message = item.get("message") or {}
        if not isinstance(message, dict):
            continue
        choices.append(
            {
                "index": item.get("index", fallback_index),
                "message": {
                    "role": "assistant",
                    "content": str(message.get("content") or ""),
                },
                "finish_reason": item.get("finish_reason") or "stop",
            }
        )
    usage = raw.get("usage")
    public_usage: dict[str, int] | None = None
    if isinstance(usage, dict):
        public_usage = {
            name: int(usage.get(name) or 0)
            for name in ("prompt_tokens", "completion_tokens", "total_tokens")
        }
    result: dict[str, Any] = {
        "id": str(raw.get("id") or f"chatcmpl-pdf-{secrets.token_hex(8)}"),
        "object": "chat.completion",
        "created": int(raw.get("created") or time.time()),
        "model": MODEL_ALIAS,
        "choices": choices,
    }
    if public_usage is not None:
        result["usage"] = public_usage
    return result


@router.post("/chat/completions")
async def create_chat_completion(
    payload: _ChatCompletionRequest,
    authorization: str | None = Header(default=None),
):
    auth_error = _authorize(authorization)
    if auth_error is not None:
        return auth_error
    try:
        raw = await get_client().acomplete_openai_response(
            [message.model_dump() for message in payload.messages],
            task="translation",
            response_format=(
                payload.response_format.model_dump()
                if payload.response_format is not None
                else None
            ),
        )
    except Exception as error:
        status_code = _upstream_status(error)
        if status_code == 429:
            message = "The translation provider is rate limited. Please retry later."
            code = "provider_rate_limited"
        elif status_code == 504:
            message = "The translation provider timed out."
            code = "provider_timeout"
        elif status_code in {401, 403}:
            message = "The translation provider rejected its configured credentials."
            code = "provider_authentication_failed"
        elif status_code == 404:
            message = "The configured translation model was not found."
            code = "provider_model_not_found"
        else:
            message = "The translation provider request failed."
            code = "provider_request_failed"
        retry_after = _retry_after(error)
        headers = {"Retry-After": retry_after} if retry_after else None
        return _error_response(status_code, message, code, headers=headers)
    return _public_response(raw)
