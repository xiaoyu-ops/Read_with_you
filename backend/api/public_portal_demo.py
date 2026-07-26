"""Fixed, public-only product demo for the public portal homepage."""

from __future__ import annotations

import json
from pathlib import Path


DEMO_ASSET_DIR = Path(__file__).with_name("public_demo_assets")
DEMO_ASSETS = {
    "attention-p1-v1.webp": DEMO_ASSET_DIR / "attention-p1-v1.webp",
    "attention-p7-v1.webp": DEMO_ASSET_DIR / "attention-p7-v1.webp",
}

_DEMO_DATA = {
    "pages": {
        "1": "/api/portal/demo-assets/attention-p1-v1.webp",
        "7": "/api/portal/demo-assets/attention-p7-v1.webp",
    },
    "focus": {
        "sentence": {"x": 0.232, "y": 0.557, "w": 0.54, "h": 0.052},
        "training": {"x": 0.17, "y": 0.375, "w": 0.66, "h": 0.54},
        "dataset": {"x": 0.17, "y": 0.442, "w": 0.66, "h": 0.128},
        "hardware": {"x": 0.17, "y": 0.586, "w": 0.66, "h": 0.108},
        "hyperparameters": {"x": 0.17, "y": 0.7, "w": 0.66, "h": 0.164},
    },
}


def resolve_demo_asset(name: str) -> Path | None:
    path = DEMO_ASSETS.get(name)
    if path is None or not path.is_file():
        return None
    return path


def demo_markup() -> str:
    data = json.dumps(_DEMO_DATA, ensure_ascii=False).replace("</", "<\\/")
    return f"""
  <section id="product-demo" class="product-demo" aria-labelledby="demo-title">
    <div class="demo-intro">
      <div>
        <p class="demo-kicker">3 分钟公开演示</p>
        <h2 id="demo-title">先体验一次真正的论文精读。</h2>
        <p>从一句原文出发，保存方法判断，再让 Pet 回到论文里核对复现证据。</p>
      </div>
      <div class="demo-paper-meta">
        <strong>Attention Is All You Need</strong>
        <span>Vaswani et al. · arXiv:1706.03762</span>
        <a href="https://arxiv.org/abs/1706.03762" target="_blank" rel="noopener noreferrer">查看 arXiv 原文 ↗</a>
        <small>固定公开样例，译文、笔记与分析结果均为预生成内容。</small>
      </div>
    </div>

    <div class="demo-workspace" data-demo-state="idle">
      <div class="demo-paper-pane">
        <div class="demo-paper-toolbar">
          <span>原始 PDF</span>
          <span>第 <strong data-demo-page-number>1</strong> 页 / 15</span>
        </div>
        <div class="demo-page-viewport" data-demo-viewport>
          <div class="demo-page-sheet" data-demo-sheet>
            <img
              data-demo-page
              src="/api/portal/demo-assets/attention-p1-v1.webp"
              alt="Attention Is All You Need 第 1 页"
              width="918"
              height="1188"
            >
            <button
              class="demo-page-highlight"
              data-demo-highlight
              type="button"
              aria-label="选择摘要中关于 Transformer 的句子"
            ><span>点击这句原文</span></button>
          </div>
        </div>
        <p class="demo-page-caption" data-demo-page-caption>第 1 页 · Abstract</p>
      </div>

      <div class="demo-panel">
        <ol class="demo-progress" aria-label="演示进度">
          <li data-demo-progress="translate" aria-current="step"><span>01</span>划选翻译</li>
          <li data-demo-progress="note"><span>02</span>保存笔记</li>
          <li data-demo-progress="evidence"><span>03</span>Pet 核证</li>
        </ol>

        <div class="demo-step demo-step-start" data-demo-start>
          <p class="demo-step-label">第一步</p>
          <h3>点击左页摘要中的高亮句子</h3>
          <p>在真实产品里，选择来自 PDF TextLayer；这里用固定区域复现同一个动作。</p>
        </div>

        <div class="demo-translation" data-demo-translation hidden>
          <div>
            <span>原文</span>
            <p>We propose a new simple network architecture, the Transformer, based solely on attention mechanisms, dispensing with recurrence and convolutions entirely.</p>
          </div>
          <div lang="zh-CN">
            <span>译文</span>
            <p>我们提出了一种新的简单网络架构 Transformer，它完全基于注意力机制，摒弃了循环与卷积结构。</p>
          </div>
          <button class="button primary" data-demo-save type="button">保存为方法笔记</button>
        </div>

        <div class="demo-note" data-demo-note hidden>
          <div class="demo-note-heading">
            <span>已保存</span>
            <strong>方法</strong>
          </div>
          <pre>### 方法判断
Transformer 的核心变化不是在 RNN 上叠加注意力，
而是完全以注意力机制替代循环与卷积。</pre>
        </div>

        <div class="demo-pet-question" data-demo-question hidden>
          <img src="/api/portal/mascot.png" alt="" aria-hidden="true">
          <div>
            <span>继续让 Pet 核对</span>
            <button type="button" data-demo-ask>这篇论文的信息足够复现吗？</button>
          </div>
        </div>

        <div class="demo-evidence" data-demo-evidence hidden>
          <div class="demo-verdict">
            <span>Pet 的预生成判断</span>
            <strong>部分可复现</strong>
            <p>数据、硬件和关键训练超参数都有正文证据；官方代码仓库和完整运行环境没有在论文正文中给出。</p>
          </div>
          <div class="demo-evidence-list" aria-label="复现证据">
            <button type="button" data-demo-evidence-key="dataset" aria-pressed="true">
              <span>数据集</span><strong>§5.1</strong><small>WMT 2014、语料规模与 BPE 词表</small>
            </button>
            <button type="button" data-demo-evidence-key="hardware" aria-pressed="false">
              <span>硬件</span><strong>§5.2</strong><small>单机 8× NVIDIA P100 与训练时长</small>
            </button>
            <button type="button" data-demo-evidence-key="hyperparameters" aria-pressed="false">
              <span>超参数</span><strong>§5.3</strong><small>Adam、warmup 与学习率计划</small>
            </button>
            <button type="button" data-demo-evidence-key="code" aria-pressed="false">
              <span>代码</span><strong>未定位</strong><small>正文未提供官方代码仓库</small>
            </button>
          </div>
          <div class="demo-finish">
            <a class="button primary" href="https://github.com/xiaoyu-ops/Read_with_you" target="_blank" rel="noopener noreferrer">前往 GitHub 继续使用 ↗</a>
            <p>如果这个方向对你有帮助，欢迎 Star。</p>
            <button class="demo-reset" type="button" data-demo-reset>重新体验</button>
          </div>
        </div>

        <p class="demo-announcement" data-demo-announcement aria-live="polite">等待选择第 1 页摘要中的 Transformer 句子。</p>
      </div>
    </div>
  </section>
  <script>
  (() => {{
    const root = document.getElementById("product-demo");
    if (!root) return;
    const data = {data};
    const viewport = root.querySelector("[data-demo-viewport]");
    const sheet = root.querySelector("[data-demo-sheet]");
    const pageImage = root.querySelector("[data-demo-page]");
    const pageNumber = root.querySelector("[data-demo-page-number]");
    const pageCaption = root.querySelector("[data-demo-page-caption]");
    const highlight = root.querySelector("[data-demo-highlight]");
    const start = root.querySelector("[data-demo-start]");
    const translation = root.querySelector("[data-demo-translation]");
    const save = root.querySelector("[data-demo-save]");
    const note = root.querySelector("[data-demo-note]");
    const question = root.querySelector("[data-demo-question]");
    const ask = root.querySelector("[data-demo-ask]");
    const evidence = root.querySelector("[data-demo-evidence]");
    const reset = root.querySelector("[data-demo-reset]");
    const announcement = root.querySelector("[data-demo-announcement]");
    const evidenceButtons = Array.from(root.querySelectorAll("[data-demo-evidence-key]"));
    const progress = Array.from(root.querySelectorAll("[data-demo-progress]"));
    let state = {{ step:"idle", page:1, evidence:null }};

    function activeFocus() {{
      if (state.page === 1) return data.focus.sentence;
      return data.focus[state.evidence] || data.focus.training;
    }}

    function positionPage() {{
      const focus = activeFocus();
      const narrow = matchMedia("(max-width: 720px)").matches;
      sheet.style.width = narrow ? `${{Math.max(viewport.clientWidth * 1.86, 600)}}px` : "";
      sheet.style.transform = "";
      if (!narrow) return;
      const sheetWidth = sheet.offsetWidth;
      const sheetHeight = sheet.offsetHeight;
      const centerX = (focus.x + focus.w / 2) * sheetWidth;
      const centerY = (focus.y + focus.h / 2) * sheetHeight;
      const minX = Math.min(0, viewport.clientWidth - sheetWidth);
      const minY = Math.min(0, viewport.clientHeight - sheetHeight);
      const x = Math.max(minX, Math.min(0, viewport.clientWidth / 2 - centerX));
      const y = Math.max(minY, Math.min(0, viewport.clientHeight / 2 - centerY));
      sheet.style.transform = `translate(${{x}}px, ${{y}}px)`;
    }}

    function setHighlight(box, visible) {{
      highlight.hidden = !visible;
      if (!visible) return;
      highlight.style.left = `${{box.x * 100}}%`;
      highlight.style.top = `${{box.y * 100}}%`;
      highlight.style.width = `${{box.w * 100}}%`;
      highlight.style.height = `${{box.h * 100}}%`;
    }}

    function setProgress() {{
      const rank = {{ idle:0, translated:0, saved:1, evidence:2 }}[state.step];
      progress.forEach((item, index) => {{
        item.classList.toggle("is-complete", index < rank);
        item.classList.toggle("is-current", index === rank);
        if (index === rank) item.setAttribute("aria-current", "step");
        else item.removeAttribute("aria-current");
      }});
    }}

    function render() {{
      const onEvidence = state.step === "evidence";
      root.querySelector(".demo-workspace").dataset.demoState = state.step;
      start.hidden = state.step !== "idle";
      translation.hidden = state.step === "idle" || onEvidence;
      save.hidden = state.step !== "translated";
      note.hidden = state.step !== "saved";
      question.hidden = state.step !== "saved";
      evidence.hidden = !onEvidence;
      pageNumber.textContent = String(state.page);
      pageCaption.textContent = state.page === 1 ? "第 1 页 · Abstract" : "第 7 页 · §5 Training";
      const nextSource = data.pages[String(state.page)];
      if (!pageImage.src.endsWith(nextSource)) pageImage.src = nextSource;
      pageImage.alt = `Attention Is All You Need 第 ${{state.page}} 页`;

      if (state.page === 1) {{
        highlight.disabled = false;
        highlight.setAttribute("aria-label", "选择摘要中关于 Transformer 的句子");
        highlight.querySelector("span").textContent = state.step === "idle" ? "点击这句原文" : "已选择";
        setHighlight(data.focus.sentence, true);
      }} else if (state.evidence && state.evidence !== "code") {{
        highlight.disabled = true;
        highlight.setAttribute("aria-label", "当前论文证据位置");
        highlight.querySelector("span").textContent = "当前证据";
        setHighlight(data.focus[state.evidence], true);
      }} else {{
        setHighlight(data.focus.training, false);
      }}

      evidenceButtons.forEach(button => {{
        button.setAttribute("aria-pressed", String(button.dataset.demoEvidenceKey === state.evidence));
      }});
      setProgress();
      requestAnimationFrame(positionPage);
    }}

    function selectSentence() {{
      if (state.page !== 1 || state.step !== "idle") return;
      state = {{ step:"translated", page:1, evidence:null }};
      announcement.textContent = "已显示固定原文与预生成译文。下一步可以保存为方法笔记。";
      render();
    }}

    function bindAction(element, action) {{
      element.addEventListener("click", action);
      element.addEventListener("keydown", event => {{
        if (!["Enter", " ", "Spacebar"].includes(event.key)) return;
        event.preventDefault();
        action();
      }});
    }}

    bindAction(highlight, selectSentence);
    bindAction(save, () => {{
      state = {{ step:"saved", page:1, evidence:null }};
      announcement.textContent = "方法笔记已在演示内存中保存。刷新页面不会保留。";
      render();
    }});
    bindAction(ask, () => {{
      state = {{ step:"evidence", page:7, evidence:"dataset" }};
      announcement.textContent = "结论为部分可复现。已定位第 7 页 §5.1 数据集证据。";
      render();
    }});
    evidenceButtons.forEach(button => bindAction(button, () => {{
      state.evidence = button.dataset.demoEvidenceKey;
      const messages = {{
        dataset:"已定位第 7 页 §5.1 数据集证据。",
        hardware:"已定位第 7 页 §5.2 硬件与训练时长证据。",
        hyperparameters:"已定位第 7 页 §5.3 优化器与学习率证据。",
        code:"论文正文未提供官方代码仓库，因此没有制造页内定位。"
      }};
      announcement.textContent = messages[state.evidence];
      render();
    }}));
    bindAction(reset, () => {{
      state = {{ step:"idle", page:1, evidence:null }};
      announcement.textContent = "演示已重置。等待选择第 1 页摘要中的 Transformer 句子。";
      render();
      root.querySelector("[data-demo-highlight]").focus();
    }});
    pageImage.addEventListener("load", positionPage);
    addEventListener("resize", positionPage, {{ passive:true }});
    render();
  }})();
  </script>
"""


DEMO_CSS = r"""
.demo-jump {
  display:inline-flex; margin-top:18px; color:var(--blue); font-weight:650;
  text-underline-offset:5px;
}
.product-demo [hidden] { display:none !important }
.product-demo {
  margin-top:76px; padding:70px 0 78px;
  border-top:1px solid var(--line); border-bottom:1px solid var(--line);
  scroll-margin-top:28px;
}
.demo-intro {
  display:grid; grid-template-columns:minmax(0,1fr) minmax(280px,.5fr);
  gap:clamp(32px,6vw,84px); align-items:end; margin-bottom:34px;
}
.demo-kicker {
  margin:0 0 13px; color:var(--amber); font-weight:720; font-size:13px;
}
.demo-intro h2 {
  max-width:760px; margin:0; font-family:ui-serif,"Songti SC",serif;
  font-size:clamp(34px,4.8vw,60px); font-weight:540; line-height:1.08;
  letter-spacing:-.035em;
}
.demo-intro>div>p:last-child {
  max-width:60ch; margin:18px 0 0; color:var(--muted); font-size:17px;
}
.demo-paper-meta { display:grid; gap:5px; justify-items:start; font-size:13px }
.demo-paper-meta strong { font-family:ui-serif,"Songti SC",serif; font-size:19px }
.demo-paper-meta span,.demo-paper-meta small { color:var(--muted) }
.demo-paper-meta small { max-width:42ch; margin-top:8px }
.demo-paper-meta a { color:var(--blue); text-underline-offset:4px }
.demo-workspace {
  display:grid; grid-template-columns:minmax(0,1.08fr) minmax(380px,.92fr);
  min-height:720px; border:1px solid var(--line); background:var(--surface);
}
.demo-paper-pane {
  min-width:0; display:grid; grid-template-rows:auto minmax(0,1fr) auto;
  padding:18px; background:oklch(30% .012 252);
}
.demo-paper-toolbar,.demo-page-caption {
  display:flex; justify-content:space-between; gap:16px; color:oklch(84% .01 83);
  font-size:12px; letter-spacing:.03em;
}
.demo-paper-toolbar { padding:0 2px 13px }
.demo-page-caption { margin:12px 2px 0 }
.demo-page-viewport {
  position:relative; min-height:0; overflow:hidden; display:grid; place-items:center;
  background:oklch(24% .01 252);
}
.demo-page-sheet {
  position:relative; width:min(100%,540px); aspect-ratio:612/792;
  flex:none; transform-origin:0 0;
  transition:transform .42s cubic-bezier(.16,1,.3,1);
}
.demo-page-sheet img {
  display:block; width:100%; height:100%; object-fit:contain; background:oklch(98% .004 83);
  box-shadow:0 18px 50px oklch(10% .01 252 / .26);
}
.demo-page-highlight {
  position:absolute; min-height:38px; padding:0; border:1px solid var(--amber);
  border-radius:3px; background:oklch(79% .13 83 / .24); cursor:pointer;
  box-shadow:0 0 0 2px oklch(98% .03 83 / .72);
}
.demo-page-highlight:disabled { cursor:default }
.demo-page-highlight span {
  position:absolute; left:0; bottom:calc(100% + 7px); padding:3px 7px;
  border-radius:3px; background:var(--ink); color:var(--surface);
  font-size:11px; font-weight:700; white-space:nowrap;
}
.demo-panel { min-width:0; display:flex; flex-direction:column; padding:30px 34px 26px }
.demo-progress {
  list-style:none; display:grid; grid-template-columns:repeat(3,1fr); gap:0;
  margin:0 0 44px; padding:0; border-bottom:1px solid var(--line);
}
.demo-progress li {
  display:flex; align-items:center; gap:8px; padding:0 0 13px;
  color:var(--muted); font-size:12px;
}
.demo-progress li span { font-variant-numeric:tabular-nums }
.demo-progress li.is-current { color:var(--ink); font-weight:720; border-bottom:2px solid var(--blue) }
.demo-progress li.is-complete { color:var(--blue) }
.demo-step-label,.demo-translation span,.demo-note-heading span,.demo-pet-question span,
.demo-verdict>span {
  display:block; margin-bottom:7px; color:var(--blue);
  font-size:12px; font-weight:720; letter-spacing:.04em;
}
.demo-step h3 { margin:0; font-family:ui-serif,"Songti SC",serif; font-size:26px }
.demo-step p:last-child { max-width:48ch; margin:14px 0 0; color:var(--muted) }
.demo-translation { display:grid; gap:18px }
.demo-translation>div { padding-bottom:16px; border-bottom:1px solid var(--line) }
.demo-translation p { margin:0; font-size:15px; line-height:1.72 }
.demo-translation .button { justify-self:start; margin-top:4px }
.demo-note { margin-top:24px; padding:18px 0; border-top:1px solid var(--line) }
.demo-note-heading { display:flex; align-items:center; gap:12px }
.demo-note-heading span { margin:0 }
.demo-note-heading strong {
  padding:2px 8px; border:1px solid var(--line); border-radius:999px; font-size:12px;
}
.demo-note pre {
  margin:13px 0 0; padding:14px; overflow:auto; background:var(--blue-soft);
  color:var(--ink); font:500 13px/1.65 ui-monospace,SFMono-Regular,Consolas,monospace;
  white-space:pre-wrap;
}
.demo-pet-question {
  display:flex; align-items:center; gap:14px; margin-top:auto; padding-top:26px;
  border-top:1px solid var(--line);
}
.demo-pet-question img { width:46px; height:56px; object-fit:contain }
.demo-pet-question span { margin-bottom:4px }
.demo-pet-question button {
  padding:0; border:0; background:transparent; color:var(--ink);
  text-align:left; text-decoration:underline; text-underline-offset:5px;
  font-weight:720; cursor:pointer;
}
.demo-evidence { display:grid; gap:24px }
.demo-verdict strong {
  display:block; font-family:ui-serif,"Songti SC",serif; font-size:31px; font-weight:600;
}
.demo-verdict p { margin:10px 0 0; color:var(--muted); font-size:14px }
.demo-evidence-list { border-top:1px solid var(--line) }
.demo-evidence-list button {
  width:100%; display:grid; grid-template-columns:76px 70px minmax(0,1fr);
  gap:12px; align-items:center; min-height:57px; padding:10px 0;
  border:0; border-bottom:1px solid var(--line); background:transparent;
  color:var(--ink); text-align:left; cursor:pointer;
}
.demo-evidence-list button[aria-pressed="true"] { color:var(--blue) }
.demo-evidence-list button[aria-pressed="true"] strong::after { content:" · 正在核对" }
.demo-evidence-list strong { font-size:12px }
.demo-evidence-list small { color:var(--muted); font-size:12px }
.demo-finish { display:grid; justify-items:start; gap:9px }
.demo-finish p { margin:0; color:var(--muted); font-size:13px }
.demo-reset {
  min-height:36px; padding:0; border:0; background:transparent;
  color:var(--muted); text-decoration:underline; text-underline-offset:4px; cursor:pointer;
}
.demo-announcement { min-height:20px; margin:auto 0 0; padding-top:20px; color:var(--muted); font-size:12px }

@media(max-width:960px) {
  .demo-workspace { grid-template-columns:minmax(0,1fr) minmax(340px,.92fr) }
  .demo-panel { padding:26px 26px 22px }
  .demo-progress { margin-bottom:32px }
}
@media(max-width:720px) {
  .product-demo { margin-top:58px; padding:54px 0 60px }
  .demo-intro { grid-template-columns:1fr; gap:26px }
  .demo-workspace { grid-template-columns:1fr; min-height:0 }
  .demo-paper-pane { min-height:440px; padding:12px }
  .demo-page-viewport { height:360px; place-items:start }
  .demo-page-sheet { max-width:none }
  .demo-panel { min-height:610px; padding:25px 20px 22px }
  .demo-progress li { align-items:flex-start; flex-direction:column; gap:2px }
  .demo-evidence-list button { grid-template-columns:64px 78px minmax(0,1fr) }
}
@media(max-width:420px) {
  .demo-intro h2 { font-size:38px }
  .demo-paper-pane { min-height:410px }
  .demo-page-viewport { height:330px }
  .demo-panel { min-height:620px }
  .demo-evidence-list button {
    grid-template-columns:62px minmax(0,1fr); gap:4px 10px;
  }
  .demo-evidence-list small { grid-column:2 }
}
@media(prefers-reduced-motion:reduce) {
  .demo-page-sheet { transition:none }
}
"""
