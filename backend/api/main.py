"""FastAPI 应用入口。

路由：
  /search      POST  标题 → 候选列表（D7 用户确认）
  /papers      POST  选定候选 → 提取 → 存盘
  /papers      GET   列出已存论文
  /papers/{id} GET   取论文 blocks
  /translate/{id}       POST  触发翻译（SSE）       [Step 2]
  /translate/{id}/block POST  单 block 重试         [Step 2]
  /analyze/{id}         POST  触发四 Agent 分析     [Step 4]
  /analyze/{id}         GET   取分析结果             [Step 4]
  /collections          GET/POST  文献库专题
  /papers/{id}/annotations GET/POST/PATCH/DELETE 用户标注
  /papers/{id}/paper-note GET/PUT 论文 Markdown 主笔记
  /literature-map/{paper_ref} GET 论文相似性与引用关系图谱
  /agent/tasks          GET  Agent 任务历史
  /agent/chat/{id}      GET/POST  Agent 对话工作区
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from contextlib import asynccontextmanager
from ipaddress import ip_address, ip_network
from urllib.parse import urlsplit

# 启动时加载 .env（本地开发用，把 DEEPSEEK_API_KEY 等注入环境变量）
# 放在所有其他 import 前，确保 config.py 的 ${ENV_VAR} 展开能拿到值
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass  # 没装 python-dotenv 也能跑，靠真实环境变量

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from ..runtime import RuntimeMode, resolve_runtime_mode
from ..storage.agent_workspace import sweep_stale_runs
from ..storage.agent_session_index import sync_agent_session_index
from ..storage.db import init_db, sweep_stale_agent_tasks
from ..pdf_export.service import sweep_stale_pdf_export_runs
from ..storage.files import COLLECTIONS_DIR, DATA_DIR, PAPERS_DIR
from ..storage.portable_cache import enforce_portable_cache_limits


logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时初始化 SQLite，并清扫上一进程遗留的 running 任务。"""
    _ensure_runtime_dirs()
    await init_db()
    await sync_agent_session_index()
    telemetry_task: asyncio.Task | None = None
    if app.state.runtime_mode == RuntimeMode.LOCAL_CORE.value:
        from ..telemetry.local_client import send_event

        telemetry_task = asyncio.create_task(
            asyncio.to_thread(send_event, "core_started")
        )
    # 后台 Run 的执行体只存活在进程内；重启后遗留的 running 状态永远不会
    # 再被更新，前端会无限轮询“执行中”，必须在启动时标记为 error
    swept_tasks = await sweep_stale_agent_tasks()
    swept_runs = sweep_stale_runs()
    swept_pdf_exports = await sweep_stale_pdf_export_runs()
    if swept_tasks or swept_runs or swept_pdf_exports:
        logger.info(
            "启动清扫孤儿任务: agent_tasks=%d, workspace runs=%d, pdf_exports=%d",
            swept_tasks,
            swept_runs,
            swept_pdf_exports,
        )
    cleanup_task = asyncio.create_task(_portable_cache_cleanup_loop())
    try:
        yield
    finally:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass
        if telemetry_task is not None:
            await asyncio.gather(telemetry_task, return_exceptions=True)


async def _portable_cache_cleanup_loop() -> None:
    while True:
        try:
            await enforce_portable_cache_limits()
        except Exception:
            logger.exception("本地文献服务端缓存清理失败")
        await asyncio.sleep(60 * 60)


_RATE_LIMIT_STATE: dict[str, tuple[float, int]] = {}
_RATE_LIMIT_WINDOW_SECONDS = 60
_RATE_LIMITED_PREFIXES = (
    "/search",
    "/literature-map",
    "/papers",
    "/translate",
    "/analyze",
    "/collections",
    "/agent",
)
_PDF_EXPORT_RATE_LIMIT_DEFAULT = 12


def _cors_origins() -> list[str]:
    """开发期 CORS origin。可用 PEINIDU_CORS_ORIGINS 追加逗号分隔地址。"""
    defaults = ["http://localhost:3000", "http://127.0.0.1:3000"]
    extra = [
        origin.strip()
        for origin in os.environ.get("PEINIDU_CORS_ORIGINS", "").split(",")
        if origin.strip()
    ]
    return list(dict.fromkeys(defaults + extra))


def _ensure_runtime_dirs() -> None:
    """确保应用导入和启动阶段需要的运行目录存在。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PAPERS_DIR.mkdir(parents=True, exist_ok=True)
    COLLECTIONS_DIR.mkdir(parents=True, exist_ok=True)


def _rate_limit_per_minute() -> int:
    try:
        return max(0, int(os.environ.get("PEINIDU_RATE_LIMIT_PER_MINUTE", "120")))
    except ValueError:
        return 120


def _pdf_export_rate_limit_per_minute() -> int:
    try:
        return max(
            0,
            int(
                os.environ.get(
                    "PEINIDU_PDF_EXPORT_RATE_LIMIT_PER_MINUTE",
                    str(_PDF_EXPORT_RATE_LIMIT_DEFAULT),
                )
            ),
        )
    except ValueError:
        return _PDF_EXPORT_RATE_LIMIT_DEFAULT


def _trusted_proxy_networks():
    networks = []
    for value in os.environ.get("PEINIDU_TRUSTED_PROXY_IPS", "").split(","):
        value = value.strip()
        if not value:
            continue
        try:
            networks.append(ip_network(value, strict=False))
        except ValueError:
            logger.warning("忽略非法 PEINIDU_TRUSTED_PROXY_IPS 条目")
    return tuple(networks)


def _client_ip(request: Request) -> str:
    """Return a non-spoofable rate-limit identity.

    Socket peer identity is authoritative by default. A single X-Forwarded-For
    value is accepted only when that peer is in an explicitly configured proxy
    IP/CIDR. The edge proxy must overwrite, rather than append to, the header.
    """
    peer = request.client.host if request.client else "unknown"
    try:
        peer_address = ip_address(peer)
    except ValueError:
        return peer
    peer_identity = peer_address.compressed
    if not any(peer_address in network for network in _trusted_proxy_networks()):
        return peer_identity
    forwarded_values = request.headers.getlist("x-forwarded-for")
    if len(forwarded_values) != 1:
        return peer_identity
    forwarded = forwarded_values[0].strip()
    if not forwarded or "," in forwarded:
        return peer_identity
    try:
        return ip_address(forwarded).compressed
    except ValueError:
        return peer_identity


def _canonical_origin(value: str) -> tuple[str, str, int] | None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    if (
        scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        return None
    if port is None:
        port = 443 if scheme == "https" else 80
    return scheme, parsed.hostname.lower().rstrip("."), port


def _pdf_export_origin_allowed(request: Request) -> bool:
    """Block browser CSRF while keeping CLI/internal/test clients usable."""
    origin_values = request.headers.getlist("origin")
    fetch_site = request.headers.get("sec-fetch-site", "").strip().lower()
    if not origin_values:
        # Non-browser clients normally send neither header. A browser that
        # identifies a cross-origin context must provide a verifiable Origin.
        return not fetch_site or fetch_site in {"same-origin", "none"}
    if len(origin_values) != 1:
        return False
    supplied = _canonical_origin(origin_values[0].strip())
    if supplied is None:
        return False
    allowed = {
        origin
        for value in _cors_origins()
        if (origin := _canonical_origin(value)) is not None
    }
    request_origin = _canonical_origin(
        f"{request.url.scheme}://{request.headers.get('host', '')}"
    )
    if request_origin is not None:
        allowed.add(request_origin)
    return supplied in allowed


def _is_pdf_export_mutation(request: Request) -> bool:
    if request.method != "POST":
        return False
    parts = [part for part in request.url.path.split("/") if part]
    return (
        len(parts) == 3
        and parts[0] == "papers"
        and parts[2] == "pdf-exports"
    ) or (
        len(parts) == 5
        and parts[0] == "papers"
        and parts[2] == "pdf-exports"
        and parts[4] == "cancel"
    )


def _rate_limit_applies(request: Request) -> bool:
    if request.method == "OPTIONS":
        return False
    path = request.url.path
    if path.startswith("/assets"):
        return False
    if path in ("/", "/health"):
        return False
    if path.startswith("/api/portal"):
        return True
    return any(path == prefix or path.startswith(f"{prefix}/") for prefix in _RATE_LIMITED_PREFIXES)


def _is_rate_limited(key: str, *, now: float, limit: int) -> bool:
    if limit <= 0:
        return False
    window_start, count = _RATE_LIMIT_STATE.get(key, (now, 0))
    if now - window_start >= _RATE_LIMIT_WINDOW_SECONDS:
        _RATE_LIMIT_STATE[key] = (now, 1)
        return False
    if count >= limit:
        return True
    _RATE_LIMIT_STATE[key] = (window_start, count + 1)
    return False


async def rate_limit_middleware(request: Request, call_next):
    """轻量 IP 限流，保护公开部署时的昂贵 API 入口。"""
    client = _client_ip(request)
    if _is_pdf_export_mutation(request):
        if not _pdf_export_origin_allowed(request):
            return JSONResponse(
                status_code=403,
                content={
                    "detail": {
                        "code": "origin_not_allowed",
                        "message": "PDF 导出请求来源无效。",
                    }
                },
            )
        export_limit = _pdf_export_rate_limit_per_minute()
        if _is_rate_limited(
            f"pdf-export:{client}",
            now=time.monotonic(),
            limit=export_limit,
        ):
            return JSONResponse(
                status_code=429,
                content={"detail": "PDF 导出操作过于频繁，请稍后再试。"},
                headers={"Retry-After": str(_RATE_LIMIT_WINDOW_SECONDS)},
            )
    limit = _rate_limit_per_minute()
    if limit > 0 and _rate_limit_applies(request):
        key = f"general:{client}"
        if _is_rate_limited(key, now=time.monotonic(), limit=limit):
            return JSONResponse(
                status_code=429,
                content={"detail": "请求过于频繁，请稍后再试。"},
                headers={"Retry-After": str(_RATE_LIMIT_WINDOW_SECONDS)},
            )
    return await call_next(request)


def _register_content_routes(application: FastAPI) -> None:
    """Register APIs that are allowed to read or process user content."""
    from .routes_agent_chat import router as agent_chat_router
    from .routes_agent_tasks import router as agent_tasks_router
    from .routes_analyze import router as analyze_router
    from .routes_annotations import router as annotations_router
    from .routes_collections import router as collections_router
    from .routes_config import router as config_router
    from .routes_internal_llm import router as internal_llm_router
    from .routes_literature_map import router as literature_map_router
    from .routes_notes import router as notes_router
    from .routes_papers import router as papers_router
    from .routes_pdf_exports import router as pdf_exports_router
    from .routes_portable_bundle import router as portable_bundle_router
    from .routes_search import router as search_router
    from .routes_translate import router as translate_router

    for router in (
        search_router,
        literature_map_router,
        papers_router,
        portable_bundle_router,
        translate_router,
        analyze_router,
        config_router,
        collections_router,
        annotations_router,
        notes_router,
        agent_chat_router,
        agent_tasks_router,
        pdf_exports_router,
        internal_llm_router,
    ):
        application.include_router(router)


def create_app(runtime_mode: RuntimeMode | str | None = None) -> FastAPI:
    mode = resolve_runtime_mode(runtime_mode)
    content_api_enabled = mode.content_api_enabled
    if content_api_enabled:
        _ensure_runtime_dirs()

    application = FastAPI(
        title="陪你读 API",
        description="科研论文辅助阅读工具 — 检索 / 提取 / 双语对照 / 多 Agent 分析",
        version="0.1.0",
        lifespan=lifespan if content_api_enabled else None,
        docs_url="/docs" if content_api_enabled else None,
        redoc_url="/redoc" if content_api_enabled else None,
        openapi_url="/openapi.json" if content_api_enabled else None,
    )
    application.state.runtime_mode = mode.value
    application.state.content_api_enabled = content_api_enabled
    application.middleware("http")(rate_limit_middleware)

    if mode is RuntimeMode.SELF_HOSTED:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=_cors_origins(),
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    if content_api_enabled:
        _register_content_routes(application)
    if mode is RuntimeMode.LOCAL_CORE:
        from .routes_local_telemetry import router as local_telemetry_router

        application.include_router(local_telemetry_router)
    if mode is RuntimeMode.PUBLIC_PORTAL:
        from .routes_public_portal import router as public_portal_router

        application.include_router(public_portal_router)
    if mode is RuntimeMode.SELF_HOSTED:
        application.mount(
            "/assets",
            StaticFiles(directory=PAPERS_DIR),
            name="paper-assets",
        )

    @application.get("/")
    async def root():
        if mode is RuntimeMode.PUBLIC_PORTAL:
            from .routes_public_portal import portal_home

            return await portal_home()
        return {
            "name": "陪你读 API",
            "version": "0.1.0",
            "runtime_mode": mode.value,
            "content_api_enabled": content_api_enabled,
            "docs": "/docs" if content_api_enabled else None,
        }

    if mode is RuntimeMode.PUBLIC_PORTAL:
        from .routes_public_portal import privacy_page, public_literature_map_page

        application.add_api_route(
            "/privacy",
            privacy_page,
            methods=["GET"],
            include_in_schema=False,
        )
        application.add_api_route(
            "/literature-map/{paper_ref:path}",
            public_literature_map_page,
            methods=["GET"],
            include_in_schema=False,
        )

    @application.get("/health")
    async def health():
        result = {
            "status": "ok",
            "runtime_mode": mode.value,
            "content_api_enabled": content_api_enabled,
        }
        if not content_api_enabled:
            return result
        result.update(
            {
                "data_dir": DATA_DIR.exists(),
                "papers_dir": PAPERS_DIR.exists(),
                "collections_dir": COLLECTIONS_DIR.exists(),
                "poppler": {
                    name: bool(shutil.which(name))
                    for name in ("pdftotext", "pdftohtml", "pdfinfo")
                },
            }
        )
        return result

    return application


app = create_app()
