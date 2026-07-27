"""Standalone Minimalism exploration page for the public portal."""

from __future__ import annotations


MINIMAL_CSS = r"""
:root {
  color-scheme:light;
  --paper:oklch(98.2% .004 90);
  --ink:oklch(22% .01 255);
  --muted:oklch(51% .012 255);
  --line:oklch(85% .006 255);
  --soft:oklch(95.5% .006 255);
  --accent:oklch(48% .19 265);
}
* { box-sizing:border-box }
html { min-width:320px; scroll-behavior:smooth; background:var(--paper) }
body {
  margin:0; background:var(--paper); color:var(--ink);
  font-family:"Helvetica Neue","PingFang SC","Microsoft YaHei",sans-serif;
  font-size:16px; line-height:1.65; text-rendering:optimizeLegibility;
}
a { color:inherit }
button { color:inherit; font:inherit }
a:focus-visible,button:focus-visible { outline:2px solid var(--accent); outline-offset:4px }
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
  align-self:stretch; padding-top:128px; border-right:1px solid var(--line);
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
.minimal-button {
  min-height:49px; padding:0 20px; display:inline-flex; align-items:center;
  justify-content:center; border:1px solid var(--ink); background:transparent;
  text-decoration:none; font-size:14px; font-weight:620;
  transition:background-color .18s cubic-bezier(.22,1,.36,1),color .18s ease-out,border-color .18s ease-out;
}
.minimal-button:hover { background:var(--ink); color:var(--paper) }
.minimal-button.is-primary { border-color:var(--accent); background:var(--accent); color:var(--paper) }
.minimal-button.is-primary:hover { background:var(--ink); border-color:var(--ink) }
.hero-aside { align-self:stretch; padding:128px 0 0 28px; border-left:1px solid var(--line) }
.hero-aside strong { display:block; margin-bottom:14px; font-size:13px }
.hero-aside p { margin-bottom:20px; color:var(--muted); font-size:13px }
.hero-aside a { color:var(--accent); font-size:13px; text-underline-offset:4px }
.proof-section { padding:96px 0 104px; border-bottom:1px solid var(--ink) }
.section-head {
  display:grid; grid-template-columns:128px minmax(0,1fr); margin-bottom:46px;
}
.section-number {
  color:var(--muted); font:600 11px/1.5 ui-monospace,monospace; letter-spacing:.1em;
}
.section-head h2 {
  margin-bottom:10px; font-size:clamp(31px,4vw,48px); font-weight:560; line-height:1.16; letter-spacing:-.035em;
}
.section-head p { max-width:600px; margin:0; color:var(--muted); font-size:14px }
.proof-grid {
  display:grid; grid-template-columns:128px minmax(0,1.25fr) minmax(300px,.75fr);
  border-top:1px solid var(--ink); border-bottom:1px solid var(--ink);
}
.proof-rail { padding:24px 18px 24px 0; border-right:1px solid var(--line) }
.proof-step {
  display:block; padding:12px 0; color:var(--muted);
  font:600 11px/1.4 ui-monospace,monospace; letter-spacing:.06em;
}
.proof-step.is-active { color:var(--accent) }
.paper-proof { padding:38px 46px 44px }
.paper-proof .meta {
  margin-bottom:24px; color:var(--muted); font:550 11px/1.5 ui-monospace,monospace;
  letter-spacing:.04em;
}
.paper-proof h3 {
  margin-bottom:8px; font-family:Georgia,"Times New Roman",serif;
  font-size:27px; line-height:1.2;
}
.authors { margin-bottom:32px; color:var(--muted); font-size:12px }
.abstract-label {
  margin-bottom:9px; font:650 10px/1.4 ui-monospace,monospace; letter-spacing:.12em;
}
.paper-proof blockquote {
  margin:0; font-family:Georgia,"Times New Roman",serif; font-size:16px; line-height:1.85;
}
.paper-proof mark { background:oklch(90% .06 265); color:var(--ink); padding:2px 0 }
.evidence-panel { border-left:1px solid var(--ink) }
.evidence-item { padding:26px 28px }
.evidence-item+.evidence-item { border-top:1px solid var(--line) }
.evidence-label {
  display:block; margin-bottom:9px; color:var(--accent);
  font:650 10px/1.4 ui-monospace,monospace; letter-spacing:.1em; text-transform:uppercase;
}
.evidence-item strong { display:block; margin-bottom:7px; font-size:14px }
.evidence-item p { margin:0; color:var(--muted); font-size:13px }
.metrics {
  display:grid; grid-template-columns:128px repeat(3,1fr); border-bottom:1px solid var(--ink);
}
.metrics-title {
  padding:30px 18px 30px 0; color:var(--muted);
  font:600 11px/1.5 ui-monospace,monospace; letter-spacing:.1em;
}
.metric { padding:27px 28px; border-left:1px solid var(--line) }
.metric span { display:block; margin-bottom:5px; color:var(--muted); font-size:12px }
.metric strong { font-size:28px; font-weight:560; font-variant-numeric:tabular-nums }
.principles { padding:92px 0 110px }
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
@media(max-width:900px) {
  .minimal-hero { grid-template-columns:92px minmax(0,1fr) }
  .hero-index { padding-top:94px }
  .hero-copy { padding:84px 48px }
  .hero-aside { grid-column:2; padding:0 48px 70px; border-left:0 }
  .section-head,.proof-grid { grid-template-columns:92px minmax(0,1fr) }
  .evidence-panel { grid-column:2; border-left:0; border-top:1px solid var(--ink) }
  .metrics,.principles-grid { grid-template-columns:92px repeat(3,1fr) }
}
@media(max-width:620px) {
  .minimal-header {
    width:calc(100% - 32px); padding:14px 0 12px;
    grid-template-columns:1fr; gap:14px;
  }
  .minimal-nav { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:8px; font-size:10px }
  .minimal-nav a { text-align:center }
  .minimal-main,.minimal-footer { width:calc(100% - 32px) }
  .minimal-hero { min-height:0; grid-template-columns:1fr }
  .hero-index { padding:54px 0 0; border-right:0 }
  .hero-copy { padding:32px 0 62px }
  h1 { font-size:52px }
  .hero-copy>p:not(.hero-kicker) { font-size:15px }
  .hero-actions { display:grid }
  .minimal-button { width:100% }
  .hero-aside { grid-column:1; padding:24px 0 52px; border-top:1px solid var(--line) }
  .proof-section { padding:68px 0 76px }
  .section-head { grid-template-columns:1fr; gap:14px }
  .proof-grid { grid-template-columns:1fr }
  .proof-rail {
    display:flex; gap:20px; padding:14px 0; border-right:0; border-bottom:1px solid var(--line);
  }
  .paper-proof { padding:30px 0 36px }
  .evidence-panel { grid-column:1 }
  .evidence-item { padding:24px 0 }
  .metrics { grid-template-columns:1fr }
  .metrics-title { padding:24px 0 10px }
  .metric { padding:20px 0; border-left:0; border-top:1px solid var(--line) }
  .principles { padding:68px 0 80px }
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


def minimal_preview_document() -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="color-scheme" content="light">
  <link rel="icon" href="data:,">
  <title>陪你读 Minimalism 探索版</title>
  <style>{MINIMAL_CSS}</style>
</head>
<body>
<div class="minimal-shell">
  <header class="minimal-header">
    <a class="minimal-brand" href="/" aria-label="返回当前正式主页">
      <span>陪你<em>读</em></span>
      <img src="/api/portal/mascot.png" alt="" aria-hidden="true">
    </a>
    <nav class="minimal-nav" aria-label="探索版导航">
      <a href="#proof">产品体验</a>
      <a href="#principles">本地使用</a>
      <a href="/privacy">隐私说明</a>
      <a class="is-primary" href="https://github.com/xiaoyu-ops/Read_with_you" target="_blank" rel="noopener noreferrer">GitHub</a>
    </nav>
  </header>

  <main class="minimal-main">
    <section class="minimal-hero" aria-labelledby="minimal-title">
      <p class="hero-index">01 / Start<br>Research workspace</p>
      <div class="hero-copy">
        <p class="hero-kicker">Local-first paper reading</p>
        <h1 id="minimal-title">读好论文，<br>沉淀基础。</h1>
        <p>在原始 PDF 上阅读、翻译和记录判断，再让 Pet 帮你沿着证据核对方法与复现条件。</p>
        <div class="hero-actions">
          <a class="minimal-button is-primary" href="http://127.0.0.1:8520" target="_blank" rel="noopener noreferrer">打开本地工作台</a>
          <a class="minimal-button" href="#proof">查看精读过程</a>
        </div>
      </div>
      <aside class="hero-aside">
        <strong>独立视觉探索</strong>
        <p>当前正式主页没有被替换。这个页面只用于判断 Minimalism 是否适合陪你读。</p>
        <a href="/">返回当前主页</a>
      </aside>
    </section>

    <section id="proof" class="proof-section" aria-labelledby="proof-title">
      <header class="section-head">
        <span class="section-number">02 / Evidence</span>
        <div>
          <h2 id="proof-title">一篇论文，一条清晰证据链。</h2>
          <p>原文、译文、笔记和 Agent 判断各自归位，不用装饰替代信息层级。</p>
        </div>
      </header>

      <div class="proof-grid">
        <nav class="proof-rail" aria-label="精读步骤">
          <span class="proof-step is-active">01 原文</span>
          <span class="proof-step">02 翻译</span>
          <span class="proof-step">03 笔记</span>
          <span class="proof-step">04 证据</span>
        </nav>
        <article class="paper-proof">
          <p class="meta">ARXIV 1706.03762 · PAGE 1 · ABSTRACT</p>
          <h3>Attention Is All You Need</h3>
          <p class="authors">Vaswani et al. · 2017</p>
          <p class="abstract-label">SELECTED TEXT</p>
          <blockquote><mark>We propose a new simple network architecture, the Transformer, based solely on attention mechanisms.</mark></blockquote>
        </article>
        <aside class="evidence-panel">
          <div class="evidence-item">
            <span class="evidence-label">Translation</span>
            <strong>当前选区译文</strong>
            <p>我们提出一种新的简洁网络架构 Transformer，它完全基于注意力机制。</p>
          </div>
          <div class="evidence-item">
            <span class="evidence-label">Method note</span>
            <strong>方法判断已保存</strong>
            <p>核心贡献是移除循环结构，以自注意力建立序列依赖。</p>
          </div>
          <div class="evidence-item">
            <span class="evidence-label">Pet check</span>
            <strong>复现信息：部分充分</strong>
            <p>正文给出数据、硬件和主要超参数，但没有官方代码仓库定位。</p>
          </div>
        </aside>
      </div>
    </section>

    <section class="metrics" aria-label="匿名使用概况">
      <p class="metrics-title">03 / Usage</p>
      <div class="metric"><span>累计访问</span><strong id="minimal-visits">读取中</strong></div>
      <div class="metric"><span>Core 启动</span><strong id="minimal-starts">读取中</strong></div>
      <div class="metric"><span>开始阅读</span><strong id="minimal-reads">读取中</strong></div>
    </section>

    <section id="principles" class="principles" aria-labelledby="principles-title">
      <header class="section-head">
        <span class="section-number">04 / Boundary</span>
        <div><h2 id="principles-title">界面安静，边界明确。</h2></div>
      </header>
      <div class="principles-grid">
        <span class="principle-index">LOCAL FIRST</span>
        <article class="principle"><b>01</b><h3>核对原始页面</h3><p>翻译、笔记和结论始终回到 PDF 证据。</p></article>
        <article class="principle"><b>02</b><h3>资料留在本机</h3><p>论文、笔记、问题与回答不成为公网账号资产。</p></article>
        <article class="principle"><b>03</b><h3>服务由你选择</h3><p>本地 Core 使用自己的配置，公网入口看不到 Key。</p></article>
      </div>
    </section>
  </main>

  <footer class="minimal-footer">
    <span>陪你读 · Minimalism 独立探索版</span>
    <a href="/">返回当前正式主页</a>
  </footer>
</div>
<script>
(() => {{
  fetch("/api/portal/stats", {{cache:"no-store"}})
    .then((response) => response.ok ? response.json() : Promise.reject())
    .then((data) => {{
      document.getElementById("minimal-visits").textContent = Number(data.total_portal_visits || 0).toLocaleString("zh-CN");
      document.getElementById("minimal-starts").textContent = Number(data.total_core_starts || 0).toLocaleString("zh-CN");
      document.getElementById("minimal-reads").textContent = Number(data.total_reader_opens || 0).toLocaleString("zh-CN");
    }})
    .catch(() => {{
      for (const id of ["minimal-visits","minimal-starts","minimal-reads"]) document.getElementById(id).textContent = "暂不可用";
    }});
}})();
</script>
</body>
</html>"""
