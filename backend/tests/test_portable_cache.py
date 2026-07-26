from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from backend.extraction.blocks import Block, PaperDocument
from backend.storage import files as storage_files
from backend.storage import portable_cache
from backend.storage.portable_bundle import build_portable_export


class PortableCacheTest(unittest.IsolatedAsyncioTestCase):
    async def test_ack_requires_the_exact_current_revision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            papers = root / "papers"
            metadata = _write_paper(papers, "ack-paper")
            export = build_portable_export(
                "ack-paper",
                metadata,
                papers_root=papers,
                agent_root=root / "agent_workspace",
                cache_root=root / "portable_manifest_cache",
            )
            with (
                patch.object(storage_files, "DATA_DIR", root),
                patch.object(storage_files, "PAPERS_DIR", papers),
                patch.object(
                    portable_cache,
                    "get_paper",
                    AsyncMock(return_value=metadata),
                ),
            ):
                state = await portable_cache.acknowledge_portable_cache(
                    "ack-paper",
                    export.manifest["revision"],
                    now=100,
                )
                self.assertEqual(state["storage_mode"], "local_folder")
                self.assertEqual(state["synced_revision"], export.manifest["revision"])
                self.assertTrue(
                    portable_cache.load_portable_cache_state("ack-paper")
                )
                with self.assertRaisesRegex(ValueError, "revision"):
                    await portable_cache.acknowledge_portable_cache(
                        "ack-paper",
                        "0" * 64,
                        now=101,
                    )

    async def test_capacity_cleanup_evicts_only_oldest_acknowledged_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            papers = root / "papers"
            first = _write_paper(papers, "first", padding=80)
            second = _write_paper(papers, "second", padding=80)
            server = _write_paper(papers, "server-only", padding=80)
            metadata = {"first": first, "second": second, "server-only": server}
            with (
                patch.object(storage_files, "DATA_DIR", root),
                patch.object(storage_files, "PAPERS_DIR", papers),
                patch.object(
                    portable_cache,
                    "get_paper",
                    AsyncMock(side_effect=lambda paper_id: metadata.get(paper_id)),
                ),
                patch.object(portable_cache, "load_runs", return_value=[]),
                patch.object(
                    portable_cache,
                    "list_pdf_export_runs",
                    AsyncMock(return_value=[]),
                ),
            ):
                for paper_id, now in (("first", 0), ("second", 10)):
                    export = build_portable_export(
                        paper_id,
                        metadata[paper_id],
                        papers_root=papers,
                        agent_root=root / "agent_workspace",
                        cache_root=root / "portable_manifest_cache",
                    )
                    await portable_cache.acknowledge_portable_cache(
                        paper_id,
                        export.manifest["revision"],
                        now=now,
                    )
                one_size = portable_cache._directory_size(papers / "second")
                result = await portable_cache.enforce_portable_cache_limits(
                    now=1000,
                    idle_seconds=10_000,
                    max_bytes=one_size + 1,
                )

            self.assertFalse((papers / "first").exists())
            self.assertTrue((papers / "second").exists())
            self.assertTrue((papers / "server-only").exists())
            self.assertEqual(result["evicted"][0]["paper_id"], "first")
            self.assertEqual(result["evicted"][0]["reason"], "capacity_lru")
            self.assertTrue(result["limit_satisfied"])

    async def test_idle_cache_expires_after_seven_days(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            papers = root / "papers"
            metadata = _write_paper(papers, "idle")
            with (
                patch.object(storage_files, "DATA_DIR", root),
                patch.object(storage_files, "PAPERS_DIR", papers),
                patch.object(
                    portable_cache,
                    "get_paper",
                    AsyncMock(return_value=metadata),
                ),
                patch.object(portable_cache, "load_runs", return_value=[]),
                patch.object(
                    portable_cache,
                    "list_pdf_export_runs",
                    AsyncMock(return_value=[]),
                ),
            ):
                export = build_portable_export(
                    "idle",
                    metadata,
                    papers_root=papers,
                    agent_root=root / "agent_workspace",
                    cache_root=root / "portable_manifest_cache",
                )
                await portable_cache.acknowledge_portable_cache(
                    "idle",
                    export.manifest["revision"],
                    now=0,
                )
                result = await portable_cache.enforce_portable_cache_limits(
                    now=portable_cache.PORTABLE_CACHE_IDLE_SECONDS + 1,
                    max_bytes=10_000,
                )
            self.assertFalse((papers / "idle").exists())
            self.assertEqual(result["evicted"][0]["reason"], "idle_expired")

    async def test_cleanup_skips_active_and_revision_stale_papers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            papers = root / "papers"
            lease_meta = _write_paper(papers, "leased")
            stale_meta = _write_paper(papers, "stale")
            metadata = {"leased": lease_meta, "stale": stale_meta}
            with (
                patch.object(storage_files, "DATA_DIR", root),
                patch.object(storage_files, "PAPERS_DIR", papers),
                patch.object(
                    portable_cache,
                    "get_paper",
                    AsyncMock(side_effect=lambda paper_id: metadata.get(paper_id)),
                ),
                patch.object(portable_cache, "load_runs", return_value=[]),
                patch.object(
                    portable_cache,
                    "list_pdf_export_runs",
                    AsyncMock(return_value=[]),
                ),
            ):
                for paper_id in metadata:
                    export = build_portable_export(
                        paper_id,
                        metadata[paper_id],
                        papers_root=papers,
                        agent_root=root / "agent_workspace",
                        cache_root=root / "portable_manifest_cache",
                    )
                    await portable_cache.acknowledge_portable_cache(
                        paper_id,
                        export.manifest["revision"],
                        now=100,
                    )
                (papers / "stale" / "paper_note.md").write_text(
                    "# Unsynced",
                    encoding="utf-8",
                )
                result = await portable_cache.enforce_portable_cache_limits(
                    now=200,
                    idle_seconds=0,
                    max_bytes=0,
                )

            self.assertTrue((papers / "leased").exists())
            self.assertTrue((papers / "stale").exists())
            reasons = {item["paper_id"]: item["reason"] for item in result["skipped"]}
            self.assertEqual(reasons["leased"], "active_lease")
            self.assertEqual(reasons["stale"], "active_lease")

            with (
                patch.object(storage_files, "DATA_DIR", root),
                patch.object(storage_files, "PAPERS_DIR", papers),
                patch.object(
                    portable_cache,
                    "get_paper",
                    AsyncMock(side_effect=lambda paper_id: metadata.get(paper_id)),
                ),
                patch.object(portable_cache, "load_runs", return_value=[]),
                patch.object(
                    portable_cache,
                    "list_pdf_export_runs",
                    AsyncMock(return_value=[]),
                ),
            ):
                later = await portable_cache.enforce_portable_cache_limits(
                    now=1000,
                    idle_seconds=0,
                    max_bytes=0,
                )
            later_reasons = {
                item["paper_id"]: item["reason"] for item in later["skipped"]
            }
            self.assertEqual(later_reasons["stale"], "local_revision_stale")

    async def test_waiting_permission_run_blocks_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            papers = root / "papers"
            metadata = _write_paper(papers, "waiting")
            with (
                patch.object(storage_files, "DATA_DIR", root),
                patch.object(storage_files, "PAPERS_DIR", papers),
                patch.object(
                    portable_cache,
                    "get_paper",
                    AsyncMock(return_value=metadata),
                ),
                patch.object(
                    portable_cache,
                    "load_runs",
                    return_value=[{"status": "waiting_permission"}],
                ),
                patch.object(
                    portable_cache,
                    "list_pdf_export_runs",
                    AsyncMock(return_value=[]),
                ),
            ):
                export = build_portable_export(
                    "waiting",
                    metadata,
                    papers_root=papers,
                    agent_root=root / "agent_workspace",
                    cache_root=root / "portable_manifest_cache",
                )
                await portable_cache.acknowledge_portable_cache(
                    "waiting",
                    export.manifest["revision"],
                    now=0,
                )
                result = await portable_cache.enforce_portable_cache_limits(
                    now=1000,
                    idle_seconds=0,
                    max_bytes=0,
                )
            self.assertTrue((papers / "waiting").exists())
            self.assertEqual(result["skipped"][0]["reason"], "agent_running")


def _write_paper(
    papers: Path,
    paper_id: str,
    *,
    padding: int = 0,
) -> dict:
    directory = papers / paper_id
    directory.mkdir(parents=True)
    document = PaperDocument(
        paper_id=paper_id,
        title=paper_id,
        source="local_pdf",
        extracted_at="2026-07-24T00:00:00Z",
        blocks=[
            Block(
                index=0,
                type="paragraph",
                original="original" + ("x" * padding),
                translation="译文",
                status="done",
            )
        ],
    )
    (directory / "translation.json").write_text(
        json.dumps(document.to_dict(), ensure_ascii=False),
        encoding="utf-8",
    )
    (directory / "original.pdf").write_bytes(b"%PDF" + (b"x" * padding))
    return {
        "arxiv_id": paper_id,
        "title": paper_id,
        "authors": [],
        "source": "local_pdf",
        "status": "translated",
    }


if __name__ == "__main__":
    unittest.main()
