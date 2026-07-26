from __future__ import annotations

import io
import json
import tempfile
import unittest
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import routes_portable_bundle
from backend.extraction.blocks import Block, PaperDocument
from backend.storage import files as storage_files
from backend.storage.portable_bundle import (
    PortableBundleError,
    apply_staged_portable_bundle,
    build_portable_export,
    parse_portable_manifest,
    stage_portable_files,
    validate_portable_path,
)


class PortableBundleTest(unittest.TestCase):
    def test_cache_ack_and_lease_endpoints_validate_state(self) -> None:
        app = FastAPI()
        app.include_router(routes_portable_bundle.router)
        revision = "a" * 64
        with (
            patch.object(
                routes_portable_bundle,
                "acknowledge_portable_cache",
                AsyncMock(
                    return_value={
                        "synced_revision": revision,
                        "storage_mode": "local_folder",
                    }
                ),
            ),
            patch.object(
                routes_portable_bundle,
                "renew_portable_cache_lease",
                AsyncMock(return_value={"lease_until": 123.0}),
            ),
            TestClient(app) as client,
        ):
            invalid = client.post(
                "/papers/cache-paper/portable-bundle/ack",
                json={"revision": "bad"},
            )
            acknowledged = client.post(
                "/papers/cache-paper/portable-bundle/ack",
                json={"revision": revision},
            )
            leased = client.post("/papers/cache-paper/portable-bundle/lease")
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(acknowledged.status_code, 200)
        self.assertTrue(acknowledged.json()["cache_acknowledged"])
        self.assertEqual(leased.json()["lease_until"], 123.0)

    def test_api_rejects_revision_conflict_before_overwrite(self) -> None:
        app = FastAPI()
        app.include_router(routes_portable_bundle.router)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            papers = root / "papers"
            paper = papers / "conflict-paper"
            paper.mkdir(parents=True)
            (paper / "translation.json").write_text(
                json.dumps(_document("conflict-paper").to_dict()),
                encoding="utf-8",
            )
            (paper / "original.pdf").write_bytes(b"%PDF-conflict")
            metadata = {
                "arxiv_id": "conflict-paper",
                "title": "Conflict Paper",
                "authors": [],
                "source": "local_pdf",
                "status": "translated",
            }
            exported = build_portable_export(
                "conflict-paper",
                metadata,
                papers_root=papers,
                agent_root=root / "agent_workspace",
                cache_root=root / "portable_manifest_cache",
            )
            manifest = {
                **exported.manifest,
                "bundle_type": "full",
                "base_revision": "0" * 64,
                "included_paths": [
                    entry["path"] for entry in exported.manifest["files"]
                ],
            }
            payloads = [
                source.file_path.read_bytes()
                for source in sorted(exported.sources, key=lambda item: item.path)
            ]
            parts = [
                (
                    "manifest",
                    (
                        "manifest.json",
                        json.dumps(manifest).encode(),
                        "application/json",
                    ),
                )
            ]
            parts.extend(
                (
                    "file",
                    (f"{index}.bin", payload, "application/octet-stream"),
                )
                for index, payload in enumerate(payloads)
            )
            with (
                patch.object(storage_files, "DATA_DIR", root),
                patch.object(storage_files, "PAPERS_DIR", papers),
                patch.object(
                    routes_portable_bundle,
                    "get_paper",
                    AsyncMock(return_value=metadata),
                ),
                TestClient(app) as client,
            ):
                response = client.post(
                    "/papers/portable-bundle",
                    files=parts,
                    data={"conflict_policy": "reject"},
                )
            self.assertEqual(response.status_code, 409)
            self.assertEqual(
                response.json()["detail"]["code"],
                "portable_revision_conflict",
            )

    def test_api_streams_bundle_and_restores_uploaded_files(self) -> None:
        app = FastAPI()
        app.include_router(routes_portable_bundle.router)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            papers = root / "papers"
            paper = papers / "api-paper"
            paper.mkdir(parents=True)
            (paper / "translation.json").write_text(
                json.dumps(_document("api-paper").to_dict()),
                encoding="utf-8",
            )
            (paper / "original.pdf").write_bytes(b"%PDF-api")
            metadata = {
                "arxiv_id": "api-paper",
                "title": "API Paper",
                "authors": ["Ada"],
                "source": "local_pdf",
                "status": "translated",
            }
            with (
                patch.object(storage_files, "DATA_DIR", root),
                patch.object(storage_files, "PAPERS_DIR", papers),
                patch.object(
                    routes_portable_bundle,
                    "get_paper",
                    AsyncMock(side_effect=[metadata, None]),
                ),
                patch.object(
                    routes_portable_bundle,
                    "insert_paper",
                    AsyncMock(return_value=1),
                ),
                patch.object(
                    routes_portable_bundle,
                    "update_status",
                    AsyncMock(),
                ),
                patch.object(
                    routes_portable_bundle,
                    "safe_sync_paper_note_index",
                    AsyncMock(return_value=True),
                ),
                patch.object(
                    routes_portable_bundle,
                    "sync_agent_session_index",
                    AsyncMock(return_value=0),
                ),
                TestClient(app) as client,
            ):
                downloaded = client.get("/papers/api-paper/portable-bundle")
                self.assertEqual(downloaded.status_code, 200)
                manifest, payloads = _parse_multipart_response(downloaded)
                self.assertEqual(manifest["paper_id"], "api-paper")
                self.assertEqual(len(payloads), len(manifest["included_paths"]))

                # Simulate a missing server cache, then recover from the local package.
                import shutil

                shutil.rmtree(paper)
                upload_parts = [
                    (
                        "manifest",
                        (
                            "manifest.json",
                            json.dumps(manifest).encode(),
                            "application/json",
                        ),
                    )
                ]
                upload_parts.extend(
                    (
                        "file",
                        (f"{index}.bin", payload, "application/octet-stream"),
                    )
                    for index, payload in enumerate(payloads)
                )
                restored = client.post(
                    "/papers/portable-bundle",
                    files=upload_parts,
                    data={"conflict_policy": "reject"},
                )
                self.assertEqual(restored.status_code, 200, restored.text)
                self.assertEqual((paper / "original.pdf").read_bytes(), b"%PDF-api")

    def test_full_delta_and_restore_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            papers = root / "papers"
            agent = root / "agent"
            cache = root / "cache"
            paper_dir = papers / "portable-paper"
            paper_dir.mkdir(parents=True)
            document = _document("portable-paper")
            (paper_dir / "translation.json").write_text(
                json.dumps(document.to_dict(), ensure_ascii=False),
                encoding="utf-8",
            )
            (paper_dir / "original.md").write_text("Paper body", encoding="utf-8")
            (paper_dir / "original.pdf").write_bytes(b"%PDF-1.7\nportable")
            (paper_dir / "paper_note.md").write_text("# Note", encoding="utf-8")
            pdf_pages = paper_dir / "pdf_pages"
            pdf_pages.mkdir()
            (pdf_pages / "page-01.png").write_bytes(b"derived-page-cache")
            chat = agent / "chats" / "portable-paper.json"
            chat.parent.mkdir(parents=True)
            chat.write_text(
                json.dumps(
                    {
                        "arxiv_id": "portable-paper",
                        "messages": [{"role": "user", "content": "Question"}],
                    }
                ),
                encoding="utf-8",
            )
            metadata = {
                "title": "Portable Paper",
                "authors": ["Ada"],
                "source": "local_pdf",
                "status": "translated",
            }

            full = build_portable_export(
                "portable-paper",
                metadata,
                papers_root=papers,
                agent_root=agent,
                cache_root=cache,
            )
            self.assertEqual(full.manifest["bundle_type"], "full")
            self.assertIn("paper/original.pdf", full.manifest["included_paths"])
            self.assertIn("agent/chat.json", full.manifest["included_paths"])
            self.assertNotIn(
                "paper/pdf_pages/page-01.png",
                full.manifest["included_paths"],
            )

            unchanged = build_portable_export(
                "portable-paper",
                metadata,
                base_revision=full.manifest["revision"],
                papers_root=papers,
                agent_root=agent,
                cache_root=cache,
            )
            self.assertEqual(unchanged.manifest["bundle_type"], "delta")
            self.assertEqual(unchanged.sources, ())

            (paper_dir / "paper_note.md").write_text("# Changed", encoding="utf-8")
            delta = build_portable_export(
                "portable-paper",
                metadata,
                base_revision=full.manifest["revision"],
                papers_root=papers,
                agent_root=agent,
                cache_root=cache,
            )
            self.assertEqual(delta.manifest["included_paths"], ["paper/paper_note.md"])

            latest = build_portable_export(
                "portable-paper",
                metadata,
                papers_root=papers,
                agent_root=agent,
                cache_root=cache,
            )
            restore_papers = root / "restore-papers"
            restore_agent = root / "restore-agent"
            manifest = parse_portable_manifest(
                json.dumps(latest.manifest).encode("utf-8")
            )
            streams = [
                io.BytesIO(source.file_path.read_bytes()) for source in latest.sources
            ]
            stage = stage_portable_files(
                manifest,
                streams,
                staging_parent=root,
            )
            apply_staged_portable_bundle(
                manifest,
                stage,
                papers_root=restore_papers,
                agent_root=restore_agent,
            )

            restored = restore_papers / "portable-paper"
            self.assertEqual((restored / "paper_note.md").read_text(), "# Changed")
            self.assertEqual(
                json.loads(
                    (restore_agent / "chats" / "portable-paper.json").read_text()
                )["messages"][0]["content"],
                "Question",
            )

    def test_manifest_rejects_traversal_duplicate_and_hash_mismatch(self) -> None:
        with self.assertRaises(PortableBundleError) as traversal:
            validate_portable_path("paper/../../config/config.yaml")
        self.assertEqual(traversal.exception.code, "portable_invalid_path")

        manifest = _manifest(
            [
                {
                    "path": "paper/original.pdf",
                    "size": 4,
                    "sha256": "0" * 64,
                },
                {
                    "path": "paper/original.pdf",
                    "size": 4,
                    "sha256": "0" * 64,
                },
            ]
        )
        with self.assertRaises(PortableBundleError) as duplicate:
            parse_portable_manifest(json.dumps(manifest).encode())
        self.assertEqual(duplicate.exception.code, "portable_duplicate_path")

        valid = _manifest(
            [
                {
                    "path": "paper/original.pdf",
                    "size": 4,
                    "sha256": "0" * 64,
                },
                {
                    "path": "paper/translation.json",
                    "size": 2,
                    "sha256": "0" * 64,
                },
            ]
        )
        parsed = parse_portable_manifest(json.dumps(valid).encode())
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(PortableBundleError) as mismatch:
                stage_portable_files(
                    parsed,
                    [io.BytesIO(b"%PDF"), io.BytesIO(b"{}")],
                    staging_parent=Path(tmp),
                )
        self.assertEqual(mismatch.exception.code, "portable_hash_mismatch")

    def test_export_rejects_symlinks_and_missing_source_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            papers = root / "papers"
            paper = papers / "unsafe"
            paper.mkdir(parents=True)
            (paper / "translation.json").write_text(
                json.dumps(_document("unsafe").to_dict()),
                encoding="utf-8",
            )
            with self.assertRaises(PortableBundleError) as missing:
                build_portable_export(
                    "unsafe",
                    {"title": "Unsafe", "authors": []},
                    papers_root=papers,
                    agent_root=root / "agent",
                    cache_root=root / "cache",
                )
            self.assertEqual(missing.exception.code, "source_pdf_missing")

            (paper / "original.pdf").write_bytes(b"%PDF")
            (paper / "assets").mkdir()
            (paper / "assets" / "link.png").symlink_to(paper / "original.pdf")
            with self.assertRaises(PortableBundleError) as symlink:
                build_portable_export(
                    "unsafe",
                    {"title": "Unsafe", "authors": []},
                    papers_root=papers,
                    agent_root=root / "agent",
                    cache_root=root / "cache",
                )
            self.assertEqual(symlink.exception.code, "portable_symlink_rejected")


def _document(paper_id: str) -> PaperDocument:
    return PaperDocument(
        paper_id=paper_id,
        title="Portable Paper",
        source="local_pdf",
        extracted_at="2026-07-24T00:00:00Z",
        blocks=[
            Block(
                index=0,
                type="paragraph",
                original="Original",
                translation="译文",
                status="done",
            )
        ],
    )


def _manifest(entries: list[dict]) -> dict:
    return {
        "version": 1,
        "paper_id": "portable-paper",
        "paper": {
            "title": "Portable Paper",
            "authors": [],
            "source": "local_pdf",
            "status": "translated",
        },
        "revision": "",
        "base_revision": None,
        "bundle_type": "full",
        "files": entries,
        "included_paths": [entry["path"] for entry in entries],
        "deleted_paths": [],
    }


def _parse_multipart_response(response) -> tuple[dict, list[bytes]]:
    message = BytesParser(policy=policy.default).parsebytes(
        (
            f"Content-Type: {response.headers['content-type']}\r\n"
            "MIME-Version: 1.0\r\n\r\n"
        ).encode()
        + response.content
    )
    parts = list(message.iter_parts())
    manifest = json.loads(parts[0].get_payload(decode=True))
    payloads = [part.get_payload(decode=True) for part in parts[1:]]
    return manifest, payloads


if __name__ == "__main__":
    unittest.main()
