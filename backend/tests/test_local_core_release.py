from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import release_macos_local_core as release


class LocalCoreReleaseTest(unittest.TestCase):
    def test_release_script_can_run_as_a_file(self) -> None:
        script = release.ROOT / "scripts" / "release_macos_local_core.py"
        result = subprocess.run(
            [sys.executable, str(script), "--help"],
            cwd=release.ROOT,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertIn("usage:", result.stdout)
        self.assertIn("--version", result.stdout)

    def test_signing_is_inside_out_and_node_receives_jit_entitlements(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / "Peinidu.app"
            node = app / "Contents" / "Resources" / "runtime" / "node"
            library = app / "Contents" / "Resources" / "runtime" / "libcore.dylib"
            executable = app / "Contents" / "MacOS" / "Peinidu"
            for path in (node, library, executable):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"mach-o")
            entitlements = Path(tmp) / "node-entitlements.plist"
            entitlements.write_text("<plist/>", encoding="utf-8")
            commands: list[list[str]] = []

            with (
                patch.object(release, "_is_macho", return_value=True),
                patch.object(
                    release,
                    "_run",
                    side_effect=lambda command: commands.append(list(command)),
                ),
            ):
                release.sign_macos_app(
                    app,
                    identity="Developer ID Application: Example (TEAM123456)",
                    node_entitlements=entitlements,
                )

            sign_commands = [
                command
                for command in commands
                if command and command[0] == "codesign" and "--sign" in command
            ]
            self.assertGreaterEqual(len(sign_commands), 4)
            self.assertEqual(sign_commands[-1][-1], str(app))
            node_command = next(
                command for command in sign_commands if command[-1] == str(node)
            )
            self.assertIn("--entitlements", node_command)
            self.assertEqual(
                node_command[node_command.index("--entitlements") + 1],
                str(entitlements),
            )
            non_node_commands = [
                command
                for command in sign_commands
                if command[-1] not in {str(node), str(app)}
            ]
            self.assertTrue(
                all("--entitlements" not in command for command in non_node_commands)
            )

    def test_notarization_must_be_accepted_before_stapling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dmg = Path(tmp) / "release.dmg"
            dmg.write_bytes(b"dmg")
            key = Path(tmp) / "AuthKey.p8"
            key.write_text("private", encoding="utf-8")
            commands: list[list[str]] = []
            with (
                patch.object(
                    release,
                    "_capture",
                    return_value=json.dumps(
                        {"status": "Accepted", "id": "submission-123"}
                    ),
                ),
                patch.object(
                    release,
                    "_run",
                    side_effect=lambda command: commands.append(list(command)),
                ),
            ):
                submission_id = release.notarize_macos_dmg(
                    dmg,
                    api_key=key,
                    key_id="KEY123",
                    issuer_id="ISSUER123",
                )

            self.assertEqual(submission_id, "submission-123")
            self.assertEqual(commands[0][:3], ["xcrun", "stapler", "staple"])
            self.assertEqual(commands[1][:3], ["xcrun", "stapler", "validate"])
            self.assertEqual(commands[2][0], "spctl")

            with (
                patch.object(
                    release,
                    "_capture",
                    return_value=json.dumps(
                        {"status": "Invalid", "id": "submission-456"}
                    ),
                ),
                patch.object(release, "_run") as run,
            ):
                with self.assertRaisesRegex(release.ReleaseError, "did not accept"):
                    release.notarize_macos_dmg(
                        dmg,
                        api_key=key,
                        key_id="KEY123",
                        issuer_id="ISSUER123",
                    )
                run.assert_not_called()

    def test_portal_manifest_only_describes_signed_notarized_github_asset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = (
                root
                / "peinidu-local-core-v0.1.0-beta.1-darwin-arm64.dmg"
            )
            artifact.write_bytes(b"signed notarized dmg")
            destination = root / "release-manifest.json"
            release.write_portal_release_manifest(
                destination,
                version="0.1.0-beta.1",
                repository="xiaoyu-ops/Read_with_you",
                artifact=artifact,
                published_at="2026-07-28T12:00:00+00:00",
            )

            payload = json.loads(destination.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["channel"], "beta")
            self.assertEqual(payload["version"], "0.1.0-beta.1")
            self.assertEqual(
                payload["release_url"],
                "https://github.com/xiaoyu-ops/Read_with_you/releases/tag/"
                "v0.1.0-beta.1",
            )
            item = payload["downloads"]["macos_arm64"]
            self.assertTrue(item["signed"])
            self.assertTrue(item["notarized"])
            self.assertEqual(item["filename"], artifact.name)
            self.assertTrue(item["url"].endswith(f"/{artifact.name}"))
            self.assertEqual(len(item["sha256"]), 64)

    def test_release_workflow_keeps_publication_behind_environment(self) -> None:
        workflow = (
            release.ROOT / ".github" / "workflows" / "local-core-release.yml"
        )
        text = workflow.read_text(encoding="utf-8")
        self.assertIn("environment: production-release", text)
        self.assertIn("contents: write", text)
        self.assertIn("MACOS_DEVELOPER_ID_CERT_P12_BASE64", text)
        self.assertIn("APPLE_API_PRIVATE_KEY_BASE64", text)
        self.assertEqual(
            text.count(
                "conda-incubator/setup-miniconda@"
                "8ee1f361103df19b6f8c8655fd3967a8ecb162d5"
            ),
            2,
        )
        self.assertEqual(text.count("miniforge-version: 25.3.1-0"), 2)
        self.assertEqual(text.count("poppler=24.08.0"), 2)
        self.assertEqual(text.count("poppler-24.08.0/COPYING"), 2)
        self.assertEqual(
            text.count(
                "ab15fd526bd8dd18a9e77ebc139656bf4d33e97fc7238cd11bf60e2b9b8666c6"
            ),
            2,
        )
        self.assertIn("--draft", text)
        self.assertIn("--prerelease", text)
        self.assertIn("--cleanup-tag", text)
        self.assertIn("gh release edit", text)
        self.assertLess(text.index("gh release upload"), text.index("gh release edit"))


if __name__ == "__main__":
    unittest.main()
