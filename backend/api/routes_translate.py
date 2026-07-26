"""翻译路由 — SSE 流式推送（D12）。

POST /translate/{arxiv_id}       触发整篇翻译，SSE 流式推送每个 block
POST /translate/{arxiv_id}/block/{idx}  重试单个 block
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import aclosing

from anyio import CancelScope
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from starlette.types import Receive, Scope, Send

from ..storage.db import try_update_status, update_status
from ..storage.files import load_document
from ..translation.translate import retry_single_block, translate_paper_sse
from ..translation.selection import (
    SelectionTranslationError,
    SelectionTranslationRequest,
    SelectionTranslationResponse,
    translate_pdf_selection,
)

router = APIRouter(tags=["translate"])
logger = logging.getLogger(__name__)


@router.post(
    "/translate/{arxiv_id}/selection",
    response_model=SelectionTranslationResponse,
)
async def translate_selection(
    arxiv_id: str,
    request: SelectionTranslationRequest,
) -> SelectionTranslationResponse:
    """Translate one verified PDF.js TextLayer selection without persisting it."""
    try:
        return await translate_pdf_selection(arxiv_id, request)
    except SelectionTranslationError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={
                "code": exc.code,
                "message": exc.message,
                "retryable": exc.retryable,
            },
        ) from exc


def _translation_final_status(
    arxiv_id: str,
    *,
    incomplete_status: str = "translation_error",
) -> str:
    """整篇翻译结束后的论文状态。"""
    doc = load_document(arxiv_id)
    if doc is None:
        return "translation_error"
    if any(b.status == "error" for b in doc.blocks):
        return "translation_error"
    if all(b.status in ("done", "skip") for b in doc.blocks):
        return "translated"
    return incomplete_status


async def _shielded_status_update(arxiv_id: str, status: str) -> None:
    """保证收尾状态落盘，但不吞掉请求任务的取消。"""
    update_task = asyncio.create_task(update_status(arxiv_id, status))
    try:
        await asyncio.shield(update_task)
    except asyncio.CancelledError:
        # ASGI 2.3 断连由 AnyIO cancel scope 驱动，仅再次使用
        # asyncio.shield 仍可能在下一个 checkpoint 被取消。
        with CancelScope(shield=True):
            try:
                await update_task
            except Exception:
                logger.exception("翻译取消后的论文状态落盘失败: %s", arxiv_id)
        raise


class _ClosingStreamingResponse(StreamingResponse):
    """断连时显式关闭 SSE iterator，使其 finally 可以取消 worker。"""

    def __init__(
        self,
        *args,
        on_close: Callable[[], Awaitable[None]] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._on_close = on_close

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                close = getattr(self.body_iterator, "aclose", None)
                if close is not None:
                    with CancelScope(shield=True):
                        await close()
            finally:
                if self._on_close is not None:
                    with CancelScope(shield=True):
                        await self._on_close()


@router.post("/translate/{arxiv_id}")
async def translate_paper(arxiv_id: str) -> StreamingResponse:
    """触发翻译，返回 SSE 流。每个 block 翻译完推送一个事件。"""
    doc = load_document(arxiv_id)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"论文未找到: {arxiv_id}")
    locked = await try_update_status(arxiv_id, "translating", blocked_status="translating")
    if not locked:
        raise HTTPException(status_code=409, detail="该论文正在翻译中，请稍后刷新。")

    completed_normally = False
    program_error = False
    finalized = False
    finalize_lock = asyncio.Lock()

    async def finalize_status() -> None:
        nonlocal finalized
        async with finalize_lock:
            if finalized:
                return
            if program_error:
                status = "translation_error"
            else:
                status = _translation_final_status(
                    arxiv_id,
                    incomplete_status=(
                        "translation_error" if completed_normally else "extracted"
                    ),
                )
            await _shielded_status_update(arxiv_id, status)
            finalized = True

    async def event_generator():
        nonlocal completed_normally, program_error
        inner = translate_paper_sse(arxiv_id)
        try:
            async with aclosing(inner):
                async for event in inner:
                    yield event
            completed_normally = True
        except asyncio.CancelledError:
            raise
        except Exception:
            program_error = True
            raise
        finally:
            await finalize_status()

    return _ClosingStreamingResponse(
        event_generator(),
        on_close=finalize_status,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 禁用 nginx 缓冲
        },
    )


class BlockRetryResponse(BaseModel):
    index: int
    translation: str | None
    status: str


@router.post("/translate/{arxiv_id}/block/{block_index}", response_model=BlockRetryResponse)
async def retry_block(arxiv_id: str, block_index: int) -> BlockRetryResponse:
    """重试单个 block 翻译。"""
    doc = load_document(arxiv_id)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"论文未找到: {arxiv_id}")

    locked = await try_update_status(arxiv_id, "translating", blocked_status="translating")
    if not locked:
        raise HTTPException(status_code=409, detail="该论文正在翻译中，请稍后刷新。")

    program_error = False
    try:
        result = await retry_single_block(arxiv_id, block_index)
        if "error" in result:
            raise HTTPException(status_code=400, detail=result["error"])
        return BlockRetryResponse(
            index=result["index"],
            translation=result.get("translation"),
            status=result["status"],
        )
    except asyncio.CancelledError:
        raise
    except HTTPException:
        raise
    except Exception:
        program_error = True
        raise
    finally:
        status = (
            "translation_error"
            if program_error
            else _translation_final_status(arxiv_id, incomplete_status="extracted")
        )
        await _shielded_status_update(arxiv_id, status)
