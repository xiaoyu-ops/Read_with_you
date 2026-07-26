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
    "page_size": {"width": 612, "height": 792},
    "pages": {
        "1": "/api/portal/demo-assets/attention-p1-v1.webp",
        "7": "/api/portal/demo-assets/attention-p7-v1.webp",
    },
    "abstract_lines": [
        {"x": 143.56, "y": 414.21, "w": 324.73, "h": 8.91, "text": "The dominant sequence transduction models are based on complex recurrent or "},
        {"x": 143.87, "y": 425.12, "w": 324.26, "h": 8.91, "text": "convolutional neural networks that include an encoder and a decoder. The best "},
        {"x": 143.87, "y": 436.03, "w": 324.26, "h": 8.91, "text": "performing models also connect the encoder and decoder through an attention "},
        {"x": 143.87, "y": 446.94, "w": 325.51, "h": 8.91, "text": "mechanism. We propose a new simple network architecture, the Transformer, "},
        {"x": 143.87, "y": 457.85, "w": 324.26, "h": 8.91, "text": "based solely on attention mechanisms, dispensing with recurrence and convolutions "},
        {"x": 143.87, "y": 468.76, "w": 324.26, "h": 8.91, "text": "entirely. Experiments on two machine translation tasks show these models to "},
        {"x": 143.87, "y": 479.66, "w": 324.61, "h": 8.91, "text": "be superior in quality while being more parallelizable and requiring significantly "},
        {"x": 143.87, "y": 490.57, "w": 325.92, "h": 8.91, "text": "less time to train. Our model achieves 28.4 BLEU on the WMT 2014 English-"},
        {"x": 143.87, "y": 501.48, "w": 324.26, "h": 8.91, "text": "to-German translation task, improving over the existing best results, including "},
        {"x": 143.87, "y": 512.39, "w": 325.51, "h": 8.91, "text": "ensembles, by over 2 BLEU. On the WMT 2014 English-to-French translation task, "},
        {"x": 143.87, "y": 523.30, "w": 324.44, "h": 8.91, "text": "our model establishes a new single-model state-of-the-art BLEU score of 41.8 after "},
        {"x": 143.87, "y": 534.21, "w": 324.26, "h": 8.91, "text": "training for 3.5 days on eight GPUs, a small fraction of the training costs of the "},
        {"x": 143.87, "y": 545.12, "w": 324.26, "h": 8.91, "text": "best models from the literature. We show that the Transformer generalizes well to "},
        {"x": 143.87, "y": 556.03, "w": 324.27, "h": 8.91, "text": "other tasks by applying it successfully to English constituency parsing both with "},
        {"x": 143.87, "y": 566.94, "w": 122.40, "h": 8.91, "text": "large and limited training data."},
    ],
    "translations": [
        {
            "key": "sequence-models",
            "source": "The dominant sequence transduction models are based on complex recurrent or convolutional neural networks that include an encoder and a decoder.",
            "target": "当前主流的序列转换模型基于复杂的循环神经网络或卷积神经网络，通常包含编码器和解码器。",
        },
        {
            "key": "attention-bridge",
            "source": "The best performing models also connect the encoder and decoder through an attention mechanism.",
            "target": "表现最好的模型还会通过注意力机制连接编码器和解码器。",
        },
        {
            "key": "transformer",
            "source": "We propose a new simple network architecture, the Transformer, based solely on attention mechanisms, dispensing with recurrence and convolutions entirely.",
            "target": "我们提出一种全新的简洁网络架构 Transformer，它完全基于注意力机制，彻底舍弃循环结构和卷积结构。",
        },
        {
            "key": "parallel-training",
            "source": "Experiments on two machine translation tasks show these models to be superior in quality while being more parallelizable and requiring significantly less time to train.",
            "target": "两项机器翻译任务的实验表明，这类模型不仅质量更高，也更易于并行计算，训练所需时间显著减少。",
        },
        {
            "key": "english-german",
            "source": "Our model achieves 28.4 BLEU on the WMT 2014 English-to-German translation task, improving over the existing best results, including ensembles, by over 2 BLEU.",
            "target": "在 WMT 2014 英德翻译任务上，我们的模型取得了 28.4 BLEU，较包括集成模型在内的当时最佳结果提升超过 2 BLEU。",
        },
        {
            "key": "english-french",
            "source": "On the WMT 2014 English-to-French translation task, our model establishes a new single-model state-of-the-art BLEU score of 41.8 after training for 3.5 days on eight GPUs, a small fraction of the training costs of the best models from the literature.",
            "target": "在 WMT 2014 英法翻译任务上，单模型在 8 块 GPU 上训练 3.5 天后达到 41.8 BLEU 的新最佳成绩，训练成本仅为已有最佳模型的一小部分。",
        },
        {
            "key": "generalization",
            "source": "We show that the Transformer generalizes well to other tasks by applying it successfully to English constituency parsing both with large and limited training data.",
            "target": "我们还将 Transformer 成功应用于英语成分句法分析，证明它在大规模和有限训练数据条件下都能良好泛化。",
        },
    ],
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
        <h2 id="demo-title">产品快速体验</h2>
        <p>直接在原始论文第 1 页划选文字，查看预生成译文，再继续保存阅读判断。</p>
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
            <div
              class="demo-text-layer"
              data-demo-text-layer
              tabindex="0"
              role="group"
              aria-label="第 1 页摘要可选择文字。拖动选择任意摘要内容；键盘按 Enter 可体验示例句。"
            ></div>
            <button
              class="demo-page-highlight"
              data-demo-highlight
              type="button"
              aria-label="当前论文证据位置"
              hidden
            ><span>当前证据</span></button>
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
          <h3>在左页摘要中划选任意文字</h3>
          <p>第 1 页摘要已完整预翻译。像阅读真实 PDF 一样拖动选择，译文会出现在这里。</p>
        </div>

        <div class="demo-translation" data-demo-translation hidden>
          <div>
            <span>你选择的原文</span>
            <p data-demo-selection-original></p>
          </div>
          <div lang="zh-CN">
            <span>预生成对应句译文</span>
            <p data-demo-selection-translation></p>
            <small class="demo-selection-hint">演示只在浏览器内匹配预生成内容，没有调用模型或上传选区。</small>
          </div>
          <button class="button primary" data-demo-save type="button">保存为方法笔记</button>
        </div>

        <div class="demo-note" data-demo-note hidden>
          <div class="demo-note-heading">
            <span>已保存</span>
            <strong data-demo-note-tag>方法</strong>
          </div>
          <pre data-demo-note-body>### 方法判断
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

        <p class="demo-announcement" data-demo-announcement aria-live="polite">等待在第 1 页摘要中划选文字。</p>
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
    const textLayer = root.querySelector("[data-demo-text-layer]");
    const highlight = root.querySelector("[data-demo-highlight]");
    const start = root.querySelector("[data-demo-start]");
    const translation = root.querySelector("[data-demo-translation]");
    const selectionOriginal = root.querySelector("[data-demo-selection-original]");
    const selectionTranslation = root.querySelector("[data-demo-selection-translation]");
    const save = root.querySelector("[data-demo-save]");
    const note = root.querySelector("[data-demo-note]");
    const noteTag = root.querySelector("[data-demo-note-tag]");
    const noteBody = root.querySelector("[data-demo-note-body]");
    const question = root.querySelector("[data-demo-question]");
    const ask = root.querySelector("[data-demo-ask]");
    const evidence = root.querySelector("[data-demo-evidence]");
    const reset = root.querySelector("[data-demo-reset]");
    const announcement = root.querySelector("[data-demo-announcement]");
    const evidenceButtons = Array.from(root.querySelectorAll("[data-demo-evidence-key]"));
    const progress = Array.from(root.querySelectorAll("[data-demo-progress]"));
    const textLineElements = data.abstract_lines.map(line => {{
      const element = document.createElement("span");
      element.className = "demo-text-line";
      element.textContent = line.text;
      element.style.left = `${{line.x / data.page_size.width * 100}}%`;
      element.style.top = `${{line.y / data.page_size.height * 100}}%`;
      textLayer.appendChild(element);
      return element;
    }});
    let state = {{ step:"idle", page:1, evidence:null, selection:null, matches:[] }};

    function activeFocus() {{
      if (state.page === 1) return data.focus.sentence;
      return data.focus[state.evidence] || data.focus.training;
    }}

    function layoutTextLayer() {{
      const scale = sheet.offsetWidth / data.page_size.width;
      textLineElements.forEach((element, index) => {{
        const line = data.abstract_lines[index];
        element.style.fontSize = `${{line.h * scale}}px`;
        element.style.lineHeight = `${{line.h * scale}}px`;
        element.style.transform = "none";
        const naturalWidth = Math.max(element.getBoundingClientRect().width, 1);
        element.style.transform = `scaleX(${{line.w * scale / naturalWidth}})`;
      }});
    }}

    function positionPage() {{
      const focus = activeFocus();
      const narrow = matchMedia("(max-width: 720px)").matches;
      sheet.style.width = narrow ? `${{Math.max(viewport.clientWidth * 1.86, 600)}}px` : "";
      sheet.style.transform = "";
      layoutTextLayer();
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
      textLayer.hidden = state.page !== 1;
      pageNumber.textContent = String(state.page);
      pageCaption.textContent = state.page === 1 ? "第 1 页 · Abstract" : "第 7 页 · §5 Training";
      const nextSource = data.pages[String(state.page)];
      if (!pageImage.src.endsWith(nextSource)) pageImage.src = nextSource;
      pageImage.alt = `Attention Is All You Need 第 ${{state.page}} 页`;

      if (state.page === 1) {{
        setHighlight(data.focus.sentence, false);
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

    function normalizeText(value) {{
      return value
        .toLowerCase()
        .replace(/-\\s+/g, "-")
        .replace(/[^a-z0-9.\\-]+/g, " ")
        .replace(/\\s+/g, " ")
        .trim();
    }}

    function contentTokens(value) {{
      const ignored = new Set(["a","an","and","are","as","at","be","by","for","from","in","is","it","of","on","or","that","the","these","this","to","while","with"]);
      return normalizeText(value).split(" ").filter(token => token.length > 1 && !ignored.has(token));
    }}

    function matchesForSelection(rawText) {{
      const selected = normalizeText(rawText);
      const selectedTokens = Array.from(new Set(contentTokens(rawText)));
      const ranked = data.translations.map(item => {{
        const source = normalizeText(item.source);
        const sourceTokens = Array.from(new Set(contentTokens(item.source)));
        const overlap = selectedTokens.filter(token => sourceTokens.includes(token)).length;
        const selectedCoverage = overlap / Math.max(selectedTokens.length, 1);
        const sourceCoverage = overlap / Math.max(sourceTokens.length, 1);
        const contains = source.includes(selected) || selected.includes(source);
        return {{ item, overlap, selectedCoverage, sourceCoverage, contains }};
      }});
      const contained = ranked.filter(result => result.contains);
      if (contained.length) return contained.map(result => result.item);
      const covered = ranked.filter(result => result.overlap >= 2 && result.sourceCoverage >= 0.45);
      if (covered.length) return covered.map(result => result.item);
      ranked.sort((left, right) =>
        (right.selectedCoverage + right.sourceCoverage) -
        (left.selectedCoverage + left.sourceCoverage)
      );
      return ranked[0] && ranked[0].overlap ? [ranked[0].item] : [];
    }}

    function showSelection(rawText, forcedMatches) {{
      const selectedText = rawText.replace(/\\s+/g, " ").trim();
      if (selectedText.length < 2 || state.page !== 1) return;
      const matches = forcedMatches || matchesForSelection(selectedText);
      if (!matches.length) return;
      const isMethod = matches.some(item => item.key === "transformer");
      selectionOriginal.textContent = selectedText;
      selectionTranslation.textContent = matches.map(item => item.target).join("\\n\\n");
      save.textContent = isMethod ? "保存为方法笔记" : "保存为摘录笔记";
      noteTag.textContent = isMethod ? "方法" : "摘录";
      noteBody.textContent = isMethod
        ? "### 方法判断\\nTransformer 的核心变化不是在 RNN 上叠加注意力，\\n而是完全以注意力机制替代循环与卷积。"
        : `### 摘录\\n${{matches.map(item => item.target).join("\\n\\n")}}`;
      state = {{ step:"translated", page:1, evidence:null, selection:selectedText, matches }};
      announcement.textContent = "已显示你选择的原文与预生成对应句译文。下一步可以保存为笔记。";
      render();
    }}

    function readNativeSelection() {{
      const selected = getSelection();
      if (!selected || selected.isCollapsed || !selected.rangeCount) return;
      const range = selected.getRangeAt(0);
      if (!textLineElements.some(element => range.intersectsNode(element))) return;
      showSelection(selected.toString());
    }}

    function bindAction(element, action) {{
      element.addEventListener("click", action);
      element.addEventListener("keydown", event => {{
        if (!["Enter", " ", "Spacebar"].includes(event.key)) return;
        event.preventDefault();
        action();
      }});
    }}

    viewport.addEventListener("pointerup", () => setTimeout(readNativeSelection));
    viewport.addEventListener("touchend", () => setTimeout(readNativeSelection));
    textLayer.addEventListener("keyup", event => {{
      if (event.key === "Enter") {{
        const sample = data.translations.find(item => item.key === "transformer");
        showSelection(sample.source, [sample]);
        return;
      }}
      readNativeSelection();
    }});
    bindAction(save, () => {{
      state = {{ ...state, step:"saved" }};
      announcement.textContent = `${{noteTag.textContent}}笔记已在演示内存中保存。刷新页面不会保留。`;
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
      state = {{ step:"idle", page:1, evidence:null, selection:null, matches:[] }};
      const selected = getSelection();
      if (selected) selected.removeAllRanges();
      announcement.textContent = "演示已重置。等待在第 1 页摘要中划选文字。";
      render();
      textLayer.focus();
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
  pointer-events:none; user-select:none;
}
.demo-text-layer {
  position:absolute; inset:0; z-index:2; overflow:hidden;
  color:transparent; cursor:text; user-select:text; -webkit-user-select:text;
}
.demo-text-layer:focus-visible {
  outline:2px solid var(--blue); outline-offset:3px;
}
.demo-text-line {
  position:absolute; width:max-content; white-space:pre;
  font-family:"Times New Roman",Times,serif; transform-origin:0 0;
}
.demo-text-line::selection {
  background:oklch(79% .13 83 / .42); color:transparent;
}
.demo-page-highlight {
  position:absolute; z-index:3; min-height:38px; padding:0; border:1px solid var(--amber);
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
.demo-translation p { margin:0; white-space:pre-line; font-size:15px; line-height:1.72 }
.demo-selection-hint {
  display:block; margin-top:9px; color:var(--muted); font-size:12px; line-height:1.55;
}
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
