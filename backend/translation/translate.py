"""翻译编排 — asyncio 并发 + 专用翻译 Provider + block 粒度缓存。

核心流程：
1. 加载 translation.json，找出 status=pending 或 error 的 block
2. 公式/代码 block（status=skip）不翻译
3. asyncio.gather + Semaphore 控并发；默认由 DeepLX 翻译当前 block
4. 每完成一个 block 即写盘（断点续翻，D16）+ 推 SSE 事件
5. 二次打开不重翻（done 的 block 跳过）

SSE 事件格式：{index, translation, status} JSON
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import AsyncIterator

from ..extraction.blocks import Block, PaperDocument
from ..llm.client import get_client
from ..llm.config import get_config
from ..storage.files import load_document, update_block_status, update_block_translation
from .deeplx import DeepLXError, translate_text as translate_with_deeplx
from .immutables import (
    ImmutablePlaceholderError,
    protect_immutable_fragments,
    restore_immutable_fragments,
)
from .prompts import build_translation_messages, compact_heading_translation

logger = logging.getLogger(__name__)

# SSE 事件类型
EVENT_BLOCK_DONE = "block_done"
EVENT_BLOCK_ERROR = "block_error"
EVENT_COMPLETE = "complete"

TRANSLATION_PROVIDER_ENV = "PEINIDU_TRANSLATION_PROVIDER"


def _needs_translation(b: Block) -> bool:
    """该 block 是否需要翻译。skip（公式/代码/表格）不翻，done 跳过。"""
    return b.status in ("pending", "error")


def _get_context_blocks(blocks: list[Block], i: int) -> tuple[str, str, str]:
    """取上下文：prev / current / next 的原文。"""
    current = blocks[i].original
    # 往前找第一个可翻译的 block（跳过 skip）
    prev = ""
    for j in range(i - 1, -1, -1):
        if blocks[j].status != "skip" and blocks[j].original.strip():
            prev = blocks[j].original
            break
    # 往后找
    next_ = ""
    for j in range(i + 1, len(blocks)):
        if blocks[j].status != "skip" and blocks[j].original.strip():
            next_ = blocks[j].original
            break
    return prev, current, next_


async def translate_single_block(
    doc: PaperDocument,
    block_index: int,
    *,
    force: bool = False,
) -> tuple[int, str | None, str]:
    """翻译单个 block。返回 (index, translation, status)。

    status: "done" | "error"
    """
    block = doc.blocks[block_index]
    if block.status == "skip":
        return block_index, None, "skip"
    if not force and block.status == "done" and block.translation:
        return block_index, block.translation, "done"

    prev, current, next_ = _get_context_blocks(doc.blocks, block_index)
    protected_current = protect_immutable_fragments(current)

    try:
        provider = _translation_provider()
        if provider == "deeplx":
            translation = await translate_with_deeplx(protected_current.text)
        else:
            # 兼容路径保留原有三段上下文 prompt；Agent/Pet 自身从不经过
            # translate_single_block，因此不会被专用翻译 Provider 接管。
            messages = build_translation_messages(
                prev,
                protected_current.text,
                next_,
                get_config().translation_prompt,
                block.type,
            )
            translation = await get_client().acomplete(messages, task="translation")
        translation = translation.strip()
        if not translation:
            return block_index, None, "error"
        # Heading responses are constrained to a single tagged title.  Compact
        # before restoring placeholders so an accidentally translated context
        # paragraph cannot introduce formula/citation fragments into the audit.
        if block.type == "heading":
            translation = compact_heading_translation(
                protected_current.text,
                translation,
            )
            if not translation:
                return block_index, None, "error"
        translation = restore_immutable_fragments(translation, protected_current).strip()
        return block_index, translation, "done"
    except ImmutablePlaceholderError as e:
        logger.warning("block %d 不可变片段校验失败: %s", block_index, e.reason)
        return block_index, None, "error"
    except DeepLXError as e:
        logger.warning("block %d DeepLX 翻译失败: %s", block_index, e.code)
        return block_index, None, "error"
    except Exception as e:
        logger.warning("block %d 翻译失败: %s", block_index, e)
        return block_index, None, "error"


def _translation_provider() -> str:
    provider = os.environ.get(TRANSLATION_PROVIDER_ENV, "deeplx").strip().casefold()
    if provider not in {"deeplx", "litellm"}:
        raise DeepLXError("translation_provider_invalid")
    return provider


async def translate_paper_sse(arxiv_id: str) -> AsyncIterator[str]:
    """翻译整篇论文，SSE 流式推送每个 block 的完成事件。

    每完成一个 block：
    1. 写盘更新 translation.json（断点续翻，D16）
    2. yield 一个 SSE 事件
    最后 yield complete 事件。
    """
    doc = load_document(arxiv_id)
    if doc is None:
        yield _sse_event(EVENT_BLOCK_ERROR, {"error": f"论文未找到: {arxiv_id}"})
        return

    # 找出需要翻译的 block index
    pending_indices = [i for i, b in enumerate(doc.blocks) if _needs_translation(b)]
    if not pending_indices:
        yield _sse_event(EVENT_COMPLETE, {"arxiv_id": arxiv_id, "translated": 0, "total": len(doc.blocks)})
        return

    cfg = get_config()
    semaphore = asyncio.Semaphore(cfg.translation_concurrency)
    completed = 0
    total = len(pending_indices)

    async def _translate_with_sem(idx: int) -> tuple[int, str | None, str]:
        async with semaphore:
            return await translate_single_block(doc, idx)

    # 并发执行，完成即写盘 + 推送。客户端断开时 StreamingResponse
    # 会取消/关闭这个 async generator；必须在 finally 内收掉所有后台
    # task，否则 LLM 调用会继续消耗配额，并可能在 SSE 结束后修改状态。
    tasks = [asyncio.create_task(_translate_with_sem(i)) for i in pending_indices]
    try:
        for coro in asyncio.as_completed(tasks):
            index, translation, status = await coro
            if status == "done" and translation is not None:
                update_block_translation(arxiv_id, index, translation, "done")
                completed += 1
                yield _sse_event(
                    EVENT_BLOCK_DONE,
                    {"index": index, "translation": translation, "status": "done"},
                )
            elif status == "error":
                update_block_status(arxiv_id, index, "error")
                yield _sse_event(EVENT_BLOCK_ERROR, {"index": index, "status": "error"})
    finally:
        unfinished = [task for task in tasks if not task.done()]
        for task in unfinished:
            task.cancel()
        if tasks:
            # 同时回收已完成但尚未被 as_completed 消费的异常。
            # 被取消的 block 保持 pending/error 原状，不写入伪完成结果。
            await asyncio.gather(*tasks, return_exceptions=True)

    yield _sse_event(
        EVENT_COMPLETE,
        {"arxiv_id": arxiv_id, "translated": completed, "total": total, "skipped": len(doc.blocks) - total},
    )


async def retry_single_block(arxiv_id: str, block_index: int) -> dict:
    """重试单个 block 翻译（非 SSE，直接返回结果）。"""
    doc = load_document(arxiv_id)
    if doc is None:
        return {"error": f"论文未找到: {arxiv_id}"}
    if block_index < 0 or block_index >= len(doc.blocks):
        return {"error": f"block index 越界: {block_index}"}

    idx, translation, status = await translate_single_block(
        doc,
        block_index,
        force=True,
    )
    if status == "done" and translation:
        update_block_translation(arxiv_id, idx, translation, "done")
    elif status == "error":
        # A forced retry replaces a known-bad prior result.  If the new attempt
        # fails, clear that stale content instead of leaving it attached to an
        # error block where a client could mistake it for a successful retry.
        update_block_translation(arxiv_id, idx, "", "error")
    return {"index": idx, "translation": translation, "status": status}


def _sse_event(event: str, data: dict) -> str:
    """组装一个 SSE 事件字符串。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
