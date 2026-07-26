"""MinerU API adapter.

支持两条 URL 解析链路：
- Agent 轻量 API：URL -> task_id -> markdown_url -> Markdown；
- 精准解析 API：URL -> task_id -> full_zip_url -> full.md。
"""

from __future__ import annotations

import asyncio
import io
import json
import math
import re
import time
import zipfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import httpx

from ..llm.models import MinerUConfig
from .blocks import Block


_MAX_ZIP_BYTES = 256 * 1024 * 1024
_MAX_ZIP_MEMBERS = 5_000
_MAX_UNCOMPRESSED_BYTES = 1024 * 1024 * 1024
_MAX_MEMBER_BYTES = 256 * 1024 * 1024
_MAX_MARKDOWN_BYTES = 64 * 1024 * 1024
_MAX_JSON_BYTES = 128 * 1024 * 1024
_MAX_AGENT_FILE_BYTES = 10 * 1024 * 1024
_MAX_STANDARD_FILE_BYTES = 200 * 1024 * 1024
_FILE_CHUNK_BYTES = 1024 * 1024
MINERU_LAYOUT_ADAPTER = "mineru_middle"
MINERU_LAYOUT_ADAPTER_VERSION = "1"


class MinerUError(RuntimeError):
    """MinerU 调用失败。"""


class MinerUAuthError(MinerUError):
    """MinerU 认证失败或缺少 token。"""


class MinerUTaskFailed(MinerUError):
    """MinerU 任务返回 failed。"""

    def __init__(self, err_code: int | None, err_msg: str) -> None:
        self.err_code = err_code
        self.err_msg = err_msg
        super().__init__(f"MinerU task failed ({err_code}): {err_msg}")


class MinerUTaskTimeout(MinerUError):
    """等待 MinerU 任务超时。"""


@dataclass
class MinerUStructuredResult:
    """MinerU 可阅读内容及稳定版面产物。

    ``layout`` 对应官方 ``middle.json``，精准 API 归档中也可能命名为
    ``layout.json``。``content_list_v2.json`` 仍处于开发版本，刻意不纳入。
    """

    markdown: str
    blocks: list[Block]
    layout: dict[str, Any] | None = None
    content_list: list[dict[str, Any]] | None = None
    layout_member: str | None = None
    content_list_member: str | None = None


class MinerUClient:
    """MinerU HTTP client."""

    def __init__(
        self,
        config: MinerUConfig | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config or MinerUConfig()
        self._http_client = http_client

    async def parse_agent_url_to_markdown(
        self,
        file_url: str,
        *,
        file_name: str | None = None,
        language: str | None = None,
        page_range: str | None = None,
    ) -> str:
        """用 Agent 轻量 URL 接口解析远程文件并返回 Markdown 文本。"""
        task_id = await self.create_agent_url_task(
            file_url,
            file_name=file_name,
            language=language,
            page_range=page_range,
        )
        markdown_url = await self.wait_for_agent_markdown_url(task_id)
        return await self.download_markdown(markdown_url)

    async def parse_agent_file_to_markdown(self, file_path: Path) -> str:
        """上传已持久化的本地文件，保证解析内容与证据 PDF 完全一致。"""
        task_id, upload_url = await self.create_agent_file_task(file_path)
        await self.upload_agent_file(upload_url, file_path)
        markdown_url = await self.wait_for_agent_markdown_url(task_id)
        return await self.download_markdown(markdown_url)

    async def create_agent_url_task(
        self,
        file_url: str,
        *,
        file_name: str | None = None,
        language: str | None = None,
        page_range: str | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "url": file_url,
            "language": language or self.config.language,
            "enable_table": self.config.enable_table,
            "is_ocr": self.config.is_ocr,
            "enable_formula": self.config.enable_formula,
        }
        if file_name:
            payload["file_name"] = file_name
        page_range = page_range if page_range is not None else self.config.page_range
        if page_range:
            payload["page_range"] = page_range

        data = await self._request_json(
            "POST",
            self._agent_url("/parse/url"),
            json_payload=payload,
        )
        task_id = (data.get("data") or {}).get("task_id")
        if not task_id:
            raise MinerUError("MinerU Agent task response missing task_id")
        return str(task_id)

    async def create_agent_file_task(self, file_path: Path) -> tuple[str, str]:
        _validate_agent_file(file_path)
        payload: dict[str, Any] = {
            "file_name": file_path.name,
            "language": self.config.language,
            "enable_table": self.config.enable_table,
            "is_ocr": self.config.is_ocr,
            "enable_formula": self.config.enable_formula,
        }
        if self.config.page_range:
            payload["page_range"] = self.config.page_range
        data = await self._request_json(
            "POST",
            self._agent_url("/parse/file"),
            json_payload=payload,
        )
        response_data = data.get("data") or {}
        task_id = response_data.get("task_id")
        upload_url = response_data.get("file_url")
        if not task_id:
            raise MinerUError("MinerU Agent file response missing task_id")
        if not isinstance(upload_url, str) or not upload_url:
            raise MinerUError("MinerU Agent file response missing upload URL")
        return str(task_id), upload_url

    async def upload_agent_file(self, upload_url: str, file_path: Path) -> None:
        _validate_agent_file(file_path)
        await self._request(
            "PUT",
            upload_url,
            content=_iter_file_chunks(file_path),
            content_length=file_path.stat().st_size,
        )

    async def wait_for_agent_markdown_url(self, task_id: str) -> str:
        deadline = time.monotonic() + self.config.max_wait_seconds
        while True:
            result = await self.get_agent_task(task_id)
            data = result.get("data") or {}
            state = data.get("state")
            if state == "done":
                markdown_url = data.get("markdown_url")
                if not markdown_url:
                    raise MinerUError("MinerU Agent task done without markdown_url")
                return str(markdown_url)
            if state == "failed":
                raise MinerUTaskFailed(data.get("err_code"), str(data.get("err_msg") or "unknown error"))
            if time.monotonic() >= deadline:
                raise MinerUTaskTimeout(f"MinerU Agent task timed out: {task_id}")
            await asyncio.sleep(max(self.config.poll_interval_seconds, 0))

    async def get_agent_task(self, task_id: str) -> dict[str, Any]:
        return await self._request_json("GET", self._agent_url(f"/parse/{task_id}"))

    async def download_markdown(self, markdown_url: str) -> str:
        resp = await self._request("GET", markdown_url)
        return resp.text

    async def create_standard_extract_task(
        self,
        file_url: str,
        *,
        model_version: str = "vlm",
    ) -> str:
        """提交精准解析任务。

        精准解析需要 API 管理页创建的 Bearer token，不接受 AK/SK。
        """
        if not self.config.api_token:
            raise MinerUAuthError("MinerU standard API requires api_token")
        payload: dict[str, Any] = {
            "url": file_url,
            "model_version": model_version,
            "is_ocr": self.config.is_ocr,
            "enable_formula": self.config.enable_formula,
            "enable_table": self.config.enable_table,
            "language": self.config.language,
        }
        if self.config.page_range:
            payload["page_ranges"] = self.config.page_range
        data = await self._request_json(
            "POST",
            self._standard_url("/extract/task"),
            json_payload=payload,
            bearer_token=self.config.api_token,
        )
        task_id = (data.get("data") or {}).get("task_id")
        if not task_id:
            raise MinerUError("MinerU standard task response missing task_id")
        return str(task_id)

    async def parse_standard_url(self, file_url: str) -> MinerUStructuredResult:
        """用精准解析 API 解析远程文件并返回结构化结果。"""
        task_id = await self.create_standard_extract_task(file_url)
        zip_url = await self.wait_for_standard_zip_url(task_id)
        return await self.download_standard_result(zip_url)

    async def parse_standard_url_to_markdown(self, file_url: str) -> str:
        """兼容旧调用：仅返回精准解析结果归档中的 full.md。"""
        return (await self.parse_standard_url(file_url)).markdown

    async def wait_for_standard_zip_url(self, task_id: str) -> str:
        if not self.config.api_token:
            raise MinerUAuthError("MinerU standard API requires api_token")
        deadline = time.monotonic() + self.config.max_wait_seconds
        while True:
            result = await self._request_json(
                "GET",
                self._standard_url(f"/extract/task/{task_id}"),
                bearer_token=self.config.api_token,
            )
            data = result.get("data") or {}
            state = str(data.get("state") or "")
            if state == "done":
                zip_url = data.get("full_zip_url")
                if not zip_url:
                    raise MinerUError("MinerU standard task done without full_zip_url")
                return str(zip_url)
            if state == "failed":
                raise MinerUTaskFailed(data.get("err_code"), str(data.get("err_msg") or "unknown error"))
            if state not in {"pending", "running", "converting"}:
                raise MinerUError(f"MinerU standard task returned unknown state: {state or 'missing'}")
            if time.monotonic() >= deadline:
                raise MinerUTaskTimeout(f"MinerU standard task timed out: {task_id}")
            await asyncio.sleep(max(self.config.poll_interval_seconds, 0))

    async def create_standard_file_batch(
        self,
        file_path: Path,
        *,
        model_version: str = "vlm",
    ) -> tuple[str, str]:
        """申请单个本地文件的精准解析上传地址。"""
        if not self.config.api_token:
            raise MinerUAuthError("MinerU standard API requires api_token")
        _validate_standard_file(file_path)
        file_payload: dict[str, Any] = {
            "name": file_path.name,
            "is_ocr": self.config.is_ocr,
        }
        if self.config.page_range:
            file_payload["page_ranges"] = self.config.page_range
        payload: dict[str, Any] = {
            "files": [file_payload],
            "model_version": model_version,
            "enable_formula": self.config.enable_formula,
            "enable_table": self.config.enable_table,
            "language": self.config.language,
        }
        data = await self._request_json(
            "POST",
            self._standard_url("/file-urls/batch"),
            json_payload=payload,
            bearer_token=self.config.api_token,
        )
        response_data = data.get("data") or {}
        batch_id = response_data.get("batch_id")
        file_urls = response_data.get("file_urls")
        if not batch_id:
            raise MinerUError("MinerU standard file response missing batch_id")
        if (
            not isinstance(file_urls, list)
            or len(file_urls) != 1
            or not isinstance(file_urls[0], str)
            or not file_urls[0]
        ):
            raise MinerUError("MinerU standard file response missing upload URL")
        return str(batch_id), file_urls[0]

    async def upload_standard_file(self, upload_url: str, file_path: Path) -> None:
        """把本地文件上传到 MinerU 返回的签名 URL，不携带 API token。"""
        _validate_standard_file(file_path)
        await self._request(
            "PUT",
            upload_url,
            content=_iter_file_chunks(file_path),
            content_length=file_path.stat().st_size,
        )

    async def wait_for_standard_batch_zip_url(self, batch_id: str, file_name: str) -> str:
        """等待单文件 batch 完成并返回 full_zip_url。"""
        if not self.config.api_token:
            raise MinerUAuthError("MinerU standard API requires api_token")
        deadline = time.monotonic() + self.config.max_wait_seconds
        while True:
            result = await self._request_json(
                "GET",
                self._standard_url(f"/extract-results/batch/{batch_id}"),
                bearer_token=self.config.api_token,
            )
            data = result.get("data") or {}
            entries = data.get("extract_result")
            if entries is None:
                entries = []
            if not isinstance(entries, list):
                raise MinerUError("MinerU standard batch result has invalid extract_result")

            entry = _select_batch_entry(entries, file_name)
            if entry is not None:
                state = str(entry.get("state") or "")
                if state == "done":
                    zip_url = entry.get("full_zip_url")
                    if not zip_url:
                        raise MinerUError("MinerU standard batch done without full_zip_url")
                    return str(zip_url)
                if state == "failed":
                    raise MinerUTaskFailed(
                        entry.get("err_code"),
                        str(entry.get("err_msg") or "unknown error"),
                    )
                if state not in {"waiting-file", "pending", "running", "converting"}:
                    raise MinerUError(
                        f"MinerU standard batch returned unknown state: {state or 'missing'}"
                    )

            if time.monotonic() >= deadline:
                raise MinerUTaskTimeout(f"MinerU standard batch timed out: {batch_id}")
            await asyncio.sleep(max(self.config.poll_interval_seconds, 0))

    async def parse_standard_file(self, file_path: Path) -> MinerUStructuredResult:
        """上传本地文件并返回精准解析的结构化结果。"""
        file_path = Path(file_path)
        batch_id, upload_url = await self.create_standard_file_batch(file_path)
        await self.upload_standard_file(upload_url, file_path)
        zip_url = await self.wait_for_standard_batch_zip_url(batch_id, file_path.name)
        return await self.download_standard_result(zip_url)

    async def download_standard_result(self, zip_url: str) -> MinerUStructuredResult:
        content = await self._download_limited(zip_url, max_bytes=_MAX_ZIP_BYTES)
        return structured_result_from_zip(content)

    async def download_standard_markdown(self, zip_url: str) -> str:
        """兼容旧调用：下载归档后仅返回 Markdown。"""
        return (await self.download_standard_result(zip_url)).markdown

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        json_payload: dict[str, Any] | None = None,
        bearer_token: str | None = None,
    ) -> dict[str, Any]:
        resp = await self._request(method, url, json_payload=json_payload, bearer_token=bearer_token)
        try:
            data = resp.json()
        except ValueError as exc:
            raise MinerUError("MinerU response is not valid JSON") from exc
        if not isinstance(data, dict):
            raise MinerUError("MinerU response JSON must be an object")
        if data.get("code") not in (0, None):
            raise MinerUError(str(data.get("msg") or "MinerU API error"))
        return data

    async def _download_limited(self, url: str, *, max_bytes: int) -> bytes:
        """Stream a response with a hard bound before buffering its payload."""

        async def consume(client: httpx.AsyncClient) -> bytes:
            try:
                async with client.stream(
                    "GET",
                    url,
                    headers={"User-Agent": "PeiNiDu/0.1"},
                ) as response:
                    if response.status_code in (401, 403):
                        raise MinerUError(
                            f"MinerU request rejected: HTTP {response.status_code}"
                        )
                    try:
                        response.raise_for_status()
                    except httpx.HTTPStatusError as exc:
                        raise MinerUError(
                            f"MinerU HTTP error: {response.status_code}"
                        ) from exc
                    content_length = response.headers.get("content-length")
                    if content_length:
                        try:
                            declared_size = int(content_length)
                        except ValueError as exc:
                            raise MinerUError(
                                "MinerU result returned invalid Content-Length"
                            ) from exc
                        if declared_size < 0:
                            raise MinerUError(
                                "MinerU result returned invalid Content-Length"
                            )
                        if declared_size > max_bytes:
                            raise MinerUError("MinerU result zip exceeds size limit")
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(content) + len(chunk) > max_bytes:
                            raise MinerUError("MinerU result zip exceeds size limit")
                        content.extend(chunk)
                    return bytes(content)
            except httpx.TimeoutException as exc:
                raise MinerUError("MinerU request timed out") from exc
            except httpx.RequestError as exc:
                raise MinerUError("MinerU network request failed") from exc

        if self._http_client is not None:
            return await consume(self._http_client)
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            return await consume(client)

    async def _request(
        self,
        method: str,
        url: str,
        *,
        json_payload: dict[str, Any] | None = None,
        bearer_token: str | None = None,
        content: Any | None = None,
        content_length: int | None = None,
    ) -> httpx.Response:
        headers = {"User-Agent": "PeiNiDu/0.1"}
        if bearer_token:
            headers["Authorization"] = f"Bearer {bearer_token}"

        client = self._http_client
        request_kwargs: dict[str, Any] = {"headers": headers}
        if json_payload is not None:
            request_kwargs["json"] = json_payload
        if content is not None:
            request_kwargs["content"] = content
        if content_length is not None:
            headers["Content-Length"] = str(content_length)
        try:
            if client is not None:
                resp = await client.request(method, url, **request_kwargs)
            else:
                async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as new_client:
                    resp = await new_client.request(method, url, **request_kwargs)
        except httpx.TimeoutException as exc:
            raise MinerUError("MinerU request timed out") from exc
        except httpx.RequestError as exc:
            raise MinerUError("MinerU network request failed") from exc

        if resp.status_code in (401, 403):
            if bearer_token:
                raise MinerUAuthError(f"MinerU auth failed: HTTP {resp.status_code}")
            raise MinerUError(f"MinerU request rejected: HTTP {resp.status_code}")
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise MinerUError(f"MinerU HTTP error: {resp.status_code}") from exc
        return resp

    def _agent_url(self, path: str) -> str:
        return f"{self.config.base_url.rstrip('/')}/api/v1/agent{path}"

    def _standard_url(self, path: str) -> str:
        return f"{self.config.base_url.rstrip('/')}/api/v4{path}"


def _validate_standard_file(file_path: Path) -> None:
    if not file_path.is_file():
        raise MinerUError(f"MinerU local file not found: {file_path.name}")
    size = file_path.stat().st_size
    if size <= 0:
        raise MinerUError("MinerU local file is empty")
    if size > _MAX_STANDARD_FILE_BYTES:
        raise MinerUError("MinerU local file exceeds size limit")


def _validate_agent_file(file_path: Path) -> None:
    if not file_path.is_file():
        raise MinerUError(f"MinerU local file not found: {file_path.name}")
    size = file_path.stat().st_size
    if size <= 0:
        raise MinerUError("MinerU local file is empty")
    if size > _MAX_AGENT_FILE_BYTES:
        raise MinerUError("MinerU Agent local file exceeds 10MB limit")


async def _iter_file_chunks(file_path: Path) -> AsyncIterator[bytes]:
    with file_path.open("rb") as file_obj:
        while True:
            chunk = await asyncio.to_thread(file_obj.read, _FILE_CHUNK_BYTES)
            if not chunk:
                return
            yield chunk


def _select_batch_entry(entries: list[Any], file_name: str) -> dict[str, Any] | None:
    if not entries:
        return None
    if any(not isinstance(entry, dict) for entry in entries):
        raise MinerUError("MinerU standard batch result contains an invalid entry")
    typed_entries = [entry for entry in entries if isinstance(entry, dict)]
    matches = [entry for entry in typed_entries if entry.get("file_name") == file_name]
    if matches:
        return matches[0]
    if len(typed_entries) == 1:
        return typed_entries[0]
    raise MinerUError(f"MinerU standard batch result missing file: {file_name}")


async def extract_from_mineru_url(
    file_url: str,
    *,
    file_name: str | None = None,
    config: MinerUConfig | None = None,
) -> list[Block]:
    """兼容旧调用：只返回项目 Block 列表。"""
    return (
        await extract_structured_from_mineru_url(
            file_url,
            file_name=file_name,
            config=config,
        )
    ).blocks


async def extract_structured_from_mineru_url(
    file_url: str,
    *,
    file_name: str | None = None,
    config: MinerUConfig | None = None,
) -> MinerUStructuredResult:
    """解析远程文件；精准模式同时返回稳定版面 JSON。"""
    client = MinerUClient(config)
    if client.config.mode == "standard":
        return await client.parse_standard_url(file_url)
    else:
        markdown = await client.parse_agent_url_to_markdown(file_url, file_name=file_name)
        return MinerUStructuredResult(markdown=markdown, blocks=markdown_to_blocks(markdown))


def markdown_from_result_zip(content: bytes) -> str:
    """兼容旧调用：安全读取结果归档中的 full.md。"""
    return structured_result_from_zip(content).markdown


def structured_result_from_zip(content: bytes) -> MinerUStructuredResult:
    """安全读取 full.md、middle/layout 与稳定版 content_list。"""
    if len(content) > _MAX_ZIP_BYTES:
        raise MinerUError("MinerU result zip exceeds size limit")
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except (zipfile.BadZipFile, OSError) as exc:
        raise MinerUError("MinerU result is not a valid zip archive") from exc

    with archive:
        members = archive.infolist()
        if len(members) > _MAX_ZIP_MEMBERS:
            raise MinerUError("MinerU result zip contains too many files")
        total_size = 0
        markdown_members: list[zipfile.ZipInfo] = []
        layout_members: list[tuple[int, zipfile.ZipInfo]] = []
        content_list_members: list[tuple[int, zipfile.ZipInfo]] = []
        for member in members:
            normalized = member.filename.replace("\\", "/")
            path = PurePosixPath(normalized)
            if path.is_absolute() or ".." in path.parts:
                raise MinerUError("MinerU result zip contains an unsafe path")
            if member.flag_bits & 0x1:
                raise MinerUError("MinerU result zip contains an encrypted file")
            if member.file_size > _MAX_MEMBER_BYTES:
                raise MinerUError("MinerU result zip member exceeds size limit")
            total_size += member.file_size
            if total_size > _MAX_UNCOMPRESSED_BYTES:
                raise MinerUError("MinerU result zip exceeds uncompressed size limit")
            if member.is_dir():
                continue
            if path.name == "full.md" and not member.is_dir():
                markdown_members.append(member)
                continue
            layout_priority = _layout_member_priority(path.name)
            if layout_priority is not None:
                layout_members.append((layout_priority, member))
                continue
            content_list_priority = _content_list_member_priority(path.name)
            if content_list_priority is not None:
                content_list_members.append((content_list_priority, member))

        if not markdown_members:
            raise MinerUError("MinerU result zip does not contain full.md")
        selected_markdown = _select_archive_member(markdown_members)
        if selected_markdown.file_size > _MAX_MARKDOWN_BYTES:
            raise MinerUError("MinerU full.md exceeds size limit")
        markdown = _read_text_member(archive, selected_markdown, "full.md")

        selected_layout = _select_prioritized_member(layout_members)
        selected_content_list = _select_prioritized_member(content_list_members)
        layout: dict[str, Any] | None = None
        content_list: list[dict[str, Any]] | None = None
        if selected_layout is not None:
            layout_data = _read_json_member(archive, selected_layout, "layout")
            _validate_layout_json(layout_data)
            layout = layout_data
        if selected_content_list is not None:
            content_list_data = _read_json_member(archive, selected_content_list, "content_list")
            _validate_content_list_json(content_list_data)
            content_list = content_list_data
        if layout is not None and content_list is not None:
            _validate_layout_content_pages(layout, content_list)

        return MinerUStructuredResult(
            markdown=markdown,
            blocks=markdown_to_blocks(markdown),
            layout=layout,
            content_list=content_list,
            layout_member=_normalized_member_name(selected_layout),
            content_list_member=_normalized_member_name(selected_content_list),
        )


def _layout_member_priority(name: str) -> int | None:
    if name == "layout.json":
        return 0
    if name == "middle.json":
        return 1
    if name.endswith("_middle.json"):
        return 2
    return None


def _content_list_member_priority(name: str) -> int | None:
    if name == "content_list_v2.json" or name.endswith("_content_list_v2.json"):
        return None
    if name == "content_list.json":
        return 0
    if name.endswith("_content_list.json"):
        return 1
    return None


def _select_archive_member(members: list[zipfile.ZipInfo]) -> zipfile.ZipInfo:
    return min(
        members,
        key=lambda item: (len(PurePosixPath(item.filename.replace("\\", "/")).parts), item.filename),
    )


def _select_prioritized_member(
    members: list[tuple[int, zipfile.ZipInfo]],
) -> zipfile.ZipInfo | None:
    if not members:
        return None
    _, member = min(
        members,
        key=lambda item: (
            item[0],
            len(PurePosixPath(item[1].filename.replace("\\", "/")).parts),
            item[1].filename,
        ),
    )
    return member


def _normalized_member_name(member: zipfile.ZipInfo | None) -> str | None:
    return member.filename.replace("\\", "/") if member is not None else None


def _read_text_member(
    archive: zipfile.ZipFile,
    member: zipfile.ZipInfo,
    label: str,
) -> str:
    try:
        return archive.read(member).decode("utf-8-sig")
    except (OSError, RuntimeError, UnicodeDecodeError, zipfile.BadZipFile) as exc:
        raise MinerUError(f"MinerU {label} could not be read as UTF-8") from exc


def _read_json_member(
    archive: zipfile.ZipFile,
    member: zipfile.ZipInfo,
    label: str,
) -> Any:
    if member.file_size > _MAX_JSON_BYTES:
        raise MinerUError(f"MinerU {label} JSON exceeds size limit")
    text = _read_text_member(archive, member, f"{label} JSON")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise MinerUError(f"MinerU {label} JSON is invalid") from exc


def _validate_layout_json(value: Any) -> None:
    if not isinstance(value, dict):
        raise MinerUError("MinerU layout JSON must be an object")
    pages = value.get("pdf_info")
    if not isinstance(pages, list):
        raise MinerUError("MinerU layout JSON missing pdf_info list")
    seen_pages: set[int] = set()
    for position, page in enumerate(pages):
        if not isinstance(page, dict):
            raise MinerUError(f"MinerU layout page {position} must be an object")
        page_idx = page.get("page_idx")
        if not _is_non_negative_int(page_idx):
            raise MinerUError(f"MinerU layout page {position} has invalid page_idx")
        if page_idx in seen_pages:
            raise MinerUError(f"MinerU layout JSON has duplicate page_idx: {page_idx}")
        seen_pages.add(page_idx)
        page_size = page.get("page_size")
        if (
            not isinstance(page_size, list)
            or len(page_size) != 2
            or any(not _is_finite_number(item) or item <= 0 for item in page_size)
        ):
            raise MinerUError(f"MinerU layout page {page_idx} has invalid page_size")
        if not isinstance(page.get("para_blocks"), list):
            raise MinerUError(f"MinerU layout page {page_idx} missing para_blocks list")
        for key in (
            "preproc_blocks",
            "images",
            "tables",
            "interline_equations",
            "discarded_blocks",
        ):
            if key in page and not isinstance(page[key], list):
                raise MinerUError(f"MinerU layout page {page_idx} has invalid {key}")
        _validate_geometry(
            page,
            f"layout page {page_idx}",
            page_width=page_size[0],
            page_height=page_size[1],
        )


def _validate_content_list_json(value: Any) -> None:
    if not isinstance(value, list):
        raise MinerUError("MinerU content_list JSON must be a list")
    previous_page = -1
    for position, item in enumerate(value):
        if not isinstance(item, dict):
            raise MinerUError(f"MinerU content_list item {position} must be an object")
        if not isinstance(item.get("type"), str) or not item["type"].strip():
            raise MinerUError(f"MinerU content_list item {position} has invalid type")
        if not _is_non_negative_int(item.get("page_idx")):
            raise MinerUError(f"MinerU content_list item {position} has invalid page_idx")
        page_idx = item["page_idx"]
        if page_idx < previous_page:
            raise MinerUError("MinerU content_list page order is not monotonic")
        previous_page = page_idx
        _validate_bbox(item.get("bbox"), f"content_list item {position}", maximum=1000)
        text_level = item.get("text_level")
        if text_level is not None and not _is_non_negative_int(text_level):
            raise MinerUError(f"MinerU content_list item {position} has invalid text_level")


def _validate_layout_content_pages(
    layout: dict[str, Any],
    content_list: list[dict[str, Any]],
) -> None:
    layout_pages = {page["page_idx"] for page in layout["pdf_info"]}
    if not layout_pages:
        return
    for position, item in enumerate(content_list):
        if item["page_idx"] not in layout_pages:
            raise MinerUError(
                f"MinerU content_list item {position} references missing layout page"
            )


def _validate_geometry(
    value: Any,
    label: str,
    *,
    page_width: float,
    page_height: float,
) -> None:
    if isinstance(value, dict):
        if "bbox" in value:
            _validate_bbox(
                value["bbox"],
                label,
                maximum_x=page_width,
                maximum_y=page_height,
            )
        if "layout_bbox" in value:
            _validate_bbox(
                value["layout_bbox"],
                label,
                maximum_x=page_width,
                maximum_y=page_height,
            )
        if "angle" in value:
            angle = value["angle"]
            if not _is_finite_number(angle) or angle not in {0, 90, 180, 270}:
                raise MinerUError(f"MinerU {label} has invalid angle")
        for child in value.values():
            _validate_geometry(
                child,
                label,
                page_width=page_width,
                page_height=page_height,
            )
    elif isinstance(value, list):
        for child in value:
            _validate_geometry(
                child,
                label,
                page_width=page_width,
                page_height=page_height,
            )


def _validate_bbox(
    value: Any,
    label: str,
    *,
    maximum: float | None = None,
    maximum_x: float | None = None,
    maximum_y: float | None = None,
) -> None:
    if (
        not isinstance(value, list)
        or len(value) != 4
        or any(not _is_finite_number(item) for item in value)
    ):
        raise MinerUError(f"MinerU {label} has invalid bbox")
    x0, y0, x1, y1 = value
    if x0 < 0 or y0 < 0 or x1 <= x0 or y1 <= y0:
        raise MinerUError(f"MinerU {label} has invalid bbox")
    if maximum is not None and any(item > maximum for item in value):
        raise MinerUError(f"MinerU {label} has invalid bbox")
    if maximum_x is not None and (x0 > maximum_x or x1 > maximum_x):
        raise MinerUError(f"MinerU {label} has invalid bbox")
    if maximum_y is not None and (y0 > maximum_y or y1 > maximum_y):
        raise MinerUError(f"MinerU {label} has invalid bbox")


def _is_finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _is_non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def markdown_to_blocks(markdown: str) -> list[Block]:
    """把 MinerU Markdown 粗切为项目现有 Block。

    轻量 API 只返回 Markdown；这里保守识别 heading/table/code/formula，其余作为 paragraph。
    """
    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[Block] = []
    paragraph: list[str] = []

    def add_block(type_: str, original: str, *, level: int | None = None, status: str = "pending") -> None:
        text = original.strip()
        if not text:
            return
        blocks.append(
            Block(
                index=len(blocks),
                type=type_,  # type: ignore[arg-type]
                original=text,
                level=level,
                status=status,  # type: ignore[arg-type]
            )
        )

    def flush_paragraph() -> None:
        nonlocal paragraph
        if paragraph:
            add_block("paragraph", " ".join(paragraph))
            paragraph = []

    i = 0
    while i < len(lines):
        raw = lines[i].rstrip()
        stripped = raw.strip()
        if not stripped:
            flush_paragraph()
            i += 1
            continue

        heading = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if heading:
            flush_paragraph()
            add_block("heading", heading.group(2), level=len(heading.group(1)))
            i += 1
            continue

        image = re.fullmatch(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+[^)]*)?\)", stripped)
        if image:
            flush_paragraph()
            label = image.group(1).strip() or image.group(2).strip()
            add_block("figure", label, status="skip")
            i += 1
            continue

        if stripped.startswith("```"):
            flush_paragraph()
            code_lines: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i].rstrip())
                i += 1
            if i < len(lines):
                i += 1
            add_block("code", "\n".join(code_lines), status="skip")
            continue

        if stripped.startswith("$$"):
            flush_paragraph()
            formula_lines: list[str] = []
            first = stripped[2:].strip()
            if first.endswith("$$"):
                formula_lines.append(first[:-2].strip())
                i += 1
            else:
                if first:
                    formula_lines.append(first)
                i += 1
                while i < len(lines):
                    current = lines[i].strip()
                    if current.endswith("$$"):
                        tail = current[:-2].strip()
                        if tail:
                            formula_lines.append(tail)
                        i += 1
                        break
                    formula_lines.append(lines[i].rstrip())
                    i += 1
            add_block("formula", "\n".join(formula_lines), status="skip")
            continue

        if _is_table_start(lines, i):
            flush_paragraph()
            table_lines: list[str] = []
            while i < len(lines) and lines[i].strip() and "|" in lines[i]:
                table_lines.append(lines[i].rstrip())
                i += 1
            add_block("table", "\n".join(table_lines), status="skip")
            continue

        paragraph.append(stripped)
        i += 1

    flush_paragraph()
    return blocks


def _is_table_start(lines: list[str], index: int) -> bool:
    if "|" not in lines[index]:
        return False
    if index + 1 >= len(lines):
        return False
    separator = lines[index + 1].strip()
    return bool(re.match(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?$", separator))
