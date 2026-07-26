"""Public portal allowlist: releases, aggregate counts and content-free telemetry."""

from __future__ import annotations

import asyncio
from html import escape

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..telemetry.contracts import PortalTelemetryEvent
from ..telemetry.portal_store import (
    aggregate_stats,
    load_release_manifest,
    record_download,
    record_event,
)


router = APIRouter(prefix="/api/portal", tags=["public portal"])
_MAX_TELEMETRY_BODY_BYTES = 2048
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
  <title>{escape(title)}</title>
  <style>
    :root {{
      color-scheme: light;
      --paper:#f7f7f5; --surface:#fff; --ink:#202326; --muted:#6f7377;
      --line:#d9dbdc; --accent:#a86900; --soft:#f2eee5; --focus:#315f92;
    }}
    @media(prefers-color-scheme:dark) {{
      :root {{ color-scheme:dark; --paper:#17191b; --surface:#202326; --ink:#f0f0ec;
        --muted:#afb2b5; --line:#3b3e41; --accent:#e1ac54; --soft:#2a2822; --focus:#8bb5e0; }}
    }}
    * {{ box-sizing:border-box }}
    body {{ margin:0; background:var(--paper); color:var(--ink);
      font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
      font-size:16px; line-height:1.7 }}
    a {{ color:inherit }}
    a:focus-visible,button:focus-visible {{ outline:3px solid var(--focus); outline-offset:3px }}
    .skip {{ position:absolute; left:1rem; top:-5rem; padding:.65rem 1rem;
      background:var(--surface); border:1px solid var(--line); z-index:5 }}
    .skip:focus {{ top:1rem }}
    header {{ min-height:72px; display:flex; align-items:center; justify-content:space-between;
      max-width:1120px; margin:auto; padding:0 28px; border-bottom:1px solid var(--line) }}
    .brand {{ text-decoration:none; font-family:ui-serif,"Songti SC",serif;
      font-size:22px; letter-spacing:.08em }}
    .brand em {{ color:var(--accent); font-style:normal }}
    nav {{ display:flex; gap:24px; font-size:14px }}
    main {{ max-width:1120px; margin:auto; padding:76px 28px 88px }}
    .hero {{ display:grid; grid-template-columns:minmax(0,1.25fr) minmax(300px,.75fr);
      gap:72px; align-items:start }}
    .eyebrow {{ margin:0 0 18px; color:var(--accent); font:600 12px/1.2 ui-monospace,monospace;
      letter-spacing:.16em; text-transform:uppercase }}
    h1 {{ max-width:780px; margin:0; font-family:ui-serif,"Songti SC",serif;
      font-size:clamp(42px,6vw,76px); font-weight:500; line-height:1.06; letter-spacing:-.035em }}
    .lede {{ max-width:680px; margin:28px 0 0; color:var(--muted); font-size:18px }}
    .actions {{ display:flex; flex-wrap:wrap; gap:12px; margin-top:34px }}
    .button {{ min-height:46px; display:inline-flex; align-items:center; justify-content:center;
      padding:0 18px; border:1px solid var(--ink); border-radius:7px; text-decoration:none;
      font-weight:600; cursor:pointer }}
    .button.primary {{ background:var(--ink); color:var(--surface) }}
    .button[aria-disabled="true"] {{ opacity:.48; pointer-events:none }}
    .receipt {{ background:var(--surface); border:1px solid var(--line); padding:26px;
      box-shadow:12px 12px 0 var(--soft) }}
    .receipt h2 {{ margin:0 0 20px; font:600 13px/1.2 ui-monospace,monospace;
      letter-spacing:.1em }}
    .receipt dl {{ margin:0 }}
    .receipt div {{ display:flex; justify-content:space-between; gap:24px;
      padding:12px 0; border-top:1px solid var(--line) }}
    .receipt dt {{ color:var(--muted) }} .receipt dd {{ margin:0; font-weight:650 }}
    .receipt .zero {{ color:var(--accent) }}
    .sections {{ display:grid; grid-template-columns:repeat(3,1fr); gap:0;
      margin-top:88px; border-top:1px solid var(--line); border-bottom:1px solid var(--line) }}
    .sections section {{ padding:28px 28px 32px 0 }}
    .sections section+section {{ padding-left:28px; border-left:1px solid var(--line) }}
    h2 {{ margin:0 0 12px; font-size:19px }} p {{ margin:0 }}
    .small {{ color:var(--muted); font-size:14px }}
    .stats {{ margin-top:32px; color:var(--muted); font-size:13px }}
    footer {{ max-width:1120px; margin:auto; padding:26px 28px 48px; color:var(--muted);
      font-size:13px; border-top:1px solid var(--line) }}
    .prose {{ max-width:760px }} .prose h1 {{ font-size:clamp(36px,5vw,58px); margin-bottom:32px }}
    .prose h2 {{ margin-top:40px }} .prose ul {{ padding-left:1.2rem }}
    @media(max-width:760px) {{
      header {{ padding:0 18px }} nav {{ gap:14px }}
      main {{ padding:48px 18px 64px }} .hero {{ grid-template-columns:1fr; gap:44px }}
      .sections {{ grid-template-columns:1fr }}
      .sections section,.sections section+section {{ padding:24px 0; border-left:0;
        border-top:1px solid var(--line) }}
      .sections section:first-child {{ border-top:0 }}
    }}
  </style>
</head>
<body>
  <a class="skip" href="#main">跳到主要内容</a>
  <header>
    <a class="brand" href="/">陪你<em>读</em></a>
    <nav aria-label="门户导航"><a href="/privacy">隐私说明</a><a href="http://127.0.0.1:8520">打开本地工作台</a></nav>
  </header>
  {body}
  <footer>陪你读 · 原始 PDF 阅读、划选翻译与论文 Agent。你的研究资料留在你的设备。</footer>
</body>
</html>"""
    return HTMLResponse(html, headers=_PORTAL_HEADERS)


def portal_home() -> HTMLResponse:
    return _page(
        title="陪你读 — 本地优先的论文阅读工作台",
        body="""<main id="main">
  <div class="hero">
    <div>
      <p class="eyebrow">Local-first paper workspace</p>
      <h1>网页是入口，论文仍在你的电脑里。</h1>
      <p class="lede">安装本地 Pet Core 后，继续在浏览器里阅读原始 PDF、划选翻译、写 Markdown 笔记并与 Agent 讨论。模型与翻译服务使用你自己的 Key。</p>
      <div class="actions" aria-label="下载本地 Pet Core">
        <a id="download-mac" class="button primary" href="/api/portal/download/macos_arm64" aria-disabled="true">macOS Apple 芯片版准备中</a>
        <a id="download-windows" class="button" href="/api/portal/download/windows_x64" aria-disabled="true">Windows 版准备中</a>
        <a class="button" href="http://127.0.0.1:8520">已安装，打开工作台</a>
      </div>
      <p id="release-note" class="stats" role="status">正在读取可用版本…</p>
    </div>
    <aside class="receipt" aria-label="隐私收据">
      <h2>PRIVACY RECEIPT</h2>
      <dl>
        <div><dt>上传论文</dt><dd class="zero">0</dd></div>
        <div><dt>上传笔记</dt><dd class="zero">0</dd></div>
        <div><dt>上传 Key</dt><dd class="zero">0</dd></div>
        <div><dt>匿名统计</dt><dd>默认关闭</dd></div>
      </dl>
    </aside>
  </div>
  <div class="sections">
    <section><h2>仍然是网页体验</h2><p class="small">阅读器运行在本机的 loopback 地址，保留浏览器交互，不把 PDF 和笔记变成服务器账号资产。</p></section>
    <section><h2>服务由你选择</h2><p class="small">LLM、DeepLX 与 MinerU 凭据保存在系统钥匙串；公网入口不代理调用，也看不到 Key。</p></section>
    <section><h2>统计先征得同意</h2><p class="small">开启后只发送每日变化的匿名标识和固定事件。不能跨天追踪，也没有论文、问题或回答。</p></section>
  </div>
  <p id="usage-stats" class="stats" aria-live="polite">匿名使用概况读取中…</p>
</main>
<script>
  const labels={macos_arm64:"下载 macOS Apple 芯片版",windows_x64:"下载 Windows x64 版"};
  fetch("/api/portal/releases/latest").then(r=>r.ok?r.json():Promise.reject()).then(data=>{
    for(const item of data.downloads){
      const node=document.getElementById(item.platform==="macos_arm64"?"download-mac":"download-windows");
      node.textContent=labels[item.platform]; node.removeAttribute("aria-disabled");
    }
    document.getElementById("release-note").textContent=`当前版本 ${data.version} · 下载后在本机启动，不需要网站账号`;
  }).catch(()=>{document.getElementById("release-note").textContent="安装包尚未公开；现有本地开发版本不作为正式下载提供。";});
  fetch("/api/portal/stats").then(r=>r.json()).then(data=>{
    document.getElementById("usage-stats").textContent=`已同意统计：今日匿名活跃 ${data.active_today} · 今日成功打开阅读器 ${data.readers_today} · 累计下载 ${data.total_downloads}`;
  }).catch(()=>{document.getElementById("usage-stats").textContent="匿名使用概况暂不可用。";});
</script>""",
    )


def privacy_page() -> HTMLResponse:
    return _page(
        title="隐私说明 — 陪你读",
        body="""<main id="main" class="prose">
  <p class="eyebrow">Privacy boundary</p>
  <h1>你的论文不是我们的数据。</h1>
  <h2>默认状态</h2>
  <p>匿名使用统计默认关闭。阅读、翻译、笔记与 Agent 对话在本地 Pet Core 中完成；公网入口不提供共享内容后端。</p>
  <h2>你主动开启统计后</h2>
  <p>只发送 UTC 日期、每日变化且不可跨天关联的匿名标识、固定事件名、系统类型和应用版本。相同安装同日重复事件会去重。</p>
  <h2>永远不发送</h2>
  <ul><li>论文、PDF、标题、作者或文件路径</li><li>选区、笔记、问题、回答或 Agent 证据</li><li>Provider、DeepLX、MinerU Key 与本地配置</li><li>原始安装标识、账号、邮箱或精确位置</li></ul>
  <h2>保留期限</h2>
  <p>用于当日去重的匿名日标识最多保留 35 天，之后只保留按日期和事件聚合的数字。应用数据库不保存 IP 或 User-Agent；部署层访问日志也应关闭。</p>
  <p class="stats"><a href="/">返回下载页</a></p>
</main>""",
    )


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
