"""Dependency-free public discovery UI for the public_portal runtime."""

from __future__ import annotations

import json


PORTAL_CSS = r"""
:root {
  color-scheme: light;
  --paper:oklch(97.4% .007 83); --surface:oklch(99.2% .004 83);
  --ink:oklch(25% .018 252); --muted:oklch(51% .018 252);
  --line:oklch(86% .012 252); --blue:oklch(43% .08 252);
  --blue-soft:oklch(93% .025 252); --amber:oklch(55% .11 74);
  --focus:oklch(58% .14 252); --danger:oklch(52% .15 25);
}
@media(prefers-color-scheme:dark) {
  :root {
    color-scheme:dark; --paper:oklch(20% .012 252); --surface:oklch(24% .014 252);
    --ink:oklch(91% .009 83); --muted:oklch(70% .014 252);
    --line:oklch(36% .018 252); --blue:oklch(72% .07 252);
    --blue-soft:oklch(29% .032 252); --amber:oklch(72% .1 74);
    --focus:oklch(76% .1 252); --danger:oklch(72% .13 25);
  }
}
* { box-sizing:border-box }
html { min-width:320px; background:var(--paper) }
body {
  margin:0; background:var(--paper); color:var(--ink);
  font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif;
  font-size:16px; line-height:1.65;
}
a { color:inherit }
button,input { font:inherit }
button { color:inherit }
a:focus-visible,button:focus-visible,input:focus-visible {
  outline:3px solid var(--focus); outline-offset:3px;
}
.skip {
  position:absolute; left:1rem; top:-5rem; padding:.65rem 1rem;
  background:var(--surface); border:1px solid var(--line); z-index:20;
}
.skip:focus { top:1rem }
.site-header {
  min-height:70px; display:flex; align-items:center; justify-content:space-between;
  max-width:1240px; margin:auto; padding:0 28px; border-bottom:1px solid var(--line);
}
.brand {
  display:inline-flex; align-items:center; gap:9px; text-decoration:none;
  font-family:ui-serif,"Songti SC",serif; font-size:22px; letter-spacing:.08em;
}
.brand em { color:var(--amber); font-style:normal }
.brand-mascot {
  width:auto; height:35px; object-fit:contain; transform-origin:50% 100%;
  animation:brand-mascot-breathe 2.8s cubic-bezier(.22,1,.36,1) infinite;
}
@keyframes brand-mascot-breathe {
  0%,100% { transform:translateY(1px) rotate(-1deg) }
  50% { transform:translateY(-2px) rotate(1deg) }
}
.site-nav { display:flex; align-items:center; gap:24px; font-size:14px }
.site-nav a { text-underline-offset:4px }
.site-footer {
  max-width:1240px; margin:auto; padding:28px; color:var(--muted);
  font-size:13px; border-top:1px solid var(--line);
}
.portal-main { max-width:1240px; margin:auto; padding:72px 28px 88px }
.hero { max-width:920px }
.home-hero {
  display:grid; grid-template-columns:minmax(0,1.22fr) minmax(280px,.78fr);
  gap:72px; align-items:start;
}
.home-hero h1 { max-width:780px }
.home-actions { display:flex; flex-wrap:wrap; gap:12px; margin-top:34px }
.core-summary {
  padding:24px 26px 28px; border:1px solid var(--line); background:var(--surface);
}
.core-summary-label {
  margin:0 0 14px; color:var(--blue);
  font:650 12px/1.4 ui-monospace,monospace; letter-spacing:.08em;
}
.core-summary strong {
  display:block; margin-bottom:20px; font-family:ui-serif,"Songti SC",serif;
  font-size:25px; font-weight:600;
}
.core-summary dl { margin:0 }
.core-summary dl div {
  display:grid; grid-template-columns:90px minmax(0,1fr); gap:14px;
  padding:12px 0; border-top:1px solid var(--line);
}
.core-summary dt { color:var(--muted) }
.core-summary dd { margin:0; text-align:right }
.core-summary[data-state="ready"] .core-summary-label { color:oklch(52% .11 150) }
.core-summary[data-state="missing"] .core-summary-label { color:var(--amber) }
.core-entry {
  display:grid; grid-template-columns:minmax(180px,.45fr) minmax(0,1.55fr);
  gap:72px; margin-top:72px; padding:32px 0 36px;
  border-top:1px solid var(--line); border-bottom:1px solid var(--line);
}
.core-entry h2 { margin-bottom:10px; font-size:24px }
.core-entry .actions { display:flex; flex-wrap:wrap; gap:12px; margin-top:22px }
.core-state {
  display:flex; align-items:center; gap:10px; margin:3px 0 0;
  color:var(--muted); font:650 13px/1.4 ui-monospace,monospace; letter-spacing:.04em;
}
.core-state-dot {
  width:9px; height:9px; border-radius:50%; background:var(--amber);
  box-shadow:0 0 0 4px var(--blue-soft);
}
.core-state.is-ready .core-state-dot { background:oklch(58% .12 150) }
.home-sections {
  display:grid; grid-template-columns:repeat(3,1fr); margin-top:64px;
  border-top:1px solid var(--line); border-bottom:1px solid var(--line);
}
.home-sections section { padding:26px 26px 30px 0 }
.home-sections section+section { padding-left:26px; border-left:1px solid var(--line) }
.home-sections h2 { margin-bottom:9px; font-size:18px }
.home-sections p { margin:0; color:var(--muted); font-size:14px }
.eyebrow {
  margin:0 0 16px; color:var(--amber); font:650 12px/1.2 ui-monospace,monospace;
  letter-spacing:.16em; text-transform:uppercase;
}
h1,h2,h3,p { margin-top:0 }
h1 {
  margin-bottom:0; font-family:ui-serif,"Songti SC",serif;
  font-size:clamp(44px,6.5vw,78px); font-weight:520; line-height:1.07; letter-spacing:-.04em;
}
.lede { max-width:720px; margin:24px 0 0; color:var(--muted); font-size:18px }
.task-tabs {
  display:flex; gap:26px; margin-top:44px; border-bottom:1px solid var(--line);
}
.task-tabs button {
  position:relative; min-height:48px; padding:0 0 11px; border:0; background:transparent;
  color:var(--muted); cursor:pointer; font-weight:650;
}
.task-tabs button[aria-selected="true"] { color:var(--ink) }
.task-tabs button[aria-selected="true"]::after {
  content:""; position:absolute; left:0; right:0; bottom:-1px; height:2px; background:var(--blue);
}
.search-form {
  display:grid; grid-template-columns:minmax(0,1fr) auto; margin-top:22px;
  border:1px solid var(--ink); background:var(--surface); border-radius:8px; overflow:hidden;
}
.search-form input {
  width:100%; min-height:58px; border:0; padding:0 18px; color:var(--ink);
  background:transparent; outline:0;
}
.search-form button,.button {
  min-height:46px; display:inline-flex; align-items:center; justify-content:center;
  border:1px solid var(--ink); border-radius:7px; padding:0 18px;
  background:transparent; color:var(--ink); text-decoration:none; font-weight:650; cursor:pointer;
}
.search-form button {
  min-height:58px; border:0; border-left:1px solid var(--ink); border-radius:0;
  background:var(--ink); color:var(--surface); padding:0 24px;
}
.button.primary { background:var(--ink); color:var(--surface) }
.button.secondary { border-color:var(--line) }
.button[aria-disabled="true"] { opacity:.5; pointer-events:none }
.search-hint,.status,.usage-line { color:var(--muted); font-size:13px }
.search-hint { margin:12px 0 0 }
.status { min-height:24px; margin:20px 0 0 }
.status.error { color:var(--danger) }
.results { margin-top:8px; border-top:1px solid var(--line) }
.result {
  display:grid; grid-template-columns:minmax(0,1fr) auto; gap:28px;
  padding:24px 0; border-bottom:1px solid var(--line);
}
.result h2 { margin:0; font-family:ui-serif,"Songti SC",serif; font-size:23px; line-height:1.28 }
.result-meta { margin:8px 0 0; color:var(--muted); font-size:14px }
.result-abstract {
  max-width:76ch; margin:13px 0 0; color:var(--muted); font-size:14px;
  display:-webkit-box; -webkit-line-clamp:3; -webkit-box-orient:vertical; overflow:hidden;
}
.result-actions { display:flex; flex-direction:column; gap:9px; align-items:stretch; min-width:136px }
.result-actions .button { font-size:14px; white-space:nowrap }
.entry-boundary {
  display:grid; grid-template-columns:minmax(180px,.42fr) minmax(0,1.58fr);
  gap:68px; margin-top:76px; padding:32px 0; border-top:1px solid var(--line);
  border-bottom:1px solid var(--line);
}
.entry-boundary h2 { margin-bottom:9px; font-size:22px }
.entry-boundary p { max-width:720px; color:var(--muted) }
.entry-boundary .actions { display:flex; flex-wrap:wrap; gap:12px; margin-top:20px }
.boundary-label {
  color:var(--blue); font:650 12px/1.5 ui-monospace,monospace; letter-spacing:.08em;
}
.plain-sections {
  display:grid; grid-template-columns:repeat(3,1fr); margin-top:64px;
  border-top:1px solid var(--line); border-bottom:1px solid var(--line);
}
.plain-sections section { padding:26px 26px 30px 0 }
.plain-sections section+section { padding-left:26px; border-left:1px solid var(--line) }
.plain-sections h2 { margin-bottom:9px; font-size:18px }
.plain-sections p { margin:0; color:var(--muted); font-size:14px }
.usage-line { margin:25px 0 0 }
.prose { max-width:790px }
.prose h1 { font-size:clamp(38px,5vw,60px); margin-bottom:34px }
.prose h2 { margin:38px 0 9px; font-size:20px }
.prose p,.prose li { color:var(--muted) }
.prose ul { padding-left:1.2rem }

.map-main { max-width:1560px; margin:auto; padding:22px 24px 48px }
.map-toolbar {
  display:flex; gap:20px; align-items:center; min-height:62px;
  border-bottom:1px solid var(--line); overflow-x:auto;
}
.map-title { min-width:max-content; margin-right:auto }
.map-title strong { display:block; font-family:ui-serif,"Songti SC",serif; font-size:19px }
.map-title span { color:var(--muted); font-size:12px }
.map-tabs,.relation-tabs,.mobile-tabs { display:flex; gap:4px }
.map-tabs button,.relation-tabs button,.mobile-tabs button {
  min-height:38px; padding:0 12px; border:0; border-radius:6px;
  background:transparent; color:var(--muted); cursor:pointer; white-space:nowrap;
}
.map-tabs button[aria-pressed="true"],.relation-tabs button[aria-pressed="true"],
.mobile-tabs button[aria-pressed="true"] {
  background:var(--blue-soft); color:var(--ink);
}
.map-warning { padding:10px 0; color:var(--amber); font-size:13px }
.filter-panel {
  display:none; grid-template-columns:2fr repeat(2,1fr) auto; gap:12px;
  padding:16px 0; border-bottom:1px solid var(--line);
}
.filter-panel.is-open { display:grid }
.filter-panel label { display:grid; gap:5px; color:var(--muted); font-size:12px }
.filter-panel input[type="text"],.filter-panel input[type="number"] {
  min-height:40px; min-width:0; border:1px solid var(--line); border-radius:6px;
  padding:0 10px; background:var(--surface); color:var(--ink);
}
.filter-checks { display:flex; align-items:end; gap:12px; padding-bottom:8px }
.filter-checks label { display:flex; align-items:center; gap:6px; white-space:nowrap }
.map-grid {
  display:grid; grid-template-columns:290px minmax(420px,1fr) 340px;
  min-height:720px; border-bottom:1px solid var(--line);
}
.map-list,.map-detail { min-width:0; padding:18px }
.map-list { border-right:1px solid var(--line); padding-left:0 }
.map-detail { border-left:1px solid var(--line); padding-right:0 }
.panel-heading { display:flex; justify-content:space-between; margin-bottom:12px; font-size:13px }
.panel-heading span { color:var(--muted) }
.paper-list { max-height:666px; overflow:auto }
.paper-row {
  width:100%; display:grid; grid-template-columns:44px minmax(0,1fr); gap:9px;
  padding:12px 8px; border:0; border-top:1px solid var(--line);
  background:transparent; text-align:left; cursor:pointer;
}
.paper-row[aria-pressed="true"] { background:var(--blue-soft) }
.paper-row .year { color:var(--blue); font:650 11px/1.5 ui-monospace,monospace }
.paper-row strong { display:block; font-size:13px; line-height:1.35 }
.paper-row small { display:block; margin-top:5px; color:var(--muted); font-size:11px }
.map-stage { position:relative; min-width:0; overflow:hidden; background:var(--surface) }
.stage-caption {
  position:absolute; z-index:2; top:12px; left:14px; right:14px;
  display:flex; justify-content:space-between; gap:16px; pointer-events:none;
  color:var(--muted); font-size:12px;
}
.graph-actions { position:absolute; z-index:3; right:14px; bottom:14px; display:flex; gap:6px }
.graph-actions button {
  width:38px; height:38px; border:1px solid var(--line); border-radius:6px;
  background:var(--surface); cursor:pointer;
}
#paper-graph { width:100%; height:720px; display:block; touch-action:none }
.graph-edge { stroke:var(--line); stroke-opacity:.7 }
.graph-edge.citation { stroke:var(--blue); stroke-opacity:.55 }
.graph-node { cursor:pointer }
.graph-node circle { stroke:var(--surface); stroke-width:2 }
.graph-node.is-selected circle { stroke:var(--ink); stroke-width:3 }
.graph-node text {
  fill:var(--ink); font:600 11px/1 ui-sans-serif,sans-serif;
  paint-order:stroke; stroke:var(--surface); stroke-width:4px; stroke-linejoin:round;
}
.detail-kicker { display:flex; justify-content:space-between; color:var(--blue); font-size:12px }
.map-detail h1 { margin:14px 0 9px; font-family:ui-serif,"Songti SC",serif; font-size:25px; line-height:1.25 }
.detail-authors,.detail-abstract { color:var(--muted); font-size:13px }
.detail-stats { display:grid; grid-template-columns:repeat(3,1fr); margin:19px 0 }
.detail-stats div { padding:10px 5px; border-top:1px solid var(--line); border-bottom:1px solid var(--line) }
.detail-stats dt { color:var(--muted); font-size:11px }
.detail-stats dd { margin:4px 0 0; font-weight:700; font-size:13px }
.detail-abstract { max-height:250px; overflow:auto }
.detail-actions { display:grid; gap:8px; margin-top:20px }
.detail-links { display:flex; flex-wrap:wrap; gap:8px 14px; margin-top:5px; font-size:13px }
.detail-links a { text-underline-offset:4px }
.table-view { height:720px; overflow:auto; padding:70px 24px 30px }
.table-view h2 { margin-bottom:5px; font-size:25px }
.table-view>p { color:var(--muted) }
.table-row {
  width:100%; display:grid; grid-template-columns:minmax(0,1fr) 72px 80px;
  gap:14px; padding:15px 0; border:0; border-top:1px solid var(--line);
  background:transparent; text-align:left; cursor:pointer;
}
.table-row strong { display:block }
.table-row span { color:var(--muted); font-size:12px }
.mobile-tabs { display:none }
.map-loading { display:grid; place-content:center; min-height:66vh; text-align:center }
.map-loading p { margin-bottom:4px; font-family:ui-serif,"Songti SC",serif; font-size:25px }
.map-loading span { color:var(--muted) }

@media(max-width:1279px) {
  .map-grid { grid-template-columns:minmax(420px,1fr) 330px }
  .map-list { display:none }
}
@media(max-width:760px) {
  .site-header { min-height:62px; padding:0 18px }
  .site-nav { gap:14px }
  .portal-main { padding:48px 18px 64px }
  .home-hero { grid-template-columns:1fr; gap:36px }
  .core-entry { grid-template-columns:1fr; gap:18px; margin-top:52px }
  .home-sections { grid-template-columns:1fr }
  .home-sections section,.home-sections section+section {
    padding:22px 0; border-left:0; border-top:1px solid var(--line);
  }
  .home-sections section:first-child { border-top:0 }
  h1 { font-size:clamp(40px,13vw,58px) }
  .search-form { grid-template-columns:1fr }
  .search-form button { border-left:0; border-top:1px solid var(--ink) }
  .result { grid-template-columns:1fr; gap:18px }
  .result-actions { flex-direction:row; flex-wrap:wrap }
  .entry-boundary { grid-template-columns:1fr; gap:12px; margin-top:56px }
  .plain-sections { grid-template-columns:1fr }
  .plain-sections section,.plain-sections section+section {
    padding:22px 0; border-left:0; border-top:1px solid var(--line);
  }
  .plain-sections section:first-child { border-top:0 }
  .site-footer { padding:24px 18px 38px }
  .map-main { padding:10px 12px 32px }
  .map-toolbar { flex-wrap:wrap; gap:8px; padding-bottom:10px; overflow:visible }
  .map-title { width:100% }
  .map-tabs { overflow-x:auto; width:100% }
  .relation-tabs { width:100% }
  .filter-panel,.filter-panel.is-open { grid-template-columns:1fr }
  .filter-checks { align-items:start; flex-wrap:wrap }
  .mobile-tabs { display:flex; margin:10px 0 }
  .map-grid { display:block; min-height:620px }
  .map-list,.map-stage,.map-detail { display:none; border:0; padding:0 }
  .map-list.is-mobile-active,.map-stage.is-mobile-active,.map-detail.is-mobile-active { display:block }
  .paper-list { max-height:none }
  #paper-graph,.table-view { height:620px }
  .map-detail { padding:18px 2px }
  .stage-caption { top:8px; font-size:11px }
}
@media(prefers-reduced-motion:reduce) {
  *,*::before,*::after {
    scroll-behavior:auto!important; transition:none!important; animation:none!important
  }
}
"""


ANALYTICS_SCRIPT = r"""
<script>
(() => {
  function randomDailyId() {
    const bytes = new Uint8Array(32);
    crypto.getRandomValues(bytes);
    return Array.from(bytes, value => value.toString(16).padStart(2, "0")).join("");
  }
  window.peiniduRecordUsage = async function(event) {
    try {
      const date = new Date().toISOString().slice(0, 10);
      const key = "peinidu.public.daily-usage";
      let state = null;
      try { state = JSON.parse(localStorage.getItem(key) || "null"); } catch {}
      if (!state || state.date !== date || !/^[0-9a-f]{64}$/.test(state.id || "")) {
        state = { date, id: randomDailyId() };
        localStorage.setItem(key, JSON.stringify(state));
      }
      await fetch("/api/portal/telemetry", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          event_date: date,
          daily_id: state.id,
          event,
          platform: "web",
          app_version: "portal"
        })
      });
    } catch {}
  };
})();
</script>
"""


CORE_STATUS_SCRIPT = r"""
<script>
(() => {
  const status = document.getElementById("core-state");
  const summary = document.getElementById("core-summary");
  const summaryLabel = document.getElementById("core-summary-label");
  const summaryTitle = document.getElementById("core-summary-title");
  const open = document.getElementById("open-core");
  const install = document.getElementById("install-core");
  if (!status || !summary || !summaryLabel || !summaryTitle || !open || !install) return;

  const showMissing = () => {
    status.classList.remove("is-ready");
    status.querySelector("span:last-child").textContent = "未检测到本地 Core";
    summary.dataset.state = "missing";
    summaryLabel.textContent = "需要先安装或启动";
    summaryTitle.textContent = "从 GitHub 开始";
    open.hidden = true;
    install.classList.add("primary");
  };
  const showReady = () => {
    status.classList.add("is-ready");
    status.querySelector("span:last-child").textContent = "已检测到本地 Core";
    summary.dataset.state = "ready";
    summaryLabel.textContent = "本地 Core 已就绪";
    summaryTitle.textContent = "可以继续阅读";
    open.hidden = false;
    install.classList.remove("primary");
  };

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 1800);
  fetch("http://127.0.0.1:8520/portal-probe", {
    cache:"no-store", mode:"cors", signal:controller.signal
  })
    .then(response => {
      if (!response.ok) throw new Error("core_unavailable");
      return response.json();
    })
    .then(data => data && data.status === "ok" ? showReady() : showMissing())
    .catch(showMissing)
    .finally(() => clearTimeout(timer));
})();
</script>
"""


HOME_SCRIPT = r"""
<script>
(() => {
  const form = document.getElementById("paper-search");
  const input = document.getElementById("paper-query");
  const submit = document.getElementById("paper-submit");
  const status = document.getElementById("search-status");
  const results = document.getElementById("paper-results");
  const taskButtons = Array.from(document.querySelectorAll("[data-task]"));
  let task = new URLSearchParams(location.search).get("task") === "map" ? "map" : "read";

  function safeUrl(value) {
    try {
      const parsed = new URL(value);
      return parsed.protocol === "https:" ? parsed.href : null;
    } catch { return null; }
  }
  function paperRef(candidate) {
    if (/^[0-9a-f]{40}$/i.test(candidate.paper_id || "")) return candidate.paper_id;
    if (candidate.arxiv_id) return `ARXIV:${candidate.arxiv_id}`;
    return null;
  }
  function setTask(next, updateUrl = true) {
    task = next;
    taskButtons.forEach(button => {
      button.setAttribute("aria-selected", String(button.dataset.task === task));
    });
    submit.textContent = task === "map" ? "查找并看关系" : "查找论文";
    input.placeholder = task === "map"
      ? "输入一篇论文标题、arXiv ID 或 DOI"
      : "输入论文标题、arXiv ID 或 DOI";
    if (updateUrl) {
      const url = new URL(location.href);
      url.searchParams.set("task", task);
      history.replaceState({}, "", url);
    }
  }
  function buttonLink(label, href, primary = false) {
    const link = document.createElement("a");
    link.className = `button ${primary ? "primary" : "secondary"}`;
    link.textContent = label;
    link.href = href;
    if (href.startsWith("https://")) {
      link.target = "_blank";
      link.rel = "noopener noreferrer";
    }
    return link;
  }
  function renderCandidate(candidate) {
    const article = document.createElement("article");
    article.className = "result";
    const content = document.createElement("div");
    const title = document.createElement("h2");
    title.textContent = candidate.title || "未命名论文";
    const meta = document.createElement("p");
    meta.className = "result-meta";
    const authors = Array.isArray(candidate.authors) ? candidate.authors.slice(0, 3).join(", ") : "";
    meta.textContent = [authors, candidate.year, candidate.venue, candidate.citation_count == null ? "" : `引用 ${candidate.citation_count}`]
      .filter(Boolean).join(" · ");
    const abstract = document.createElement("p");
    abstract.className = "result-abstract";
    abstract.textContent = candidate.abstract || "当前来源未提供摘要。";
    content.append(title, meta, abstract);

    const actions = document.createElement("div");
    actions.className = "result-actions";
    const ref = paperRef(candidate);
    const source = safeUrl(candidate.pdf_url) || safeUrl(candidate.url);
    const mapLink = ref ? `/literature-map/${encodeURIComponent(ref)}` : null;
    if (task === "map" && mapLink) actions.append(buttonLink("查看图谱", mapLink, true));
    if (source) actions.append(buttonLink("打开原文 ↗", source, task === "read"));
    if (task !== "map" && mapLink) actions.append(buttonLink("查看图谱", mapLink));
    if (!source && !mapLink) {
      const unavailable = document.createElement("span");
      unavailable.className = "status";
      unavailable.textContent = "暂无可打开来源";
      actions.append(unavailable);
    }
    article.append(content, actions);
    return article;
  }
  async function runSearch(query) {
    submit.disabled = true;
    status.className = "status";
    status.textContent = "正在同时检索 arXiv 与 Semantic Scholar…";
    results.replaceChildren();
    window.peiniduRecordUsage("search_submitted");
    try {
      const response = await fetch("/api/portal/search", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query })
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "检索暂时不可用");
      if (!data.candidates.length) {
        status.textContent = "没有找到候选。可以换成完整英文标题、arXiv ID 或 DOI。";
        return;
      }
      status.textContent = `找到 ${data.candidates.length} 个候选，请确认论文后再继续。`;
      data.candidates.forEach(candidate => results.append(renderCandidate(candidate)));
      const url = new URL(location.href);
      url.searchParams.set("q", query);
      history.replaceState({}, "", url);
    } catch (error) {
      status.className = "status error";
      status.textContent = error instanceof Error ? error.message : "检索暂时不可用";
    } finally {
      submit.disabled = false;
    }
  }
  taskButtons.forEach(button => button.addEventListener("click", () => setTask(button.dataset.task)));
  form.addEventListener("submit", event => {
    event.preventDefault();
    const query = input.value.trim();
    if (query) runSearch(query);
  });
  setTask(task, false);
  const initialQuery = new URLSearchParams(location.search).get("q");
  if (initialQuery) {
    input.value = initialQuery;
    runSearch(initialQuery);
  }
  window.peiniduRecordUsage("portal_visited").then(() => {
    fetch("/api/portal/stats").then(response => response.json()).then(data => {
      const usage = document.getElementById("usage-stats");
      usage.textContent = `今日匿名使用 ${data.active_today} · 打开图谱 ${data.maps_today} · 本地阅读 ${data.readers_today}`;
    }).catch(() => {});
  });
})();
</script>
"""


MAP_SCRIPT_TEMPLATE = r"""
<script>
(() => {
  const paperRef = __PAPER_REF__;
  const state = {
    data:null, selectedId:"", relation:"similarity", view:"graph", mobile:"graph",
    filters:{ keyword:"", from:"", to:"", pdf:false, oa:false },
    viewport:{ x:0, y:0, w:1000, h:720 }
  };
  const $ = selector => document.querySelector(selector);
  const list = $("#map-paper-list");
  const detail = $("#map-detail");
  const stage = $("#map-stage");
  const count = $("#map-count");
  const warning = $("#map-warning");
  const filterPanel = $("#map-filters");
  const ns = "http://www.w3.org/2000/svg";

  function safeUrl(value) {
    try {
      const parsed = new URL(value);
      return parsed.protocol === "https:" ? parsed.href : null;
    } catch { return null; }
  }
  function shortCount(value) {
    if (value == null) return "—";
    if (value >= 10000) return `${(value / 1000).toFixed(0)}k`;
    if (value >= 1000) return `${(value / 1000).toFixed(1)}k`;
    return String(value);
  }
  function matches(paper) {
    const query = state.filters.keyword.trim().toLocaleLowerCase();
    const haystack = [paper.title, ...(paper.authors || []), paper.venue || ""].join(" ").toLocaleLowerCase();
    if (query && !haystack.includes(query)) return false;
    if (state.filters.from && (!paper.year || paper.year < Number(state.filters.from))) return false;
    if (state.filters.to && (!paper.year || paper.year > Number(state.filters.to))) return false;
    if (state.filters.pdf && !paper.pdf_url && !paper.arxiv_id) return false;
    if (state.filters.oa && !paper.is_open_access) return false;
    return true;
  }
  function visibleNodes() {
    return state.data.nodes.filter(paper => paper.id === state.data.origin.id || matches(paper));
  }
  function setMobile(panel) {
    state.mobile = panel;
    document.querySelectorAll("[data-mobile]").forEach(button => {
      button.setAttribute("aria-pressed", String(button.dataset.mobile === panel));
    });
    const panels = { list:"map-list", graph:"map-stage", detail:"map-detail" };
    Object.entries(panels).forEach(([name,id]) => {
      document.getElementById(id).classList.toggle("is-mobile-active", name === panel);
    });
  }
  function select(id) {
    state.selectedId = id;
    renderList();
    renderDetail();
    if (state.view === "graph") renderGraph();
    if (innerWidth <= 760) setMobile("detail");
  }
  function renderList() {
    const nodes = visibleNodes();
    count.textContent = `${nodes.length}/${state.data.nodes.length}`;
    list.replaceChildren();
    nodes.forEach(paper => {
      const button = document.createElement("button");
      button.className = "paper-row";
      button.type = "button";
      button.setAttribute("aria-pressed", String(paper.id === state.selectedId));
      const year = document.createElement("span");
      year.className = "year";
      year.textContent = paper.role === "origin" ? "核心" : (paper.year || "—");
      const copy = document.createElement("span");
      const title = document.createElement("strong");
      title.textContent = paper.title;
      const meta = document.createElement("small");
      meta.textContent = `${paper.authors?.[0] || "未知作者"} · 引用 ${shortCount(paper.citation_count)}`;
      copy.append(title, meta);
      button.append(year, copy);
      button.addEventListener("click", () => select(paper.id));
      list.append(button);
    });
  }
  function link(label, href, className = "") {
    const anchor = document.createElement("a");
    anchor.textContent = label;
    anchor.href = href;
    anchor.className = className;
    if (href.startsWith("https://")) {
      anchor.target = "_blank";
      anchor.rel = "noopener noreferrer";
    }
    return anchor;
  }
  function renderDetail() {
    const paper = state.data.nodes.find(item => item.id === state.selectedId) || state.data.origin;
    detail.replaceChildren();
    const kicker = document.createElement("div");
    kicker.className = "detail-kicker";
    const role = document.createElement("span");
    role.textContent = paper.role === "origin" ? "核心论文" : "当前论文";
    const year = document.createElement("span");
    year.textContent = paper.year || "年份未知";
    kicker.append(role, year);
    const title = document.createElement("h1");
    title.textContent = paper.title;
    const authors = document.createElement("p");
    authors.className = "detail-authors";
    authors.textContent = paper.authors?.join(", ") || "作者未知";
    const stats = document.createElement("dl");
    stats.className = "detail-stats";
    [["引用",shortCount(paper.citation_count)],["参考",shortCount(paper.reference_count)],["来源",paper.venue || "—"]]
      .forEach(([name,value]) => {
        const item = document.createElement("div");
        const dt = document.createElement("dt"); dt.textContent = name;
        const dd = document.createElement("dd"); dd.textContent = value;
        item.append(dt,dd); stats.append(item);
      });
    const abstract = document.createElement("p");
    abstract.className = "detail-abstract";
    abstract.textContent = paper.abstract || "Semantic Scholar 暂未提供摘要。";
    const actions = document.createElement("div");
    actions.className = "detail-actions";
    actions.append(link("以此论文为中心展开", `/literature-map/${encodeURIComponent(paper.id)}`, "button primary"));
    const links = document.createElement("div");
    links.className = "detail-links";
    const sourceLinks = [
      ["PDF ↗", safeUrl(paper.pdf_url)],
      ["Semantic Scholar ↗", safeUrl(paper.url)],
      ["arXiv ↗", paper.arxiv_id ? `https://arxiv.org/abs/${encodeURIComponent(paper.arxiv_id)}` : null],
      ["DOI ↗", paper.doi ? `https://doi.org/${encodeURIComponent(paper.doi)}` : null]
    ];
    sourceLinks.forEach(([label,href]) => { if (href) links.append(link(label, href)); });
    actions.append(links);
    if (paper.arxiv_id) {
      actions.append(link("去本地工作台精读", "http://127.0.0.1:8520/?task=read", "button secondary"));
    }
    detail.append(kicker,title,authors,stats,abstract,actions);
  }
  function positions(nodes) {
    const map = new Map([[state.data.origin.id, { x:500, y:360 }]]);
    nodes.filter(node => node.id !== state.data.origin.id).forEach((node,index) => {
      const angle = index * 2.399963;
      const radius = 84 + Math.sqrt(index + 1) * 48;
      map.set(node.id, { x:500 + Math.cos(angle) * radius, y:360 + Math.sin(angle) * radius * .78 });
    });
    return map;
  }
  function nodeLabel(paper) {
    const author = paper.authors?.[0] || "Unknown";
    const surname = author.trim().split(/\s+/).pop();
    return `${surname} ${paper.year || ""}`.trim();
  }
  function renderGraph() {
    stage.replaceChildren();
    const caption = document.createElement("div");
    caption.className = "stage-caption";
    const note = document.createElement("span");
    note.textContent = state.relation === "similarity"
      ? "相似关系来自 SPECTER2 或 Semantic Scholar 推荐"
      : "箭头从引用论文指向被引用论文";
    const edgeCount = document.createElement("strong");
    const nodes = visibleNodes();
    const ids = new Set(nodes.map(node => node.id));
    const edges = state.data.edges.filter(edge => edge.kind === state.relation && ids.has(edge.source) && ids.has(edge.target));
    edgeCount.textContent = `${edges.length} 条关系`;
    caption.append(note,edgeCount);
    const svg = document.createElementNS(ns,"svg");
    svg.id = "paper-graph";
    svg.setAttribute("viewBox", `${state.viewport.x} ${state.viewport.y} ${state.viewport.w} ${state.viewport.h}`);
    svg.setAttribute("aria-label", "论文关系图谱");
    const defs = document.createElementNS(ns,"defs");
    const marker = document.createElementNS(ns,"marker");
    marker.id = "citation-arrow"; marker.setAttribute("viewBox","0 0 10 10");
    marker.setAttribute("refX","9"); marker.setAttribute("refY","5");
    marker.setAttribute("markerWidth","5"); marker.setAttribute("markerHeight","5");
    marker.setAttribute("orient","auto-start-reverse");
    const path = document.createElementNS(ns,"path");
    path.setAttribute("d","M 0 0 L 10 5 L 0 10 z"); path.setAttribute("fill","var(--blue)");
    marker.append(path); defs.append(marker); svg.append(defs);
    const pos = positions(nodes);
    edges.forEach(edge => {
      const source = pos.get(edge.source), target = pos.get(edge.target);
      if (!source || !target) return;
      const line = document.createElementNS(ns,"line");
      line.setAttribute("x1",source.x); line.setAttribute("y1",source.y);
      line.setAttribute("x2",target.x); line.setAttribute("y2",target.y);
      line.setAttribute("class",`graph-edge ${edge.kind}`);
      line.setAttribute("stroke-width",String(1 + Math.min(2, edge.weight || 0)));
      if (edge.kind === "citation") line.setAttribute("marker-end","url(#citation-arrow)");
      svg.append(line);
    });
    const years = nodes.map(node => node.year).filter(Boolean);
    const minYear = Math.min(...years), maxYear = Math.max(...years);
    nodes.forEach(paper => {
      const point = pos.get(paper.id);
      const group = document.createElementNS(ns,"g");
      group.setAttribute("class",`graph-node ${paper.id === state.selectedId ? "is-selected" : ""}`);
      group.setAttribute("transform",`translate(${point.x} ${point.y})`);
      group.setAttribute("tabindex","0"); group.setAttribute("role","button");
      group.setAttribute("aria-label",paper.title);
      const circle = document.createElementNS(ns,"circle");
      const radius = paper.role === "origin" ? 24 : Math.max(9,Math.min(22,8 + Math.sqrt(Math.max(0,paper.citation_count || 0)) * .45));
      circle.setAttribute("r",String(radius));
      if (paper.role === "origin") circle.setAttribute("fill","var(--blue)");
      else {
        const ratio = maxYear === minYear ? .5 : ((paper.year || minYear) - minYear) / (maxYear - minYear);
        circle.setAttribute("fill",`oklch(${82 - ratio * 18}% ${.018 + ratio * .052} 252)`);
      }
      const text = document.createElementNS(ns,"text");
      text.setAttribute("x",String(radius + 5)); text.setAttribute("y","4");
      text.textContent = nodeLabel(paper);
      group.append(circle,text);
      group.addEventListener("click",() => select(paper.id));
      group.addEventListener("keydown",event => {
        if (event.key === "Enter" || event.key === " ") { event.preventDefault(); select(paper.id); }
      });
      svg.append(group);
    });
    let drag = null;
    svg.addEventListener("wheel",event => {
      event.preventDefault();
      const factor = event.deltaY > 0 ? 1.12 : .89;
      const nextW = Math.max(360,Math.min(1600,state.viewport.w * factor));
      const nextH = nextW * .72;
      state.viewport.x += (state.viewport.w - nextW) / 2;
      state.viewport.y += (state.viewport.h - nextH) / 2;
      state.viewport.w = nextW; state.viewport.h = nextH;
      svg.setAttribute("viewBox",`${state.viewport.x} ${state.viewport.y} ${state.viewport.w} ${state.viewport.h}`);
    },{ passive:false });
    svg.addEventListener("pointerdown",event => {
      drag = { x:event.clientX, y:event.clientY, vx:state.viewport.x, vy:state.viewport.y };
      svg.setPointerCapture(event.pointerId);
    });
    svg.addEventListener("pointermove",event => {
      if (!drag) return;
      state.viewport.x = drag.vx - (event.clientX - drag.x) * state.viewport.w / svg.clientWidth;
      state.viewport.y = drag.vy - (event.clientY - drag.y) * state.viewport.h / svg.clientHeight;
      svg.setAttribute("viewBox",`${state.viewport.x} ${state.viewport.y} ${state.viewport.w} ${state.viewport.h}`);
    });
    svg.addEventListener("pointerup",() => { drag = null; });
    const controls = document.createElement("div");
    controls.className = "graph-actions";
    [["−",1.15],["+",.86],["↺",0]].forEach(([label,factor]) => {
      const button = document.createElement("button"); button.type = "button"; button.textContent = label;
      button.setAttribute("aria-label",label === "↺" ? "重置图谱" : label === "+" ? "放大图谱" : "缩小图谱");
      button.addEventListener("click",() => {
        if (!factor) state.viewport = { x:0,y:0,w:1000,h:720 };
        else {
          const nextW = Math.max(360,Math.min(1600,state.viewport.w * factor));
          const nextH = nextW * .72;
          state.viewport.x += (state.viewport.w - nextW) / 2;
          state.viewport.y += (state.viewport.h - nextH) / 2;
          state.viewport.w = nextW; state.viewport.h = nextH;
        }
        svg.setAttribute("viewBox",`${state.viewport.x} ${state.viewport.y} ${state.viewport.w} ${state.viewport.h}`);
      });
      controls.append(button);
    });
    stage.append(caption,svg,controls);
  }
  function renderTable() {
    stage.replaceChildren();
    const wrap = document.createElement("div");
    wrap.className = "table-view";
    const title = document.createElement("h2");
    title.textContent = state.view === "prior" ? "先行工作" : state.view === "derivative" ? "后续工作" : "论文列表";
    const intro = document.createElement("p");
    intro.textContent = state.view === "prior"
      ? "被图中多篇论文共同引用的工作"
      : state.view === "derivative" ? "引用了图中多篇论文的工作" : "当前筛选范围内的论文";
    wrap.append(title,intro);
    const source = state.view === "prior" ? state.data.prior_works : state.view === "derivative" ? state.data.derivative_works : visibleNodes().map(paper => ({ paper }));
    source.filter(row => matches(row.paper)).forEach(row => {
      const paper = row.paper;
      const button = document.createElement("button");
      button.type = "button"; button.className = "table-row";
      const copy = document.createElement("span");
      const strong = document.createElement("strong"); strong.textContent = paper.title;
      const meta = document.createElement("span"); meta.textContent = paper.authors?.slice(0,2).join(", ") || "未知作者";
      copy.append(strong,meta);
      const year = document.createElement("span"); year.textContent = paper.year || "—";
      const citations = document.createElement("span"); citations.textContent = `引用 ${shortCount(paper.citation_count)}`;
      button.append(copy,year,citations);
      button.addEventListener("click",() => {
        if (state.data.nodes.some(node => node.id === paper.id)) select(paper.id);
        else location.href = `/literature-map/${encodeURIComponent(paper.id)}`;
      });
      wrap.append(button);
    });
    stage.append(wrap);
  }
  function renderStage() { state.view === "graph" ? renderGraph() : renderTable(); }
  function applyFilters() {
    state.filters = {
      keyword:$("#filter-keyword").value, from:$("#filter-from").value,
      to:$("#filter-to").value, pdf:$("#filter-pdf").checked, oa:$("#filter-oa").checked
    };
    renderList(); renderStage();
    if (!visibleNodes().some(node => node.id === state.selectedId)) select(state.data.origin.id);
  }
  document.querySelectorAll("[data-view]").forEach(button => button.addEventListener("click",() => {
    state.view = button.dataset.view;
    document.querySelectorAll("[data-view]").forEach(item => item.setAttribute("aria-pressed",String(item.dataset.view === state.view)));
    renderStage();
  }));
  document.querySelectorAll("[data-relation]").forEach(button => button.addEventListener("click",() => {
    state.relation = button.dataset.relation;
    document.querySelectorAll("[data-relation]").forEach(item => item.setAttribute("aria-pressed",String(item.dataset.relation === state.relation)));
    if (state.view === "graph") renderGraph();
  }));
  document.querySelectorAll("[data-mobile]").forEach(button => button.addEventListener("click",() => setMobile(button.dataset.mobile)));
  $("#filter-toggle").addEventListener("click",event => {
    const open = !filterPanel.classList.contains("is-open");
    filterPanel.classList.toggle("is-open",open);
    event.currentTarget.setAttribute("aria-expanded",String(open));
  });
  filterPanel.querySelectorAll("input").forEach(input => input.addEventListener("input",applyFilters));
  $("#filter-clear").addEventListener("click",() => {
    filterPanel.querySelectorAll("input").forEach(input => {
      if (input.type === "checkbox") input.checked = false; else input.value = "";
    });
    applyFilters();
  });
  fetch(`/api/portal/literature-map/${encodeURIComponent(paperRef)}`)
    .then(async response => {
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail?.message || data.detail || "论文图谱暂时不可用");
      return data;
    })
    .then(data => {
      state.data = data; state.selectedId = data.origin.id;
      $("#map-loading").remove();
      $("#map-shell").hidden = false;
      $("#map-subtitle").textContent = `${data.nodes.length} 篇 · Semantic Scholar`;
      if (data.status === "partial" || data.stale || data.warnings.length) {
        warning.textContent = `${data.stale ? "当前使用最近缓存。 " : ""}${data.warnings.join(" ")}`;
      }
      renderList(); renderStage(); renderDetail(); setMobile("graph");
      window.peiniduRecordUsage("map_opened");
    })
    .catch(error => {
      $("#map-loading").querySelector("p").textContent = "论文图谱暂时无法打开";
      $("#map-loading").querySelector("span").textContent = error instanceof Error ? error.message : "请稍后重试";
    });
})();
</script>
"""


def map_script(paper_ref: str) -> str:
    encoded = json.dumps(paper_ref, ensure_ascii=False).replace("<", "\\u003c")
    return MAP_SCRIPT_TEMPLATE.replace("__PAPER_REF__", encoded)
