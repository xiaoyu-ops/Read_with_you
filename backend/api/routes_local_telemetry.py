"""Loopback-only controls for explicit anonymous usage opt-in."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request

from ..runtime import RuntimeMode
from ..telemetry.contracts import (
    LocalTelemetryEventRequest,
    LocalTelemetrySettingsUpdate,
)
from ..telemetry.local_client import get_settings, send_event, update_settings


router = APIRouter(prefix="/telemetry", tags=["telemetry"])


def _require_local_core(request: Request) -> None:
    if request.app.state.runtime_mode != RuntimeMode.LOCAL_CORE.value:
        raise HTTPException(status_code=404, detail="not_found")


@router.get("/settings")
async def read_telemetry_settings(request: Request) -> dict:
    _require_local_core(request)
    return await asyncio.to_thread(get_settings)


@router.put("/settings")
async def write_telemetry_settings(
    request: Request,
    body: LocalTelemetrySettingsUpdate,
) -> dict:
    _require_local_core(request)
    return await asyncio.to_thread(update_settings, body.enabled)


@router.post("/events")
async def record_local_telemetry_event(
    request: Request,
    body: LocalTelemetryEventRequest,
) -> dict:
    _require_local_core(request)
    return await asyncio.to_thread(send_event, body.event)
