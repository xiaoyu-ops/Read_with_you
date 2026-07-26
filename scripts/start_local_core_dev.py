#!/usr/bin/env python3
"""Build the local frontend when needed, then start the source Pet Core."""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path
from typing import Iterable, Sequence


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
BUILD_NAME = ".next-local-core-dev"
RUNTIME_NAME = ".next-local-core-dev-runtime"
FINGERPRINT_PATH = FRONTEND / RUNTIME_NAME / ".source-fingerprint"
_FRONTEND_SOURCE_DIRS = ("app", "components", "lib", "public")
_FRONTEND_SOURCE_FILES = (
    "next.config.ts",
    "package-lock.json",
    "package.json",
    "postcss.config.mjs",
    "tsconfig.json",
)

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.local_core import launcher
from scripts import build_local_core_bundle as bundle


def _frontend_sources() -> Iterable[Path]:
    for name in _FRONTEND_SOURCE_FILES:
        path = FRONTEND / name
        if path.is_file():
            yield path
    for name in _FRONTEND_SOURCE_DIRS:
        directory = FRONTEND / name
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*")):
            if path.is_file() and not path.is_symlink():
                yield path


def frontend_source_fingerprint() -> str:
    digest = hashlib.sha256()
    for path in _frontend_sources():
        relative = path.relative_to(ROOT).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
    return digest.hexdigest()


def prepare_frontend_runtime(*, force: bool = False) -> tuple[Path, bool]:
    runtime = FRONTEND / RUNTIME_NAME
    fingerprint = frontend_source_fingerprint()
    if (
        not force
        and (runtime / "server.js").is_file()
        and FINGERPRINT_PATH.is_file()
        and FINGERPRINT_PATH.read_text(encoding="utf-8").strip() == fingerprint
    ):
        bundle.validate_local_api_runtime(runtime)
        return runtime, False

    build = bundle.build_frontend(BUILD_NAME)
    bundle.copy_frontend_standalone(build, runtime)
    bundle.validate_local_api_runtime(runtime)
    FINGERPRINT_PATH.write_text(f"{fingerprint}\n", encoding="utf-8")
    return runtime, True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="构建所需前端并启动源码版陪你读本地 Core",
        add_help=False,
    )
    parser.add_argument("--force-rebuild", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args, launcher_args = build_parser().parse_known_args(argv)
    gateway_url = f"http://127.0.0.1:{launcher.DEFAULT_GATEWAY_PORT}"
    if launcher.local_core_is_running(gateway_url):
        return launcher.main(launcher_args)

    runtime, rebuilt = prepare_frontend_runtime(force=args.force_rebuild)
    print("本地阅读界面已重新构建。" if rebuilt else "复用已验证的本地阅读界面。")
    return launcher.main(
        [
            "--package-root",
            str(ROOT),
            "--frontend-dir",
            str(runtime),
            *launcher_args,
        ]
    )


if __name__ == "__main__":
    raise SystemExit(main())
