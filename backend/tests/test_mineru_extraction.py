from __future__ import annotations

import asyncio
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException
from starlette.datastructures import UploadFile

from backend.api import routes_papers
from backend.extraction.blocks import Block, PaperDocument
from backend.extraction.extract import extract_paper
from backend.extraction.local_pdf import LocalPdfExtractionError, text_to_blocks
from backend.extraction.mineru import (
    MinerUAuthError,
    MinerUClient,
    MinerUError,
    MinerUStructuredResult,
    MinerUTaskFailed,
    MinerUTaskTimeout,
    extract_from_mineru_url,
    markdown_from_result_zip,
    markdown_to_blocks,
    structured_result_from_zip,
)
from backend.llm.models import MinerUConfig


class MinerUExtractionTest(unittest.TestCase):
    @staticmethod
    def _result_zip(files: dict[str, str]) -> bytes:
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, text in files.items():
                archive.writestr(name, text)
        return output.getvalue()

    @staticmethod
    def _layout_json(*, bbox: list[int] | None = None, angle: int = 0) -> str:
        block_bbox = bbox or [40, 60, 300, 90]
        return json.dumps(
            {
                "pdf_info": [
                    {
                        "page_idx": 0,
                        "page_size": [612, 792],
                        "para_blocks": [
                            {
                                "type": "title",
                                "bbox": block_bbox,
                                "angle": angle,
                                "lines": [],
                            }
                        ],
                        "discarded_blocks": [],
                    }
                ],
                "_backend": "vlm",
                "_version_name": "test",
            }
        )

    @staticmethod
    def _content_list_json(*, bbox: list[int] | None = None, page_idx: int = 0) -> str:
        return json.dumps(
            [
                {
                    "type": "text",
                    "text": "Precise title",
                    "text_level": 1,
                    "bbox": bbox or [65, 75, 490, 115],
                    "page_idx": page_idx,
                }
            ]
        )

    def test_markdown_to_blocks_maps_basic_markdown(self) -> None:
        blocks = markdown_to_blocks(
            """
# Paper Title

This is the first paragraph
continued on the next line.

| A | B |
| --- | --- |
| 1 | 2 |

$$
a=b
$$

```python
print("hello")
```
""".strip()
        )

        self.assertEqual([b.type for b in blocks], ["heading", "paragraph", "table", "formula", "code"])
        self.assertEqual(blocks[0].level, 1)
        self.assertEqual(blocks[1].original, "This is the first paragraph continued on the next line.")
        self.assertEqual(blocks[2].status, "skip")
        self.assertEqual(blocks[3].original, "a=b")
        self.assertEqual([b.index for b in blocks], list(range(len(blocks))))

    def test_markdown_to_blocks_keeps_standalone_image_out_of_translation(self) -> None:
        blocks = markdown_to_blocks(
            """
![](images/figure-1.jpg)

Figure 1: A reliable caption.
""".strip()
        )

        self.assertEqual([block.type for block in blocks], ["figure", "paragraph"])
        self.assertEqual(blocks[0].original, "images/figure-1.jpg")
        self.assertEqual(blocks[0].status, "skip")
        self.assertEqual(blocks[1].original, "Figure 1: A reliable caption.")

    def test_local_pdf_text_to_blocks_maps_plain_text(self) -> None:
        blocks = text_to_blocks(
            """
1 Introduction

This is the first paragraph
continued on the next line.

Conclusion

Final paragraph.
""".strip()
        )

        self.assertEqual([b.type for b in blocks], ["heading", "paragraph", "heading", "paragraph"])
        self.assertEqual(blocks[0].original, "1 Introduction")
        self.assertEqual(blocks[1].original, "This is the first paragraph continued on the next line.")
        self.assertEqual([b.index for b in blocks], list(range(len(blocks))))

    def test_paper_document_round_trips_optional_source_page_range(self) -> None:
        document = PaperDocument(
            paper_id="partial-paper",
            title="Partial paper",
            source="mineru",
            extracted_at="2026-07-21T00:00:00Z",
            blocks=[Block(index=0, type="heading", original="Title")],
            source_page_range="2-4",
        )

        payload = document.to_dict()
        restored = PaperDocument.from_dict(payload)
        legacy = PaperDocument.from_dict(
            {key: value for key, value in payload.items() if key != "source_page_range"}
        )

        self.assertEqual(payload["source_page_range"], "2-4")
        self.assertEqual(restored.source_page_range, "2-4")
        self.assertIsNone(legacy.source_page_range)

    def test_partial_document_quality_is_explicitly_scoped(self) -> None:
        document = PaperDocument(
            paper_id="partial-paper",
            title="Partial paper",
            source="mineru",
            extracted_at="2026-07-21T00:00:00Z",
            blocks=[
                Block(index=index, type="paragraph", original=f"Paragraph {index}")
                for index in range(3)
            ],
            source_page_range="2-4",
        )

        with (
            patch.object(routes_papers, "save_document"),
            patch.object(routes_papers, "save_extraction_quality") as save_quality,
        ):
            routes_papers._save_document_with_quality(document, "mineru")

        report = save_quality.call_args.args[1]
        self.assertEqual(report["document_scope"], "partial")
        self.assertFalse(report["complete_document"])
        self.assertEqual(report["source_page_range"], "2-4")
        self.assertIn("partial_page_range", [item["code"] for item in report["findings"]])

    def test_local_pdf_layout_requires_full_ocr_for_blank_or_rotated_page(self) -> None:
        safe_page = SimpleNamespace(blocks=(object(),), rotation=0)
        blank_page = SimpleNamespace(blocks=(), rotation=0)
        rotated_page = SimpleNamespace(blocks=(object(),), rotation=90)

        self.assertFalse(
            routes_papers._local_pdf_requires_full_ocr(
                SimpleNamespace(pages=(safe_page,))
            )
        )
        self.assertTrue(
            routes_papers._local_pdf_requires_full_ocr(
                SimpleNamespace(pages=(safe_page, blank_page))
            )
        )
        self.assertTrue(
            routes_papers._local_pdf_requires_full_ocr(
                SimpleNamespace(pages=(rotated_page,))
            )
        )

    def test_agent_url_parse_polls_and_downloads_markdown(self) -> None:
        calls = {"poll": 0, "payload": {}}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST" and str(request.url) == "https://mineru.test/api/v1/agent/parse/url":
                calls["payload"] = json.loads(request.content.decode("utf-8"))
                return httpx.Response(200, json={"code": 0, "msg": "ok", "data": {"task_id": "task-1"}})
            if request.method == "GET" and str(request.url) == "https://mineru.test/api/v1/agent/parse/task-1":
                calls["poll"] += 1
                if calls["poll"] == 1:
                    return httpx.Response(200, json={"code": 0, "msg": "ok", "data": {"state": "running"}})
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "msg": "ok",
                        "data": {"state": "done", "markdown_url": "https://cdn-mineru.test/full.md"},
                    },
                )
            if request.method == "GET" and str(request.url) == "https://cdn-mineru.test/full.md":
                return httpx.Response(200, text="# Done\n\nParsed text.")
            return httpx.Response(404)

        async def run() -> str:
            config = MinerUConfig(
                base_url="https://mineru.test",
                poll_interval_seconds=0,
                max_wait_seconds=1,
                language="en",
                page_range="1-2",
            )
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
                client = MinerUClient(config, http_client)
                return await client.parse_agent_url_to_markdown("https://example.com/paper.pdf", file_name="paper.pdf")

        markdown = asyncio.run(run())

        self.assertEqual(markdown, "# Done\n\nParsed text.")
        self.assertEqual(calls["poll"], 2)
        self.assertEqual(calls["payload"]["file_name"], "paper.pdf")
        self.assertEqual(calls["payload"]["page_range"], "1-2")

    def test_extract_from_mineru_url_returns_blocks(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(200, json={"code": 0, "msg": "ok", "data": {"task_id": "task-1"}})
            if str(request.url).endswith("/api/v1/agent/parse/task-1"):
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "msg": "ok",
                        "data": {"state": "done", "markdown_url": "https://cdn-mineru.test/full.md"},
                    },
                )
            return httpx.Response(200, text="# Title\n\nBody.")

        async def run():
            config = MinerUConfig(base_url="https://mineru.test", poll_interval_seconds=0)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
                client = MinerUClient(config, http_client)
                markdown = await client.parse_agent_url_to_markdown("https://example.com/paper.pdf")
                return markdown_to_blocks(markdown)

        blocks = asyncio.run(run())

        self.assertEqual([b.type for b in blocks], ["heading", "paragraph"])

    def test_agent_failed_task_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(200, json={"code": 0, "msg": "ok", "data": {"task_id": "task-1"}})
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "msg": "ok",
                    "data": {"state": "failed", "err_code": -30003, "err_msg": "page limit"},
                },
            )

        async def run() -> None:
            config = MinerUConfig(base_url="https://mineru.test", poll_interval_seconds=0)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
                client = MinerUClient(config, http_client)
                await client.parse_agent_url_to_markdown("https://example.com/paper.pdf")

        with self.assertRaises(MinerUTaskFailed):
            asyncio.run(run())

    def test_standard_api_requires_token(self) -> None:
        async def run() -> None:
            client = MinerUClient(MinerUConfig(base_url="https://mineru.test"))
            await client.create_standard_extract_task("https://example.com/paper.pdf")

        with self.assertRaises(MinerUAuthError):
            asyncio.run(run())

    def test_standard_api_auth_failure_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"code": 401, "msg": "unauthorized"})

        async def run() -> None:
            config = MinerUConfig(base_url="https://mineru.test", api_token="test-token")
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
                client = MinerUClient(config, http_client)
                await client.create_standard_extract_task("https://example.com/paper.pdf")

        with self.assertRaises(MinerUAuthError):
            asyncio.run(run())

    def test_transport_failure_is_wrapped_as_mineru_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        async def run() -> None:
            config = MinerUConfig(base_url="https://mineru.test")
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
                await MinerUClient(config, http_client).parse_agent_url_to_markdown(
                    "https://example.com/paper.pdf"
                )

        with self.assertRaisesRegex(MinerUError, "network request failed"):
            asyncio.run(run())

    def test_transport_timeout_is_wrapped_as_mineru_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("read timed out", request=request)

        async def run() -> None:
            config = MinerUConfig(base_url="https://mineru.test")
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
                await MinerUClient(config, http_client).parse_agent_url_to_markdown(
                    "https://example.com/paper.pdf"
                )

        with self.assertRaisesRegex(MinerUError, "request timed out"):
            asyncio.run(run())

    def test_standard_url_parse_polls_and_consumes_result_zip(self) -> None:
        calls = {"poll": 0, "payload": {}}
        result_zip = self._result_zip(
            {
                "images/figure.png": "not-an-image",
                "nested/full.md": "# Nested fallback",
                "full.md": "# Precise title\n\nParsed body.",
            }
        )

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if request.method == "POST" and url == "https://mineru.test/api/v4/extract/task":
                calls["payload"] = json.loads(request.content.decode("utf-8"))
                return httpx.Response(200, json={"code": 0, "data": {"task_id": "standard-1"}})
            if request.method == "GET" and url == "https://mineru.test/api/v4/extract/task/standard-1":
                calls["poll"] += 1
                if calls["poll"] == 1:
                    return httpx.Response(200, json={"code": 0, "data": {"state": "converting"}})
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {"state": "done", "full_zip_url": "https://cdn-mineru.test/result.zip"},
                    },
                )
            if request.method == "GET" and url == "https://cdn-mineru.test/result.zip":
                return httpx.Response(200, content=result_zip)
            return httpx.Response(404)

        async def run() -> list[Block]:
            config = MinerUConfig(
                enabled=True,
                base_url="https://mineru.test",
                mode="standard",
                api_token="test-token",
                language="en",
                page_range="2,4-6",
                enable_table=False,
                is_ocr=True,
                poll_interval_seconds=0,
                max_wait_seconds=1,
            )
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
                client = MinerUClient(config, http_client)
                with patch("backend.extraction.mineru.MinerUClient", return_value=client):
                    return await extract_from_mineru_url("https://example.com/non-arxiv.pdf", config=config)

        blocks = asyncio.run(run())

        self.assertEqual([block.type for block in blocks], ["heading", "paragraph"])
        self.assertEqual(blocks[0].original, "Precise title")
        self.assertEqual(calls["poll"], 2)
        self.assertEqual(calls["payload"]["model_version"], "vlm")
        self.assertEqual(calls["payload"]["page_ranges"], "2,4-6")
        self.assertTrue(calls["payload"]["is_ocr"])
        self.assertFalse(calls["payload"]["enable_table"])

    def test_standard_result_zip_returns_stable_structured_artifacts(self) -> None:
        result = structured_result_from_zip(
            self._result_zip(
                {
                    "full.md": "# Precise title\n\nParsed body.",
                    "layout.json": self._layout_json(),
                    "paper_content_list.json": self._content_list_json(),
                    # V2 remains a development format and must not be parsed.
                    "paper_content_list_v2.json": "{not-valid-json",
                }
            )
        )

        self.assertIsInstance(result, MinerUStructuredResult)
        self.assertEqual(result.layout_member, "layout.json")
        self.assertEqual(result.content_list_member, "paper_content_list.json")
        self.assertEqual(result.layout["pdf_info"][0]["page_size"], [612, 792])
        self.assertEqual(result.content_list[0]["page_idx"], 0)
        self.assertEqual([block.type for block in result.blocks], ["heading", "paragraph"])

    def test_standard_result_zip_accepts_prefixed_middle_json(self) -> None:
        result = structured_result_from_zip(
            self._result_zip(
                {
                    "full.md": "# Title",
                    "nested/paper_middle.json": self._layout_json(),
                    "nested/content_list.json": self._content_list_json(),
                }
            )
        )

        self.assertEqual(result.layout_member, "nested/paper_middle.json")
        self.assertEqual(result.content_list_member, "nested/content_list.json")

    def test_standard_result_zip_rejects_invalid_layout_schema(self) -> None:
        result_zip = self._result_zip(
            {
                "full.md": "body",
                "layout.json": self._layout_json(angle=45),
            }
        )

        with self.assertRaisesRegex(MinerUError, "invalid angle"):
            structured_result_from_zip(result_zip)

    def test_standard_result_zip_rejects_invalid_layout_bbox(self) -> None:
        layout = json.loads(self._layout_json())
        layout["pdf_info"][0]["layout_bboxes"] = [
            {"layout_bbox": [0, 0, 613, 20]}
        ]
        result_zip = self._result_zip(
            {
                "full.md": "body",
                "middle.json": json.dumps(layout),
            }
        )

        with self.assertRaisesRegex(MinerUError, "invalid bbox"):
            structured_result_from_zip(result_zip)

    def test_standard_result_zip_rejects_invalid_content_list_schema(self) -> None:
        result_zip = self._result_zip(
            {
                "full.md": "body",
                "layout.json": self._layout_json(),
                "content_list.json": self._content_list_json(bbox=[0, 0, 1001, 20]),
            }
        )

        with self.assertRaisesRegex(MinerUError, "invalid bbox"):
            structured_result_from_zip(result_zip)

    def test_standard_result_zip_rejects_malformed_authoritative_json(self) -> None:
        for member_name in ("layout.json", "content_list.json"):
            with self.subTest(member_name=member_name):
                result_zip = self._result_zip(
                    {
                        "full.md": "body",
                        member_name: "{not-valid-json",
                    }
                )
                with self.assertRaisesRegex(MinerUError, "JSON is invalid"):
                    structured_result_from_zip(result_zip)

    def test_standard_result_zip_rejects_content_page_without_layout_page(self) -> None:
        result_zip = self._result_zip(
            {
                "full.md": "body",
                "layout.json": self._layout_json(),
                "content_list.json": self._content_list_json(page_idx=1),
            }
        )

        with self.assertRaisesRegex(MinerUError, "missing layout page"):
            structured_result_from_zip(result_zip)

    def test_standard_result_zip_rejects_content_page_order_regression(self) -> None:
        layout = json.loads(self._layout_json())
        second_page = json.loads(json.dumps(layout["pdf_info"][0]))
        second_page["page_idx"] = 1
        layout["pdf_info"].append(second_page)
        content = json.loads(self._content_list_json(page_idx=1))
        content.extend(json.loads(self._content_list_json(page_idx=0)))
        result_zip = self._result_zip(
            {
                "full.md": "body",
                "layout.json": json.dumps(layout),
                "content_list.json": json.dumps(content),
            }
        )

        with self.assertRaisesRegex(MinerUError, "page order"):
            structured_result_from_zip(result_zip)

    def test_standard_result_zip_enforces_member_size_limit(self) -> None:
        result_zip = self._result_zip({"full.md": "body"})
        with (
            patch("backend.extraction.mineru._MAX_MEMBER_BYTES", 2),
            self.assertRaisesRegex(MinerUError, "member exceeds size limit"),
        ):
            structured_result_from_zip(result_zip)

    def test_standard_result_zip_rejects_too_many_members(self) -> None:
        result_zip = self._result_zip({"full.md": "body", "extra.txt": "extra"})
        with (
            patch("backend.extraction.mineru._MAX_ZIP_MEMBERS", 1),
            self.assertRaisesRegex(MinerUError, "too many files"),
        ):
            structured_result_from_zip(result_zip)

    def test_standard_result_zip_rejects_invalid_utf8(self) -> None:
        result_zip = self._result_zip({"full.md": b"\xff\xfe"})
        with self.assertRaisesRegex(MinerUError, "UTF-8"):
            structured_result_from_zip(result_zip)

    def test_standard_result_zip_rejects_encrypted_member_flag(self) -> None:
        result_zip = self._result_zip({"full.md": "body"})
        original_infolist = zipfile.ZipFile.infolist

        def encrypted_infolist(archive: zipfile.ZipFile):
            members = original_infolist(archive)
            members[0].flag_bits |= 0x1
            return members

        with (
            patch.object(zipfile.ZipFile, "infolist", encrypted_infolist),
            self.assertRaisesRegex(MinerUError, "encrypted file"),
        ):
            structured_result_from_zip(result_zip)

    def test_standard_local_file_upload_polls_and_returns_structure(self) -> None:
        calls: dict[str, object] = {
            "poll": 0,
            "payload": {},
            "upload": b"",
            "upload_auth": None,
            "upload_length": None,
            "upload_type": None,
            "upload_transfer": None,
        }
        result_zip = self._result_zip(
            {
                "full.md": "# Uploaded title\n\nUploaded body.",
                "layout.json": self._layout_json(),
                "content_list.json": self._content_list_json(),
            }
        )

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if request.method == "POST" and url == "https://mineru.test/api/v4/file-urls/batch":
                calls["payload"] = json.loads(request.content.decode("utf-8"))
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "batch_id": "batch-1",
                            "file_urls": ["https://upload-mineru.test/signed"],
                        },
                    },
                )
            if request.method == "PUT" and url == "https://upload-mineru.test/signed":
                calls["upload"] = request.content
                calls["upload_auth"] = request.headers.get("Authorization")
                calls["upload_length"] = request.headers.get("Content-Length")
                calls["upload_type"] = request.headers.get("Content-Type")
                calls["upload_transfer"] = request.headers.get("Transfer-Encoding")
                return httpx.Response(200)
            if request.method == "GET" and url == "https://mineru.test/api/v4/extract-results/batch/batch-1":
                calls["poll"] = int(calls["poll"]) + 1
                state = "running" if calls["poll"] == 1 else "done"
                entry = {"file_name": "scan.pdf", "state": state, "err_msg": ""}
                if state == "done":
                    entry["full_zip_url"] = "https://cdn-mineru.test/uploaded.zip"
                return httpx.Response(
                    200,
                    json={"code": 0, "data": {"batch_id": "batch-1", "extract_result": [entry]}},
                )
            if request.method == "GET" and url == "https://cdn-mineru.test/uploaded.zip":
                return httpx.Response(200, content=result_zip)
            return httpx.Response(404)

        async def run(file_path: Path) -> MinerUStructuredResult:
            config = MinerUConfig(
                enabled=True,
                base_url="https://mineru.test",
                mode="standard",
                api_token="test-token",
                language="en",
                page_range="1-2",
                is_ocr=True,
                enable_table=False,
                poll_interval_seconds=0,
                max_wait_seconds=1,
            )
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
                return await MinerUClient(config, http_client).parse_standard_file(file_path)

        with tempfile.TemporaryDirectory() as tmp:
            file_path = Path(tmp) / "scan.pdf"
            file_bytes = b"%PDF-1.4\nscan"
            file_path.write_bytes(file_bytes)
            result = asyncio.run(run(file_path))

        payload = calls["payload"]
        self.assertEqual(
            payload["files"],
            [{"name": "scan.pdf", "is_ocr": True, "page_ranges": "1-2"}],
        )
        self.assertEqual(payload["model_version"], "vlm")
        self.assertFalse(payload["enable_table"])
        self.assertEqual(calls["upload"], file_bytes)
        self.assertIsNone(calls["upload_auth"])
        self.assertEqual(calls["upload_length"], str(len(file_bytes)))
        self.assertIsNone(calls["upload_type"])
        self.assertIsNone(calls["upload_transfer"])
        self.assertEqual(calls["poll"], 2)
        self.assertEqual(result.blocks[0].original, "Uploaded title")
        self.assertIsNotNone(result.layout)

    def test_agent_local_file_upload_parses_the_exact_persisted_bytes(self) -> None:
        calls: dict[str, object] = {
            "payload": {},
            "upload": b"",
            "upload_auth": None,
            "upload_length": None,
            "upload_type": None,
            "upload_transfer": None,
        }

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if request.method == "POST" and url == "https://mineru.test/api/v1/agent/parse/file":
                calls["payload"] = json.loads(request.content.decode("utf-8"))
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "task_id": "agent-file-1",
                            "file_url": "https://upload-mineru.test/signed",
                        },
                    },
                )
            if request.method == "PUT" and url == "https://upload-mineru.test/signed":
                calls["upload"] = request.content
                calls["upload_auth"] = request.headers.get("Authorization")
                calls["upload_length"] = request.headers.get("Content-Length")
                calls["upload_type"] = request.headers.get("Content-Type")
                calls["upload_transfer"] = request.headers.get("Transfer-Encoding")
                return httpx.Response(200)
            if (
                request.method == "GET"
                and url == "https://mineru.test/api/v1/agent/parse/agent-file-1"
            ):
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "task_id": "agent-file-1",
                            "state": "done",
                            "markdown_url": "https://cdn-mineru.test/full.md",
                        },
                    },
                )
            if request.method == "GET" and url == "https://cdn-mineru.test/full.md":
                return httpx.Response(200, text="# Uploaded exact source")
            return httpx.Response(404)

        async def run(file_path: Path) -> str:
            config = MinerUConfig(
                enabled=True,
                base_url="https://mineru.test",
                mode="agent_lite",
                page_range="1-2",
                poll_interval_seconds=0,
                max_wait_seconds=1,
            )
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
                return await MinerUClient(config, http_client).parse_agent_file_to_markdown(
                    file_path
                )

        with tempfile.TemporaryDirectory() as tmp:
            file_path = Path(tmp) / "original.pdf"
            file_bytes = b"%PDF-1.4\nexact persisted source"
            file_path.write_bytes(file_bytes)
            markdown = asyncio.run(run(file_path))

        self.assertEqual(markdown, "# Uploaded exact source")
        self.assertEqual(calls["payload"]["file_name"], "original.pdf")
        self.assertEqual(calls["payload"]["page_range"], "1-2")
        self.assertEqual(calls["upload"], file_bytes)
        self.assertIsNone(calls["upload_auth"])
        self.assertEqual(calls["upload_length"], str(len(file_bytes)))
        self.assertIsNone(calls["upload_type"])
        self.assertIsNone(calls["upload_transfer"])

    def test_signed_upload_403_is_not_reported_as_api_token_failure(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403)

        async def run(file_path: Path) -> None:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
                await MinerUClient(
                    MinerUConfig(base_url="https://mineru.test"),
                    http_client,
                ).upload_agent_file("https://upload-mineru.test/signed", file_path)

        with tempfile.TemporaryDirectory() as tmp:
            file_path = Path(tmp) / "original.pdf"
            file_path.write_bytes(b"%PDF-1.4\nsource")
            with self.assertRaisesRegex(MinerUError, "request rejected") as raised:
                asyncio.run(run(file_path))

        self.assertNotIsInstance(raised.exception, MinerUAuthError)

    def test_standard_failed_task_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(200, json={"code": 0, "data": {"task_id": "failed-1"}})
            return httpx.Response(
                200,
                json={"code": 0, "data": {"state": "failed", "err_msg": "unsupported document"}},
            )

        async def run() -> None:
            config = MinerUConfig(
                base_url="https://mineru.test",
                mode="standard",
                api_token="test-token",
                poll_interval_seconds=0,
            )
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
                await MinerUClient(config, http_client).parse_standard_url_to_markdown("https://example.com/file.pdf")

        with self.assertRaises(MinerUTaskFailed):
            asyncio.run(run())

    def test_standard_running_task_times_out(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(200, json={"code": 0, "data": {"task_id": "slow-1"}})
            return httpx.Response(200, json={"code": 0, "data": {"state": "running"}})

        async def run() -> None:
            config = MinerUConfig(
                base_url="https://mineru.test",
                mode="standard",
                api_token="test-token",
                poll_interval_seconds=0,
                max_wait_seconds=0,
            )
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
                await MinerUClient(config, http_client).parse_standard_url_to_markdown("https://example.com/file.pdf")

        with self.assertRaises(MinerUTaskTimeout):
            asyncio.run(run())

    def test_standard_result_zip_rejects_unsafe_path(self) -> None:
        with self.assertRaisesRegex(MinerUError, "unsafe path"):
            markdown_from_result_zip(self._result_zip({"../full.md": "unsafe"}))

    def test_standard_result_zip_requires_full_markdown(self) -> None:
        with self.assertRaisesRegex(MinerUError, "does not contain full.md"):
            markdown_from_result_zip(self._result_zip({"result.md": "missing"}))

    def test_standard_result_zip_enforces_uncompressed_limit(self) -> None:
        result_zip = self._result_zip({"full.md": "too large"})
        with (
            patch("backend.extraction.mineru._MAX_UNCOMPRESSED_BYTES", 2),
            self.assertRaisesRegex(MinerUError, "uncompressed size limit"),
        ):
            markdown_from_result_zip(result_zip)

    def test_standard_result_zip_enforces_download_limit(self) -> None:
        result_zip = self._result_zip({"full.md": "body"})
        with (
            patch("backend.extraction.mineru._MAX_ZIP_BYTES", 2),
            self.assertRaisesRegex(MinerUError, "zip exceeds size limit"),
        ):
            markdown_from_result_zip(result_zip)

    def test_standard_result_download_rejects_oversize_before_zip_parsing(self) -> None:
        async def run() -> None:
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda _: httpx.Response(200, content=b"0123456789")
                )
            ) as http_client:
                await MinerUClient(MinerUConfig(), http_client).download_standard_result(
                    "https://cdn-mineru.test/result.zip"
                )

        with (
            patch("backend.extraction.mineru._MAX_ZIP_BYTES", 8),
            self.assertRaisesRegex(MinerUError, "zip exceeds size limit"),
        ):
            asyncio.run(run())

    def test_extract_paper_uses_enabled_mineru_as_last_fallback(self) -> None:
        async def run():
            mineru_blocks = [Block(index=0, type="paragraph", original="MinerU parsed text.")]
            with (
                patch("backend.extraction.extract.extract_from_ar5iv", AsyncMock(return_value=None)),
                patch("backend.extraction.latex.extract_from_latex", AsyncMock(return_value=None)),
                patch(
                    "backend.extraction.extract._get_enabled_mineru_config",
                    return_value=MinerUConfig(enabled=True, base_url="https://mineru.test"),
                ),
                patch("backend.extraction.mineru.extract_from_mineru_url", AsyncMock(return_value=mineru_blocks)) as mineru,
            ):
                blocks, source = await extract_paper("1234.56789")
            return blocks, source, mineru

        blocks, source, mineru = asyncio.run(run())

        self.assertEqual(source, "mineru")
        self.assertEqual(blocks[0].original, "MinerU parsed text.")
        mineru.assert_awaited_once()

    def test_extract_paper_rejects_unacceptable_latex_before_mineru(self) -> None:
        async def run():
            incomplete_latex = [
                Block(index=0, type="paragraph", original="Only one fragment.")
            ]
            mineru_blocks = [
                Block(index=0, type="paragraph", original="Complete MinerU fallback."),
                Block(index=1, type="paragraph", original="Second paragraph."),
                Block(index=2, type="paragraph", original="Third paragraph."),
            ]
            with (
                patch(
                    "backend.extraction.extract.extract_from_ar5iv",
                    AsyncMock(return_value=None),
                ),
                patch(
                    "backend.extraction.latex.extract_from_latex",
                    AsyncMock(return_value=incomplete_latex),
                ),
                patch(
                    "backend.extraction.extract._get_enabled_mineru_config",
                    return_value=MinerUConfig(enabled=True, base_url="https://mineru.test"),
                ),
                patch(
                    "backend.extraction.mineru.extract_from_mineru_url",
                    AsyncMock(return_value=mineru_blocks),
                ) as mineru,
            ):
                blocks, source = await extract_paper("1234.56789")
            return blocks, source, mineru

        blocks, source, mineru = asyncio.run(run())

        self.assertEqual(source, "mineru")
        self.assertEqual(blocks[0].original, "Complete MinerU fallback.")
        mineru.assert_awaited_once()

    def test_create_arxiv_paper_reuses_existing_document(self) -> None:
        async def run():
            with (
                patch.object(
                    routes_papers,
                    "get_paper",
                    AsyncMock(
                        return_value={
                            "arxiv_id": "1706.03762",
                            "title": "Attention Is All You Need",
                            "authors": ["A. Vaswani"],
                            "source": "ar5iv",
                            "status": "extracted",
                            "created_at": "2026-07-07T00:00:00Z",
                        }
                    ),
                ) as get_paper,
                patch.object(routes_papers, "load_document", return_value=object()) as load_document,
                patch.object(routes_papers, "extract_paper", AsyncMock()) as extract,
            ):
                response = await routes_papers.create_paper(
                    routes_papers.CreatePaperRequest(
                        arxiv_id="1706.03762",
                        title="Attention Is All You Need",
                        authors=["A. Vaswani"],
                    )
                )
            return response, get_paper, load_document, extract

        response, get_paper, load_document, extract = asyncio.run(run())

        self.assertEqual(response.arxiv_id, "1706.03762")
        self.assertEqual(response.source, "ar5iv")
        get_paper.assert_awaited_once_with("1706.03762")
        load_document.assert_called_once_with("1706.03762")
        extract.assert_not_awaited()

    def test_create_mineru_paper_saves_parsed_blocks(self) -> None:
        async def run():
            url = "https://example.com/paper.pdf"
            paper_id = routes_papers._mineru_paper_id(url, "1-2")
            parse_file = AsyncMock(return_value="# Parsed title")
            client = SimpleNamespace(parse_agent_file_to_markdown=parse_file)

            async def download_pdf(_url: str, destination: Path) -> Path:
                destination.write_bytes(b"%PDF-1.4\n")
                return destination

            with tempfile.TemporaryDirectory() as tmp:
                paper_path = Path(tmp)
                with (
                    patch.object(
                        routes_papers,
                        "get_config",
                        return_value=SimpleNamespace(
                            mineru=MinerUConfig(enabled=True, page_range="1-2")
                        ),
                    ),
                    patch.object(routes_papers, "ensure_paper_dir", return_value=paper_path),
                    patch.object(routes_papers, "paper_dir", return_value=paper_path),
                    patch.object(
                        routes_papers,
                        "download_source_pdf",
                        AsyncMock(side_effect=download_pdf),
                    ) as download,
                    patch.object(routes_papers, "MinerUClient", return_value=client),
                    patch.object(routes_papers, "_warm_translation_layout", AsyncMock()) as warm,
                    patch.object(routes_papers, "save_document") as save_document,
                    patch.object(routes_papers, "save_extraction_quality") as save_quality,
                    patch.object(routes_papers, "insert_paper", AsyncMock(return_value=1)) as insert,
                    patch.object(
                        routes_papers,
                        "get_paper",
                        AsyncMock(
                            return_value={
                                "arxiv_id": paper_id,
                                "title": "Parsed title",
                                "authors": [],
                                "source": "mineru",
                                "status": "extracted",
                                "created_at": "2026-07-05T00:00:00Z",
                            }
                        ),
                    ),
                ):
                    response = await routes_papers.create_mineru_paper(
                        routes_papers.CreateMinerUPaperRequest(url=url, page_range="1-2")
                    )
                return response, download, parse_file, warm, save_document, save_quality, insert

        response, download, parse_file, warm, save_document, save_quality, insert = asyncio.run(run())

        self.assertTrue(response.arxiv_id.startswith("mineru-"))
        self.assertEqual(response.title, "Parsed title")
        self.assertEqual(response.source, "mineru")
        download.assert_awaited_once()
        parse_file.assert_awaited_once()
        self.assertRegex(
            parse_file.await_args.args[0].name,
            r"^\.incoming-[0-9a-f]{32}\.pdf$",
        )
        warm.assert_awaited_once()
        save_document.assert_called_once()
        save_quality.assert_called_once()
        insert.assert_awaited_once()

    def test_create_mineru_paper_accepts_standard_mode(self) -> None:
        async def run():
            url = "https://example.com/complex.pdf"
            config = MinerUConfig(enabled=True, mode="standard", api_token="test-token")
            paper_id = routes_papers._mineru_paper_id(url, None, "standard")
            blocks = [Block(index=0, type="heading", original="Precise title")]
            result = MinerUStructuredResult(markdown="# Precise title", blocks=blocks)
            parse_file = AsyncMock(return_value=result)
            client = SimpleNamespace(parse_standard_file=parse_file)

            async def download_pdf(_url: str, destination: Path) -> Path:
                destination.write_bytes(b"%PDF-1.4\n")
                return destination

            with tempfile.TemporaryDirectory() as tmp:
                paper_path = Path(tmp)
                with (
                    patch.object(
                        routes_papers,
                        "get_config",
                        return_value=SimpleNamespace(mineru=config),
                    ),
                    patch.object(routes_papers, "ensure_paper_dir", return_value=paper_path),
                    patch.object(routes_papers, "paper_dir", return_value=paper_path),
                    patch.object(
                        routes_papers,
                        "download_source_pdf",
                        AsyncMock(side_effect=download_pdf),
                    ),
                    patch.object(routes_papers, "MinerUClient", return_value=client) as client_factory,
                    patch.object(routes_papers, "_warm_translation_layout", AsyncMock()),
                    patch.object(routes_papers, "save_document"),
                    patch.object(routes_papers, "save_extraction_quality"),
                    patch.object(routes_papers, "insert_paper", AsyncMock(return_value=1)),
                    patch.object(
                        routes_papers,
                        "get_paper",
                        AsyncMock(
                            return_value={
                                "arxiv_id": paper_id,
                                "title": "Precise title",
                                "authors": [],
                                "source": "mineru",
                                "status": "extracted",
                                "created_at": "2026-07-16T00:00:00Z",
                            }
                        ),
                    ),
                ):
                    response = await routes_papers.create_mineru_paper(
                        routes_papers.CreateMinerUPaperRequest(url=url)
                    )
                return response, parse_file, client_factory

        response, parse_file, client_factory = asyncio.run(run())

        self.assertEqual(response.source, "mineru")
        self.assertNotEqual(
            response.arxiv_id,
            routes_papers._mineru_paper_id("https://example.com/complex.pdf", None),
        )
        self.assertEqual(client_factory.call_args.args[0].mode, "standard")
        parse_file.assert_awaited_once()
        self.assertRegex(
            parse_file.await_args.args[0].name,
            r"^\.incoming-[0-9a-f]{32}\.pdf$",
        )

    def test_failed_mineru_reimport_keeps_previous_pdf_and_layout_cache(self) -> None:
        url = "https://example.com/reimport.pdf"
        config = MinerUConfig(enabled=True, mode="agent_lite")
        paper_id = routes_papers._mineru_paper_id(url, None, "agent_lite")

        async def download_pdf(_url: str, destination: Path) -> Path:
            destination.write_bytes(b"%PDF-new-source")
            return destination

        with tempfile.TemporaryDirectory() as tmp:
            paper_path = Path(tmp)
            original_pdf = paper_path / "original.pdf"
            original_pdf.write_bytes(b"%PDF-previous-source")
            layout_cache = paper_path / "translation_layout.json"
            layout_cache.write_text("{}", encoding="utf-8")
            with (
                patch.object(
                    routes_papers,
                    "get_config",
                    return_value=SimpleNamespace(mineru=config),
                ),
                patch.object(routes_papers, "ensure_paper_dir", return_value=paper_path),
                patch.object(routes_papers, "paper_dir", return_value=paper_path),
                patch.object(
                    routes_papers,
                    "download_source_pdf",
                    AsyncMock(side_effect=download_pdf),
                ),
                patch.object(
                    routes_papers,
                    "MinerUClient",
                    return_value=SimpleNamespace(
                        parse_agent_file_to_markdown=AsyncMock(
                            side_effect=MinerUError("parse failed")
                        )
                    ),
                ),
            ):
                with self.assertRaises(routes_papers.HTTPException):
                    asyncio.run(
                        routes_papers.create_mineru_paper(
                            routes_papers.CreateMinerUPaperRequest(url=url)
                        )
                    )

            self.assertEqual(original_pdf.read_bytes(), b"%PDF-previous-source")
            self.assertTrue(layout_cache.exists())
            self.assertFalse(list(paper_path.glob(".incoming-*.pdf")))

    def test_create_mineru_paper_keeps_partial_standard_text_import_compatible(self) -> None:
        config = MinerUConfig(
            enabled=True,
            mode="standard",
            api_token="test-token",
            page_range="2-4",
        )
        url = "https://example.com/partial.pdf"
        paper_id = routes_papers._mineru_paper_id(url, "2-4", "standard")
        result = MinerUStructuredResult(
            markdown="# Partial title",
            blocks=[Block(index=0, type="heading", original="Partial title")],
            layout={"pdf_info": [{"page_idx": 0}]},
            content_list=[{"type": "text", "page_idx": 0, "text": "Partial title"}],
        )
        parse_file = AsyncMock(return_value=result)

        async def download_pdf(_url: str, destination: Path) -> Path:
            destination.write_bytes(b"%PDF-1.4\n")
            return destination

        with tempfile.TemporaryDirectory() as tmp:
            paper_path = Path(tmp)
            with (
                patch.object(
                    routes_papers,
                    "get_config",
                    return_value=SimpleNamespace(mineru=config),
                ),
                patch.object(routes_papers, "ensure_paper_dir", return_value=paper_path),
                patch.object(routes_papers, "paper_dir", return_value=paper_path),
                patch.object(
                    routes_papers,
                    "download_source_pdf",
                    AsyncMock(side_effect=download_pdf),
                ),
                patch.object(
                    routes_papers,
                    "MinerUClient",
                    return_value=SimpleNamespace(parse_standard_file=parse_file),
                ) as client_factory,
                patch.object(routes_papers, "_save_mineru_result") as save_artifacts,
                patch.object(
                    routes_papers,
                    "_save_document_with_quality",
                ) as save_document,
                patch.object(routes_papers, "_warm_translation_layout", AsyncMock()) as warm,
                patch.object(routes_papers, "insert_paper", AsyncMock(return_value=1)),
                patch.object(
                    routes_papers,
                    "get_paper",
                    AsyncMock(
                        return_value={
                            "arxiv_id": paper_id,
                            "title": "Partial title",
                            "authors": [],
                            "source": "mineru",
                            "status": "extracted",
                            "created_at": "2026-07-21T00:00:00Z",
                        }
                    ),
                ),
            ):
                response = asyncio.run(
                    routes_papers.create_mineru_paper(
                        routes_papers.CreateMinerUPaperRequest(url=url)
                    )
                )

        self.assertEqual(response.arxiv_id, paper_id)
        self.assertEqual(client_factory.call_args.args[0].page_range, "2-4")
        parse_file.assert_awaited_once()
        save_artifacts.assert_not_called()
        warm.assert_awaited_once()
        saved_document = save_document.call_args.args[0]
        self.assertEqual(saved_document.source_page_range, "2-4")

    def test_create_local_file_paper_saves_uploaded_pdf(self) -> None:
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                tmp_dir = Path(tmp)
                upload_tmp = tmp_dir / "upload.pdf"
                upload_tmp.write_bytes(b"%PDF-1.4\n")
                paper_dir = tmp_dir / "paper"
                paper_dir.mkdir()
                paper_id = routes_papers._local_file_paper_id("paper.pdf", "abcdef1234567890")
                local_blocks = [Block(index=0, type="heading", original="Local title")]
                upload = UploadFile(file=io.BytesIO(b""), filename="paper.pdf")
                with (
                    patch.object(
                        routes_papers,
                        "_save_uploaded_pdf_tmp",
                        AsyncMock(return_value=(upload_tmp, "abcdef1234567890", 9)),
                    ),
                    patch.object(routes_papers, "ensure_paper_dir", return_value=paper_dir),
                    patch.object(routes_papers, "paper_dir", return_value=paper_dir),
                    patch.object(
                        routes_papers,
                        "extract_pdf_layout",
                        return_value=SimpleNamespace(
                            pages=(SimpleNamespace(blocks=(object(),), rotation=0),)
                        ),
                    ),
                    patch.object(routes_papers, "extract_blocks_from_local_pdf", return_value=local_blocks) as extract,
                    patch.object(routes_papers, "_warm_translation_layout", AsyncMock()),
                    patch.object(routes_papers, "save_document") as save_document,
                    patch.object(routes_papers, "save_extraction_quality") as save_quality,
                    patch.object(routes_papers, "insert_paper", AsyncMock(return_value=1)) as insert,
                    patch.object(
                        routes_papers,
                        "get_paper",
                        AsyncMock(
                            return_value={
                                "arxiv_id": paper_id,
                                "title": "Local title",
                                "authors": [],
                                "source": "local_pdf",
                                "status": "extracted",
                                "created_at": "2026-07-06T00:00:00Z",
                            }
                        ),
                    ),
                ):
                    response = await routes_papers.create_local_file_paper(upload, title="")
                original_pdf_saved = (paper_dir / "original.pdf").exists()
                return response, original_pdf_saved, extract, save_document, save_quality, insert

        response, original_pdf_saved, extract, save_document, save_quality, insert = asyncio.run(run())

        self.assertTrue(original_pdf_saved)
        self.assertEqual(response.source, "local_pdf")
        self.assertEqual(response.title, "Local title")
        self.assertEqual(extract.call_args.args[0].name, "upload.pdf")
        save_document.assert_called_once()
        save_quality.assert_called_once()
        insert.assert_awaited_once()

    def test_local_pdf_with_blank_text_layer_page_uses_full_mineru_ocr_blocks(self) -> None:
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                tmp_dir = Path(tmp)
                upload_tmp = tmp_dir / "upload.pdf"
                upload_tmp.write_bytes(b"%PDF-1.4\n")
                paper_path = tmp_dir / "paper"
                paper_path.mkdir()
                mineru_blocks = [
                    Block(index=0, type="heading", original="OCR title"),
                    Block(index=1, type="paragraph", original="OCR body"),
                ]
                mineru_result = MinerUStructuredResult(
                    markdown="# OCR title\n\nOCR body",
                    blocks=mineru_blocks,
                    layout={"pdf_info": []},
                    content_list=[],
                )
                upload = UploadFile(file=io.BytesIO(b""), filename="hybrid.pdf")
                with (
                    patch.object(
                        routes_papers,
                        "_save_uploaded_pdf_tmp",
                        AsyncMock(return_value=(upload_tmp, "abcdef1234567890", 9)),
                    ),
                    patch.object(routes_papers, "ensure_paper_dir", return_value=paper_path),
                    patch.object(routes_papers, "paper_dir", return_value=paper_path),
                    patch.object(
                        routes_papers,
                        "extract_pdf_layout",
                        return_value=SimpleNamespace(
                            pages=(
                                SimpleNamespace(blocks=(object(),), rotation=0),
                                SimpleNamespace(blocks=(), rotation=0),
                            )
                        ),
                    ),
                    patch.object(routes_papers, "extract_blocks_from_local_pdf") as local_extract,
                    patch.object(
                        routes_papers,
                        "_parse_pdf_with_standard_mineru",
                        AsyncMock(return_value=mineru_result),
                    ) as parse_mineru,
                    patch.object(
                        routes_papers,
                        "translation_layout_from_mineru",
                        return_value=SimpleNamespace(
                            pdf_url="",
                            model_dump=lambda **_: {},
                        ),
                    ),
                    patch.object(routes_papers, "_save_mineru_result"),
                    patch.object(routes_papers, "save_translation_layout"),
                    patch.object(routes_papers, "_save_document_with_quality") as save_document,
                    patch.object(routes_papers, "_warm_translation_layout", AsyncMock()),
                    patch.object(routes_papers, "insert_paper", AsyncMock(return_value=1)),
                    patch.object(
                        routes_papers,
                        "get_paper",
                        AsyncMock(
                            return_value={
                                "arxiv_id": "local-hybrid-abcdef123456",
                                "title": "OCR title",
                                "authors": [],
                                "source": "mineru",
                                "status": "extracted",
                                "created_at": "2026-07-21T00:00:00Z",
                            }
                        ),
                    ),
                ):
                    response = await routes_papers.create_local_file_paper(upload, title="")
                return response, local_extract, parse_mineru, save_document

        response, local_extract, parse_mineru, save_document = asyncio.run(run())

        self.assertEqual(response.source, "mineru")
        local_extract.assert_not_called()
        parse_mineru.assert_awaited_once()
        self.assertTrue(parse_mineru.await_args.kwargs["is_ocr"])
        saved_document = save_document.call_args.args[0]
        self.assertEqual(saved_document.source, "mineru")
        self.assertEqual(
            [block.original for block in saved_document.blocks],
            ["OCR title", "OCR body"],
        )

    def test_failed_local_reimport_keeps_previous_pdf_and_layout_cache(self) -> None:
        async def run(paper_path: Path, upload_tmp: Path) -> None:
            upload = UploadFile(file=io.BytesIO(b""), filename="paper.pdf")
            with (
                patch.object(
                    routes_papers,
                    "_save_uploaded_pdf_tmp",
                    AsyncMock(return_value=(upload_tmp, "abcdef1234567890", 9)),
                ),
                patch.object(routes_papers, "ensure_paper_dir", return_value=paper_path),
                patch.object(routes_papers, "paper_dir", return_value=paper_path),
                patch.object(
                    routes_papers,
                    "extract_pdf_layout",
                    return_value=SimpleNamespace(
                        pages=(SimpleNamespace(blocks=(object(),), rotation=0),)
                    ),
                ),
                patch.object(
                    routes_papers,
                    "extract_blocks_from_local_pdf",
                    side_effect=LocalPdfExtractionError("parse failed"),
                ),
                patch.object(
                    routes_papers,
                    "_parse_pdf_with_standard_mineru",
                    AsyncMock(side_effect=MinerUError("ocr failed")),
                ),
            ):
                await routes_papers.create_local_file_paper(upload, title="")

        with tempfile.TemporaryDirectory() as tmp:
            paper_path = Path(tmp) / "paper"
            paper_path.mkdir()
            original_pdf = paper_path / "original.pdf"
            original_pdf.write_bytes(b"%PDF-previous-local")
            layout_cache = paper_path / "translation_layout.json"
            layout_cache.write_text("{}", encoding="utf-8")
            upload_tmp = Path(tmp) / "upload.pdf"
            upload_tmp.write_bytes(b"%PDF-new-local")

            with self.assertRaises(routes_papers.HTTPException):
                asyncio.run(run(paper_path, upload_tmp))

            self.assertEqual(original_pdf.read_bytes(), b"%PDF-previous-local")
            self.assertTrue(layout_cache.exists())
            self.assertFalse(upload_tmp.exists())

    def test_create_mineru_paper_requires_enabled_provider(self) -> None:
        async def run():
            with patch.object(
                routes_papers,
                "get_config",
                return_value=SimpleNamespace(mineru=MinerUConfig(enabled=False)),
            ):
                await routes_papers.create_mineru_paper(
                    routes_papers.CreateMinerUPaperRequest(url="https://example.com/paper.pdf")
                )

        with self.assertRaises(routes_papers.HTTPException) as caught:
            asyncio.run(run())

        self.assertEqual(caught.exception.status_code, 400)

    def test_create_standard_mineru_paper_requires_token(self) -> None:
        async def run() -> None:
            with patch.object(
                routes_papers,
                "get_config",
                return_value=SimpleNamespace(mineru=MinerUConfig(enabled=True, mode="standard")),
            ):
                await routes_papers.create_mineru_paper(
                    routes_papers.CreateMinerUPaperRequest(url="https://example.com/paper.pdf")
                )

        with self.assertRaises(routes_papers.HTTPException) as caught:
            asyncio.run(run())

        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("API token", caught.exception.detail)


if __name__ == "__main__":
    unittest.main()
