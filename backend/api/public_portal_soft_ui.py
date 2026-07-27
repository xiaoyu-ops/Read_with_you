"""Standalone Soft UI exploration page for the public portal."""

from __future__ import annotations


SOFT_UI_CSS = r"""
:root {
  color-scheme:light;
  --base:oklch(93.5% .018 242);
  --base-deep:oklch(90% .026 242);
  --surface:oklch(96% .014 242);
  --ink:oklch(29% .032 248);
  --muted:oklch(53% .028 248);
  --accent:oklch(57% .125 250);
  --accent-soft:oklch(88% .058 247);
  --warm:oklch(72% .105 72);
  --light:oklch(99% .008 238 / .92);
  --shade:oklch(66% .045 246 / .34);
  --raised:-10px -10px 24px var(--light),10px 10px 24px var(--shade);
  --raised-small:-5px -5px 12px var(--light),5px 5px 12px var(--shade);
  --pressed:inset -4px -4px 9px var(--light),inset 4px 4px 9px var(--shade);
}
* { box-sizing:border-box }
html { min-width:320px; scroll-behavior:smooth; background:var(--base) }
body {
  margin:0; color:var(--ink);
  background:
    radial-gradient(circle at 15% 0%,oklch(97% .025 220),transparent 32rem),
    linear-gradient(145deg,var(--base),var(--base-deep));
  font-family:"Avenir Next","PingFang SC","Microsoft YaHei",sans-serif;
  font-size:16px; line-height:1.65;
}
a { color:inherit }
button { color:inherit; font:inherit }
a:focus-visible,button:focus-visible {
  outline:3px solid oklch(65% .16 250 / .7); outline-offset:4px;
}
.soft-shell { min-height:100vh }
.soft-header {
  width:min(1180px,calc(100% - 40px)); min-height:84px; margin:18px auto 0;
  padding:12px 14px 12px 22px; display:flex; align-items:center;
  justify-content:space-between; gap:24px; border-radius:24px;
  background:color-mix(in oklch,var(--surface) 72%,transparent);
  box-shadow:var(--raised);
}
.soft-brand {
  display:inline-flex; align-items:center; gap:10px; text-decoration:none;
  font-family:"Songti SC","STSong",serif; font-size:22px; letter-spacing:.08em;
}
.soft-brand strong { font-weight:600 }
.soft-brand em { color:var(--warm); font-style:normal }
.soft-brand img {
  width:auto; height:38px; object-fit:contain; filter:drop-shadow(0 5px 5px oklch(45% .04 245 / .18));
}
.soft-nav { display:flex; align-items:center; gap:8px }
.soft-nav a,.soft-button {
  min-height:42px; padding:0 17px; display:inline-flex; align-items:center;
  justify-content:center; gap:8px; border:0; border-radius:999px;
  background:var(--base); box-shadow:var(--raised-small); text-decoration:none;
  font-size:13px; font-weight:650; cursor:pointer;
  transition:transform .18s cubic-bezier(.22,1,.36,1),box-shadow .18s cubic-bezier(.22,1,.36,1),color .18s ease-out;
}
.soft-nav a:hover,.soft-button:hover { transform:translateY(-2px); box-shadow:-7px -7px 15px var(--light),7px 7px 15px var(--shade) }
.soft-nav a:active,.soft-button:active { transform:translateY(1px); box-shadow:var(--pressed) }
.soft-nav .is-accent,.soft-button.is-primary {
  color:oklch(98% .01 242); background:linear-gradient(145deg,oklch(63% .13 248),oklch(52% .13 252));
  box-shadow:-5px -5px 13px var(--light),7px 7px 17px oklch(48% .1 250 / .36);
}
.soft-main { width:min(1120px,calc(100% - 48px)); margin:0 auto; padding:92px 0 96px }
.soft-hero { display:grid; grid-template-columns:minmax(0,1.02fr) minmax(390px,.98fr); gap:72px; align-items:center }
.soft-eyebrow {
  margin:0 0 20px; color:var(--accent); font:700 12px/1.2 ui-monospace,monospace;
  letter-spacing:.15em; text-transform:uppercase;
}
h1,h2,p { margin-top:0 }
h1 {
  max-width:680px; margin-bottom:25px; font-family:"Songti SC","STSong",serif;
  font-size:clamp(48px,6vw,78px); font-weight:580; line-height:1.08; letter-spacing:-.045em;
}
.soft-lede { max-width:630px; margin-bottom:0; color:var(--muted); font-size:17px }
.soft-actions { display:flex; flex-wrap:wrap; gap:16px; margin-top:36px }
.soft-button { min-height:52px; padding:0 23px; font-size:15px }
.soft-note { margin:22px 0 0; color:var(--muted); font-size:13px }
.soft-note a { text-underline-offset:3px }
.research-console {
  position:relative; padding:20px; border-radius:38px; background:var(--base);
  box-shadow:-16px -16px 34px var(--light),16px 16px 34px var(--shade);
}
.console-top { display:flex; align-items:center; justify-content:space-between; gap:16px; padding:4px 5px 18px }
.console-top strong { font-size:14px }
.console-status {
  display:inline-flex; align-items:center; gap:7px; color:var(--muted);
  font-size:11px; font-weight:650; letter-spacing:.04em;
}
.console-status::before {
  content:""; width:8px; height:8px; border-radius:50%; background:oklch(64% .14 150);
  box-shadow:0 0 0 5px oklch(84% .08 150 / .45);
}
.paper-well {
  min-height:360px; padding:30px; border-radius:25px; background:var(--base-deep);
  box-shadow:var(--pressed); overflow:hidden;
}
.paper-sheet {
  position:relative; min-height:300px; padding:29px 30px; border-radius:12px;
  background:oklch(98% .008 82); color:oklch(29% .018 250);
  box-shadow:0 12px 25px oklch(45% .035 248 / .2),inset 0 1px 0 oklch(100% 0 0 / .8);
}
.paper-kicker { margin:0 0 8px; color:var(--muted); font:650 9px/1.4 ui-monospace,monospace; letter-spacing:.1em }
.paper-sheet h2 { margin-bottom:4px; font:700 22px/1.25 Georgia,"Times New Roman",serif }
.paper-meta { margin-bottom:25px; color:var(--muted); font-size:10px }
.paper-line { height:7px; margin:9px 0; border-radius:99px; background:oklch(82% .012 248) }
.paper-line.short { width:67% }
.paper-line.medium { width:86% }
.selection {
  margin:14px -7px 12px; padding:7px; border-radius:7px;
  background:var(--accent-soft); color:oklch(39% .1 250); font:600 11px/1.55 Georgia,serif;
}
.translation-bubble {
  position:absolute; right:-16px; bottom:22px; width:68%; padding:13px 15px;
  border-radius:16px; background:var(--base); box-shadow:var(--raised-small);
  color:var(--ink); font-size:11px; line-height:1.55;
}
.translation-bubble span { display:block; margin-bottom:3px; color:var(--accent); font-size:9px; font-weight:750; letter-spacing:.08em }
.console-tools { display:grid; grid-template-columns:repeat(3,1fr); gap:12px; padding-top:17px }
.tool {
  min-height:70px; padding:11px 8px; display:flex; flex-direction:column;
  align-items:center; justify-content:center; gap:5px; border:0; border-radius:18px;
  background:var(--base); box-shadow:var(--raised-small); cursor:pointer;
  font-size:11px; font-weight:650; transition:transform .18s cubic-bezier(.22,1,.36,1),box-shadow .18s ease-out;
}
.tool:hover { transform:translateY(-2px) }
.tool:active,.tool[aria-pressed="true"] { transform:translateY(1px); box-shadow:var(--pressed); color:var(--accent) }
.tool svg { width:20px; height:20px; fill:none; stroke:currentColor; stroke-width:1.7; stroke-linecap:round; stroke-linejoin:round }
.soft-metrics {
  display:grid; grid-template-columns:repeat(3,1fr); gap:20px; margin:88px 0 0;
}
.metric {
  padding:22px 24px; border-radius:22px; background:var(--base); box-shadow:var(--raised-small);
}
.metric span { display:block; color:var(--muted); font-size:12px }
.metric strong { display:block; margin-top:4px; font-size:27px; font-variant-numeric:tabular-nums }
.soft-principles {
  display:grid; grid-template-columns:.7fr 1.3fr; gap:64px; margin-top:88px; padding-top:14px; align-items:start;
}
.soft-principles h2 { margin-bottom:0; font:600 34px/1.25 "Songti SC","STSong",serif }
.principle-list { display:grid; gap:14px }
.principle {
  display:grid; grid-template-columns:42px 1fr; gap:16px; align-items:start;
  padding:18px 20px; border-radius:20px; background:var(--base); box-shadow:var(--raised-small);
}
.principle b {
  width:42px; height:42px; display:grid; place-items:center; border-radius:14px;
  color:var(--accent); box-shadow:var(--pressed); font-size:13px;
}
.principle strong { display:block; margin-bottom:3px; font-size:15px }
.principle p { margin:0; color:var(--muted); font-size:13px }
.soft-footer {
  width:min(1120px,calc(100% - 48px)); margin:0 auto; padding:28px 0 42px;
  display:flex; justify-content:space-between; gap:20px; color:var(--muted); font-size:12px;
}
@media(max-width:860px) {
  .soft-header { align-items:flex-start; flex-wrap:wrap }
  .soft-nav { width:100%; display:grid; grid-template-columns:repeat(4,minmax(0,1fr)) }
  .soft-nav a { min-width:0; padding:0 6px }
  .soft-main { padding-top:62px }
  .soft-hero { grid-template-columns:1fr; gap:52px }
  .research-console { max-width:560px }
}
@media(max-width:560px) {
  .soft-header { width:calc(100% - 28px); margin-top:14px; padding:12px }
  .soft-brand { padding-left:4px }
  .soft-brand img { height:32px }
  .soft-nav { gap:6px }
  .soft-nav a { min-height:38px; font-size:10px }
  .soft-main { width:calc(100% - 32px); padding:52px 0 72px }
  h1 { font-size:45px }
  .soft-lede { font-size:15px }
  .soft-actions { display:grid }
  .soft-button { width:100% }
  .research-console { padding:14px; border-radius:28px }
  .paper-well { min-height:330px; padding:18px }
  .paper-sheet { min-height:285px; padding:24px 20px }
  .translation-bubble { right:-8px; width:82% }
  .soft-metrics { grid-template-columns:1fr; margin-top:64px }
  .soft-principles { grid-template-columns:1fr; gap:26px; margin-top:64px }
  .soft-footer { width:calc(100% - 32px); flex-direction:column }
}
@media(prefers-reduced-motion:reduce) {
  html { scroll-behavior:auto }
  *,*::before,*::after { transition-duration:.01ms !important }
}
"""


def soft_ui_preview_document() -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="color-scheme" content="light">
  <link rel="icon" href="data:,">
  <title>陪你读 Soft UI 探索版</title>
  <style>{SOFT_UI_CSS}</style>
</head>
<body>
<div class="soft-shell">
  <header class="soft-header">
    <a class="soft-brand" href="/" aria-label="返回当前正式主页">
      <strong>陪你<em>读</em></strong>
      <img src="/api/portal/mascot.png" alt="" aria-hidden="true">
    </a>
    <nav class="soft-nav" aria-label="探索版导航">
      <a href="#soft-demo">产品体验</a>
      <a href="#soft-principles">本地使用</a>
      <a href="/privacy">隐私说明</a>
      <a class="is-accent" href="https://github.com/xiaoyu-ops/Read_with_you" target="_blank" rel="noopener noreferrer">GitHub</a>
    </nav>
  </header>

  <main class="soft-main">
    <section class="soft-hero" aria-labelledby="soft-title">
      <div>
        <p class="soft-eyebrow">Soft research workspace</p>
        <h1 id="soft-title">读好论文，<br>沉淀基础。</h1>
        <p class="soft-lede">从原始 PDF 出发，划选翻译、保存判断，再让 Pet 沿着论文证据核对方法与复现条件。</p>
        <div class="soft-actions">
          <a class="soft-button is-primary" href="http://127.0.0.1:8520" target="_blank" rel="noopener noreferrer">打开本地工作台</a>
          <a class="soft-button" href="#soft-demo">先看看怎么用</a>
        </div>
        <p class="soft-note">这是独立的视觉探索页。<a href="/">返回当前主页</a></p>
      </div>

      <div id="soft-demo" class="research-console" aria-label="论文精读工作台示意">
        <div class="console-top">
          <strong>Attention Is All You Need</strong>
          <span class="console-status">本地 Core 已连接</span>
        </div>
        <div class="paper-well">
          <article class="paper-sheet">
            <p class="paper-kicker">ARXIV 1706.03762 · PAGE 1</p>
            <h2>Attention Is All You Need</h2>
            <p class="paper-meta">Vaswani et al. · 2017</p>
            <div class="paper-line medium"></div>
            <div class="paper-line"></div>
            <p class="selection">We propose a new simple network architecture, the Transformer, based solely on attention mechanisms.</p>
            <div class="paper-line"></div>
            <div class="paper-line short"></div>
            <aside class="translation-bubble">
              <span>当前选区 · 已翻译</span>
              我们提出一种新的简洁网络架构 Transformer，它完全基于注意力机制。
            </aside>
          </article>
        </div>
        <div class="console-tools" aria-label="工作台操作示意">
          <button class="tool" type="button" aria-pressed="true">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 4h9M9 4v3c0 4-2 7-5 9M7 12c1 2 3 4 6 5M14 10h5M16.5 10l-4 10M16.5 10l4 10M14 17h5"/></svg>
            划选翻译
          </button>
          <button class="tool" type="button">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 3h12v18l-6-4-6 4z"/></svg>
            保存笔记
          </button>
          <button class="tool" type="button">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 5h16v11H8l-4 4zM8 9h8M8 12h5"/></svg>
            问 Pet
          </button>
        </div>
      </div>
    </section>

    <section class="soft-metrics" aria-label="匿名使用概况">
      <div class="metric"><span>累计访问</span><strong id="soft-visits">读取中</strong></div>
      <div class="metric"><span>Core 启动</span><strong id="soft-starts">读取中</strong></div>
      <div class="metric"><span>开始阅读</span><strong id="soft-reads">读取中</strong></div>
    </section>

    <section id="soft-principles" class="soft-principles" aria-labelledby="soft-principles-title">
      <h2 id="soft-principles-title">柔和界面，<br>清晰边界。</h2>
      <div class="principle-list">
        <article class="principle"><b>PDF</b><div><strong>始终核对原始页面</strong><p>翻译、笔记和 Agent 结论都回到论文证据。</p></div></article>
        <article class="principle"><b>01</b><div><strong>研究资料留在本机</strong><p>论文、笔记、问题与回答不成为公网账号资产。</p></div></article>
        <article class="principle"><b>Key</b><div><strong>模型服务由你选择</strong><p>本地 Core 使用自己的配置，公网入口看不到 Key。</p></div></article>
      </div>
    </section>
  </main>

  <footer class="soft-footer">
    <span>陪你读 · Soft UI 独立探索版</span>
    <a href="/">返回当前正式主页</a>
  </footer>
</div>
<script>
(() => {{
  fetch("/api/portal/stats", {{cache:"no-store"}})
    .then((response) => response.ok ? response.json() : Promise.reject())
    .then((data) => {{
      document.getElementById("soft-visits").textContent = Number(data.total_portal_visits || 0).toLocaleString("zh-CN");
      document.getElementById("soft-starts").textContent = Number(data.total_core_starts || 0).toLocaleString("zh-CN");
      document.getElementById("soft-reads").textContent = Number(data.total_reader_opens || 0).toLocaleString("zh-CN");
    }})
    .catch(() => {{
      for (const id of ["soft-visits","soft-starts","soft-reads"]) document.getElementById(id).textContent = "暂不可用";
    }});
}})();
</script>
</body>
</html>"""
