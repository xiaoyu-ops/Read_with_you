"""Public discovery allowlist: academic metadata, releases and aggregate counts."""

from __future__ import annotations

import asyncio
from html import escape
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from .public_portal_demo import (
    DEMO_CSS,
    demo_markup,
    resolve_demo_asset,
)
from .public_portal_ui import (
    ANALYTICS_SCRIPT,
    CORE_STATUS_SCRIPT,
    PORTAL_CSS,
    map_script,
)
from .routes_search import search_papers
from ..retrieval.literature_map import (
    LITERATURE_MAP_DEFAULT_NODES,
    LITERATURE_MAP_MAX_NODES,
    LiteratureMapError,
    get_literature_map,
    normalize_paper_ref,
)
from ..telemetry.contracts import PortalTelemetryEvent
from ..telemetry.portal_store import (
    aggregate_stats,
    load_release_manifest,
    record_download,
    record_event,
)


router = APIRouter(prefix="/api/portal", tags=["public portal"])
_MASCOT_PATH = (
    Path(__file__).resolve().parents[2]
    / "frontend"
    / "public"
    / "mascot"
    / "home-mascot.png"
)
_GITHUB_INSTALL_URL = "https://github.com/xiaoyu-ops/Read_with_you#本地启动"
_MAX_TELEMETRY_BODY_BYTES = 2048
_DOWNLOAD_LABELS = {
    "macos_arm64": "下载 macOS Apple 芯片版",
    "windows_x64": "下载 Windows x64 版",
}
_PORTAL_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'unsafe-inline'; "
        "script-src 'unsafe-inline'; connect-src 'self' http://127.0.0.1:8520; "
        "frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'self'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
}


def _page(*, title: str, body: str) -> HTMLResponse:
    html = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <link rel="icon" href="data:,">
  <title>{escape(title)}</title>
  <style>{PORTAL_CSS}{DEMO_CSS}</style>
</head>
<body>
  <a class="skip" href="#main">跳到主要内容</a>
  <header class="site-header">
    <a class="brand" href="/" aria-label="陪你读"><span>陪你<em>读</em></span><img class="brand-mascot" src="/api/portal/mascot.png" alt="" aria-hidden="true"></a>
    <nav class="site-nav" aria-label="门户导航"><a href="/#product-demo">产品演示</a><a href="/#local-core">本地使用</a><a href="/privacy">隐私说明</a><a href="{_GITHUB_INSTALL_URL}" target="_blank" rel="noopener noreferrer">GitHub</a></nav>
  </header>
  {ANALYTICS_SCRIPT}
  {body}
  <footer class="site-footer">陪你读 · 读好论文，沉淀基础。</footer>
</body>
</html>"""
    return HTMLResponse(html, headers=_PORTAL_HEADERS)


async def portal_home() -> HTMLResponse:
    return _page(
        title="陪你读 — 读好论文，沉淀基础",
        body=f"""<main id="main" class="portal-main">
  <div class="home-hero">
    <div>
      <p class="eyebrow">Local-first paper workspace</p>
      <h1>读好论文，沉淀基础。</h1>
      <div class="home-actions">
        <a id="open-core" class="button secondary" href="http://127.0.0.1:8520" target="_blank" rel="noopener noreferrer">尝试打开本地工作台</a>
        <a id="install-core" class="button primary" href="{_GITHUB_INSTALL_URL}" target="_blank" rel="noopener noreferrer">前往 GitHub 安装 / 启动</a>
      </div>
      <p class="status">本页会先检查这台电脑上的 Core；未运行时请按 GitHub 说明安装或启动。</p>
      <a class="demo-jump" href="#product-demo">看看精读与证据分析怎么工作 ↓</a>
    </div>
    <aside class="usage-summary" aria-labelledby="usage-summary-title">
      <p class="usage-summary-label">匿名使用概况</p>
      <strong id="usage-summary-title">陪你读正在被使用</strong>
      <dl>
        <div><dt>累计访问</dt><dd id="total-portal-visits" aria-live="polite">读取中…</dd></div>
        <div><dt>Core 启动</dt><dd id="total-core-starts" aria-live="polite">读取中…</dd></div>
        <div><dt>开始阅读</dt><dd id="total-reader-opens" aria-live="polite">读取中…</dd></div>
      </dl>
      <small>累计匿名次数，不代表唯一用户人数；不包含论文、笔记、问题或回答。</small>
    </aside>
  </div>
  {demo_markup()}
  <section id="local-core" class="core-entry" aria-labelledby="local-core-title">
    <p id="core-state" class="core-state"><span class="core-state-dot" aria-hidden="true"></span><span>正在检查本地 Core</span></p>
    <div>
      <h2 id="local-core-title">先启动，再打开</h2>
      <p class="lede">网页只能确认本机 <code>127.0.0.1:8520</code> 的 Core 是否正在运行，不能静默读取电脑里是否装过应用。已经安装时请先启动 Core；尚未安装时再前往 GitHub。</p>
      <p class="status">Chrome 首次检查时可能询问是否允许本站访问本地网络；该权限只用于连接这台电脑上的 <code>127.0.0.1</code> Core。即使检测被浏览器拦截，也可以直接尝试打开本地工作台。</p>
      <div class="actions">
        <a class="button primary" href="{_GITHUB_INSTALL_URL}" target="_blank" rel="noopener noreferrer">查看 GitHub 安装流程 ↗</a>
        <button id="retry-core" class="button secondary" type="button">重新检查本地 Core</button>
      </div>
    </div>
  </section>
  <div class="home-sections">
    <section><h2>仍然是网页体验</h2><p>工作台运行在本机浏览器中，原始 PDF 仍是阅读主面。</p></section>
    <section><h2>研究资料留在本机</h2><p>论文、笔记、问题、回答与 Key 不成为公网账号资产。</p></section>
    <section><h2>无需注册账号</h2><p>不需要网站账号；安装和更新流程以 GitHub 项目说明为准。</p></section>
  </div>
</main>{CORE_STATUS_SCRIPT}""",
    )


@router.get("/mascot.png", include_in_schema=False)
async def portal_mascot() -> FileResponse:
    if not _MASCOT_PATH.is_file():
        raise HTTPException(status_code=404, detail="mascot_not_found")
    return FileResponse(
        _MASCOT_PATH,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/demo-assets/{name:path}", include_in_schema=False)
async def portal_demo_asset(name: str) -> FileResponse:
    path = resolve_demo_asset(name)
    if path is None:
        raise HTTPException(status_code=404, detail="demo_asset_not_found")
    return FileResponse(
        path,
        media_type="image/webp",
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
        },
    )


def privacy_page() -> HTMLResponse:
    return _page(
        title="隐私说明 — 陪你读",
        body="""<main id="main" class="portal-main prose">
  <p class="eyebrow">Privacy boundary</p>
  <h1>你的论文不是我们的数据。</h1>
  <h2>默认匿名计数</h2>
  <p>我们默认记录访问、提交检索、打开图谱、本地 Core 启动和成功打开阅读器等固定事件，只用于判断产品是否真的有人使用。每日匿名标识会跨日更换，同日重复事件会去重。</p>
  <h2>搜索如何处理</h2>
  <p>你提交的检索词会发送给 arXiv 与 Semantic Scholar 来返回结果，但不会写入匿名使用统计。论文关系图谱只缓存公开学术元数据，不包含你的阅读资料。</p>
  <h2>不进入统计</h2>
  <ul><li>论文、PDF、标题、作者、搜索词或文件路径</li><li>选区、笔记、问题、回答或 Agent 证据</li><li>Provider、DeepLX、MinerU Key 与本地配置</li><li>原始安装标识、账号、邮箱或精确位置</li></ul>
  <h2>保留期限</h2>
  <p>用于当日去重的匿名日标识最多保留 35 天，之后只保留按日期和事件聚合的数字。应用数据库不保存 IP 或 User-Agent；部署层访问日志也应关闭。</p>
  <p class="usage-line"><a href="/">返回论文发现</a></p>
</main>""",
    )


def public_literature_map_page(paper_ref: str) -> HTMLResponse:
    try:
        normalized = normalize_paper_ref(paper_ref)
    except LiteratureMapError as exc:
        raise HTTPException(status_code=404, detail=exc.code) from exc
    return _page(
        title="论文关系图谱 — 陪你读",
        body=f"""<main id="main" class="map-main">
  <div id="map-loading" class="map-loading" aria-live="polite">
    <p>正在构建论文关系…</p>
    <span>合并推荐、参考文献和引用关系。</span>
  </div>
  <section id="map-shell" hidden>
    <header class="map-toolbar">
      <div class="map-title"><strong>论文关系</strong><span id="map-subtitle">Semantic Scholar</span></div>
      <nav class="map-tabs" aria-label="图谱视图">
        <button type="button" data-view="graph" aria-pressed="true">图谱</button>
        <button type="button" data-view="prior" aria-pressed="false">先行工作</button>
        <button type="button" data-view="derivative" aria-pressed="false">后续工作</button>
        <button type="button" data-view="list" aria-pressed="false">列表</button>
        <button id="filter-toggle" type="button" aria-expanded="false">筛选</button>
      </nav>
      <div class="relation-tabs" aria-label="关系类型">
        <button type="button" data-relation="similarity" aria-pressed="true">相似关系</button>
        <button type="button" data-relation="citation" aria-pressed="false">引用关系</button>
      </div>
    </header>
    <section id="map-filters" class="filter-panel" aria-label="筛选论文">
      <label>关键词<input id="filter-keyword" type="text" placeholder="标题、作者或会议"></label>
      <label>起始年份<input id="filter-from" type="number" inputmode="numeric" placeholder="2018"></label>
      <label>截止年份<input id="filter-to" type="number" inputmode="numeric" placeholder="2026"></label>
      <div class="filter-checks">
        <label><input id="filter-pdf" type="checkbox">PDF 可用</label>
        <label><input id="filter-oa" type="checkbox">开放获取</label>
        <button id="filter-clear" type="button">清除</button>
      </div>
    </section>
    <p id="map-warning" class="map-warning" role="status"></p>
    <div class="mobile-tabs" aria-label="移动端图谱区域">
      <button type="button" data-mobile="graph" aria-pressed="true">图谱</button>
      <button type="button" data-mobile="list" aria-pressed="false">论文列表</button>
      <button type="button" data-mobile="detail" aria-pressed="false">当前论文</button>
    </div>
    <div class="map-grid">
      <aside id="map-list" class="map-list">
        <div class="panel-heading"><strong>相关论文</strong><span id="map-count"></span></div>
        <div id="map-paper-list" class="paper-list"></div>
      </aside>
      <section id="map-stage" class="map-stage is-mobile-active"></section>
      <aside id="map-detail" class="map-detail"></aside>
    </div>
  </section>
</main>{map_script(normalized)}""",
    )


class PublicSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=300)


@router.post("/search")
async def public_search(body: PublicSearchRequest) -> dict:
    response = await search_papers(body.query, max_results=10, use_cache=False)
    return response.model_dump()


@router.get("/literature-map/{paper_ref:path}")
async def public_literature_map(
    paper_ref: str,
    max_nodes: int = Query(
        default=LITERATURE_MAP_DEFAULT_NODES,
        ge=10,
        le=LITERATURE_MAP_MAX_NODES,
    ),
) -> dict:
    try:
        return await get_literature_map(paper_ref, max_nodes=max_nodes)
    except LiteratureMapError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc


@router.get("/releases/latest")
async def latest_release() -> dict:
    manifest = await asyncio.to_thread(load_release_manifest)
    if manifest is None:
        raise HTTPException(status_code=503, detail="release_not_configured")
    return {
        "version": manifest["version"],
        "downloads": [
            {
                "platform": platform_name,
                "download_url": f"/api/portal/download/{platform_name}",
                "sha256": item["sha256"],
                "size_bytes": item["size_bytes"],
            }
            for platform_name, item in sorted(manifest["downloads"].items())
        ],
    }


@router.get("/download/{platform_name}")
async def download_release(platform_name: str) -> RedirectResponse:
    manifest = await asyncio.to_thread(load_release_manifest)
    item = (manifest or {}).get("downloads", {}).get(platform_name)
    if item is None:
        raise HTTPException(status_code=404, detail="release_not_available")
    await asyncio.to_thread(record_download, platform_name)
    return RedirectResponse(item["url"], status_code=307)


@router.post("/telemetry")
async def accept_telemetry(request: Request, body: PortalTelemetryEvent) -> dict:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > _MAX_TELEMETRY_BODY_BYTES:
                raise HTTPException(status_code=413, detail="payload_too_large")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid_content_length") from exc
    try:
        inserted = await asyncio.to_thread(record_event, body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": "recorded" if inserted else "duplicate"}


@router.get("/stats")
async def portal_stats() -> dict:
    return await asyncio.to_thread(aggregate_stats)
