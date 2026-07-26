"""Single-origin loopback gateway for the browser-shaped local Pet Core."""

from __future__ import annotations

from contextlib import asynccontextmanager
from ipaddress import ip_address
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from ..api.main import create_app
from ..runtime import RuntimeMode
from ..storage.files import PAPERS_DIR


DEFAULT_GATEWAY_HOST = "127.0.0.1"
DEFAULT_GATEWAY_PORT = 8520
DEFAULT_FRONTEND_ORIGIN = "http://127.0.0.1:8521"
_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _is_loopback_peer(value: str) -> bool:
    try:
        return ip_address(value).is_loopback
    except ValueError:
        return False


def _is_allowed_host(value: str, *, gateway_port: int) -> bool:
    try:
        parsed = urlsplit(f"//{value}")
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        and port in {None, gateway_port}
        and parsed.username is None
        and parsed.password is None
    )


def _allowed_origins(gateway_port: int) -> set[str]:
    return {
        f"http://127.0.0.1:{gateway_port}",
        f"http://localhost:{gateway_port}",
        f"http://[::1]:{gateway_port}",
    }


def _request_is_allowed(request: Request, *, gateway_port: int) -> bool:
    peer = request.client.host if request.client else ""
    if not _is_loopback_peer(peer):
        return False
    if not _is_allowed_host(request.headers.get("host", ""), gateway_port=gateway_port):
        return False

    fetch_site = request.headers.get("sec-fetch-site", "").strip().lower()
    fetch_mode = request.headers.get("sec-fetch-mode", "").strip().lower()
    path = request.url.path
    if fetch_site == "cross-site":
        top_level_navigation = (
            request.method in {"GET", "HEAD"}
            and fetch_mode == "navigate"
            and not path.startswith(("/api", "/assets"))
        )
        if not top_level_navigation:
            return False

    if request.method in _UNSAFE_METHODS:
        origin = request.headers.get("origin", "").strip().rstrip("/")
        if origin and origin not in _allowed_origins(gateway_port):
            return False
        if not origin and fetch_site not in {"", "none", "same-origin"}:
            return False
    return True


def _security_headers(response):
    response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response


def _forward_headers(request: Request) -> dict[str, str]:
    return {
        name: value
        for name, value in request.headers.items()
        if name.lower() not in _HOP_BY_HOP_HEADERS | {"host", "content-length"}
    }


def _response_headers(response: httpx.Response) -> dict[str, str]:
    return {
        name: value
        for name, value in response.headers.items()
        if name.lower() not in _HOP_BY_HOP_HEADERS
    }


def create_local_core_gateway(
    *,
    content_app: FastAPI | None = None,
    frontend_origin: str = DEFAULT_FRONTEND_ORIGIN,
    gateway_port: int = DEFAULT_GATEWAY_PORT,
    paper_assets_dir: Path | None = None,
) -> FastAPI:
    local_content_app = content_app or create_app(RuntimeMode.LOCAL_CORE)
    resolved_assets = paper_assets_dir
    if content_app is None and resolved_assets is None:
        resolved_assets = PAPERS_DIR

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        async with local_content_app.router.lifespan_context(local_content_app):
            async with httpx.AsyncClient(timeout=30.0) as client:
                application.state.frontend_client = client
                yield

    gateway = FastAPI(
        title="陪你读 Local Core",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    gateway.state.runtime_mode = RuntimeMode.LOCAL_CORE.value
    gateway.state.frontend_origin = frontend_origin.rstrip("/")

    @gateway.middleware("http")
    async def local_core_security(request: Request, call_next):
        if not _request_is_allowed(request, gateway_port=gateway_port):
            return _security_headers(
                JSONResponse(
                    status_code=403,
                    content={
                        "detail": {
                            "code": "local_core_request_rejected",
                            "message": "本地 Pet Core 拒绝了非本机或跨站请求。",
                        }
                    },
                )
            )
        return _security_headers(await call_next(request))

    gateway.mount("/api", local_content_app, name="local-content-api")
    if resolved_assets is not None:
        resolved_assets.mkdir(parents=True, exist_ok=True)
        gateway.mount(
            "/assets",
            StaticFiles(directory=resolved_assets),
            name="local-paper-assets",
        )

    @gateway.api_route(
        "/{path:path}",
        methods=["GET", "HEAD"],
        include_in_schema=False,
    )
    async def frontend_proxy(request: Request, path: str):
        client: httpx.AsyncClient = request.app.state.frontend_client
        query = request.url.query
        suffix = f"/{path}" if path else "/"
        target = f"{request.app.state.frontend_origin}{suffix}"
        if query:
            target = f"{target}?{query}"
        upstream_request = client.build_request(
            request.method,
            target,
            headers=_forward_headers(request),
        )
        try:
            upstream = await client.send(upstream_request, stream=True)
        except httpx.RequestError:
            return JSONResponse(
                status_code=503,
                content={
                    "detail": {
                        "code": "local_frontend_unavailable",
                        "message": "本地阅读界面尚未启动，请稍后重试。",
                    }
                },
            )
        return StreamingResponse(
            upstream.aiter_raw(),
            status_code=upstream.status_code,
            headers=_response_headers(upstream),
            background=BackgroundTask(upstream.aclose),
        )

    return gateway
