from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from backend.extraction.source_pdf import SourcePdfError, download_source_pdf
from backend.extraction import pdf_mapping


class SourcePdfTest(unittest.TestCase):
    def test_default_client_ignores_environment_proxies(self) -> None:
        captured: dict[str, object] = {}

        class FakeAsyncClient:
            def __init__(self, **kwargs: object) -> None:
                captured.update(kwargs)

            async def __aenter__(self) -> "FakeAsyncClient":
                return self

            async def __aexit__(self, *_: object) -> None:
                return None

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "original.pdf"
            with (
                patch(
                    "backend.extraction.source_pdf.httpx.AsyncClient",
                    FakeAsyncClient,
                ),
                patch(
                    "backend.extraction.source_pdf._download_with_client",
                    new=AsyncMock(return_value=target),
                ) as download,
            ):
                result = asyncio.run(
                    download_source_pdf("https://papers.example/paper.pdf", target)
                )

        self.assertEqual(result, target)
        self.assertEqual(captured, {"timeout": 60.0, "trust_env": False})
        self.assertEqual(download.await_count, 1)

    def test_arxiv_pdf_download_uses_the_hardened_source_downloader(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paper_path = Path(tmp) / "1706.03762"
            target = paper_path / "original.pdf"
            with (
                patch.object(pdf_mapping, "paper_dir", return_value=paper_path),
                patch.object(pdf_mapping, "ensure_paper_dir", return_value=paper_path),
                patch.object(
                    pdf_mapping,
                    "download_source_pdf",
                    new=AsyncMock(return_value=target),
                ) as download,
            ):
                result = asyncio.run(pdf_mapping.ensure_pdf("1706.03762"))

        self.assertEqual(result, target)
        self.assertEqual(
            download.await_args.args[:2],
            ("https://arxiv.org/pdf/1706.03762.pdf", target),
        )

    def test_arxiv_pdf_download_client_ignores_environment_proxies(self) -> None:
        captured: dict[str, object] = {}

        class FakeAsyncClient:
            def __init__(self, **kwargs: object) -> None:
                captured.update(kwargs)

            async def __aenter__(self) -> "FakeAsyncClient":
                return self

            async def __aexit__(self, *_: object) -> None:
                return None

        with tempfile.TemporaryDirectory() as tmp:
            paper_path = Path(tmp) / "1706.03762"
            target = paper_path / "original.pdf"
            with (
                patch.object(pdf_mapping, "paper_dir", return_value=paper_path),
                patch.object(pdf_mapping, "ensure_paper_dir", return_value=paper_path),
                patch.object(pdf_mapping.httpx, "AsyncClient", FakeAsyncClient),
                patch.object(
                    pdf_mapping,
                    "download_source_pdf",
                    new=AsyncMock(return_value=target),
                ),
            ):
                result = asyncio.run(pdf_mapping.ensure_pdf("1706.03762"))

        self.assertEqual(result, target)
        self.assertEqual(captured, {"timeout": 45.0, "trust_env": False})

    def test_download_follows_validated_redirect_and_writes_atomically(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path == "/source":
                return httpx.Response(302, headers={"Location": "/paper.pdf"})
            return httpx.Response(200, content=b"prefix\n%PDF-1.7\nbody")

        async def run(target: Path) -> Path:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await download_source_pdf(
                    "https://papers.example/source",
                    target,
                    http_client=client,
                )

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "paper" / "original.pdf"
            with patch(
                "backend.extraction.source_pdf._resolve_hostname",
                new=AsyncMock(return_value=("93.184.216.34",)),
            ):
                result = asyncio.run(run(target))
            self.assertEqual(result, target)
            self.assertEqual(target.read_bytes(), b"prefix\n%PDF-1.7\nbody")
            self.assertFalse(list(target.parent.glob(".source-*.pdf")))

        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0].headers["user-agent"], "PeiNiDu/0.1")
        self.assertEqual(requests[0].headers["host"], "papers.example")
        self.assertEqual(requests[0].url.host, "93.184.216.34")
        self.assertEqual(requests[0].extensions["sni_hostname"], "papers.example")

    def test_download_rejects_non_pdf_and_removes_temporary_file(self) -> None:
        async def run(target: Path) -> None:
            transport = httpx.MockTransport(lambda _: httpx.Response(200, content=b"not a pdf"))
            async with httpx.AsyncClient(transport=transport) as client:
                await download_source_pdf(
                    "https://papers.example/not-pdf",
                    target,
                    http_client=client,
                )

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "original.pdf"
            with (
                patch(
                    "backend.extraction.source_pdf._resolve_hostname",
                    new=AsyncMock(return_value=("93.184.216.34",)),
                ),
                self.assertRaisesRegex(SourcePdfError, "not a PDF"),
            ):
                asyncio.run(run(target))
            self.assertFalse(target.exists())
            self.assertFalse(list(Path(tmp).glob(".source-*.pdf")))

    def test_download_rejects_private_addresses_before_request(self) -> None:
        async def run() -> None:
            transport = httpx.MockTransport(
                lambda _: self.fail("private URL must not reach the HTTP client")
            )
            async with httpx.AsyncClient(transport=transport) as client:
                await download_source_pdf(
                    "http://127.0.0.1/internal.pdf",
                    Path("/tmp/unused.pdf"),
                    http_client=client,
                )

        with self.assertRaisesRegex(SourcePdfError, "private address"):
            asyncio.run(run())

    def test_download_rejects_hostname_resolving_to_private_address(self) -> None:
        async def run() -> None:
            transport = httpx.MockTransport(
                lambda _: self.fail("private hostname must not reach the HTTP client")
            )
            async with httpx.AsyncClient(transport=transport) as client:
                await download_source_pdf(
                    "https://papers.example/internal.pdf",
                    Path("/tmp/unused.pdf"),
                    http_client=client,
                )

        with (
            patch(
                "backend.extraction.source_pdf._resolve_hostname",
                new=AsyncMock(return_value=("127.0.0.1",)),
            ),
            self.assertRaisesRegex(SourcePdfError, "private address"),
        ):
            asyncio.run(run())

    def test_download_rejects_invalid_port_for_hostname_or_ip_literal(self) -> None:
        async def run(url: str) -> None:
            transport = httpx.MockTransport(
                lambda _: self.fail("invalid URL must not reach the HTTP client")
            )
            async with httpx.AsyncClient(transport=transport) as client:
                await download_source_pdf(
                    url,
                    Path("/tmp/unused.pdf"),
                    http_client=client,
                )

        for url in (
            "https://papers.example:bad/paper.pdf",
            "https://8.8.8.8:bad/paper.pdf",
            "https://[2001:4860:4860::8888]:bad/paper.pdf",
        ):
            with self.subTest(url=url), self.assertRaisesRegex(
                SourcePdfError,
                "invalid port",
            ):
                asyncio.run(run(url))

    def test_download_enforces_stream_size_limit(self) -> None:
        async def run(target: Path) -> None:
            transport = httpx.MockTransport(
                lambda _: httpx.Response(200, content=b"%PDF-1.7\n0123456789")
            )
            async with httpx.AsyncClient(transport=transport) as client:
                await download_source_pdf(
                    "https://papers.example/large.pdf",
                    target,
                    http_client=client,
                    max_bytes=8,
                )

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "original.pdf"
            with (
                patch(
                    "backend.extraction.source_pdf._resolve_hostname",
                    new=AsyncMock(return_value=("93.184.216.34",)),
                ),
                self.assertRaisesRegex(SourcePdfError, "size limit"),
            ):
                asyncio.run(run(target))
            self.assertFalse(target.exists())

    def test_download_wraps_transport_failures(self) -> None:
        async def run(target: Path) -> None:
            def fail(request: httpx.Request) -> httpx.Response:
                raise httpx.ConnectError("offline", request=request)

            async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
                await download_source_pdf(
                    "https://papers.example/offline.pdf",
                    target,
                    http_client=client,
                )

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch(
                    "backend.extraction.source_pdf._resolve_hostname",
                    new=AsyncMock(return_value=("93.184.216.34",)),
                ),
                self.assertRaisesRegex(SourcePdfError, "network request failed"),
            ):
                asyncio.run(run(Path(tmp) / "original.pdf"))


if __name__ == "__main__":
    unittest.main()
