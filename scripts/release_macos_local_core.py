#!/usr/bin/env python3
"""Sign, notarize and prepare a public Peinidu macOS release.

This script is intentionally separate from the credential-free candidate
builder. It must only run inside the protected production-release environment.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from scripts import build_local_core_bundle as bundle


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_NODE_ENTITLEMENTS = ROOT / "config" / "macos-node.entitlements.plist"
DEFAULT_REPOSITORY = "xiaoyu-ops/Read_with_you"


class ReleaseError(RuntimeError):
    pass


def _run(command: Sequence[str]) -> None:
    subprocess.run(list(command), cwd=ROOT, check=True)


def _capture(command: Sequence[str]) -> str:
    result = subprocess.run(
        list(command),
        cwd=ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout


def _is_macho(path: Path) -> bool:
    if not path.is_file() or path.is_symlink():
        return False
    result = subprocess.run(
        ["file", "-b", str(path)],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return "Mach-O" in result.stdout


def _nested_code_targets(app: Path) -> list[Path]:
    targets: list[Path] = []
    for path in app.rglob("*"):
        if path.is_symlink():
            continue
        if path.is_file() and _is_macho(path):
            targets.append(path)
        elif path.is_dir() and path.suffix.lower() in {
            ".app",
            ".appex",
            ".framework",
            ".plugin",
            ".xpc",
        }:
            targets.append(path)
    targets = [path for path in targets if path != app]
    return sorted(
        targets,
        key=lambda path: (len(path.relative_to(app).parts), path.as_posix()),
        reverse=True,
    )


def _codesign(
    path: Path,
    *,
    identity: str,
    entitlements: Path | None = None,
) -> None:
    command = [
        "codesign",
        "--force",
        "--timestamp",
        "--options",
        "runtime",
        "--sign",
        identity,
    ]
    if entitlements is not None:
        command.extend(["--entitlements", str(entitlements)])
    command.append(str(path))
    _run(command)


def sign_macos_app(
    app: Path,
    *,
    identity: str,
    node_entitlements: Path = DEFAULT_NODE_ENTITLEMENTS,
) -> None:
    if app.name != "Peinidu.app" or not app.is_dir():
        raise ReleaseError(f"Expected Peinidu.app, got: {app}")
    if not identity.startswith("Developer ID Application:"):
        raise ReleaseError("A Developer ID Application identity is required")
    if not node_entitlements.is_file():
        raise ReleaseError(f"Node entitlements file missing: {node_entitlements}")

    for target in _nested_code_targets(app):
        relative = target.relative_to(app).as_posix()
        entitlements = (
            node_entitlements
            if relative.endswith("Contents/Resources/runtime/node")
            else None
        )
        _codesign(target, identity=identity, entitlements=entitlements)
    _codesign(app, identity=identity)
    _run(
        [
            "codesign",
            "--verify",
            "--deep",
            "--strict",
            "--verbose=2",
            str(app),
        ]
    )


def sign_macos_dmg(dmg: Path, *, identity: str) -> None:
    _run(
        [
            "codesign",
            "--force",
            "--timestamp",
            "--sign",
            identity,
            str(dmg),
        ]
    )
    _run(["codesign", "--verify", "--strict", "--verbose=2", str(dmg)])


def notarize_macos_dmg(
    dmg: Path,
    *,
    api_key: Path,
    key_id: str,
    issuer_id: str,
) -> str:
    try:
        payload = json.loads(
            _capture(
                [
                    "xcrun",
                    "notarytool",
                    "submit",
                    str(dmg),
                    "--key",
                    str(api_key),
                    "--key-id",
                    key_id,
                    "--issuer",
                    issuer_id,
                    "--wait",
                    "--output-format",
                    "json",
                ]
            )
        )
    except (json.JSONDecodeError, subprocess.CalledProcessError) as exc:
        raise ReleaseError("Apple notarization request failed") from exc
    status = payload.get("status")
    submission_id = payload.get("id")
    if status != "Accepted" or not isinstance(submission_id, str) or not submission_id:
        raise ReleaseError(
            "Apple notarization did not accept the DMG: "
            f"{status or 'unknown status'}"
        )
    _run(["xcrun", "stapler", "staple", str(dmg)])
    _run(["xcrun", "stapler", "validate", str(dmg)])
    _run(
        [
            "spctl",
            "--assess",
            "--type",
            "open",
            "--context",
            "context:primary-signature",
            "--verbose=2",
            str(dmg),
        ]
    )
    return submission_id


def write_portal_release_manifest(
    destination: Path,
    *,
    version: str,
    repository: str,
    artifact: Path,
    published_at: str,
) -> Path:
    normalized_version = bundle.normalize_version(version)
    tag = f"v{normalized_version}"
    repository = repository.strip().strip("/")
    if repository.count("/") != 1:
        raise ReleaseError("GitHub repository must be in owner/name form")
    if not artifact.is_file() or artifact.stat().st_size <= 0:
        raise ReleaseError(f"Release artifact missing: {artifact}")
    payload = {
        "schema_version": 1,
        "channel": "beta",
        "version": normalized_version,
        "release_url": f"https://github.com/{repository}/releases/tag/{tag}",
        "published_at": published_at,
        "downloads": {
            "macos_arm64": {
                "filename": artifact.name,
                "url": (
                    f"https://github.com/{repository}/releases/download/"
                    f"{tag}/{artifact.name}"
                ),
                "sha256": bundle._sha256_file(artifact),
                "size_bytes": artifact.stat().st_size,
                "signed": True,
                "notarized": True,
            }
        },
    }
    return bundle.write_manifest(destination, payload)


def write_release_notes(
    destination: Path,
    *,
    version: str,
    commit_sha: str,
    artifact: Path,
) -> Path:
    sha256 = bundle._sha256_file(artifact)
    destination.write_text(
        f"""# 陪你读 Local Core v{bundle.normalize_version(version)}

首个公开 Beta，支持 Apple Silicon Mac。安装包已使用 Developer ID 签名并通过
Apple notarization。

## 安装

1. 下载 `{artifact.name}` 并打开。
2. 将 `Peinidu.app` 拖入 Applications。
3. 启动“陪你读”，然后打开 `http://127.0.0.1:8520`。

需要 macOS 14 或更高版本，推荐使用最新版 Chrome/Chromium。

## 完整性

- SHA-256：`{sha256}`
- 源码 commit：`{commit_sha}`

## 隐私边界

Core 只监听本机 `127.0.0.1`。论文、PDF、翻译、笔记、对话与 Provider Key
不会成为公网账号资产；Key 只进入 macOS Keychain。

## 已知限制

- 当前只提供 Apple Silicon 版本，不支持 Intel Mac。
- 本 Beta 不包含自动更新；新版本继续通过 GitHub Releases 发布。
- Playwright browser 与中文 PDF 导出不进入默认 Core。

## 卸载

将 `Peinidu.app` 移到废纸篓只删除应用，不会自动删除论文与笔记。需要彻底清理时，
请另行删除用户应用数据目录；操作前先备份本地文献文件夹。
""",
        encoding="utf-8",
    )
    return destination


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sign and notarize a Peinidu macOS Local Core release"
    )
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--candidate-manifest", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--repository", default=DEFAULT_REPOSITORY)
    parser.add_argument("--identity", required=True)
    parser.add_argument("--team-id", required=True)
    parser.add_argument("--notary-key", type=Path, required=True)
    parser.add_argument("--notary-key-id", required=True)
    parser.add_argument("--notary-issuer-id", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if os.sys.platform != "darwin":
        raise ReleaseError("macOS releases must be prepared on macOS")
    version = bundle.normalize_version(args.version)
    if len(args.commit_sha) != 40 or any(
        value not in "0123456789abcdef" for value in args.commit_sha.lower()
    ):
        raise ReleaseError("commit SHA must be the reviewed 40-character Git SHA")
    if f"({args.team_id})" not in args.identity:
        raise ReleaseError("Signing identity does not belong to APPLE_TEAM_ID")
    try:
        candidate = json.loads(
            args.candidate_manifest.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseError("Candidate provenance manifest is invalid") from exc
    if (
        candidate.get("version") != version
        or candidate.get("platform") != "darwin"
        or candidate.get("architecture") != "arm64"
        or candidate.get("signed") is not False
        or candidate.get("notarized") is not False
    ):
        raise ReleaseError("Candidate provenance does not match the release target")

    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    app = args.app.expanduser().resolve()
    sign_macos_app(app, identity=args.identity)
    asset_stem = bundle.release_asset_stem(
        version,
        system_name="darwin",
        machine_name="arm64",
    )
    notices = bundle.write_third_party_notices(
        output_dir / f"{asset_stem}-THIRD_PARTY_NOTICES.txt",
        candidate.get("third_party", []),
    )
    artifact = bundle.create_macos_dmg(
        app,
        output_dir / f"{asset_stem}.dmg",
        third_party_notices=notices,
    )
    sign_macos_dmg(artifact, identity=args.identity)
    submission_id = notarize_macos_dmg(
        artifact,
        api_key=args.notary_key.expanduser().resolve(),
        key_id=args.notary_key_id,
        issuer_id=args.notary_issuer_id,
    )
    _run(["spctl", "--assess", "--type", "execute", "--verbose=2", str(app)])
    artifact_sha256 = bundle._sha256_file(artifact)
    sha256_path = bundle.write_sha256_sidecar(artifact)

    provenance = bundle.build_manifest(
        app,
        version=version,
        build_epoch=int(candidate["build_epoch"]),
        third_party=candidate.get("third_party", []),
    )
    provenance["signed"] = True
    provenance["notarized"] = True
    provenance["signing"] = {
        "team_id": args.team_id,
        "notary_submission_id": submission_id,
    }
    provenance["artifact"] = {
        "filename": artifact.name,
        "media_type": "application/x-apple-diskimage",
        "size_bytes": artifact.stat().st_size,
        "sha256": artifact_sha256,
    }
    provenance["archive"] = artifact.name
    provenance["archive_sha256"] = artifact_sha256
    provenance_path = bundle.write_manifest(
        output_dir / f"{artifact.name}.manifest.json",
        provenance,
    )
    published_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    portal_manifest = write_portal_release_manifest(
        output_dir / "release-manifest.json",
        version=version,
        repository=args.repository,
        artifact=artifact,
        published_at=published_at,
    )
    release_notes = write_release_notes(
        output_dir / "RELEASE_NOTES.md",
        version=version,
        commit_sha=args.commit_sha.lower(),
        artifact=artifact,
    )
    print(
        json.dumps(
            {
                "artifact": str(artifact),
                "sha256": str(sha256_path),
                "provenance": str(provenance_path),
                "portal_manifest": str(portal_manifest),
                "release_notes": str(release_notes),
                "signed": True,
                "notarized": True,
                "notary_submission_id": submission_id,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
