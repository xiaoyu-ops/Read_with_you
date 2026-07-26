"""Minimal public-portal store for content-free aggregate usage counts."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .contracts import PortalTelemetryEvent


_LOCK = threading.Lock()
_RETENTION_DAYS = 35
_ALLOWED_DOWNLOAD_PLATFORMS = {"macos_arm64", "windows_x64"}


def database_path() -> Path:
    configured = os.environ.get("PEINIDU_PORTAL_DATA_DIR", "").strip()
    root = Path(configured).expanduser().resolve() if configured else Path("portal-data").resolve()
    return root / "usage.sqlite3"


def _connect() -> sqlite3.Connection:
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS daily_events (
            event_date TEXT NOT NULL,
            daily_id TEXT NOT NULL,
            event TEXT NOT NULL,
            platform TEXT NOT NULL,
            app_version TEXT NOT NULL,
            received_at TEXT NOT NULL,
            PRIMARY KEY (event_date, daily_id, event)
        );
        CREATE TABLE IF NOT EXISTS daily_totals (
            event_date TEXT NOT NULL,
            event TEXT NOT NULL,
            platform TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (event_date, event, platform)
        );
        CREATE TABLE IF NOT EXISTS download_totals (
            event_date TEXT NOT NULL,
            platform TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (event_date, platform)
        );
        """
    )
    return connection


def record_event(item: PortalTelemetryEvent, *, today: date | None = None) -> bool:
    current = today or datetime.now(timezone.utc).date()
    if abs((item.event_date - current).days) > 1:
        raise ValueError("event_date_out_of_range")
    cutoff = (current - timedelta(days=_RETENTION_DAYS)).isoformat()
    now = datetime.now(timezone.utc).isoformat()
    with _LOCK, closing(_connect()) as connection:
        with connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO daily_events
                    (event_date, daily_id, event, platform, app_version, received_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    item.event_date.isoformat(),
                    item.daily_id,
                    item.event,
                    item.platform,
                    item.app_version,
                    now,
                ),
            )
            inserted = cursor.rowcount == 1
            if inserted:
                connection.execute(
                    """
                    INSERT INTO daily_totals (event_date, event, platform, count)
                    VALUES (?, ?, ?, 1)
                    ON CONFLICT(event_date, event, platform)
                    DO UPDATE SET count = count + 1
                    """,
                    (item.event_date.isoformat(), item.event, item.platform),
                )
            connection.execute(
                "DELETE FROM daily_events WHERE event_date < ?",
                (cutoff,),
            )
    return inserted


def record_download(platform_name: str, *, today: date | None = None) -> None:
    if platform_name not in _ALLOWED_DOWNLOAD_PLATFORMS:
        raise ValueError("unsupported_platform")
    event_date = (today or datetime.now(timezone.utc).date()).isoformat()
    with _LOCK, closing(_connect()) as connection:
        with connection:
            connection.execute(
                """
                INSERT INTO download_totals (event_date, platform, count)
                VALUES (?, ?, 1)
                ON CONFLICT(event_date, platform)
                DO UPDATE SET count = count + 1
                """,
                (event_date, platform_name),
            )


def aggregate_stats(*, today: date | None = None) -> dict[str, Any]:
    current = today or datetime.now(timezone.utc).date()
    start = (current - timedelta(days=29)).isoformat()
    with _LOCK, closing(_connect()) as connection:
        events = connection.execute(
            """
            SELECT event_date, event, SUM(count) AS count
            FROM daily_totals
            WHERE event_date >= ?
            GROUP BY event_date, event
            ORDER BY event_date ASC
            """,
            (start,),
        ).fetchall()
        downloads = connection.execute(
            "SELECT COALESCE(SUM(count), 0) AS count FROM download_totals"
        ).fetchone()["count"]
    daily: dict[str, dict[str, int]] = {}
    for row in events:
        daily.setdefault(row["event_date"], {})[row["event"]] = row["count"]
    today_values = daily.get(current.isoformat(), {})
    return {
        "date": current.isoformat(),
        "active_today": today_values.get("core_started", 0),
        "readers_today": today_values.get("reader_opened", 0),
        "total_downloads": downloads,
        "daily": [
            {"date": day, **counts}
            for day, counts in sorted(daily.items())
        ],
        "privacy": {
            "raw_daily_ids_retained_days": _RETENTION_DAYS,
            "cross_day_identifier": False,
            "content_collected": False,
        },
    }


def _manifest_path() -> Path | None:
    value = os.environ.get("PEINIDU_RELEASE_MANIFEST", "").strip()
    return Path(value).expanduser().resolve() if value else None


def load_release_manifest() -> dict[str, Any] | None:
    path = _manifest_path()
    if path is None:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    version = payload.get("version")
    downloads = payload.get("downloads")
    if not isinstance(version, str) or not version or not isinstance(downloads, dict):
        return None
    normalized: dict[str, Any] = {"version": version, "downloads": {}}
    for platform_name in _ALLOWED_DOWNLOAD_PLATFORMS:
        item = downloads.get(platform_name)
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        sha256 = item.get("sha256")
        size_bytes = item.get("size_bytes")
        if (
            isinstance(url, str)
            and url.startswith("https://")
            and isinstance(sha256, str)
            and len(sha256) == 64
            and isinstance(size_bytes, int)
            and size_bytes > 0
        ):
            normalized["downloads"][platform_name] = {
                "url": url,
                "sha256": sha256,
                "size_bytes": size_bytes,
            }
    return normalized if normalized["downloads"] else None
