"""HTTP client for the isolated PDF translation sidecar."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .errors import PdfExportError

DEFAULT_SIDECAR_URL = "http://pdf-export:8090"


@dataclass(frozen=True)
class SidecarJob:
    status: str
    progress: float | None = None
    stage: str = ""
    pages_done: int | None = None
    error: str = ""


def sidecar_environment() -> tuple[str, str]:
    base_url = os.environ.get(
        "PEINIDU_PDF_EXPORT_SIDECAR_URL", DEFAULT_SIDECAR_URL
    ).strip().rstrip("/")
    token = (
        os.environ.get("PEINIDU_PDF_EXPORT_SIDECAR_TOKEN", "").strip()
        or os.environ.get("PEINIDU_PDF_EXPORT_INTERNAL_TOKEN", "").strip()
    )
    return base_url, token


class PdfExportSidecarClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        request_timeout: float = 120.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.request_timeout = request_timeout
        self.transport = transport

    @classmethod
    def from_environment(cls) -> "PdfExportSidecarClient":
        base_url, token = sidecar_environment()
        return cls(base_url, token)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "PeiNiDu-PdfExport/1.0",
        }

    async def health(self) -> None:
        response = await self._request("GET", "/health")
        payload = self._json_object(response)
        status = str(payload.get("status") or "").strip().lower()
        if status not in {"ok", "healthy"}:
            raise PdfExportError(
                "sidecar_unavailable", "PDF 导出服务健康检查失败。", retryable=True
            )

    async def info(self) -> dict[str, Any]:
        response = await self._request("GET", "/info")
        return self._json_object(response)

    async def create_job(self, source_path: Path, job_id: str) -> str:
        try:
            with source_path.open("rb") as source:
                async with httpx.AsyncClient(
                    timeout=self.request_timeout,
                    trust_env=False,
                    transport=self.transport,
                ) as client:
                    response = await client.post(
                        f"{self.base_url}/jobs",
                        headers=self._headers(),
                        data={"job_id": job_id},
                        files={"file": (source_path.name, source, "application/pdf")},
                    )
        except (OSError, httpx.RequestError, httpx.TimeoutException) as exc:
            raise PdfExportError(
                "sidecar_unavailable", "PDF 导出服务暂时不可用。", retryable=True
            ) from exc
        self._raise_for_status(response)
        payload = self._json_object(response)
        returned_job_id = str(payload.get("job_id") or "").strip()
        if returned_job_id != job_id:
            raise PdfExportError(
                "sidecar_unavailable", "PDF 导出服务返回了无效任务。", retryable=True
            )
        return returned_job_id

    async def get_job(self, job_id: str) -> SidecarJob:
        response = await self._request("GET", f"/jobs/{job_id}")
        payload = self._json_object(response)
        status = str(payload.get("status") or "").strip().lower()
        if status in {"failed", "error", "crashed"}:
            error = payload.get("error")
            if isinstance(error, dict):
                error_code = str(error.get("code") or "")
                message = str(error.get("message") or "PDF 导出服务执行失败。")
            else:
                error_code = ""
                message = str(error or "PDF 导出服务执行失败。")
            mapped_code = {
                "provider_authentication_failed": "sidecar_auth_failed",
                "provider_rate_limited": "sidecar_rate_limited",
                "provider_timeout": "export_timeout",
            }.get(error_code, "sidecar_crashed")
            raise PdfExportError(
                mapped_code,
                message,
                retryable=mapped_code != "sidecar_auth_failed",
            )
        if status not in {"queued", "running", "done", "cancelled"}:
            raise PdfExportError(
                "sidecar_unavailable", "PDF 导出服务返回了未知状态。", retryable=True
            )
        progress: float | None = None
        raw_progress = payload.get("progress")
        if raw_progress is not None:
            try:
                progress = min(1.0, max(0.0, float(raw_progress)))
            except (TypeError, ValueError):
                progress = None
        pages_done: int | None = None
        raw_pages_done = payload.get("pages_done")
        if raw_pages_done is not None:
            try:
                parsed_pages_done = int(raw_pages_done)
                if parsed_pages_done >= 0:
                    pages_done = parsed_pages_done
            except (TypeError, ValueError):
                pages_done = None
        return SidecarJob(
            status=status,
            progress=progress,
            stage=str(payload.get("stage") or "").strip(),
            pages_done=pages_done,
            error=str(payload.get("error") or ""),
        )

    async def cancel_job(self, job_id: str) -> None:
        await self._request("POST", f"/jobs/{job_id}/cancel", allow_not_found=True)

    async def delete_job(self, job_id: str) -> None:
        await self._request("DELETE", f"/jobs/{job_id}", allow_not_found=True)

    async def download_output(
        self,
        job_id: str,
        target: Path,
        *,
        max_bytes: int,
    ) -> Path:
        total = 0
        try:
            async with httpx.AsyncClient(
                timeout=self.request_timeout,
                trust_env=False,
                transport=self.transport,
            ) as client:
                async with client.stream(
                    "GET",
                    f"{self.base_url}/jobs/{job_id}/download",
                    headers=self._headers(),
                ) as response:
                    self._raise_for_status(response)
                    declared = response.headers.get("content-length")
                    if declared:
                        try:
                            if int(declared) > max_bytes:
                                raise PdfExportError(
                                    "output_validation_failed",
                                    "导出 PDF 超过大小限制。",
                                )
                        except ValueError as exc:
                            raise PdfExportError(
                                "output_validation_failed",
                                "导出服务返回了无效文件长度。",
                            ) from exc
                    with target.open("wb") as output:
                        async for chunk in response.aiter_bytes():
                            if not chunk:
                                continue
                            total += len(chunk)
                            if total > max_bytes:
                                raise PdfExportError(
                                    "output_validation_failed",
                                    "导出 PDF 超过大小限制。",
                                )
                            output.write(chunk)
        except PdfExportError:
            raise
        except (OSError, httpx.RequestError, httpx.TimeoutException) as exc:
            raise PdfExportError(
                "sidecar_unavailable", "无法下载导出 PDF。", retryable=True
            ) from exc
        if total <= 0:
            raise PdfExportError("output_validation_failed", "导出 PDF 为空。")
        return target

    async def _request(
        self,
        method: str,
        path: str,
        *,
        allow_not_found: bool = False,
    ) -> httpx.Response:
        try:
            async with httpx.AsyncClient(
                timeout=self.request_timeout,
                trust_env=False,
                transport=self.transport,
            ) as client:
                response = await client.request(
                    method, f"{self.base_url}{path}", headers=self._headers()
                )
        except (httpx.RequestError, httpx.TimeoutException) as exc:
            raise PdfExportError(
                "sidecar_unavailable", "PDF 导出服务暂时不可用。", retryable=True
            ) from exc
        if allow_not_found and response.status_code == 404:
            return response
        self._raise_for_status(response)
        return response

    @staticmethod
    def _json_object(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise PdfExportError(
                "sidecar_unavailable", "PDF 导出服务返回了无效响应。", retryable=True
            ) from exc
        if not isinstance(payload, dict):
            raise PdfExportError(
                "sidecar_unavailable", "PDF 导出服务返回了无效响应。", retryable=True
            )
        return payload

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        if response.status_code in {401, 403}:
            raise PdfExportError(
                "sidecar_auth_failed", "PDF 导出服务鉴权失败。"
            )
        if response.status_code == 429:
            raise PdfExportError(
                "sidecar_rate_limited", "PDF 导出服务当前繁忙，请稍后重试。", retryable=True
            )
        if response.status_code == 413:
            raise PdfExportError(
                "source_pdf_too_large", "原始 PDF 超过导出大小限制。"
            )
        raise PdfExportError(
            "sidecar_unavailable",
            f"PDF 导出服务返回 HTTP {response.status_code}。",
            retryable=True,
        )
