"""Minimalism homepage for the public portal."""

from __future__ import annotations

from html import escape
from typing import Any


MINIMAL_CSS = r"""
:root {
  color-scheme:light;
  --paper:oklch(98.2% .004 90);
  --surface:var(--paper);
  --ink:oklch(22% .01 255);
  --muted:oklch(51% .012 255);
  --line:oklch(85% .006 255);
  --soft:oklch(95.5% .006 255);
  --accent:oklch(48% .19 265);
  --blue:var(--accent);
  --blue-soft:oklch(93% .035 265);
  --amber:var(--accent);
  --focus:oklch(55% .2 265);
}
* { box-sizing:border-box }
html { min-width:320px; scroll-behavior:smooth; background:var(--paper) }
body {
  margin:0; background:var(--paper); color:var(--ink);
  font-family:"Helvetica Neue","PingFang SC","Microsoft YaHei",sans-serif;
  font-size:16px; line-height:1.65; text-rendering:optimizeLegibility;
}
a { color:inherit }
button,input { color:inherit; font:inherit }
[hidden] { display:none !important }
a:focus-visible,button:focus-visible,input:focus-visible {
  outline:2px solid var(--focus); outline-offset:4px;
}
.skip {
  position:absolute; left:24px; top:-60px; z-index:20; padding:8px 12px;
  border:1px solid var(--ink); background:var(--paper); text-decoration:none;
}
.skip:focus { top:12px }
.minimal-shell { min-height:100vh }
.minimal-header {
  width:min(1220px,calc(100% - 48px)); min-height:76px; margin:auto;
  display:grid; grid-template-columns:1fr auto; align-items:center;
  border-bottom:1px solid var(--ink);
}
.minimal-brand {
  display:inline-flex; width:max-content; align-items:center; gap:8px;
  text-decoration:none; font-size:18px; font-weight:650; letter-spacing:.08em;
}
.minimal-brand em { color:var(--accent); font-style:normal }
.minimal-brand img { width:auto; height:31px; object-fit:contain }
.minimal-nav { display:flex; align-items:center; gap:30px; font-size:13px }
.minimal-nav a {
  position:relative; padding:5px 0; text-decoration:none;
  transition:color .18s cubic-bezier(.22,1,.36,1),opacity .18s ease-out;
}
.minimal-nav a::after {
  content:""; position:absolute; left:0; right:0; bottom:0; height:1px;
  background:currentColor; transform:scaleX(0); transform-origin:right;
  transition:transform .18s cubic-bezier(.22,1,.36,1);
}
.minimal-nav a:hover::after { transform:scaleX(1); transform-origin:left }
.minimal-nav .is-primary { color:var(--accent); font-weight:650 }
.minimal-main { width:min(1220px,calc(100% - 48px)); margin:auto }
.minimal-hero {
  min-height:670px; display:grid; grid-template-columns:128px minmax(0,1fr) 310px;
  align-items:center; border-bottom:1px solid var(--ink);
}
.hero-index {
  align-self:stretch; margin:0; padding-top:128px; border-right:1px solid var(--line);
  color:var(--muted); font:600 11px/1.5 ui-monospace,monospace; letter-spacing:.1em;
  text-transform:uppercase;
}
.hero-copy { padding:108px 72px 108px 64px }
.hero-kicker {
  margin:0 0 26px; color:var(--accent); font:650 12px/1.3 ui-monospace,monospace;
  letter-spacing:.12em; text-transform:uppercase;
}
h1,h2,h3,p { margin-top:0 }
h1 {
  max-width:760px; margin-bottom:28px; font-size:clamp(52px,7.2vw,94px);
  font-weight:560; line-height:1.02; letter-spacing:-.065em;
}
.hero-copy>p:not(.hero-kicker) {
  max-width:620px; margin-bottom:0; color:var(--muted); font-size:17px; line-height:1.8;
}
.hero-actions { display:flex; flex-wrap:wrap; gap:12px; margin-top:36px }
.button,.minimal-button {
  min-height:49px; padding:0 20px; display:inline-flex; align-items:center;
  justify-content:center; border:1px solid var(--ink); border-radius:0;
  background:transparent; color:var(--ink); text-decoration:none;
  font-size:14px; font-weight:620; cursor:pointer;
  transition:background-color .18s cubic-bezier(.22,1,.36,1),color .18s ease-out,border-color .18s ease-out;
}
.button:hover,.minimal-button:hover { background:var(--ink); color:var(--paper) }
.button.primary,.minimal-button.is-primary {
  border-color:var(--accent); background:var(--accent); color:var(--paper);
}
.button.primary:hover,.minimal-button.is-primary:hover {
  background:var(--ink); border-color:var(--ink);
}
.hero-usage {
  align-self:stretch; padding:128px 0 108px 28px; border-left:1px solid var(--line);
}
.hero-usage-title {
  margin:0 0 44px; color:var(--muted);
  font:600 11px/1.5 ui-monospace,monospace; letter-spacing:.1em;
  text-transform:uppercase;
}
.hero-metric { padding:19px 0; border-top:1px solid var(--line) }
.hero-metric span {
  display:block; margin-bottom:7px; color:var(--muted); font-size:12px;
}
.hero-metric strong {
  display:block; font-size:30px; font-weight:560; line-height:1.15;
  font-variant-numeric:tabular-nums;
}
.hero-usage-note {
  margin:20px 0 0; color:var(--muted); font-size:11px; line-height:1.6;
}

.product-demo {
  margin:0; padding:96px 0 104px; border-top:0; border-bottom:1px solid var(--ink);
}
.demo-intro {
  grid-template-columns:minmax(0,1.25fr) minmax(300px,.75fr);
  align-items:start; margin-bottom:44px;
}
.demo-kicker {
  color:var(--accent); font:650 11px/1.4 ui-monospace,monospace;
  letter-spacing:.1em; text-transform:uppercase;
}
.demo-intro h2,.demo-step h3,.demo-verdict strong {
  font-family:inherit; font-weight:560;
}
.demo-intro h2 { font-size:clamp(38px,5vw,62px); letter-spacing:-.045em }
.demo-paper-meta strong { font-family:inherit; font-size:17px }
.demo-workspace {
  border-color:var(--ink); background:var(--paper);
  grid-template-columns:minmax(0,1.08fr) minmax(380px,.92fr);
}
.demo-paper-pane { background:oklch(27% .008 255) }
.demo-page-viewport { background:oklch(21% .006 255) }
.demo-page-sheet img { box-shadow:0 12px 30px oklch(10% .01 255 / .18) }
.demo-panel { border-left:1px solid var(--ink); background:var(--paper) }
.demo-progress li.is-current { border-bottom-color:var(--accent) }
.demo-note pre { border:1px solid var(--line); background:var(--soft) }
.demo-note-heading strong { border-radius:0 }
.demo-page-highlight { border-color:var(--accent); background:oklch(90% .07 265 / .34) }
.demo-page-highlight span { border-radius:0 }

.minimal-core {
  display:grid; grid-template-columns:128px minmax(0,1fr) 310px;
  border-bottom:1px solid var(--ink);
}
.core-index {
  margin:0; padding:72px 18px 72px 0; color:var(--muted);
  font:600 11px/1.5 ui-monospace,monospace; letter-spacing:.1em;
}
.core-copy { padding:68px 64px; border-left:1px solid var(--line) }
.core-copy h2 {
  margin-bottom:14px; font-size:clamp(32px,4vw,48px);
  font-weight:560; line-height:1.16; letter-spacing:-.035em;
}
.core-copy>p { max-width:690px; margin-bottom:0; color:var(--muted); font-size:14px }
.core-copy>p+p { margin-top:12px }
.release-kicker {
  margin:0 0 12px; color:var(--accent);
  font:650 11px/1.5 ui-monospace,monospace; letter-spacing:.08em;
  text-transform:uppercase;
}
.install-steps {
  max-width:760px; margin:28px 0 0; padding:0; list-style:none;
  display:grid; grid-template-columns:repeat(3,1fr); border-top:1px solid var(--ink);
  counter-reset:install-step;
}
.install-steps li {
  min-height:112px; padding:18px 20px 18px 0; border-right:1px solid var(--line);
  counter-increment:install-step; color:var(--muted); font-size:13px;
}
.install-steps li+li { padding-left:20px }
.install-steps li:last-child { border-right:0 }
.install-steps li::before {
  content:"0" counter(install-step); display:block; margin-bottom:22px;
  color:var(--accent); font:650 11px/1 ui-monospace,monospace;
}
.browser-compat {
  max-width:690px; margin-top:16px !important; color:var(--ink) !important;
  padding-left:13px; border-left:2px solid var(--accent);
}
.core-actions { display:flex; flex-wrap:wrap; gap:12px; margin-top:28px }
.core-state {
  margin:0; padding:72px 0 72px 28px; border-left:1px solid var(--line);
  color:var(--muted); font:600 11px/1.5 ui-monospace,monospace; letter-spacing:.04em;
}
.core-state::before {
  content:""; display:inline-block; width:8px; height:8px; margin-right:9px;
  border-radius:50%; background:var(--muted);
}
.core-state.is-ready { color:oklch(45% .12 150) }
.core-state.is-ready::before { background:currentColor }
code { padding:1px 4px; background:var(--soft); font:500 .92em ui-monospace,monospace }

.principles { padding:92px 0 110px }
.section-head {
  display:grid; grid-template-columns:128px minmax(0,1fr); margin-bottom:46px;
}
.section-number {
  color:var(--muted); font:600 11px/1.5 ui-monospace,monospace; letter-spacing:.1em;
}
.section-head h2 {
  margin-bottom:10px; font-size:clamp(31px,4vw,48px);
  font-weight:560; line-height:1.16; letter-spacing:-.035em;
}
.principles-grid {
  display:grid; grid-template-columns:128px repeat(3,1fr); border-top:1px solid var(--ink);
}
.principle-index { padding-top:25px; color:var(--muted); font:600 11px/1.5 ui-monospace,monospace }
.principle { min-height:210px; padding:26px 30px; border-left:1px solid var(--line) }
.principle b {
  display:block; margin-bottom:52px; color:var(--accent);
  font:600 11px/1.5 ui-monospace,monospace;
}
.principle h3 { margin-bottom:8px; font-size:17px; font-weight:620 }
.principle p { margin:0; color:var(--muted); font-size:13px }
.minimal-footer {
  width:min(1220px,calc(100% - 48px)); margin:auto; padding:28px 0 42px;
  display:flex; justify-content:space-between; gap:20px; border-top:1px solid var(--line);
  color:var(--muted); font-size:12px;
}

@media(max-width:960px) {
  .minimal-hero { grid-template-columns:92px minmax(0,1fr) }
  .hero-index { padding-top:94px }
  .hero-copy { padding:84px 48px }
  .hero-usage {
    grid-column:2; padding:0 48px 70px; border-left:0;
    display:grid; grid-template-columns:repeat(3,minmax(0,1fr));
  }
  .hero-usage-title { grid-column:1/-1; margin-bottom:18px }
  .hero-metric { padding:18px 20px 0 0 }
  .hero-metric+.hero-metric { padding-left:20px; border-left:1px solid var(--line) }
  .hero-usage-note { grid-column:1/-1; margin-top:22px }
  .demo-workspace { grid-template-columns:minmax(0,1fr) minmax(340px,.92fr) }
  .principles-grid { grid-template-columns:92px repeat(3,1fr) }
  .minimal-core { grid-template-columns:92px minmax(0,1fr) }
  .core-copy { padding:60px 48px }
  .core-state { grid-column:2; padding:0 48px 56px; border-left:1px solid var(--line) }
  .section-head { grid-template-columns:92px minmax(0,1fr) }
}
@media(max-width:720px) {
  .minimal-header {
    width:calc(100% - 32px); padding:14px 0 12px;
    grid-template-columns:1fr; gap:14px;
  }
  .minimal-nav {
    display:grid; grid-template-columns:repeat(4,minmax(0,1fr));
    gap:8px; font-size:10px;
  }
  .minimal-nav a { text-align:center }
  .minimal-main,.minimal-footer { width:calc(100% - 32px) }
  .minimal-hero { min-height:0; grid-template-columns:1fr }
  .hero-index { padding:54px 0 0; border-right:0 }
  .hero-copy { padding:32px 0 62px }
  h1 { font-size:52px }
  .hero-copy>p:not(.hero-kicker) { font-size:15px }
  .hero-actions,.core-actions { display:grid }
  .minimal-button,.core-actions .button { width:100% }
  .hero-usage {
    grid-column:1; padding:24px 0 52px; border-top:1px solid var(--line);
    display:block;
  }
  .hero-usage-title { margin-bottom:10px }
  .hero-metric { padding:18px 0 }
  .hero-metric+.hero-metric { padding-left:0; border-left:0 }
  .hero-usage-note { margin-top:12px }
  .product-demo { padding:68px 0 76px }
  .demo-intro { grid-template-columns:1fr }
  .demo-workspace { grid-template-columns:1fr }
  .demo-panel { border-left:0; border-top:1px solid var(--ink) }
  .minimal-core { grid-template-columns:1fr }
  .core-index { padding:62px 0 14px }
  .core-copy { padding:20px 0 48px; border-left:0 }
  .install-steps { grid-template-columns:1fr }
  .install-steps li,.install-steps li+li {
    min-height:0; padding:18px 0; border-right:0; border-bottom:1px solid var(--line);
  }
  .install-steps li:last-child { border-bottom:0 }
  .install-steps li::before { margin-bottom:10px }
  .core-state { grid-column:1; padding:24px 0 48px; border-left:0; border-top:1px solid var(--line) }
  .principles { padding:68px 0 80px }
  .section-head { grid-template-columns:1fr; gap:14px }
  .principles-grid { grid-template-columns:1fr }
  .principle-index { padding:18px 0 }
  .principle { min-height:0; padding:24px 0; border-left:0; border-top:1px solid var(--line) }
  .principle b { margin-bottom:26px }
  .minimal-footer { flex-direction:column }
}
@media(prefers-reduced-motion:reduce) {
  html { scroll-behavior:auto }
  *,*::before,*::after { transition-duration:.01ms !important }
}
"""


def minimal_home_document(
    *,
    demo_css: str,
    demo_html: str,
    analytics_script: str,
    core_script: str,
    github_url: str,
    release_manifest: dict[str, Any] | None,
) -> str:
    github = escape(github_url, quote=True)
    mac_release = (release_manifest or {}).get("downloads", {}).get("macos_arm64")
    has_mac_release = isinstance(mac_release, dict)
    if has_mac_release:
        version = escape(str(release_manifest["version"]))
        release_url = escape(str(release_manifest["release_url"]), quote=True)
        install_href = "/api/portal/download/macos_arm64"
        install_label = "下载 macOS Beta"
        release_status = (
            f'<p class="release-kicker">v{version} · Developer ID signed · '
            "Apple notarized</p>"
        )
        install_guidance = """<ol class="install-steps" aria-label="安装步骤">
          <li>打开下载的 DMG</li>
          <li>将 Peinidu.app 拖入 Applications</li>
          <li>启动陪你读，再返回检查 Core</li>
        </ol>"""
        core_install_actions = f"""
          <a class="button primary" href="{install_href}">下载 Apple 芯片 Mac Beta</a>
          <a class="button" href="{release_url}" target="_blank" rel="noopener noreferrer">查看 Release notes ↗</a>
          <a class="button" href="{github}" target="_blank" rel="noopener noreferrer">开发者源码安装 ↗</a>"""
    else:
        install_href = github
        install_label = "查看开发者源码安装"
        release_status = (
            '<p class="release-kicker">当前公开安装方式 · Developer source setup</p>'
        )
        install_guidance = ""
        core_install_actions = f"""
          <a class="button primary" href="{github}" target="_blank" rel="noopener noreferrer">查看开发者源码安装 ↗</a>"""
    install_target = (
        ""
        if has_mac_release
        else ' target="_blank" rel="noopener noreferrer"'
    )
    browser_script = r"""
<script>
(() => {
  const warning = document.getElementById("browser-compat");
  if (!warning) return;
  const value = navigator.userAgent || "";
  const chromium = /(Chrome|Chromium|CriOS|Edg|OPR)\//.test(value);
  warning.hidden = chromium;
})();
</script>"""
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="color-scheme" content="light">
  <link rel="icon" href="data:,">
  <title>陪你读 — 读好论文，沉淀基础</title>
  <style>{demo_css}{MINIMAL_CSS}</style>
</head>
<body>
<a class="skip" href="#main">跳到主要内容</a>
<div class="minimal-shell">
  <header class="minimal-header">
    <a class="minimal-brand" href="/" aria-label="陪你读">
      <span>陪你<em>读</em></span>
      <img src="/api/portal/mascot.png" alt="" aria-hidden="true">
    </a>
    <nav class="minimal-nav" aria-label="门户导航">
      <a href="#product-demo">产品演示</a>
      <a href="#local-core">本地使用</a>
      <a href="/privacy">隐私说明</a>
      <a class="is-primary" href="{github}" target="_blank" rel="noopener noreferrer">GitHub</a>
    </nav>
  </header>

  {analytics_script}

  <main id="main" class="minimal-main">
    <section class="minimal-hero" aria-labelledby="minimal-title">
      <p class="hero-index">01 / Start<br>Research workspace</p>
      <div class="hero-copy">
        <p class="hero-kicker">Local-first paper reading</p>
        <h1 id="minimal-title">读好论文，<br>沉淀基础。</h1>
        <p>在原始 PDF 上阅读、翻译和记录判断，再让 Pet 帮你沿着证据核对方法与复现条件。</p>
        <div class="hero-actions">
          <a id="open-core" class="minimal-button is-primary" href="http://127.0.0.1:8520" target="_blank" rel="noopener noreferrer">尝试打开本地工作台</a>
          <a id="install-core" class="minimal-button" href="{install_href}"{install_target}>{install_label}</a>
        </div>
      </div>
      <aside class="hero-usage" aria-label="匿名使用概况">
        <p class="hero-usage-title">03 / Usage</p>
        <div class="hero-metric"><span>累计访问</span><strong id="total-portal-visits" aria-live="polite">读取中…</strong></div>
        <div class="hero-metric"><span>Core 启动</span><strong id="total-core-starts" aria-live="polite">读取中…</strong></div>
        <div class="hero-metric"><span>开始阅读</span><strong id="total-reader-opens" aria-live="polite">读取中…</strong></div>
        <p class="hero-usage-note">累计匿名次数，不代表唯一用户人数</p>
      </aside>
    </section>

    {demo_html}

    <section id="local-core" class="minimal-core" aria-labelledby="local-core-title">
      <p class="core-index">04 / Local core</p>
      <div class="core-copy">
        {release_status}
        <h2 id="local-core-title">先启动，再打开。</h2>
        <p>网页只能确认本机 <code>127.0.0.1:8520</code> 的 Core 是否正在运行，不能静默读取电脑里是否装过应用。已经安装时请先启动 Core；尚未安装时再前往 GitHub。</p>
        <p>Chrome 首次检查时可能询问是否允许本站访问本地网络；该权限只用于连接这台电脑上的 <code>127.0.0.1</code> Core。即使检测被浏览器拦截，也可以直接尝试打开本地工作台。</p>
        <p id="browser-compat" class="browser-compat" hidden>当前浏览器不是 Chrome/Chromium。你仍可下载安装，但本地网络检测和本地文件夹能力建议使用最新版 Chrome；网页无法判断电脑里是否安装过 Chrome。</p>
        {install_guidance}
        <div class="core-actions">
          {core_install_actions}
          <button id="retry-core" class="button" type="button">重新检查本地 Core</button>
        </div>
      </div>
      <p id="core-state" class="core-state"><span>正在检查本地 Core</span></p>
    </section>

    <section class="principles" aria-labelledby="principles-title">
      <header class="section-head">
        <span class="section-number">05 / Boundary</span>
        <div><h2 id="principles-title">界面安静，边界明确。</h2></div>
      </header>
      <div class="principles-grid">
        <span class="principle-index">LOCAL FIRST</span>
        <article class="principle"><b>01</b><h3>核对原始页面</h3><p>工作台运行在本机浏览器中，原始 PDF 始终是阅读主面。</p></article>
        <article class="principle"><b>02</b><h3>研究资料留在本机</h3><p>论文、笔记、问题、回答与 Key 不成为公网账号资产。</p></article>
        <article class="principle"><b>03</b><h3>无需注册账号</h3><p>不需要网站账号，安装和更新流程以 GitHub 项目说明为准。</p></article>
      </div>
    </section>
  </main>

  <footer class="minimal-footer">
    <span>陪你读 · 读好论文，沉淀基础。</span>
    <a href="/privacy">隐私说明</a>
  </footer>
</div>
{core_script}{browser_script}
</body>
</html>"""
