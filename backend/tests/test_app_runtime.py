from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.api import main as api_main
from backend.api.main import app
from backend.runtime import RuntimeMode, resolve_runtime_mode


class AppRuntimeTest(unittest.TestCase):
    def test_runtime_mode_defaults_to_self_hosted(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(resolve_runtime_mode(), RuntimeMode.SELF_HOSTED)

    def test_invalid_runtime_mode_fails_closed(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "PEINIDU_RUNTIME_MODE"):
            resolve_runtime_mode("shared_cloud")

    def test_public_portal_does_not_create_content_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            papers_dir = data_dir / "papers"
            collections_dir = data_dir / "collections"
            with (
                patch.object(api_main, "DATA_DIR", data_dir),
                patch.object(api_main, "PAPERS_DIR", papers_dir),
                patch.object(api_main, "COLLECTIONS_DIR", collections_dir),
            ):
                portal_app = api_main.create_app(RuntimeMode.PUBLIC_PORTAL)
                with TestClient(portal_app) as client:
                    health = client.get("/health")

            self.assertEqual(health.status_code, 200)
            self.assertEqual(health.json()["runtime_mode"], "public_portal")
            self.assertFalse(health.json()["content_api_enabled"])
            self.assertFalse(data_dir.exists())
            self.assertFalse(papers_dir.exists())
            self.assertFalse(collections_dir.exists())

    def test_public_portal_exposes_no_content_or_credential_routes(self) -> None:
        portal_app = api_main.create_app("public_portal")
        prohibited = (
            "/papers",
            "/papers/example/portable-bundle",
            "/translate/example",
            "/analyze/example",
            "/collections",
            "/agent/chat/example",
            "/config",
            "/internal/llm/v1/chat/completions",
            "/assets/example/original.pdf",
            "/docs",
            "/openapi.json",
        )
        with TestClient(portal_app) as client:
            for path in prohibited:
                response = client.get(path)
                self.assertEqual(response.status_code, 404, path)

    def test_local_core_keeps_content_routes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch.object(api_main, "DATA_DIR", root / "data"),
                patch.object(api_main, "PAPERS_DIR", root / "data" / "papers"),
                patch.object(
                    api_main,
                    "COLLECTIONS_DIR",
                    root / "data" / "collections",
                ),
            ):
                local_app = api_main.create_app("local_core")
            paths = set(local_app.openapi()["paths"])

        self.assertIn("/papers", paths)
        self.assertIn("/config", paths)
        self.assertIn("/agent/chat/{arxiv_id}", paths)
        self.assertEqual(local_app.state.runtime_mode, "local_core")
        self.assertTrue(local_app.state.content_api_enabled)

    def test_ensure_runtime_dirs_creates_static_asset_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            papers_dir = data_dir / "papers"
            collections_dir = data_dir / "collections"

            with (
                patch.object(api_main, "DATA_DIR", data_dir),
                patch.object(api_main, "PAPERS_DIR", papers_dir),
                patch.object(api_main, "COLLECTIONS_DIR", collections_dir),
            ):
                api_main._ensure_runtime_dirs()

            self.assertTrue(data_dir.is_dir())
            self.assertTrue(papers_dir.is_dir())
            self.assertTrue(collections_dir.is_dir())

    def test_rate_limit_can_block_expensive_routes(self) -> None:
        api_main._RATE_LIMIT_STATE.clear()
        with (
            patch.dict("os.environ", {"PEINIDU_RATE_LIMIT_PER_MINUTE": "1"}),
            patch.object(api_main, "_rate_limit_applies", return_value=True),
            TestClient(app) as client,
        ):
            first = client.get("/health")
            second = client.get("/health")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)
        self.assertEqual(second.headers.get("Retry-After"), "60")

    def test_rate_limit_can_be_disabled(self) -> None:
        api_main._RATE_LIMIT_STATE.clear()
        with (
            patch.dict("os.environ", {"PEINIDU_RATE_LIMIT_PER_MINUTE": "0"}),
            patch.object(api_main, "_rate_limit_applies", return_value=True),
            TestClient(app) as client,
        ):
            first = client.get("/health")
            second = client.get("/health")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)


if __name__ == "__main__":
    unittest.main()
