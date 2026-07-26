from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI, HTTPException

from backend.extraction.blocks import Block, PaperDocument
from backend.api import routes_translate
from backend.api.routes_translate import _translation_final_status
from backend.storage import db as db_module
from backend.storage import files as storage_files
from backend.storage.files import load_document, save_document
from backend.translation.translate import translate_paper_sse


class TranslationStatusTest(unittest.TestCase):
    def test_sse_translation_error_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            papers_dir = Path(tmp) / "papers"
            doc = PaperDocument(
                paper_id="test-paper",
                title="Test Paper",
                source="ar5iv",
                extracted_at="2026-07-03T00:00:00Z",
                blocks=[
                    Block(index=0, type="paragraph", original="First paragraph.", status="pending"),
                ],
            )

            async def fail_block(_doc: PaperDocument, block_index: int):
                return block_index, None, "error"

            with (
                patch.object(storage_files, "PAPERS_DIR", papers_dir),
                patch(
                    "backend.translation.translate.get_config",
                    return_value=SimpleNamespace(translation_concurrency=1),
                ),
                patch("backend.translation.translate.translate_single_block", fail_block),
            ):
                save_document(doc)
                events = asyncio.run(_collect_events("test-paper"))
                reloaded = load_document("test-paper")

            self.assertTrue(any("event: block_error" in event for event in events))
            self.assertIsNotNone(reloaded)
            self.assertEqual(reloaded.blocks[0].status, "error")

    def test_final_paper_status_reflects_block_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            papers_dir = Path(tmp) / "papers"
            doc = PaperDocument(
                paper_id="test-paper",
                title="Test Paper",
                source="ar5iv",
                extracted_at="2026-07-03T00:00:00Z",
                blocks=[
                    Block(index=0, type="paragraph", original="First.", status="done", translation="第一。"),
                    Block(index=1, type="paragraph", original="Second.", status="error"),
                    Block(index=2, type="formula", original="x=y", status="skip"),
                ],
            )

            with patch.object(storage_files, "PAPERS_DIR", papers_dir):
                save_document(doc)
                self.assertEqual(_translation_final_status("test-paper"), "translation_error")

                doc.blocks[1].status = "done"
                doc.blocks[1].translation = "第二。"
                save_document(doc)
                self.assertEqual(_translation_final_status("test-paper"), "translated")

    def test_final_paper_status_rejects_incomplete_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            papers_dir = Path(tmp) / "papers"
            doc = PaperDocument(
                paper_id="test-paper",
                title="Test Paper",
                source="ar5iv",
                extracted_at="2026-07-03T00:00:00Z",
                blocks=[
                    Block(index=0, type="paragraph", original="First.", status="done", translation="第一。"),
                    Block(index=1, type="paragraph", original="Second.", status="pending"),
                ],
            )

            with patch.object(storage_files, "PAPERS_DIR", papers_dir):
                save_document(doc)
                self.assertEqual(_translation_final_status("test-paper"), "translation_error")

                doc.blocks[1].status = "translating"
                save_document(doc)
                self.assertEqual(_translation_final_status("test-paper"), "translation_error")

    def test_sse_close_cancels_unfinished_tasks_and_resumes_pending_only(self) -> None:
        async def scenario(papers_dir: Path) -> None:
            doc = PaperDocument(
                paper_id="test-paper",
                title="Test Paper",
                source="ar5iv",
                extracted_at="2026-07-03T00:00:00Z",
                blocks=[
                    Block(index=0, type="paragraph", original="First.", status="pending"),
                    Block(index=1, type="paragraph", original="Second.", status="pending"),
                    Block(index=2, type="paragraph", original="Third.", status="pending"),
                ],
            )
            save_document(doc)
            waiting = {1: asyncio.Event(), 2: asyncio.Event()}
            cancelled: set[int] = set()

            async def translate_then_wait(_doc: PaperDocument, block_index: int):
                if block_index == 0:
                    return block_index, "第一。", "done"
                try:
                    await waiting[block_index].wait()
                except asyncio.CancelledError:
                    cancelled.add(block_index)
                    raise
                return block_index, f"第 {block_index + 1} 段。", "done"

            with patch("backend.translation.translate.translate_single_block", translate_then_wait):
                stream = translate_paper_sse("test-paper")
                first_event = await anext(stream)
                self.assertIn("event: block_done", first_event)
                self.assertIn('"index": 0', first_event)
                await stream.aclose()

            self.assertEqual(cancelled, {1, 2})
            interrupted = load_document("test-paper")
            self.assertIsNotNone(interrupted)
            self.assertEqual(
                [block.status for block in interrupted.blocks],
                ["done", "pending", "pending"],
            )
            self.assertEqual(interrupted.blocks[0].translation, "第一。")
            self.assertIsNone(interrupted.blocks[1].translation)
            self.assertIsNone(interrupted.blocks[2].translation)

            resumed_indexes: list[int] = []

            async def resume_pending(_doc: PaperDocument, block_index: int):
                resumed_indexes.append(block_index)
                return block_index, f"第 {block_index + 1} 段。", "done"

            with patch("backend.translation.translate.translate_single_block", resume_pending):
                resumed_events = await _collect_events("test-paper")

            self.assertCountEqual(resumed_indexes, [1, 2])
            self.assertEqual(
                sum("event: block_done" in event for event in resumed_events),
                2,
            )
            self.assertTrue(any("event: complete" in event for event in resumed_events))
            completed = load_document("test-paper")
            self.assertIsNotNone(completed)
            self.assertEqual(
                [block.status for block in completed.blocks],
                ["done", "done", "done"],
            )

        with tempfile.TemporaryDirectory() as tmp:
            papers_dir = Path(tmp) / "papers"
            with (
                patch.object(storage_files, "PAPERS_DIR", papers_dir),
                patch(
                    "backend.translation.translate.get_config",
                    return_value=SimpleNamespace(translation_concurrency=3),
                ),
            ):
                asyncio.run(scenario(papers_dir))

    def test_sse_consumer_cancellation_cleans_up_without_persisting_errors(self) -> None:
        async def scenario() -> None:
            doc = PaperDocument(
                paper_id="test-paper",
                title="Test Paper",
                source="ar5iv",
                extracted_at="2026-07-03T00:00:00Z",
                blocks=[
                    Block(index=0, type="paragraph", original="First.", status="pending"),
                    Block(index=1, type="paragraph", original="Second.", status="pending"),
                ],
            )
            save_document(doc)
            started = {0: asyncio.Event(), 1: asyncio.Event()}
            cancelled: set[int] = set()

            async def wait_forever(_doc: PaperDocument, block_index: int):
                started[block_index].set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.add(block_index)
                    raise

            with patch("backend.translation.translate.translate_single_block", wait_forever):
                consumer = asyncio.create_task(_collect_events("test-paper"))
                await asyncio.gather(*(event.wait() for event in started.values()))
                consumer.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await consumer

            self.assertEqual(cancelled, {0, 1})
            interrupted = load_document("test-paper")
            self.assertIsNotNone(interrupted)
            self.assertEqual(
                [block.status for block in interrupted.blocks],
                ["pending", "pending"],
            )

        with tempfile.TemporaryDirectory() as tmp:
            papers_dir = Path(tmp) / "papers"
            with (
                patch.object(storage_files, "PAPERS_DIR", papers_dir),
                patch(
                    "backend.translation.translate.get_config",
                    return_value=SimpleNamespace(translation_concurrency=2),
                ),
            ):
                asyncio.run(scenario())

    def test_asgi_23_disconnect_closes_route_and_leaves_translation_resumable(self) -> None:
        async def scenario(papers_dir: Path) -> None:
            await db_module.init_db()
            await db_module.insert_paper(
                "test-paper",
                "Test Paper",
                ["Tester"],
                "ar5iv",
                str(papers_dir / "test-paper"),
            )
            save_document(
                PaperDocument(
                    paper_id="test-paper",
                    title="Test Paper",
                    source="ar5iv",
                    extracted_at="2026-07-03T00:00:00Z",
                    blocks=[
                        Block(index=0, type="paragraph", original="First.", status="pending"),
                        Block(index=1, type="paragraph", original="Second.", status="pending"),
                        Block(index=2, type="paragraph", original="Third.", status="pending"),
                    ],
                )
            )

            app = FastAPI()
            app.include_router(routes_translate.router)
            first_body_sent = asyncio.Event()
            cancelled: set[int] = set()
            worker_tasks: set[asyncio.Task] = set()

            async def translate_then_wait(_doc: PaperDocument, block_index: int):
                task = asyncio.current_task()
                assert task is not None
                worker_tasks.add(task)
                if block_index == 0:
                    return block_index, "第一。", "done"
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.add(block_index)
                    raise

            request_sent = False

            async def receive_disconnect() -> dict:
                nonlocal request_sent
                if not request_sent:
                    request_sent = True
                    return {"type": "http.request", "body": b"", "more_body": False}
                await first_body_sent.wait()
                return {"type": "http.disconnect"}

            sent: list[dict] = []

            async def send_disconnect(message: dict) -> None:
                sent.append(message)
                if (
                    message["type"] == "http.response.body"
                    and b"event: block_done" in message.get("body", b"")
                ):
                    first_body_sent.set()
                    # Reproduce a client that disconnects while the server is
                    # still blocked in the send path, not between two anext calls.
                    await asyncio.Event().wait()

            scope = _post_scope("/translate/test-paper")
            baseline_tasks = set(asyncio.all_tasks())
            with patch("backend.translation.translate.translate_single_block", translate_then_wait):
                await asyncio.wait_for(
                    app(scope, receive_disconnect, send_disconnect),
                    timeout=2,
                )
            await asyncio.sleep(0)

            self.assertTrue(first_body_sent.is_set())
            self.assertEqual(cancelled, {1, 2})
            self.assertTrue(worker_tasks)
            self.assertTrue(all(task.done() for task in worker_tasks))
            residual_tasks = set(asyncio.all_tasks()) - baseline_tasks
            self.assertFalse(
                residual_tasks,
                [(task.get_name(), repr(task.get_coro())) for task in residual_tasks],
            )

            interrupted = load_document("test-paper")
            self.assertIsNotNone(interrupted)
            self.assertEqual(
                [block.status for block in interrupted.blocks],
                ["done", "pending", "pending"],
            )
            self.assertEqual(interrupted.blocks[0].translation, "第一。")
            meta = await db_module.get_paper("test-paper")
            self.assertIsNotNone(meta)
            self.assertEqual(meta["status"], "extracted")

            resumed_indexes: list[int] = []

            async def resume_pending(_doc: PaperDocument, block_index: int):
                resumed_indexes.append(block_index)
                return block_index, f"第 {block_index + 1} 段。", "done"

            resume_request_sent = False

            async def receive_resume() -> dict:
                nonlocal resume_request_sent
                if not resume_request_sent:
                    resume_request_sent = True
                    return {"type": "http.request", "body": b"", "more_body": False}
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

            resumed_messages: list[dict] = []

            async def send_resume(message: dict) -> None:
                resumed_messages.append(message)

            with patch("backend.translation.translate.translate_single_block", resume_pending):
                await app(scope, receive_resume, send_resume)

            self.assertCountEqual(resumed_indexes, [1, 2])
            self.assertTrue(
                any(
                    b"event: complete" in message.get("body", b"")
                    for message in resumed_messages
                    if message["type"] == "http.response.body"
                )
            )
            completed = load_document("test-paper")
            self.assertIsNotNone(completed)
            self.assertEqual(
                [block.status for block in completed.blocks],
                ["done", "done", "done"],
            )
            meta = await db_module.get_paper("test-paper")
            self.assertIsNotNone(meta)
            self.assertEqual(meta["status"], "translated")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch.object(storage_files, "PAPERS_DIR", root / "papers"),
                patch.object(db_module, "DB_PATH", root / "papers.db"),
                patch(
                    "backend.translation.translate.get_config",
                    return_value=SimpleNamespace(translation_concurrency=3),
                ),
            ):
                asyncio.run(scenario(root / "papers"))

    def test_asgi_disconnect_before_first_anext_releases_translation_lock(self) -> None:
        async def scenario(papers_dir: Path) -> None:
            await db_module.init_db()
            await db_module.insert_paper(
                "test-paper",
                "Test Paper",
                ["Tester"],
                "ar5iv",
                str(papers_dir / "test-paper"),
            )
            save_document(
                PaperDocument(
                    paper_id="test-paper",
                    title="Test Paper",
                    source="ar5iv",
                    extracted_at="2026-07-03T00:00:00Z",
                    blocks=[
                        Block(index=0, type="paragraph", original="First.", status="pending"),
                    ],
                )
            )

            app = FastAPI()
            app.include_router(routes_translate.router)
            stream_started = False

            async def tracked_stream(_arxiv_id: str):
                nonlocal stream_started
                stream_started = True
                yield "event: complete\n\n"

            request_sent = False

            async def receive_disconnect() -> dict:
                nonlocal request_sent
                if not request_sent:
                    request_sent = True
                    return {"type": "http.request", "body": b"", "more_body": False}
                return {"type": "http.disconnect"}

            async def blocked_response_start(message: dict) -> None:
                if message["type"] == "http.response.start":
                    await asyncio.Event().wait()

            with patch.object(routes_translate, "translate_paper_sse", tracked_stream):
                await asyncio.wait_for(
                    app(
                        _post_scope("/translate/test-paper"),
                        receive_disconnect,
                        blocked_response_start,
                    ),
                    timeout=2,
                )

            self.assertFalse(stream_started)
            meta = await db_module.get_paper("test-paper")
            self.assertIsNotNone(meta)
            self.assertEqual(meta["status"], "extracted")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch.object(storage_files, "PAPERS_DIR", root / "papers"),
                patch.object(db_module, "DB_PATH", root / "papers.db"),
            ):
                asyncio.run(scenario(root / "papers"))

    def test_retry_conflicts_while_another_retry_holds_translation_lock(self) -> None:
        async def scenario(papers_dir: Path) -> None:
            await db_module.init_db()
            await db_module.insert_paper(
                "test-paper",
                "Test Paper",
                ["Tester"],
                "ar5iv",
                str(papers_dir / "test-paper"),
            )
            save_document(
                PaperDocument(
                    paper_id="test-paper",
                    title="Test Paper",
                    source="ar5iv",
                    extracted_at="2026-07-03T00:00:00Z",
                    blocks=[
                        Block(index=0, type="paragraph", original="First.", status="pending"),
                    ],
                )
            )
            started = asyncio.Event()
            release = asyncio.Event()

            async def slow_retry(_arxiv_id: str, block_index: int) -> dict:
                started.set()
                await release.wait()
                storage_files.update_block_translation(
                    "test-paper", block_index, "第一。", "done"
                )
                return {"index": block_index, "translation": "第一。", "status": "done"}

            with patch.object(routes_translate, "retry_single_block", slow_retry):
                first = asyncio.create_task(routes_translate.retry_block("test-paper", 0))
                await started.wait()
                with self.assertRaises(HTTPException) as conflict:
                    await routes_translate.retry_block("test-paper", 0)
                self.assertEqual(conflict.exception.status_code, 409)
                release.set()
                response = await first

            self.assertEqual(response.status, "done")
            meta = await db_module.get_paper("test-paper")
            self.assertIsNotNone(meta)
            self.assertEqual(meta["status"], "translated")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch.object(storage_files, "PAPERS_DIR", root / "papers"),
                patch.object(db_module, "DB_PATH", root / "papers.db"),
            ):
                asyncio.run(scenario(root / "papers"))

    def test_route_program_error_forces_translation_error_status(self) -> None:
        async def scenario(papers_dir: Path) -> None:
            await db_module.init_db()
            await db_module.insert_paper(
                "test-paper",
                "Test Paper",
                ["Tester"],
                "ar5iv",
                str(papers_dir / "test-paper"),
            )
            save_document(
                PaperDocument(
                    paper_id="test-paper",
                    title="Test Paper",
                    source="ar5iv",
                    extracted_at="2026-07-03T00:00:00Z",
                    blocks=[
                        Block(index=0, type="paragraph", original="First.", status="pending"),
                    ],
                )
            )

            async def broken_stream(_arxiv_id: str):
                if False:
                    yield ""
                raise RuntimeError("translation worker crashed")

            with patch.object(routes_translate, "translate_paper_sse", broken_stream):
                response = await routes_translate.translate_paper("test-paper")
                with self.assertRaisesRegex(RuntimeError, "worker crashed"):
                    async for _event in response.body_iterator:
                        pass

            meta = await db_module.get_paper("test-paper")
            self.assertIsNotNone(meta)
            self.assertEqual(meta["status"], "translation_error")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch.object(storage_files, "PAPERS_DIR", root / "papers"),
                patch.object(db_module, "DB_PATH", root / "papers.db"),
            ):
                asyncio.run(scenario(root / "papers"))

    def test_route_close_retries_a_failed_final_status_write(self) -> None:
        async def scenario(papers_dir: Path) -> None:
            await db_module.init_db()
            await db_module.insert_paper(
                "test-paper",
                "Test Paper",
                ["Tester"],
                "ar5iv",
                str(papers_dir / "test-paper"),
            )
            save_document(
                PaperDocument(
                    paper_id="test-paper",
                    title="Test Paper",
                    source="ar5iv",
                    extracted_at="2026-07-03T00:00:00Z",
                    blocks=[
                        Block(
                            index=0,
                            type="paragraph",
                            original="First.",
                            status="done",
                            translation="第一。",
                        ),
                    ],
                )
            )
            update_attempts = 0

            async def completed_stream(_arxiv_id: str):
                yield "event: complete\n\n"

            async def flaky_update(arxiv_id: str, status: str) -> None:
                nonlocal update_attempts
                update_attempts += 1
                if update_attempts == 1:
                    raise RuntimeError("temporary status write failure")
                await db_module.update_status(arxiv_id, status)

            with (
                patch.object(routes_translate, "translate_paper_sse", completed_stream),
                patch.object(routes_translate, "update_status", flaky_update),
            ):
                response = await routes_translate.translate_paper("test-paper")
                with self.assertRaisesRegex(RuntimeError, "temporary status write failure"):
                    async for _event in response.body_iterator:
                        pass
                assert response._on_close is not None
                await response._on_close()

            self.assertEqual(update_attempts, 2)
            meta = await db_module.get_paper("test-paper")
            self.assertIsNotNone(meta)
            self.assertEqual(meta["status"], "translated")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch.object(storage_files, "PAPERS_DIR", root / "papers"),
                patch.object(db_module, "DB_PATH", root / "papers.db"),
            ):
                asyncio.run(scenario(root / "papers"))


async def _collect_events(arxiv_id: str) -> list[str]:
    return [event async for event in translate_paper_sse(arxiv_id)]


def _post_scope(path: str) -> dict:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
        "root_path": "",
    }


if __name__ == "__main__":
    unittest.main()
