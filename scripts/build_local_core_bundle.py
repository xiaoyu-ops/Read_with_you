#!/usr/bin/env python3
"""Build reproducible, credential-free Peinidu local Core bundles.

The script runs on the target OS. PyInstaller freezes the Python Core, while
the exact Node executable, Next standalone output and Poppler tools are copied
beside it. Signing/notarization remain an explicit release step.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
DEFAULT_DIST_DIR = ROOT / "dist" / "local-core"
APP_NAME = "Peinidu"
DMG_VOLUME_NAME = "陪你读"
_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_TEXT_SCAN_SUFFIXES = {
    ".css",
    ".html",
    ".js",
    ".json",
    ".md",
    ".mjs",
    ".txt",
    ".yaml",
    ".yml",
}
_SECRET_PATTERNS = (
    re.compile(rb"(?i)(?:api[_-]?key|secret|token)\s*[:=]\s*[\"']?[A-Za-z0-9_-]{20,}"),
    re.compile(rb"\bsk-[A-Za-z0-9_-]{16,}\b"),
)
_DENIED_RELATIVE_PATHS = {
    ".env",
    "config/config.yaml",
    "data",
    "cache",
    "papers",
    "chats",
    "runs",
}
_FORBIDDEN_LOCAL_API_BASE = b"http://127.0.0.1:8000"


class BundleError(RuntimeError):
    pass


def normalize_version(value: str) -> str:
    version = value.strip().removeprefix("v")
    if not _SEMVER_RE.fullmatch(version):
        raise BundleError(f"Local Core version must be SemVer: {value!r}")
    return version


def normalized_platform(
    system_name: str | None = None,
    machine_name: str | None = None,
) -> tuple[str, str]:
    system_value = (system_name or platform.system()).strip().lower()
    machine_value = (machine_name or platform.machine()).strip().lower()
    system_aliases = {
        "darwin": "darwin",
        "windows": "windows",
    }
    architecture_aliases = {
        "aarch64": "arm64",
        "arm64": "arm64",
        "amd64": "x64",
        "x64": "x64",
        "x86_64": "x64",
    }
    normalized_system = system_aliases.get(system_value)
    normalized_architecture = architecture_aliases.get(machine_value)
    if normalized_system is None or normalized_architecture is None:
        raise BundleError(
            "Unsupported release target: "
            f"{system_name or platform.system()} / {machine_name or platform.machine()}"
        )
    if normalized_system == "darwin" and normalized_architecture != "arm64":
        raise BundleError("The first public macOS release only supports Apple Silicon")
    return normalized_system, normalized_architecture


def release_asset_stem(
    version: str,
    *,
    system_name: str | None = None,
    machine_name: str | None = None,
) -> str:
    normalized_version = normalize_version(version)
    system_value, architecture = normalized_platform(system_name, machine_name)
    return (
        f"peinidu-local-core-v{normalized_version}-"
        f"{system_value}-{architecture}"
    )


def _run(command: Sequence[str], *, cwd: Path = ROOT, env: dict | None = None) -> None:
    subprocess.run(list(command), cwd=cwd, env=env, check=True)


def npm_executable_name(os_name: str | None = None) -> str:
    return "npm.cmd" if (os_name or os.name) == "nt" else "npm"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_symlink(path: Path) -> str:
    return hashlib.sha256(f"symlink:{os.readlink(path)}".encode()).hexdigest()


def _copy_tree(source: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination, symlinks=True)


def build_frontend(build_dir_name: str) -> Path:
    build_dir = FRONTEND / build_dir_name
    env = os.environ.copy()
    env.update(
        {
            "NEXT_PUBLIC_API_BASE": "/api",
            "NEXT_DIST_DIR": build_dir_name,
            "NODE_ENV": "production",
        }
    )
    generated_config = (FRONTEND / "next-env.d.ts", FRONTEND / "tsconfig.json")
    originals = {
        path: path.read_bytes() if path.exists() else None for path in generated_config
    }
    try:
        _run([npm_executable_name(), "run", "build"], cwd=FRONTEND, env=env)
    finally:
        for path, original in originals.items():
            if original is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(original)
    return build_dir


def copy_frontend_standalone(build_dir: Path, destination: Path) -> None:
    standalone = build_dir / "standalone"
    static_dir = build_dir / "static"
    public_dir = FRONTEND / "public"
    if not (standalone / "server.js").is_file():
        raise BundleError(f"Next standalone server missing: {standalone / 'server.js'}")
    server_files_path = build_dir / "required-server-files.json"
    try:
        server_files = json.loads(server_files_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BundleError(
            f"Next required-server-files missing or invalid: {server_files_path}"
        ) from exc
    if server_files.get("config", {}).get("images", {}).get("unoptimized") is not True:
        raise BundleError(
            "Next images.unoptimized must be true so the signed app stays immutable"
        )
    _copy_tree(standalone, destination)
    dist_name = build_dir.name
    _copy_tree(static_dir, destination / dist_name / "static")
    _copy_tree(public_dir, destination / "public")


def validate_local_api_runtime(frontend_runtime: Path) -> None:
    for path in sorted(frontend_runtime.rglob("*")):
        if (
            not path.is_file()
            or path.is_symlink()
            or path.suffix.lower() not in {".html", ".js", ".json", ".mjs"}
        ):
            continue
        if _FORBIDDEN_LOCAL_API_BASE in path.read_bytes():
            raise BundleError(
                "Local Core frontend contains the development API base: "
                f"{path.relative_to(frontend_runtime).as_posix()}"
            )


def _find_license(binary: Path, candidates: Iterable[str]) -> Path | None:
    binary = binary.resolve()
    roots = [binary.parent, binary.parent.parent]
    for root in roots:
        for candidate in candidates:
            path = root / candidate
            if path.is_file():
                return path
    return None


def copy_node_runtime(node: Path, resource_root: Path) -> dict:
    node = node.expanduser().resolve()
    if not node.is_file():
        raise BundleError(f"Node executable missing: {node}")
    runtime = resource_root / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    target = runtime / ("node.exe" if os.name == "nt" else "node")
    if sys.platform == "darwin":
        _copy_macos_node(node, target)
    else:
        shutil.copy2(node, target)
        target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    license_path = _find_license(node, ("LICENSE", "../LICENSE"))
    if license_path is None:
        raise BundleError("Node LICENSE was not found beside the selected runtime")
    notice = resource_root / "third_party" / "node"
    notice.mkdir(parents=True, exist_ok=True)
    shutil.copy2(license_path, notice / "LICENSE")
    version = subprocess.check_output([str(node), "--version"], text=True).strip()
    return {"name": "Node.js", "version": version, "license": "third_party/node/LICENSE"}


def _mac_rpaths(path: Path) -> list[Path]:
    output = subprocess.check_output(["otool", "-l", str(path)], text=True)
    values: list[Path] = []
    lines = output.splitlines()
    for index, line in enumerate(lines):
        if line.strip() != "cmd LC_RPATH":
            continue
        for detail in lines[index + 1 : index + 5]:
            match = re.search(r"\bpath (.+?) \(offset \d+\)", detail.strip())
            if not match:
                continue
            value = match.group(1)
            value = value.replace("@loader_path", str(path.parent))
            value = value.replace("@executable_path", str(path.parent))
            values.append(Path(value))
            break
    return values


def _resolve_mac_dependency(path: Path, install_name: str) -> Path | None:
    if install_name.startswith(("/System/Library/", "/usr/lib/")):
        return None
    if install_name.startswith("/"):
        return Path(install_name).resolve()
    if install_name.startswith("@loader_path/"):
        return (path.parent / install_name.removeprefix("@loader_path/")).resolve()
    if install_name.startswith("@executable_path/"):
        return (path.parent / install_name.removeprefix("@executable_path/")).resolve()
    if install_name.startswith("@rpath/"):
        suffix = install_name.removeprefix("@rpath/")
        for rpath in _mac_rpaths(path):
            candidate = (rpath / suffix).resolve()
            if candidate.is_file():
                return candidate
    raise BundleError(f"Unable to resolve macOS dependency {install_name} for {path}")


def _mac_dependencies(path: Path) -> list[tuple[str, Path]]:
    output = subprocess.check_output(["otool", "-L", str(path)], text=True)
    dependencies: list[tuple[str, Path]] = []
    for line in output.splitlines()[1:]:
        value = line.strip().split(" (", 1)[0]
        resolved = _resolve_mac_dependency(path, value)
        if resolved is None:
            continue
        dependencies.append((value, resolved))
    return dependencies


def _copy_macos_node(source: Path, target: Path) -> None:
    lib_dir = target.parent / "node-lib"
    if target.exists():
        target.chmod(target.stat().st_mode | stat.S_IWUSR)
    if lib_dir.exists():
        shutil.rmtree(lib_dir)
    lib_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    sources: dict[Path, Path] = {source.resolve(): target}
    queue: deque[Path] = deque([source.resolve()])
    while queue:
        current = queue.popleft()
        for _install_name, resolved in _mac_dependencies(current):
            if resolved in sources:
                continue
            destination = lib_dir / resolved.name
            if destination.exists() and _sha256_file(destination) != _sha256_file(resolved):
                raise BundleError(f"Conflicting Node dependency basename: {resolved.name}")
            shutil.copy2(resolved, destination)
            sources[resolved] = destination
            queue.append(resolved)

    dependency_names = {dependency: destination.name for dependency, destination in sources.items()}
    for original, destination in sources.items():
        for install_name, resolved in _mac_dependencies(original):
            if resolved not in dependency_names:
                continue
            if destination == target:
                replacement = f"@loader_path/node-lib/{dependency_names[resolved]}"
            else:
                replacement = f"@loader_path/{dependency_names[resolved]}"
            _run(
                [
                    "install_name_tool",
                    "-change",
                    install_name,
                    replacement,
                    str(destination),
                ]
            )
        _run(["codesign", "--force", "--sign", "-", str(destination)])


def _copy_macos_poppler(source_bin: Path, destination: Path) -> None:
    bin_dir = destination / "bin"
    lib_dir = destination / "lib"
    bin_dir.mkdir(parents=True, exist_ok=True)
    lib_dir.mkdir(parents=True, exist_ok=True)
    sources: dict[Path, Path] = {}
    queue: deque[Path] = deque()
    for name in ("pdfinfo", "pdftotext"):
        source = (source_bin / name).resolve()
        if not source.is_file():
            raise BundleError(f"Poppler executable missing: {source_bin / name}")
        target = bin_dir / name
        shutil.copy2(source, target)
        target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        sources[source] = target
        queue.append(source)

    while queue:
        current = queue.popleft()
        for _install_name, resolved in _mac_dependencies(current):
            if resolved in sources:
                continue
            target = lib_dir / resolved.name
            if target.exists() and _sha256_file(target) != _sha256_file(resolved):
                raise BundleError(f"Conflicting Poppler dependency basename: {resolved.name}")
            shutil.copy2(resolved, target)
            sources[resolved] = target
            queue.append(resolved)

    dependency_names = {source: target.name for source, target in sources.items()}
    for source, target in sources.items():
        for install_name, resolved in _mac_dependencies(source):
            if resolved not in dependency_names:
                continue
            if target.parent == bin_dir:
                replacement = f"@loader_path/../lib/{dependency_names[resolved]}"
            else:
                replacement = f"@loader_path/{dependency_names[resolved]}"
            _run(
                [
                    "install_name_tool",
                    "-change",
                    install_name,
                    replacement,
                    str(target),
                ]
            )
        _run(["codesign", "--force", "--sign", "-", str(target)])


def _copy_windows_poppler(source_bin: Path, destination: Path) -> None:
    for name in ("pdfinfo.exe", "pdftotext.exe"):
        if not (source_bin / name).is_file():
            raise BundleError(f"Poppler executable missing: {source_bin / name}")
    _copy_tree(source_bin, destination / "bin")
    share = source_bin.parent / "share" / "poppler"
    if share.is_dir():
        _copy_tree(share, destination / "share" / "poppler")


def copy_poppler_runtime(source_bin: Path, resource_root: Path) -> dict:
    source_bin = source_bin.expanduser().resolve()
    destination = resource_root / "runtime" / "poppler"
    if sys.platform == "darwin":
        _copy_macos_poppler(source_bin, destination)
    elif os.name == "nt":
        _copy_windows_poppler(source_bin, destination)
    else:
        raise BundleError("Local Core release bundles currently support macOS and Windows")

    license_path = _find_license(
        source_bin / ("pdftotext.exe" if os.name == "nt" else "pdftotext"),
        ("../COPYING", "../LICENSE", "../share/licenses/poppler/COPYING"),
    )
    if license_path is None:
        raise BundleError("Poppler license was not found beside the selected runtime")
    notice = resource_root / "third_party" / "poppler"
    notice.mkdir(parents=True, exist_ok=True)
    shutil.copy2(license_path, notice / "COPYING")
    executable = destination / "bin" / ("pdfinfo.exe" if os.name == "nt" else "pdfinfo")
    version_output = subprocess.check_output([str(executable), "-v"], text=True, stderr=subprocess.STDOUT)
    version_line = version_output.splitlines()[0].strip() if version_output else "unknown"
    return {
        "name": "Poppler",
        "version": version_line,
        "license": "third_party/poppler/COPYING",
    }


def freeze_python_core(work_dir: Path) -> Path:
    dist_dir = work_dir / "pyinstaller-dist"
    import importlib.util

    litellm_spec = importlib.util.find_spec("litellm")
    if litellm_spec is None or not litellm_spec.submodule_search_locations:
        raise BundleError("LiteLLM is not installed in the build environment")
    litellm_root = Path(next(iter(litellm_spec.submodule_search_locations)))
    keyring_backend = (
        "keyring.backends.macOS" if sys.platform == "darwin" else "keyring.backends.Windows"
    )
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--clean",
        "--noconfirm",
        "--onedir",
        "--name",
        APP_NAME,
        "--distpath",
        str(dist_dir),
        "--workpath",
        str(work_dir / "pyinstaller-work"),
        "--specpath",
        str(work_dir / "pyinstaller-spec"),
        "--paths",
        str(ROOT),
        "--copy-metadata",
        "litellm",
        "--copy-metadata",
        "keyring",
        "--hidden-import",
        keyring_backend,
        "--collect-submodules",
        "litellm.llms.openai",
        "--collect-submodules",
        "litellm.llms.deepseek",
        "--collect-submodules",
        "litellm.llms.anthropic",
        "--collect-submodules",
        "litellm.llms.gemini",
        "--collect-submodules",
        "litellm.llms.ollama",
        "--collect-submodules",
        "litellm.litellm_core_utils",
        "--hidden-import",
        "tiktoken_ext.openai_public",
        "--hidden-import",
        "litellm.proxy._types",
        "--hidden-import",
        "uvicorn.logging",
        "--hidden-import",
        "uvicorn.loops.auto",
        "--hidden-import",
        "uvicorn.protocols.http.auto",
        "--hidden-import",
        "uvicorn.protocols.websockets.auto",
        "--add-data",
        f"{ROOT / 'sidecar' / 'pdf_export'}{os.pathsep}sidecar/pdf_export",
        "--add-data",
        f"{ROOT / 'deploy' / 'nginx.conf'}{os.pathsep}deploy",
        "--exclude-module",
        "numpy",
        "--exclude-module",
        "pandas",
        "--exclude-module",
        "PIL",
        "--exclude-module",
        "scipy",
        "--exclude-module",
        "torch",
    ]
    for data_name in (
        "anthropic_beta_headers_config.json",
        "cost.json",
        "model_prices_and_context_window_backup.json",
        "provider_endpoints_support_backup.json",
    ):
        source = litellm_root / data_name
        if source.is_file():
            command.extend(["--add-data", f"{source}{os.pathsep}litellm"])
    for source, destination in (
        (
            litellm_root / "containers" / "endpoints.json",
            "litellm/containers",
        ),
        (
            litellm_root / "llms" / "openai_like" / "providers.json",
            "litellm/llms/openai_like",
        ),
    ):
        if source.is_file():
            command.extend(["--add-data", f"{source}{os.pathsep}{destination}"])
    tokenizer_dir = litellm_root / "litellm_core_utils" / "tokenizers"
    for source in sorted(tokenizer_dir.iterdir()):
        if source.is_file() and source.suffix not in {".py", ".pyc"}:
            command.extend(
                [
                    "--add-data",
                    f"{source}{os.pathsep}litellm/litellm_core_utils/tokenizers",
                ]
            )
    if sys.platform == "darwin" or os.name == "nt":
        command.append("--windowed")
    command.append(str(ROOT / "scripts" / "peinidu_local_core.py"))
    env = os.environ.copy()
    env["PYINSTALLER_CONFIG_DIR"] = str(work_dir / "pyinstaller-cache")
    _run(command, env=env)
    if sys.platform == "darwin":
        result = dist_dir / f"{APP_NAME}.app"
    else:
        result = dist_dir / APP_NAME
    if not result.exists():
        raise BundleError(f"PyInstaller output missing: {result}")
    return result


def _resource_root(bundle_root: Path) -> Path:
    if bundle_root.suffix == ".app":
        return bundle_root / "Contents" / "Resources"
    return bundle_root


def assemble_bundle(
    frozen: Path,
    *,
    frontend_build: Path,
    node: Path,
    poppler_bin: Path,
    output_dir: Path,
) -> tuple[Path, list[dict]]:
    bundle_root = output_dir / frozen.name
    _copy_tree(frozen, bundle_root)
    resources = _resource_root(bundle_root)
    copy_frontend_standalone(frontend_build, resources / "frontend")
    validate_local_api_runtime(resources / "frontend")
    config_dir = resources / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "config" / "config.example.yaml", config_dir / "config.example.yaml")
    third_party = [
        copy_node_runtime(node, resources),
        copy_poppler_runtime(poppler_bin, resources),
    ]
    return bundle_root, third_party


def scan_bundle(bundle_root: Path) -> None:
    violations: list[str] = []
    environment_secrets = [
        value.encode()
        for name, value in os.environ.items()
        if re.search(r"(?i)(?:key|secret|token|password)", name)
        and len(value) >= 12
    ]
    for path in sorted(bundle_root.rglob("*")):
        if path.is_dir() or path.is_symlink():
            continue
        relative = path.relative_to(bundle_root).as_posix()
        normalized = relative.removeprefix("Contents/Resources/")
        if normalized in _DENIED_RELATIVE_PATHS or normalized.startswith(
            ("data/", "cache/", "papers/", "chats/", "runs/")
        ):
            violations.append(f"denied path: {relative}")
            continue
        if path.name == ".env":
            violations.append(f"denied path: {relative}")
            continue
        size = path.stat().st_size
        if size <= 25_000_000 and environment_secrets:
            content = path.read_bytes()
            if any(secret in content for secret in environment_secrets):
                violations.append(f"environment credential: {relative}")
                continue
        if (
            path.suffix.lower() not in _TEXT_SCAN_SUFFIXES
            or size > 2_000_000
            or normalized.startswith(("frontend/node_modules/", "third_party/", "litellm/"))
        ):
            continue
        content = path.read_bytes()
        if any(pattern.search(content) for pattern in _SECRET_PATTERNS):
            violations.append(f"possible credential: {relative}")
    if violations:
        raise BundleError("Bundle privacy scan failed: " + "; ".join(violations[:10]))


def build_manifest(
    bundle_root: Path,
    *,
    version: str,
    build_epoch: int,
    third_party: list[dict],
) -> dict:
    normalized_version = normalize_version(version)
    system_value, architecture = normalized_platform()
    files: list[dict] = []
    for path in sorted(bundle_root.rglob("*")):
        if path.is_dir():
            continue
        relative = path.relative_to(bundle_root).as_posix()
        if relative.endswith("release-manifest.json"):
            continue
        if path.is_symlink():
            files.append(
                {
                    "path": relative,
                    "type": "symlink",
                    "target": os.readlink(path),
                    "sha256": _hash_symlink(path),
                }
            )
        else:
            files.append(
                {
                    "path": relative,
                    "type": "file",
                    "size": path.stat().st_size,
                    "sha256": _sha256_file(path),
                }
            )
    return {
        "schema_version": 1,
        "product": "Peinidu Local Core",
        "version": normalized_version,
        "platform": system_value,
        "architecture": architecture,
        "build_epoch": build_epoch,
        "signed": False,
        "notarized": False,
        "third_party": third_party,
        "files": files,
    }


def write_manifest(destination: Path, manifest: dict) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return destination


def finalize_bundle(bundle_root: Path, *, version: str) -> None:
    normalized_version = normalize_version(version)
    resources = _resource_root(bundle_root)
    (resources / "release-info.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "product": "Peinidu Local Core",
                "version": normalized_version,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    if bundle_root.suffix == ".app":
        _run(["codesign", "--force", "--deep", "--sign", "-", str(bundle_root)])


def check_frozen_runtime(bundle_root: Path) -> None:
    if bundle_root.suffix == ".app":
        executable = bundle_root / "Contents" / "MacOS" / APP_NAME
    else:
        executable = bundle_root / f"{APP_NAME}.exe"
    if not executable.is_file():
        raise BundleError(f"Frozen Core executable missing: {executable}")
    with tempfile.TemporaryDirectory(prefix="peinidu-runtime-check-") as tmp:
        result = subprocess.run(
            [
                str(executable),
                "--package-root",
                str(_resource_root(bundle_root)),
                "--app-data-dir",
                tmp,
                "--check-runtime",
                "--no-browser",
            ],
            check=False,
            timeout=45,
        )
    if result.returncode != 0:
        raise BundleError(f"Frozen Core runtime check failed with exit {result.returncode}")


def write_reproducible_zip(source: Path, destination: Path, *, epoch: int) -> None:
    timestamp = datetime.fromtimestamp(max(epoch, 315532800), timezone.utc)
    zip_time = (timestamp.year, timestamp.month, timestamp.day, timestamp.hour, timestamp.minute, timestamp.second)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(source.rglob("*")):
            if path.is_dir():
                continue
            relative = (Path(source.name) / path.relative_to(source)).as_posix()
            info = zipfile.ZipInfo(relative, zip_time)
            mode = stat.S_IMODE(path.lstat().st_mode)
            if path.is_symlink():
                info.external_attr = (stat.S_IFLNK | mode) << 16
                archive.writestr(info, os.readlink(path).encode())
            else:
                info.external_attr = (stat.S_IFREG | mode) << 16
                with path.open("rb") as handle:
                    archive.writestr(info, handle.read())


def write_third_party_notices(destination: Path, third_party: list[dict]) -> Path:
    lines = [
        "陪你读 / Peinidu Local Core",
        "Third-party runtime notices",
        "",
        "The corresponding license files are included inside Peinidu.app.",
        "",
    ]
    for item in sorted(third_party, key=lambda value: str(value.get("name", ""))):
        name = str(item.get("name", "Unknown component"))
        version = str(item.get("version", "unknown"))
        license_path = str(item.get("license", "license path unavailable"))
        lines.extend((f"- {name} {version}", f"  License: {license_path}"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return destination


def create_macos_dmg(
    bundle_root: Path,
    destination: Path,
    *,
    third_party_notices: Path,
) -> Path:
    if bundle_root.suffix != ".app" or not bundle_root.is_dir():
        raise BundleError(f"macOS application bundle missing: {bundle_root}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="peinidu-dmg-") as tmp:
        staging = Path(tmp) / "volume"
        staging.mkdir()
        _copy_tree(bundle_root, staging / bundle_root.name)
        shutil.copy2(third_party_notices, staging / "THIRD_PARTY_NOTICES.txt")
        os.symlink("/Applications", staging / "Applications")
        _run(
            [
                "hdiutil",
                "create",
                "-quiet",
                "-volname",
                DMG_VOLUME_NAME,
                "-srcfolder",
                str(staging),
                "-ov",
                "-format",
                "UDZO",
                str(destination),
            ]
        )
    if not destination.is_file() or destination.stat().st_size <= 0:
        raise BundleError(f"macOS DMG was not created: {destination}")
    return destination


def write_sha256_sidecar(artifact: Path) -> Path:
    destination = artifact.with_name(f"{artifact.name}.sha256")
    destination.write_text(
        f"{_sha256_file(artifact)}  {artifact.name}\n",
        encoding="ascii",
    )
    return destination


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a Peinidu local Core bundle")
    parser.add_argument("--version", required=True)
    parser.add_argument("--node", type=Path, default=Path(shutil.which("node") or ""))
    parser.add_argument(
        "--poppler-bin",
        type=Path,
        default=Path(shutil.which("pdftotext") or "").parent,
    )
    parser.add_argument("--frontend-build-dir", type=Path)
    parser.add_argument("--frontend-dist-name", default=".next-local-core")
    parser.add_argument("--frozen-core", type=Path)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_DIST_DIR)
    parser.add_argument(
        "--source-date-epoch",
        type=int,
        default=int(os.environ.get("SOURCE_DATE_EPOCH", "315532800")),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if sys.platform != "darwin" and os.name != "nt":
        raise BundleError("Run the local Core builder on macOS or Windows")
    version = normalize_version(args.version)
    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    frontend_build = (
        args.frontend_build_dir.expanduser().resolve()
        if args.frontend_build_dir
        else build_frontend(args.frontend_dist_name)
    )
    with tempfile.TemporaryDirectory(prefix="peinidu-local-core-") as tmp:
        frozen = (
            args.frozen_core.expanduser().resolve()
            if args.frozen_core
            else freeze_python_core(Path(tmp))
        )
        bundle_root, third_party = assemble_bundle(
            frozen,
            frontend_build=frontend_build,
            node=args.node,
            poppler_bin=args.poppler_bin,
            output_dir=output_dir,
        )
        scan_bundle(bundle_root)
        finalize_bundle(bundle_root, version=version)
        check_frozen_runtime(bundle_root)
        manifest = build_manifest(
            bundle_root,
            version=version,
            build_epoch=args.source_date_epoch,
            third_party=third_party,
        )
        asset_stem = release_asset_stem(
            version,
            system_name=manifest["platform"],
            machine_name=manifest["architecture"],
        )
        notices = write_third_party_notices(
            output_dir / f"{asset_stem}-THIRD_PARTY_NOTICES.txt",
            third_party,
        )
        if manifest["platform"] == "darwin":
            artifact = create_macos_dmg(
                bundle_root,
                output_dir / f"{asset_stem}.dmg",
                third_party_notices=notices,
            )
            media_type = "application/x-apple-diskimage"
        else:
            artifact = output_dir / f"{asset_stem}.zip"
            write_reproducible_zip(
                bundle_root,
                artifact,
                epoch=args.source_date_epoch,
            )
            media_type = "application/zip"
        artifact_sha256 = _sha256_file(artifact)
        sha256_path = write_sha256_sidecar(artifact)
        manifest["artifact"] = {
            "filename": artifact.name,
            "media_type": media_type,
            "size_bytes": artifact.stat().st_size,
            "sha256": artifact_sha256,
        }
        # Preserve the v1 archive keys for existing tooling.
        manifest["archive"] = artifact.name
        manifest["archive_sha256"] = artifact_sha256
        manifest_path = write_manifest(
            output_dir / f"{artifact.name}.manifest.json",
            manifest,
        )
        summary = {
            "bundle": str(bundle_root),
            "artifact": str(artifact),
            "sha256": str(sha256_path),
            "manifest": str(manifest_path),
            "third_party_notices": str(notices),
            "artifact_sha256": artifact_sha256,
            "signed": False,
            "notarized": False,
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
