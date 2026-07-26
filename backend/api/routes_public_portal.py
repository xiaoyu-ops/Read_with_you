"""Public discovery allowlist: academic metadata, releases and aggregate counts."""

from __future__ import annotations

import asyncio
from html import escape

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from .public_portal_ui import ANALYTICS_SCRIPT, HOME_SCRIPT, PORTAL_CSS, map_script
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
_MAX_TELEMETRY_BODY_BYTES = 2048
_DOWNLOAD_LABELS = {
    "macos_arm64": "下载 macOS Apple 芯片版",
    "windows_x64": "下载 Windows x64 版",
}
_PORTAL_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'unsafe-inline'; "
        "script-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; "
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
  <style>{PORTAL_CSS}</style>
</head>
<body>
  <a class="skip" href="#main">跳到主要内容</a>
  <header class="site-header">
    <a class="brand" href="/">陪你<em>读</em></a>
    <nav class="site-nav" aria-label="门户导航"><a href="/">论文发现</a><a href="/privacy">隐私说明</a><a href="/#local-core">本地工作台</a></nav>
  </header>
  {ANALYTICS_SCRIPT}
  {body}
  <footer class="site-footer">陪你读 · 公网发现论文，本地阅读、翻译、做笔记和问 Pet。</footer>
</body>
</html>"""
    return HTMLResponse(html, headers=_PORTAL_HEADERS)


async def portal_home() -> HTMLResponse:
    manifest = await asyncio.to_thread(load_release_manifest)
    downloads = [
        (platform_name, _DOWNLOAD_LABELS[platform_name])
        for platform_name in sorted((manifest or {}).get("downloads", {}))
        if platform_name in _DOWNLOAD_LABELS
    ]
    if manifest is not None and downloads:
        download_actions = "".join(
            f'<a class="button{" primary" if index == 0 else ""}" '
            f'href="/api/portal/download/{escape(platform_name, quote=True)}">'
            f"{escape(label)}</a>"
            for index, (platform_name, label) in enumerate(downloads)
        )
        release_note = (
            f"当前版本 {escape(str(manifest['version']))} · "
            "下载后在本机启动，不需要网站账号"
        )
    else:
        download_actions = (
            '<span class="button primary" aria-disabled="true">安装包尚未开放</span>'
        )
        release_note = "当前为开发预览，安装包尚未开放；已有源码环境可按下方说明启动。"
    return _page(
        title="陪你读 — 论文发现与关系图谱",
        body=f"""<main id="main" class="portal-main">
  <section class="hero" aria-labelledby="portal-title">
    <p class="eyebrow">Public research discovery</p>
    <h1 id="portal-title">找论文，也看清它的来路。</h1>
    <p class="lede">直接检索 arXiv 与 Semantic Scholar，确认论文后打开原文或探索相似、引用、先行与后续工作。不需要安装，也不需要登录。</p>
    <div class="task-tabs" role="tablist" aria-label="论文任务">
      <button type="button" role="tab" data-task="read" aria-selected="true">找论文</button>
      <button type="button" role="tab" data-task="map" aria-selected="false">看论文关系</button>
    </div>
    <form id="paper-search" class="search-form" role="search">
      <label class="skip" for="paper-query">论文标题、arXiv ID 或 DOI</label>
      <input id="paper-query" name="q" autocomplete="off" maxlength="300" required>
      <button id="paper-submit" type="submit">查找论文</button>
    </form>
    <p class="search-hint">多条结果不会自动猜测，请先确认标题与作者。</p>
    <p id="search-status" class="status" aria-live="polite"></p>
  </section>
  <section id="paper-results" class="results" aria-label="论文候选"></section>
  <section id="local-core" class="entry-boundary" aria-labelledby="local-core-title">
    <p class="boundary-label">PRIVATE WORKSPACE</p>
    <div>
      <h2 id="local-core-title">精读、翻译和笔记留在你的电脑里</h2>
      <p>公网只处理公开学术元数据。原始 PDF、划选翻译、笔记、文献库、Pet 对话和 Key 由本地 Core 提供，不进入这个公网服务。</p>
      <div class="actions">
        <a class="button primary" href="http://127.0.0.1:8520" target="_blank" rel="noopener noreferrer">本地 Core 已启动，打开工作台</a>
        {download_actions}
        <a class="button secondary" href="https://github.com/xiaoyu-ops/Read_with_you#本地启动" target="_blank" rel="noopener noreferrer">源码启动说明 ↗</a>
      </div>
      <p class="status">{release_note}</p>
    </div>
  </section>
  <div class="plain-sections">
    <section><h2>公开元数据</h2><p>检索与图谱来自 arXiv、Semantic Scholar。相似关系和有向引用关系分开呈现。</p></section>
    <section><h2>私人研究资料</h2><p>PDF、笔记、问题、回答和 Key 不进入公网统计，也不会成为服务器账号资产。</p></section>
    <section><h2>轻量使用计数</h2><p>默认只数访问、检索、图谱、本地启动与阅读成功。每日匿名标识跨日变化，不记录研究内容。</p></section>
  </div>
  <p id="usage-stats" class="usage-line" aria-live="polite">匿名使用概况读取中…</p>
</main>{HOME_SCRIPT}""",
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
