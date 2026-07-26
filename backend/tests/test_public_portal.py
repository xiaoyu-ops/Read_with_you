from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.api import main as api_main
from backend.telemetry import portal_store


class PublicPortalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manifest = self.root / "release.json"
        self.manifest.write_text(
            json.dumps(
                {
                    "version": "0.2.0",
                    "downloads": {
                        "macos_arm64": {
                            "url": "https://downloads.example.com/peinidu-mac.zip",
                            "sha256": "a" * 64,
                            "size_bytes": 1024,
                        },
                        "windows_x64": {
                            "url": "https://downloads.example.com/peinidu-win.zip",
                            "sha256": "b" * 64,
                            "size_bytes": 2048,
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        self.env = patch.dict(
            "os.environ",
            {
                "PEINIDU_PORTAL_DATA_DIR": str(self.root / "portal-data"),
                "PEINIDU_RELEASE_MANIFEST": str(self.manifest),
                "PEINIDU_RATE_LIMIT_PER_MINUTE": "0",
            },
            clear=False,
        )
        self.env.start()
        self.app = api_main.create_app("public_portal")
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.client.close()
        self.env.stop()
        self.temp.cleanup()

    def _event(self, **overrides):
        payload = {
            "event_date": datetime.now(timezone.utc).date().isoformat(),
            "daily_id": "c" * 64,
            "event": "core_started",
            "platform": "macos",
            "app_version": "0.2.0",
        }
        payload.update(overrides)
        return payload

    def test_portal_home_and_privacy_are_content_free_html(self) -> None:
        home = self.client.get("/")
        privacy = self.client.get("/privacy")

        self.assertEqual(home.status_code, 200)
        self.assertIn("网页是入口，论文仍在你的电脑里", home.text)
        self.assertIn("上传论文</dt><dd class=\"zero\">0", home.text)
        self.assertEqual(home.headers["Referrer-Policy"], "no-referrer")
        self.assertIn("frame-ancestors 'none'", home.headers["Content-Security-Policy"])
        self.assertEqual(privacy.status_code, 200)
        self.assertIn("你的论文不是我们的数据", privacy.text)
        for path in (
            "/papers",
            "/config",
            "/telemetry/settings",
            "/agent/chat/example",
            "/assets/example/original.pdf",
            "/openapi.json",
        ):
            self.assertEqual(self.client.get(path).status_code, 404, path)

    def test_release_metadata_hides_origin_url_and_download_counts(self) -> None:
        release = self.client.get("/api/portal/releases/latest")
        self.assertEqual(release.status_code, 200)
        text = release.text
        self.assertNotIn("downloads.example.com", text)
        self.assertEqual(len(release.json()["downloads"]), 2)

        download = self.client.get(
            "/api/portal/download/macos_arm64",
            follow_redirects=False,
        )
        self.assertEqual(download.status_code, 307)
        self.assertEqual(
            download.headers["location"],
            "https://downloads.example.com/peinidu-mac.zip",
        )
        self.assertEqual(self.client.get("/api/portal/stats").json()["total_downloads"], 1)

    def test_telemetry_is_strict_daily_deduplicated_and_aggregated(self) -> None:
        first = self.client.post("/api/portal/telemetry", json=self._event())
        duplicate = self.client.post("/api/portal/telemetry", json=self._event())
        reader = self.client.post(
            "/api/portal/telemetry",
            json=self._event(event="reader_opened"),
        )
        stats = self.client.get("/api/portal/stats").json()

        self.assertEqual(first.json(), {"status": "recorded"})
        self.assertEqual(duplicate.json(), {"status": "duplicate"})
        self.assertEqual(reader.json(), {"status": "recorded"})
        self.assertEqual(stats["active_today"], 1)
        self.assertEqual(stats["readers_today"], 1)
        self.assertFalse(stats["privacy"]["cross_day_identifier"])
        self.assertFalse(stats["privacy"]["content_collected"])

    def test_telemetry_rejects_content_and_invalid_identity(self) -> None:
        with_content = self._event(paper_title="secret paper")
        invalid_id = self._event(daily_id="device-123")
        old_date = self._event(
            event_date=(datetime.now(timezone.utc).date() - timedelta(days=3)).isoformat()
        )

        self.assertEqual(
            self.client.post("/api/portal/telemetry", json=with_content).status_code,
            422,
        )
        self.assertEqual(
            self.client.post("/api/portal/telemetry", json=invalid_id).status_code,
            422,
        )
        self.assertEqual(
            self.client.post("/api/portal/telemetry", json=old_date).status_code,
            422,
        )

    def test_database_has_no_ip_user_agent_or_content_columns(self) -> None:
        self.client.post("/api/portal/telemetry", json=self._event())
        with closing(sqlite3.connect(portal_store.database_path())) as connection:
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(daily_events)").fetchall()
            }
            stored = connection.execute(
                "SELECT daily_id, event, platform, app_version FROM daily_events"
            ).fetchone()

        self.assertEqual(
            columns,
            {
                "event_date",
                "daily_id",
                "event",
                "platform",
                "app_version",
                "received_at",
            },
        )
        self.assertEqual(stored[0], "c" * 64)
        self.assertFalse(
            {"ip", "user_agent", "paper_id", "title", "path", "prompt", "key"} & columns
        )

    def test_invalid_release_manifest_fails_closed(self) -> None:
        self.manifest.write_text(
            json.dumps(
                {
                    "version": "0.2.0",
                    "downloads": {
                        "macos_arm64": {
                            "url": "http://insecure.example.com/app.zip",
                            "sha256": "a" * 64,
                            "size_bytes": 1024,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual(
            self.client.get("/api/portal/releases/latest").status_code,
            503,
        )
        self.assertEqual(
            self.client.get(
                "/api/portal/download/macos_arm64",
                follow_redirects=False,
            ).status_code,
            404,
        )


if __name__ == "__main__":
    unittest.main()
