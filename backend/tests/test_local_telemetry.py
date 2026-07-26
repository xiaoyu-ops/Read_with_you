from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.api import main as api_main
from backend.telemetry import local_client


class LocalTelemetryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sent: list[dict] = []
        self.env = patch.dict(
            "os.environ",
            {
                "PEINIDU_APP_DATA_DIR": str(self.root),
                "PEINIDU_TELEMETRY_ENDPOINT": "https://telemetry.example.com/events",
                "PEINIDU_APP_VERSION": "0.2.0",
            },
            clear=False,
        )
        self.sender = patch.object(
            local_client,
            "_send_payload",
            side_effect=lambda payload: self.sent.append(dict(payload)),
        )
        self.env.start()
        self.sender.start()

    def tearDown(self) -> None:
        self.sender.stop()
        self.env.stop()
        self.temp.cleanup()

    def test_default_is_disabled_and_public_settings_hide_install_id(self) -> None:
        settings = local_client.get_settings()
        raw = json.loads(
            (self.root / "settings" / "anonymous-usage.json").read_text(encoding="utf-8")
        )

        self.assertFalse(settings["enabled"])
        self.assertEqual(local_client.send_event("reader_opened"), {"status": "disabled"})
        self.assertEqual(self.sent, [])
        self.assertIn("install_id", raw)
        self.assertNotIn("install_id", json.dumps(settings))

    def test_explicit_enable_sends_daily_pseudonym_and_deduplicates(self) -> None:
        enabled = local_client.update_settings(True)
        reader = local_client.send_event("reader_opened")
        duplicate = local_client.send_event("reader_opened")
        raw = json.loads(
            (self.root / "settings" / "anonymous-usage.json").read_text(encoding="utf-8")
        )

        self.assertTrue(enabled["enabled"])
        self.assertEqual(enabled["initial_event"], {"status": "sent"})
        self.assertEqual(reader, {"status": "sent"})
        self.assertEqual(duplicate, {"status": "already_sent"})
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.sent[0]["daily_id"], self.sent[1]["daily_id"])
        self.assertEqual(len(self.sent[0]["daily_id"]), 64)
        self.assertNotEqual(self.sent[0]["daily_id"], raw["install_id"])
        self.assertEqual(
            set(self.sent[0]),
            {"event_date", "daily_id", "event", "platform", "app_version"},
        )
        serialized = json.dumps(self.sent)
        for forbidden in ("paper", "note", "prompt", "api_key", "path"):
            self.assertNotIn(forbidden, serialized)

    def test_disable_stops_future_events(self) -> None:
        local_client.update_settings(True)
        disabled = local_client.update_settings(False)
        result = local_client.send_event("agent_response")

        self.assertFalse(disabled["enabled"])
        self.assertEqual(result, {"status": "disabled"})
        self.assertEqual(len(self.sent), 1)

    def test_unsafe_endpoint_fails_closed(self) -> None:
        with patch.dict(
            "os.environ",
            {"PEINIDU_TELEMETRY_ENDPOINT": "http://telemetry.example.com/events"},
        ):
            settings = local_client.get_settings()
            result = local_client.update_settings(True)

        self.assertFalse(settings["available"])
        self.assertFalse(result["enabled"])
        self.assertEqual(result["initial_event"], {"status": "failed"})
        self.assertEqual(self.sent, [])

    def test_local_core_routes_and_schema(self) -> None:
        app = api_main.create_app("local_core")
        with TestClient(app) as client:
            read = client.get("/telemetry/settings")
            invalid = client.put(
                "/telemetry/settings",
                json={"enabled": True, "paper_id": "secret"},
            )
            enabled = client.put("/telemetry/settings", json={"enabled": True})
            event = client.post("/telemetry/events", json={"event": "reader_opened"})
            forbidden = client.post(
                "/telemetry/events",
                json={"event": "reader_opened", "title": "secret"},
            )

        self.assertEqual(read.status_code, 200)
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(enabled.status_code, 200)
        self.assertIn(event.json()["status"], {"sent", "already_sent"})
        self.assertEqual(forbidden.status_code, 422)

    def test_local_core_startup_does_not_emit_telemetry(self) -> None:
        with patch.object(local_client, "send_event") as send_event:
            app = api_main.create_app("local_core")
            with TestClient(app) as client:
                self.assertEqual(client.get("/health").status_code, 200)

        send_event.assert_not_called()

    def test_self_hosted_and_public_portal_do_not_expose_local_controls(self) -> None:
        for mode in ("self_hosted", "public_portal"):
            app = api_main.create_app(mode)
            with TestClient(app) as client:
                self.assertEqual(client.get("/telemetry/settings").status_code, 404)


if __name__ == "__main__":
    unittest.main()
