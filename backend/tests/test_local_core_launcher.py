from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from backend.local_core import launcher


class _FakeResponse:
    status = 200

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class LocalCoreLauncherTest(unittest.TestCase):
    def test_configure_local_environment_uses_user_writable_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {},
            clear=True,
        ):
            root = (Path(tmp) / "Peinidu").resolve()
            launcher.configure_local_environment(root)

            self.assertEqual(os.environ["PEINIDU_RUNTIME_MODE"], "local_core")
            self.assertEqual(Path(os.environ["PEINIDU_APP_DATA_DIR"]), root)
            self.assertEqual(
                Path(os.environ["PEINIDU_DATA_DIR"]),
                root / "cache",
            )
            self.assertEqual(
                Path(os.environ["PEINIDU_CONFIG_PATH"]),
                root / "config" / "config.yaml",
            )
            self.assertTrue((root / "logs").is_dir())

    def test_bundled_runtime_prepends_poppler_without_overwriting_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"PATH": "/existing/bin", "DYLD_LIBRARY_PATH": "/existing/lib"},
            clear=True,
        ):
            root = Path(tmp)
            (root / "runtime" / "poppler" / "bin").mkdir(parents=True)
            (root / "runtime" / "poppler" / "lib").mkdir(parents=True)
            with patch.object(launcher.sys, "platform", "darwin"):
                launcher.configure_bundled_runtime(root)

            self.assertEqual(os.environ["PEINIDU_PACKAGE_ROOT"], str(root.resolve()))
            self.assertEqual(
                os.environ["PATH"].split(os.pathsep),
                [str(root.resolve() / "runtime" / "poppler" / "bin"), "/existing/bin"],
            )
            self.assertEqual(
                os.environ["DYLD_LIBRARY_PATH"].split(os.pathsep),
                [str(root.resolve() / "runtime" / "poppler" / "lib"), "/existing/lib"],
            )

    def test_configure_app_version_reads_release_metadata_without_overriding_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "release-info.json").write_text(
                json.dumps({"version": "0.2.0-dev"}),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                launcher.configure_app_version(root)
                self.assertEqual(os.environ["PEINIDU_APP_VERSION"], "0.2.0-dev")
            with patch.dict(
                os.environ,
                {"PEINIDU_APP_VERSION": "custom"},
                clear=True,
            ):
                launcher.configure_app_version(root)
                self.assertEqual(os.environ["PEINIDU_APP_VERSION"], "custom")

    def test_frozen_macos_package_root_uses_app_resources(self) -> None:
        with (
            patch.object(launcher.sys, "frozen", True, create=True),
            patch.object(
                launcher.sys,
                "executable",
                "/Applications/Peinidu.app/Contents/MacOS/Peinidu",
            ),
            patch.object(launcher.sys, "platform", "darwin"),
        ):
            self.assertEqual(
                launcher.default_package_root(),
                Path("/Applications/Peinidu.app/Contents/Resources"),
            )

    def test_running_probe_requires_local_core_identity(self) -> None:
        healthy = _FakeResponse(
            {
                "runtime_mode": "local_core",
                "content_api_enabled": True,
            }
        )
        portal = _FakeResponse(
            {
                "runtime_mode": "public_portal",
                "content_api_enabled": False,
            }
        )
        with patch("urllib.request.urlopen", return_value=healthy):
            self.assertTrue(launcher.local_core_is_running("http://127.0.0.1:8520"))
        with patch("urllib.request.urlopen", return_value=portal):
            self.assertFalse(launcher.local_core_is_running("http://127.0.0.1:8520"))

    def test_repeated_launch_opens_existing_core_without_starting_processes(self) -> None:
        with (
            patch.object(launcher, "local_core_is_running", return_value=True),
            patch.object(launcher.webbrowser, "open") as open_browser,
            patch.object(launcher, "_spawn_next") as spawn_next,
        ):
            result = launcher.main([])

        self.assertEqual(result, 0)
        open_browser.assert_called_once_with("http://127.0.0.1:8520")
        spawn_next.assert_not_called()

    def test_missing_node_returns_deterministic_error(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(launcher, "local_core_is_running", return_value=False),
            patch.object(launcher, "_resolve_node", side_effect=FileNotFoundError("missing")),
            patch("sys.stderr", new=MagicMock()),
        ):
            result = launcher.main(
                [
                    "--package-root",
                    tmp,
                    "--app-data-dir",
                    str(Path(tmp) / "data"),
                    "--no-browser",
                ]
            )

        self.assertEqual(result, 2)

    def test_runtime_check_does_not_start_services(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(launcher, "local_core_is_running", return_value=False),
            patch.object(launcher, "_resolve_node", return_value="/runtime/node"),
            patch.object(launcher, "_next_process_command", return_value=["node", "server.js"]),
            patch.object(launcher, "check_packaged_runtime", return_value=True) as check,
            patch.object(launcher, "_spawn_next") as spawn_next,
        ):
            result = launcher.main(
                [
                    "--package-root",
                    tmp,
                    "--frontend-dir",
                    str(Path(tmp) / "frontend"),
                    "--app-data-dir",
                    str(Path(tmp) / "data"),
                    "--check-runtime",
                    "--no-browser",
                ]
            )

        self.assertEqual(result, 0)
        check.assert_called_once()
        spawn_next.assert_not_called()


if __name__ == "__main__":
    unittest.main()
