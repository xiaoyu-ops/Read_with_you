from __future__ import annotations

import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


class OptionalBrowserRuntimeTest(unittest.TestCase):
    def test_default_backend_does_not_bundle_browser_runtime(self) -> None:
        dockerfile = (ROOT / "backend" / "Dockerfile").read_text(encoding="utf-8")
        lowered = dockerfile.casefold()

        self.assertNotIn("nodejs", lowered)
        self.assertNotIn("npm install", lowered)
        self.assertNotIn("playwright install", lowered)
        self.assertNotIn("playwright_browsers_path", lowered)

    def test_browser_is_an_isolated_optional_profile(self) -> None:
        compose = yaml.safe_load(
            (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        )
        backend = compose["services"]["backend"]
        browser = compose["services"]["browser"]

        self.assertNotIn("security_opt", backend)
        self.assertEqual(browser["profiles"], ["browser"])
        self.assertNotIn("ports", browser)
        self.assertEqual(browser["mem_limit"], "1g")
        self.assertEqual(float(browser["cpus"]), 1.0)
        self.assertIn("@sha256:", browser["image"])
        self.assertNotIn("latest", browser["image"])
        self.assertEqual(
            set(browser["networks"]),
            {"browser-control-internal", "browser-egress"},
        )
        self.assertTrue(
            compose["networks"]["browser-control-internal"]["internal"]
        )
        self.assertNotIn("browser", backend.get("depends_on", {}))


if __name__ == "__main__":
    unittest.main()
