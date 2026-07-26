"""Deployment runtime modes for the Pet application."""

from __future__ import annotations

import os
from enum import Enum


RUNTIME_MODE_ENV = "PEINIDU_RUNTIME_MODE"


class RuntimeMode(str, Enum):
    SELF_HOSTED = "self_hosted"
    LOCAL_CORE = "local_core"
    PUBLIC_PORTAL = "public_portal"

    @property
    def content_api_enabled(self) -> bool:
        return self is not RuntimeMode.PUBLIC_PORTAL


def resolve_runtime_mode(value: RuntimeMode | str | None = None) -> RuntimeMode:
    if isinstance(value, RuntimeMode):
        return value
    raw = value if value is not None else os.environ.get(RUNTIME_MODE_ENV, "")
    normalized = str(raw).strip().lower() or RuntimeMode.SELF_HOSTED.value
    try:
        return RuntimeMode(normalized)
    except ValueError as exc:
        allowed = ", ".join(mode.value for mode in RuntimeMode)
        raise RuntimeError(
            f"invalid {RUNTIME_MODE_ENV}={normalized!r}; expected one of: {allowed}"
        ) from exc
