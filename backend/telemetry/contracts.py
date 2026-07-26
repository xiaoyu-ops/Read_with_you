"""Strict telemetry contracts shared by the local Core and public portal."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


TelemetryEventName = Literal[
    "core_started",
    "reader_opened",
    "translation_succeeded",
    "agent_response",
]
TelemetryPlatform = Literal["macos", "windows", "linux", "other"]


class PortalTelemetryEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_date: date
    daily_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    event: TelemetryEventName
    platform: TelemetryPlatform
    app_version: str = Field(min_length=1, max_length=40, pattern=r"^[0-9A-Za-z._+-]+$")


class LocalTelemetrySettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class LocalTelemetryEventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: Literal["reader_opened", "translation_succeeded", "agent_response"]
