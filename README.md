# 陪你读 / Read with you

在原始 PDF 上阅读、划选翻译、记录 Markdown 笔记，再和 Pet 一起把论文研究清楚。

[项目入口](https://readwithyou.xiaoyu666.cyou) · [部署说明](docs/deployment.md)

![陪你读首页](docs/images/readme/home.png)

> 当前完整阅读器运行在用户自己的 Pet Core 中。公网入口只用于了解项目与下载，不接收论文、笔记、问题、回答或 API Key。

## 能做什么

- **读原始 PDF**：PDF.js canvas 保留论文原页，官方 TextLayer 负责搜索和准确划选。
- **划选即翻译**：选中英文后自动请求基础翻译，译文固定显示在右侧，不覆盖论文。
- **边读边记**：保存重要、疑问、方法、结论等语义高亮，并维护整篇论文 Markdown 笔记。
- **问 Pet**：围绕当前论文、选区和你的笔记快速提问。
- **进入 Agent 工作台**：进行长对话、方法分析、复现判断、外部检索与证据核对。
- **数据留在本机**：论文工作区可写入用户选择的本地文件夹；Provider Key 进入系统凭据库。

## 使用方式

### 1. 检索或导入论文

在首页输入论文标题、arXiv ID 或 URL，确认检索候选；也可以直接导入本地 PDF。

### 2. 在原始 PDF 中划选

左侧始终是原始 PDF。选中文字后，右侧呈现译文、语义高亮和选区笔记；整篇论文笔记会随论文长期保存。

![原始 PDF 与论文笔记工作台](docs/images/readme/reader.png)

### 3. 随时问 Pet

阅读页的 Pet 适合快速解释当前段落、核对术语或把问题带入同一论文会话。

### 4. 用 Agent 深入研究

Agent 页面采用“论文会话 / 对话 / 证据与笔记”三栏工作台。它与阅读页 Pet 共用会话，不会重复发送问题。

![Agent 研究工作台](docs/images/readme/agent.png)

### 5. 保存到自己的文件夹

在“设置 → 文献与存储”选择本地文件夹。论文、PDF、翻译、笔记、标注、对话和分析结果会经过逐文件 SHA-256 校验后写入该目录。

## 本地启动

当前推荐从源码启动本地 Core。它只监听 `127.0.0.1:8520`，并在同一个 origin 下提供页面、API、PDF 资产与 SSE。

环境要求：

- Python 3.11+
- Node.js 22
- Poppler（`pdfinfo`、`pdftotext`）

```bash
git clone https://github.com/xiaoyu-ops/Read_with_you.git
cd Read_with_you

python -m pip install -r backend/requirements-local-core.txt
cd frontend && npm ci && cd ..

python scripts/start_local_core_dev.py
```

不自动打开浏览器：

```bash
python scripts/start_local_core_dev.py --no-browser
```

随后访问 [http://127.0.0.1:8520](http://127.0.0.1:8520)。

## 配置自己的服务

打开“设置 → 模型与翻译”填写：

- **LLM Provider**：用于 Pet、Agent、摘要与专业分析，统一经过 LiteLLM。
- **DeepLX**：用于不需要 Agent 推理的基础选区翻译。
- **MinerU**：可选，用于复杂版面或扫描 PDF 的解析兜底。

本地 Core 在 macOS 只接受 Keychain，在 Windows 只接受 Credential Manager。API 仅返回“已配置”状态与固定掩码，不回显 Key、前缀或凭据引用。

## 数据与隐私

- 公网门户不加载论文、翻译、Agent、配置、内部 LLM、PDF 资产或 OpenAPI。
- 本地 Core 不发送匿名使用统计，也没有隐藏遥测。
- API Key 不进入浏览器存储、论文目录、portable bundle、日志、Prompt 或对话记录。
- Agent 引用你的笔记时会明确标记“你的笔记”，不会把用户判断冒充论文事实。
- 本地文件夹模式由用户主动授权；写入和恢复均校验 revision、路径与 SHA-256。

## 技术结构

| 层 | 实现 |
|---|---|
| Web | Next.js、React、Tailwind CSS、PDF.js |
| Core | FastAPI、SQLite / FTS5、Poppler |
| 翻译 | DeepLX；旧 LiteLLM 翻译路径仅作兼容 |
| Agent | 统一迭代 Agent Loop、LiteLLM、权限按 Run 暂停与恢复 |
| 提取 | ar5iv / LaTeX 优先，MinerU 可选兜底 |
| 存储 | 本机应用数据目录或用户选择的本地文件夹 |

Playwright 浏览器自动化与中文 PDF 导出都是默认关闭的可选 profile，不进入核心镜像，也不影响日常阅读、翻译和 Agent。

## 开发与验证

开发时也可以分别启动前后端：

```bash
uvicorn backend.api.main:app --reload --port 8000
cd frontend && npm run dev
```

主要验证命令：

```bash
python -m unittest discover backend/tests
python -m compileall -q backend scripts
cd frontend && npx tsc --noEmit
```

完整部署、安全边界和可选服务说明见 [docs/deployment.md](docs/deployment.md)。

## 当前边界

- arXiv 优先走结构化源码；扫描件需要 MinerU/OCR，无法证明准确时会明确降级。
- 公网入口目前不是共享论文后端，完整能力需要先在本机启动 Core；门户中的
  loopback 入口只有在 `127.0.0.1:8520` 健康时才可用。
- macOS `Peinidu.app` 目前是开发用 ad-hoc 构建，尚未作为已签名、公证的公开安装包发布。
- Windows 已有构建工作流，但仍需在干净 Windows 机器上完成发行验收。
