from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts import build_local_core_bundle as bundle


class LocalCorePackagingTest(unittest.TestCase):
    def test_copy_frontend_standalone_preserves_dist_and_public_assets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build = root / ".next-local-core"
            (build / "standalone").mkdir(parents=True)
            (build / "standalone" / "server.js").write_text("server", encoding="utf-8")
            (build / "required-server-files.json").write_text(
                json.dumps({"config": {"images": {"unoptimized": True}}}),
                encoding="utf-8",
            )
            (build / "static").mkdir()
            (build / "static" / "app.js").write_text("static", encoding="utf-8")
            public = root / "public"
            public.mkdir()
            (public / "icon.svg").write_text("<svg/>", encoding="utf-8")
            destination = root / "bundle" / "frontend"

            original_frontend = bundle.FRONTEND
            bundle.FRONTEND = root
            try:
                bundle.copy_frontend_standalone(build, destination)
            finally:
                bundle.FRONTEND = original_frontend

            self.assertTrue((destination / "server.js").is_file())
            self.assertEqual(
                (destination / ".next-local-core" / "static" / "app.js").read_text(),
                "static",
            )
            self.assertTrue((destination / "public" / "icon.svg").is_file())

    def test_copy_frontend_rejects_a_runtime_image_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build = root / ".next-local-core"
            (build / "standalone").mkdir(parents=True)
            (build / "standalone" / "server.js").write_text("server", encoding="utf-8")
            (build / "required-server-files.json").write_text(
                json.dumps({"config": {"images": {"unoptimized": False}}}),
                encoding="utf-8",
            )
            (build / "static").mkdir()
            with self.assertRaisesRegex(bundle.BundleError, "images.unoptimized"):
                bundle.copy_frontend_standalone(build, root / "bundle" / "frontend")

    def test_local_runtime_rejects_compiled_development_api_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "server.js").write_text(
                'fetch("http://127.0.0.1:8000/papers")',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(bundle.BundleError, "development API base"):
                bundle.validate_local_api_runtime(root)

    def test_privacy_scan_rejects_env_and_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text("SAFE=1", encoding="utf-8")
            with self.assertRaises(bundle.BundleError):
                bundle.scan_bundle(root)

            (root / ".env").unlink()
            (root / "settings.json").write_text(
                '{"api_key":"sk-examplecredential123456"}',
                encoding="utf-8",
            )
            with self.assertRaises(bundle.BundleError):
                bundle.scan_bundle(root)

    def test_manifest_and_archive_are_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Peinidu"
            root.mkdir()
            executable = root / "Peinidu.exe"
            executable.write_bytes(b"binary")
            manifest = bundle.build_manifest(
                root,
                version="0.1.0",
                build_epoch=315532800,
                third_party=[],
            )
            manifest_path = Path(tmp) / "release-manifest.json"
            bundle.write_manifest(manifest_path, manifest)
            first = Path(tmp) / "first.zip"
            second = Path(tmp) / "second.zip"
            bundle.write_reproducible_zip(root, first, epoch=315532800)
            bundle.write_reproducible_zip(root, second, epoch=315532800)

            self.assertEqual(bundle._sha256_file(first), bundle._sha256_file(second))
            self.assertEqual(manifest["files"][0]["sha256"], bundle._sha256_file(executable))
            saved = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["version"], "0.1.0")
            self.assertFalse(saved["signed"])


if __name__ == "__main__":
    unittest.main()
