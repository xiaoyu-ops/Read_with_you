"""Cross-platform launcher for the browser-shaped local Pet Core."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Sequence


DEFAULT_GATEWAY_PORT = 8520
DEFAULT_FRONTEND_PORT = 8521
_CORE_START_TIMEOUT_SECONDS = 30.0


def default_app_data_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Peinidu"
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "Peinidu"
        return Path.home() / "AppData" / "Local" / "Peinidu"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "peinidu"


def configure_local_environment(app_data_dir: Path) -> None:
    app_data_dir = app_data_dir.expanduser().resolve()
    data_dir = app_data_dir / "cache"
    config_path = app_data_dir / "config" / "config.yaml"
    log_dir = app_data_dir / "logs"
    for directory in (data_dir, config_path.parent, log_dir):
        directory.mkdir(parents=True, exist_ok=True)
    os.environ["PEINIDU_RUNTIME_MODE"] = "local_core"
    os.environ["PEINIDU_APP_DATA_DIR"] = str(app_data_dir)
    os.environ["PEINIDU_DATA_DIR"] = str(data_dir)
    os.environ["PEINIDU_CONFIG_PATH"] = str(config_path)
    os.environ["PEINIDU_LOCAL_LOG_DIR"] = str(log_dir)


def default_package_root() -> Path:
    """Resolve source checkout or frozen bundle resources."""
    if getattr(sys, "frozen", False):
        executable = Path(sys.executable).resolve()
        if sys.platform == "darwin" and executable.parent.name == "MacOS":
            return executable.parent.parent / "Resources"
        return executable.parent
    return Path(__file__).resolve().parents[2]


def configure_bundled_runtime(package_root: Path) -> None:
    package_root = package_root.expanduser().resolve()
    os.environ["PEINIDU_PACKAGE_ROOT"] = str(package_root)
    poppler_root = package_root / "runtime" / "poppler"
    poppler_bin = poppler_root / "bin"
    if not poppler_bin.is_dir():
        return
    current_path = os.environ.get("PATH", "")
    os.environ["PATH"] = os.pathsep.join(
        part for part in (str(poppler_bin), current_path) if part
    )
    if sys.platform == "darwin":
        poppler_lib = poppler_root / "lib"
        if poppler_lib.is_dir():
            current_lib_path = os.environ.get("DYLD_LIBRARY_PATH", "")
            os.environ["DYLD_LIBRARY_PATH"] = os.pathsep.join(
                part for part in (str(poppler_lib), current_lib_path) if part
            )


def configure_app_version(package_root: Path) -> None:
    if os.environ.get("PEINIDU_APP_VERSION", "").strip():
        return
    try:
        payload = json.loads(
            (package_root / "release-info.json").read_text(encoding="utf-8")
        )
        version = payload.get("version", "")
    except (OSError, json.JSONDecodeError):
        version = ""
    if (
        isinstance(version, str)
        and 1 <= len(version) <= 40
        and all(character.isalnum() or character in "._+-" for character in version)
    ):
        os.environ["PEINIDU_APP_VERSION"] = version
    else:
        os.environ["PEINIDU_APP_VERSION"] = "dev"


def local_core_is_running(url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{url}/api/health", timeout=1.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return False
    return (
        response.status == 200
        and payload.get("runtime_mode") == "local_core"
        and payload.get("content_api_enabled") is True
    )


def _resolve_node(package_root: Path, explicit: str | None) -> str:
    if explicit:
        candidate = Path(explicit).expanduser()
        if candidate.is_file():
            return str(candidate)
        resolved = shutil.which(explicit)
        if resolved:
            return resolved
        raise FileNotFoundError(f"Node runtime not found: {explicit}")
    bundled = package_root / "runtime" / ("node.exe" if os.name == "nt" else "node")
    if bundled.is_file():
        return str(bundled)
    resolved = shutil.which("node")
    if resolved:
        return resolved
    raise FileNotFoundError("Node runtime not found; reinstall Pet Core")


def _next_process_command(node: str, frontend_dir: Path) -> list[str]:
    server = frontend_dir / "server.js"
    if not server.is_file():
        raise FileNotFoundError(f"Next standalone server not found: {server}")
    return [node, str(server)]


def _spawn_next(
    command: Sequence[str],
    *,
    frontend_dir: Path,
    frontend_port: int,
    log_path: Path,
) -> subprocess.Popen:
    env = os.environ.copy()
    env.update(
        {
            "HOSTNAME": "127.0.0.1",
            "PORT": str(frontend_port),
            "NODE_ENV": "production",
        }
    )
    kwargs: dict = {
        "cwd": str(frontend_dir),
        "env": env,
        "stdin": subprocess.DEVNULL,
        "start_new_session": os.name != "nt",
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    log_file = open(log_path, "ab", buffering=0)
    try:
        process = subprocess.Popen(
            list(command),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            **kwargs,
        )
    finally:
        log_file.close()
    return process


def _terminate_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def _wait_for_url(url: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as response:
                if response.status < 500:
                    return True
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.1)
    return False


def _open_when_ready(url: str, *, enabled: bool) -> None:
    if not enabled:
        return

    def worker() -> None:
        if _wait_for_url(f"{url}/api/health", _CORE_START_TIMEOUT_SECONDS):
            webbrowser.open(url)

    threading.Thread(target=worker, name="pet-core-browser", daemon=True).start()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="启动本地陪你读工作台")
    parser.add_argument("--package-root", type=Path)
    parser.add_argument("--frontend-dir", type=Path)
    parser.add_argument("--app-data-dir", type=Path)
    parser.add_argument("--node")
    parser.add_argument("--gateway-port", type=int, default=DEFAULT_GATEWAY_PORT)
    parser.add_argument("--frontend-port", type=int, default=DEFAULT_FRONTEND_PORT)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument(
        "--check-runtime",
        action="store_true",
        help="validate bundled Node, Poppler, frontend and LiteLLM, then exit",
    )
    return parser


def check_packaged_runtime(package_root: Path, node: str, frontend_dir: Path) -> bool:
    checks = (
        Path(node).is_file(),
        (frontend_dir / "server.js").is_file(),
        shutil.which("pdfinfo") is not None,
        shutil.which("pdftotext") is not None,
    )
    if not all(checks):
        return False
    module = importlib.import_module("litellm")
    if not callable(getattr(module, "acompletion", None)):
        return False
    credentials = importlib.import_module("backend.security.credentials")
    try:
        credentials.get_system_credential_store()
    except credentials.CredentialStoreError:
        return False
    return True


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    gateway_url = f"http://127.0.0.1:{args.gateway_port}"
    if not args.check_runtime and local_core_is_running(gateway_url):
        if not args.no_browser:
            webbrowser.open(gateway_url)
        return 0
    package_root = (
        args.package_root.expanduser().resolve()
        if args.package_root
        else default_package_root()
    )
    frontend_dir = (
        args.frontend_dir.expanduser().resolve()
        if args.frontend_dir
        else package_root / "frontend"
    )
    app_data_dir = args.app_data_dir or default_app_data_dir()
    configure_local_environment(app_data_dir)
    configure_bundled_runtime(package_root)
    configure_app_version(package_root)

    try:
        node = _resolve_node(package_root, args.node)
        next_command = _next_process_command(node, frontend_dir)
    except FileNotFoundError as exc:
        print(f"Pet Core 启动失败：{exc}", file=sys.stderr)
        return 2
    if args.check_runtime:
        return 0 if check_packaged_runtime(package_root, node, frontend_dir) else 4

    frontend_origin = f"http://127.0.0.1:{args.frontend_port}"
    next_process = _spawn_next(
        next_command,
        frontend_dir=frontend_dir,
        frontend_port=args.frontend_port,
        log_path=Path(os.environ["PEINIDU_LOCAL_LOG_DIR"]) / "frontend.log",
    )
    if not _wait_for_url(frontend_origin, _CORE_START_TIMEOUT_SECONDS):
        _terminate_process(next_process)
        print("Pet Core 启动失败：本地阅读界面未能启动。", file=sys.stderr)
        return 3

    from uvicorn import Config, Server

    from .gateway import create_local_core_gateway

    gateway = create_local_core_gateway(
        frontend_origin=frontend_origin,
        gateway_port=args.gateway_port,
    )
    _open_when_ready(gateway_url, enabled=not args.no_browser)
    server = Server(
        Config(
            gateway,
            host="127.0.0.1",
            port=args.gateway_port,
            proxy_headers=False,
            access_log=False,
        )
    )
    try:
        try:
            server.run()
        except KeyboardInterrupt:
            pass
    finally:
        _terminate_process(next_process)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
