from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import start_local_core_dev as dev


class LocalCoreDevStartTest(unittest.TestCase):
    def test_prepare_frontend_reuses_matching_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            frontend = Path(tmp)
            runtime = frontend / dev.RUNTIME_NAME
            runtime.mkdir()
            (runtime / "server.js").write_text("server", encoding="utf-8")
            fingerprint = runtime / ".source-fingerprint"
            fingerprint.write_text("same\n", encoding="utf-8")
            with (
                patch.object(dev, "FRONTEND", frontend),
                patch.object(dev, "FINGERPRINT_PATH", fingerprint),
                patch.object(dev, "frontend_source_fingerprint", return_value="same"),
                patch.object(dev.bundle, "build_frontend") as build,
                patch.object(dev.bundle, "copy_frontend_standalone") as copy,
                patch.object(dev.bundle, "validate_local_api_runtime") as validate,
            ):
                prepared, rebuilt = dev.prepare_frontend_runtime()

            self.assertEqual(prepared, runtime)
            self.assertFalse(rebuilt)
            build.assert_not_called()
            copy.assert_not_called()
            validate.assert_called_once_with(runtime)

    def test_prepare_frontend_rebuilds_changed_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            frontend = Path(tmp)
            runtime = frontend / dev.RUNTIME_NAME
            build_dir = frontend / dev.BUILD_NAME
            fingerprint = runtime / ".source-fingerprint"

            def copy_runtime(_build: Path, destination: Path) -> None:
                destination.mkdir(parents=True)
                (destination / "server.js").write_text("server", encoding="utf-8")

            with (
                patch.object(dev, "FRONTEND", frontend),
                patch.object(dev, "FINGERPRINT_PATH", fingerprint),
                patch.object(dev, "frontend_source_fingerprint", return_value="changed"),
                patch.object(dev.bundle, "build_frontend", return_value=build_dir) as build,
                patch.object(
                    dev.bundle,
                    "copy_frontend_standalone",
                    side_effect=copy_runtime,
                ) as copy,
                patch.object(dev.bundle, "validate_local_api_runtime") as validate,
            ):
                prepared, rebuilt = dev.prepare_frontend_runtime()

            self.assertEqual(prepared, runtime)
            self.assertTrue(rebuilt)
            self.assertEqual(fingerprint.read_text(encoding="utf-8"), "changed\n")
            build.assert_called_once_with(dev.BUILD_NAME)
            copy.assert_called_once_with(build_dir, runtime)
            validate.assert_called_once_with(runtime)


if __name__ == "__main__":
    unittest.main()
