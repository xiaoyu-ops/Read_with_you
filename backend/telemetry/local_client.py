"""Default-on, content-free local usage counter.

The installation identifier never leaves the device. A different SHA-256
identifier is derived for each UTC day, so the public portal can count daily
active installations without building a cross-day user profile.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import TelemetryEventName


DEFAULT_TELEMETRY_ENDPOINT = (
    "https://readwithyou.xiaoyu666.cyou/api/portal/telemetry"
)
_SETTINGS_FILE = "anonymous-usage.json"
_SETTINGS_VERSION = 2
_LOCK = threading.Lock()


def _app_data_dir() -> Path:
    configured = os.environ.get("PEINIDU_APP_DATA_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    config_path = os.environ.get("PEINIDU_CONFIG_PATH", "").strip()
    if config_path:
        return Path(config_path).expanduser().resolve().parent.parent
    return Path(tempfile.gettempdir()) / "peinidu-local-core"


def _settings_path() -> Path:
    return _app_data_dir() / "settings" / _SETTINGS_FILE


def _default_state() -> dict[str, Any]:
    return {
        "version": _SETTINGS_VERSION,
        "enabled": True,
        "install_id": uuid.uuid4().hex,
        "last_sent": {},
    }


def _load_state() -> dict[str, Any]:
    path = _settings_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        raw = _default_state()
        _write_state(raw)
        return raw
    install_id = raw.get("install_id")
    if not isinstance(install_id, str) or len(install_id) != 32:
        raw = _default_state()
        _write_state(raw)
        return raw
    if raw.get("version") != _SETTINGS_VERSION:
        raw["version"] = _SETTINGS_VERSION
        raw["enabled"] = True
        raw["last_sent"] = {}
        _write_state(raw)
    raw.setdefault("enabled", True)
    raw.setdefault("last_sent", {})
    return raw


def _write_state(state: dict[str, Any]) -> None:
    path = _settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        handle.write(payload)
        temp_path = Path(handle.name)
    os.chmod(temp_path, 0o600)
    temp_path.replace(path)


def _endpoint() -> str:
    return os.environ.get(
        "PEINIDU_TELEMETRY_ENDPOINT",
        DEFAULT_TELEMETRY_ENDPOINT,
    ).strip()


def _validated_endpoint() -> str:
    value = _endpoint()
    parsed = urllib.parse.urlsplit(value)
    hostname = (parsed.hostname or "").lower()
    loopback = hostname in {"127.0.0.1", "localhost", "::1"}
    if (
        not value
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (parsed.scheme != "https" and not (parsed.scheme == "http" and loopback))
    ):
        raise ValueError("telemetry_endpoint_unavailable")
    return value


def _platform_name() -> str:
    current = platform.system().lower()
    if current == "darwin":
        return "macos"
    if current == "windows":
        return "windows"
    if current == "linux":
        return "linux"
    return "other"


def get_settings() -> dict[str, Any]:
    with _LOCK:
        state = _load_state()
    try:
        endpoint_origin = urllib.parse.urlsplit(_validated_endpoint())
        portal = f"{endpoint_origin.scheme}://{endpoint_origin.netloc}"
        available = True
    except ValueError:
        portal = ""
        available = False
    return {
        "enabled": bool(state.get("enabled")) and available,
        "available": available,
        "portal": portal,
        "privacy": {
            "sent": [
                "UTC 日期",
                "每日变化的匿名标识",
                "固定事件名",
                "系统类型",
                "应用版本",
            ],
            "never_sent": [
                "论文与 PDF",
                "笔记与划线",
                "问题与回答",
                "API Key",
                "文件名与路径",
                "原始安装标识",
            ],
        },
    }


def update_settings(enabled: bool) -> dict[str, Any]:
    if enabled:
        try:
            _validated_endpoint()
        except ValueError:
            enabled = False
    with _LOCK:
        state = _load_state()
        state["enabled"] = enabled
        if not enabled:
            state["last_sent"] = {}
        _write_state(state)
    result = get_settings()
    if enabled:
        result["initial_event"] = send_event("core_started")
    elif not result["available"]:
        result["initial_event"] = {"status": "failed"}
    return result


def _daily_id(install_id: str, event_date: str) -> str:
    return hashlib.sha256(f"{install_id}:{event_date}".encode("utf-8")).hexdigest()


def _send_payload(payload: dict[str, Any]) -> None:
    request = urllib.request.Request(
        _validated_endpoint(),
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": f"Peinidu-Telemetry/{payload['app_version']}",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=4) as response:
        if not 200 <= response.status < 300:
            raise urllib.error.HTTPError(
                request.full_url,
                response.status,
                "telemetry_failed",
                response.headers,
                None,
            )


def send_event(event: TelemetryEventName) -> dict[str, Any]:
    today = datetime.now(timezone.utc).date().isoformat()
    with _LOCK:
        state = _load_state()
        if not state.get("enabled"):
            return {"status": "disabled"}
        last_sent = state.get("last_sent")
        if isinstance(last_sent, dict) and last_sent.get(event) == today:
            return {"status": "already_sent"}
        payload = {
            "event_date": today,
            "daily_id": _daily_id(state["install_id"], today),
            "event": event,
            "platform": _platform_name(),
            "app_version": os.environ.get("PEINIDU_APP_VERSION", "dev"),
        }
    try:
        _validated_endpoint()
        _send_payload(payload)
    except (OSError, ValueError, urllib.error.URLError):
        return {"status": "failed"}
    with _LOCK:
        state = _load_state()
        if state.get("enabled"):
            state.setdefault("last_sent", {})[event] = today
            _write_state(state)
    return {"status": "sent"}
