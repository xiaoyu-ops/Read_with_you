from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import build_local_core_bundle as bundle


class LocalCorePackagingTest(unittest.TestCase):
    def test_npm_executable_is_platform_specific(self) -> None:
        self.assertEqual(bundle.npm_executable_name("nt"), "npm.cmd")
        self.assertEqual(bundle.npm_executable_name("posix"), "npm")

    def test_build_workflow_bootstraps_pinned_runtimes_and_licenses(self) -> None:
        workflow = bundle.ROOT / ".github" / "workflows" / "local-core-build.yml"
        text = workflow.read_text(encoding="utf-8")
        setup = (
            "conda-incubator/setup-miniconda@"
            "8ee1f361103df19b6f8c8655fd3967a8ecb162d5"
        )
        self.assertIn(setup, text)
        self.assertIn("miniforge-version: 25.3.1-0", text)
        self.assertIn("--override-channels --channel conda-forge poppler=24.08.0", text)
        self.assertIn('"$RUNNER_TEMP/peinidu-poppler/bin/pdftotext" -v', text)
        license_url = (
            "https://invent.kde.org/mirrors/poppler/-/raw/"
            "poppler-24.08.0/COPYING"
        )
        license_sha256 = (
            "ab15fd526bd8dd18a9e77ebc139656bf4d33e97fc7238cd11bf60e2b9b8666c6"
        )
        self.assertEqual(text.count(license_url), 2)
        self.assertEqual(text.count(license_sha256), 2)
        self.assertIn('$licensePath = Join-Path $popplerRoot "COPYING"', text)
        self.assertIn("Poppler license checksum mismatch", text)
        self.assertLess(text.index(setup), text.index("Install pinned Poppler on macOS"))

    def test_release_asset_names_are_versioned_and_architecture_is_canonical(self) -> None:
        self.assertEqual(
            bundle.release_asset_stem(
                "v0.1.0-beta.1",
                system_name="Darwin",
                machine_name="aarch64",
            ),
            "peinidu-local-core-v0.1.0-beta.1-darwin-arm64",
        )
        self.assertEqual(
            bundle.release_asset_stem(
                "0.1.0-beta.1",
                system_name="Windows",
                machine_name="AMD64",
            ),
            "peinidu-local-core-v0.1.0-beta.1-windows-x64",
        )
        with self.assertRaisesRegex(bundle.BundleError, "SemVer"):
            bundle.normalize_version("../../release")
        with self.assertRaisesRegex(bundle.BundleError, "Apple Silicon"):
            bundle.normalized_platform("Darwin", "x86_64")

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
            with patch.object(
                bundle,
                "normalized_platform",
                return_value=("windows", "x64"),
            ):
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
            self.assertEqual(saved["architecture"], "x64")
            self.assertFalse(saved["signed"])
            self.assertFalse(saved["notarized"])

    def test_candidate_dmg_contains_applications_link_and_notices(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = root / "Peinidu.app"
            (app / "Contents" / "MacOS").mkdir(parents=True)
            (app / "Contents" / "MacOS" / "Peinidu").write_bytes(b"binary")
            notices = bundle.write_third_party_notices(
                root / "notices.txt",
                [
                    {
                        "name": "Node.js",
                        "version": "v22.14.0",
                        "license": "third_party/node/LICENSE",
                    }
                ],
            )
            destination = root / "candidate.dmg"
            observed: dict[str, object] = {}

            def fake_run(command, *, cwd=bundle.ROOT, env=None):
                observed["command"] = list(command)
                staging = Path(command[command.index("-srcfolder") + 1])
                observed["has_app"] = (staging / "Peinidu.app").is_dir()
                observed["applications_target"] = (
                    staging / "Applications"
                ).readlink()
                observed["notices"] = (
                    staging / "THIRD_PARTY_NOTICES.txt"
                ).read_text(encoding="utf-8")
                Path(command[-1]).write_bytes(b"candidate dmg")

            with patch.object(bundle, "_run", side_effect=fake_run):
                bundle.create_macos_dmg(
                    app,
                    destination,
                    third_party_notices=notices,
                )

            sidecar = bundle.write_sha256_sidecar(destination)
            self.assertEqual(observed["command"][0], "hdiutil")
            self.assertTrue(observed["has_app"])
            self.assertEqual(observed["applications_target"], Path("/Applications"))
            self.assertIn("Node.js v22.14.0", observed["notices"])
            self.assertEqual(
                sidecar.read_text(encoding="ascii"),
                f"{bundle._sha256_file(destination)}  candidate.dmg\n",
            )


if __name__ == "__main__":
    unittest.main()
