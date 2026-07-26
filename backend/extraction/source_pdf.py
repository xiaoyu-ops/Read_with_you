"""Safe persistence for remote source PDFs used by layout adapters."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

import httpx


SOURCE_PDF_MAX_BYTES = 200 * 1024 * 1024
SOURCE_PDF_MAX_REDIRECTS = 5


class SourcePdfError(RuntimeError):
    """The remote source could not be persisted as a PDF."""


@dataclass(frozen=True)
class _ResolvedRequestTarget:
    url: str
    host_header: str
    sni_hostname: str | None


async def download_source_pdf(
    url: str,
    target: Path,
    *,
    http_client: httpx.AsyncClient | None = None,
    max_bytes: int = SOURCE_PDF_MAX_BYTES,
) -> Path:
    """Download *url* atomically after validating each redirect and the PDF header."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if await asyncio.to_thread(_existing_pdf_is_valid, target, max_bytes):
        return target

    if http_client is not None:
        return await _download_with_client(http_client, url, target, max_bytes=max_bytes)

    # The downloader resolves and validates every target address itself before
    # connecting to that pinned IP. Environment proxies would instead receive
    # the IP URL, bypass the pinned connection, and validate TLS against the IP
    # rather than the original hostname. Keep this security boundary direct.
    async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
        return await _download_with_client(client, url, target, max_bytes=max_bytes)


async def _download_with_client(
    client: httpx.AsyncClient,
    url: str,
    target: Path,
    *,
    max_bytes: int,
) -> Path:
    current_url = url
    for redirect_count in range(SOURCE_PDF_MAX_REDIRECTS + 1):
        request_target = await _resolved_request_target(current_url)
        headers = {
            "Host": request_target.host_header,
            "User-Agent": "PeiNiDu/0.1",
        }
        extensions = (
            {"sni_hostname": request_target.sni_hostname}
            if request_target.sni_hostname is not None
            else None
        )
        try:
            async with client.stream(
                "GET",
                request_target.url,
                follow_redirects=False,
                headers=headers,
                extensions=extensions,
            ) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise SourcePdfError("PDF source redirect is missing Location")
                    if redirect_count >= SOURCE_PDF_MAX_REDIRECTS:
                        raise SourcePdfError("PDF source exceeded redirect limit")
                    current_url = urljoin(current_url, location)
                    continue

                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise SourcePdfError(
                        f"PDF source returned HTTP {response.status_code}"
                    ) from exc

                content_length = response.headers.get("content-length")
                if content_length:
                    try:
                        declared_size = int(content_length)
                    except ValueError as exc:
                        raise SourcePdfError(
                            "PDF source returned invalid Content-Length"
                        ) from exc
                    if declared_size < 0:
                        raise SourcePdfError("PDF source returned invalid Content-Length")
                    if declared_size > max_bytes:
                        raise SourcePdfError("PDF source exceeds size limit")

                return await _write_pdf_stream(response, target, max_bytes=max_bytes)
        except httpx.TimeoutException as exc:
            raise SourcePdfError("PDF source request timed out") from exc
        except httpx.RequestError as exc:
            raise SourcePdfError("PDF source network request failed") from exc

    raise SourcePdfError("PDF source exceeded redirect limit")


async def _write_pdf_stream(
    response: httpx.Response,
    target: Path,
    *,
    max_bytes: int,
) -> Path:
    fd, temp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".source-", suffix=".pdf")
    temp_path = Path(temp_name)
    total = 0
    header = bytearray()
    try:
        with os.fdopen(fd, "wb") as output:
            async for chunk in response.aiter_bytes():
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise SourcePdfError("PDF source exceeds size limit")
                if len(header) < 1024:
                    header.extend(chunk[: 1024 - len(header)])
                await asyncio.to_thread(output.write, chunk)
            await asyncio.to_thread(_flush_and_fsync, output)
        if total == 0:
            raise SourcePdfError("PDF source is empty")
        if not _has_pdf_header(bytes(header)):
            raise SourcePdfError("PDF source is not a PDF file")
        os.replace(temp_path, target)
        return target
    except Exception:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def _flush_and_fsync(output) -> None:
    output.flush()
    os.fsync(output.fileno())


def _has_pdf_header(data: bytes) -> bool:
    return b"%PDF-" in data[:1024]


def _existing_pdf_is_valid(path: Path, max_bytes: int) -> bool:
    try:
        size = path.stat().st_size
        if size <= 0 or size > max_bytes:
            return False
        with path.open("rb") as handle:
            return _has_pdf_header(handle.read(1024))
    except OSError:
        return False


async def _resolved_request_target(url: str) -> _ResolvedRequestTarget:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SourcePdfError("PDF source must use http or https")
    if parsed.username or parsed.password:
        raise SourcePdfError("PDF source URL cannot contain credentials")
    try:
        explicit_port = parsed.port
    except ValueError as exc:
        raise SourcePdfError("PDF source URL has an invalid port") from exc
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise SourcePdfError("PDF source cannot use a local address")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        port = explicit_port or (443 if parsed.scheme == "https" else 80)
        ascii_hostname = hostname.encode("idna").decode("ascii")
        addresses = await _resolve_hostname(ascii_hostname, port)
        sni_hostname = ascii_hostname if parsed.scheme == "https" else None
        host_header_name = ascii_hostname
    else:
        addresses = (str(address),)
        sni_hostname = str(address) if parsed.scheme == "https" else None
        host_header_name = f"[{address}]" if address.version == 6 else str(address)
    if any(not ipaddress.ip_address(value).is_global for value in addresses):
        raise SourcePdfError("PDF source cannot use a private address")
    selected = ipaddress.ip_address(addresses[0])
    request_host = f"[{selected}]" if selected.version == 6 else str(selected)
    request_netloc = (
        f"{request_host}:{explicit_port}" if explicit_port is not None else request_host
    )
    host_header = (
        f"{host_header_name}:{explicit_port}"
        if explicit_port is not None
        else host_header_name
    )
    request_url = urlunparse(parsed._replace(netloc=request_netloc))
    return _ResolvedRequestTarget(
        url=request_url,
        host_header=host_header,
        sni_hostname=sni_hostname,
    )


async def _resolve_hostname(hostname: str, port: int) -> tuple[str, ...]:
    try:
        records = await asyncio.get_running_loop().getaddrinfo(
            hostname,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise SourcePdfError("PDF source hostname could not be resolved") from exc
    addresses = tuple(sorted({str(record[4][0]) for record in records}))
    if not addresses:
        raise SourcePdfError("PDF source hostname returned no addresses")
    return addresses
