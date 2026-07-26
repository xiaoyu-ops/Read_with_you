# 部署说明

## 服务器依赖

- Docker Engine + Docker Compose plugin
- 可访问 arXiv、ar5iv、Semantic Scholar 和所选 LLM provider
- `.env` 中配置真实 API key
- 如启用 MinerU 精准解析 API，`.env` 中配置 `MINERU_API_TOKEN`；Agent 轻量解析无需 token

后端镜像已内置 Poppler (`pdftotext` / `pdftohtml` / `pdfinfo`)，用于 PDF/block 映射。
后端启动时会自动创建 `data/`、`data/papers/`、`data/collections/`，避免 fresh deploy 时静态资源目录缺失导致应用导入失败。

## Docker Compose

```bash
cp .env.example .env
cp config/config.example.yaml config/config.yaml
```

填写 `.env` 后启动：

```bash
docker compose up -d --build
```

该命令不会启动可选的中文 PDF 导出 sidecar；其 `pdf-export` profile 默认关闭，网页原位阅读、翻译和原始 PDF 下载均不依赖它。

公开部署时必须设置：

```bash
PEINIDU_ADMIN_TOKEN=use-a-long-random-token
```

设置后，`/config` 和 `/config/models` 需要在设置页输入该 token 才能访问。普通检索、阅读、翻译和 Agent 分析不需要 token。

默认访问：

```text
http://SERVER_IP:8080
```

Compose 已配置 healthcheck：

- backend: `GET /health`
- frontend: `GET /`
- nginx: `GET /api/health`（容器内使用 `127.0.0.1`，避免 `localhost` 解析差异）

## 可选中文 PDF 导出

中文 PDF 导出使用独立的 PDFMathTranslate-next/BabelDOC sidecar，只生成单语、带水印的 `zh-CN` PDF。它不替换网页 `InlinePdfReader`，也不覆盖原始 PDF。启用前先阅读并发布 [`docs/third-party/pdf-export-sidecar.md`](third-party/pdf-export-sidecar.md)；后端镜像会包含同一份声明，并通过 `GET /api/pdf-exports/third-party-notice` 提供。

运行时固定为 PDFMathTranslate-next `v2.9.0`、commit `f8dffcf4c3a33b254391d43514439b975ce8d966`、OCI digest `sha256:c737d5342c9220a56026733f3a42182581bb4d8e5052b133e3326babffea109a`，项目 wrapper 固定为 `1.0.1`。`GET /api/pdf-exports/wrapper-source` 公开返回当前部署 wrapper 的确定性 ZIP：文件来自精确白名单，顺序、时间戳和权限固定，拒绝符号链接，也不包含 `.env`、Provider 配置、`data/` 或缓存；同一份源码会生成逐字节相同的归档。

默认配置有两道显式门，缺一不可：

```yaml
pdf_export:
  enabled: true
  license_disclosure_complete: true
```

另有一项必须人工核验的生产资源门：主机必须至少有 8 GiB 可供本部署使用的内存，再进行 Linux AMD64 容器和真实 PDF 验收。该门槛目前不是运行时代码的自动检测；不满足时必须保持上述两项为 `false`，也不得启动 `pdf-export` profile。

截至 2026-07-22，当前目标生产主机总内存仅 3.8 GiB、当前可用约 1.9 GiB，不满足激活门槛。主应用、披露页与公开 wrapper 源码接口已经以 `feature_disabled` 状态部署并完成健康检查，中文 PDF sidecar 不在该 VPS 启动。T13 的最终产品边界已改为 MacBook 本地导出，因此 VPS 禁用不再是项目阻塞；但本地测试或可阅读样例 PDF 仍不能写成“公网导出已经上线”。

本次禁用态部署已经确认：backend/frontend/nginx 三项服务 healthy；公网 `/api/health`、论文页和原始 PDF 正常；`/api/pdf-exports/capability` 返回 `feature_disabled`；第三方声明与确定性 wrapper 源码 ZIP 可读取并独立验证；生产容器内真实 LiteLLM 调用成功；没有 `pdf-export` 容器。该检查只证明主应用安全兼容新接口，不替代后续 sidecar 激活验收。

### MacBook 本地启用（T13 最终目标）

本机 backend 先监听 `127.0.0.1:8000`，根目录 0600 `.env` 同时配置 `PEINIDU_PDF_EXPORT_INTERNAL_TOKEN`、`PEINIDU_PDF_EXPORT_SIDECAR_TOKEN` 和 `PEINIDU_PDF_EXPORT_SIDECAR_URL=http://127.0.0.1:8091`。随后运行：

```bash
chmod 600 .env
./scripts/start_local_pdf_export_sidecar.sh
```

脚本从固定上游镜像重建已披露 wrapper，只保留一个 `peinidu-pdf-export-local` 容器，绑定 `127.0.0.1:8091`，限制为 4 GiB/2 CPU，使用只读根文件系统和 tmpfs 工作区，并通过 Docker healthcheck 验证服务。它只把共享内部 token 和 `host.docker.internal:8000/internal/llm/v1` 传给 sidecar，不传任何 Provider key。MacBook 本地容器不等同于公网 Compose 网络边界；不得把 8091 绑定改为 `0.0.0.0`。

随后在 `.env` 生成并保存一个独立随机 token。该 token 只用于后端与 sidecar 互相鉴权，不是 LLM Provider key：

```bash
PEINIDU_PDF_EXPORT_INTERNAL_TOKEN=replace-with-a-long-random-value
PEINIDU_PDF_EXPORT_SIDECAR_URL=http://pdf-export:8090
PEINIDU_PDF_EXPORT_RATE_LIMIT_PER_MINUTE=12
PEINIDU_PDF_EXPORT_MAX_ACTIVE_RUNS=2
```

Compose 会把同一个内部 token 注入后端和 sidecar；sidecar 只接收固定内部 LiteLLM 地址与 `pdf-translation` 模型别名，不接收 DeepSeek、OpenAI、Anthropic 或中转站密钥。后端内部 OpenAI-compatible 入口再通过统一 LiteLLM client 选择 `translation` 任务模型，并隐藏真实 Provider/模型信息。只有主机满足上述门槛时才执行以下启动与检查：

```bash
docker compose --profile pdf-export up -d --build
docker compose --profile pdf-export ps
curl http://127.0.0.1:8080/api/pdf-exports/capability
curl http://127.0.0.1:8080/api/pdf-exports/third-party-notice
python scripts/verify_pdf_export_sidecar.py
```

网络边界固定为：

- backend 同时加入默认网络和 `pdf-export-internal`。
- sidecar 只加入 `internal: true` 的 `pdf-export-internal`，没有宿主端口和默认/公网网络。
- frontend 与 nginx 只加入默认网络；公网 `/api/internal/*` 明确返回 404。

资源限制分三层执行。`config/config.yaml` 的 `pdf_export` 控制源文件、页数、输出大小、并发和总超时；`.env` 中的 `PEINIDU_PDF_EXPORT_MAX_FILE_BYTES`、`PEINIDU_PDF_EXPORT_MAX_PAGES`、`PEINIDU_PDF_EXPORT_CONCURRENCY`、`PEINIDU_PDF_EXPORT_TIMEOUT_SECONDS` 是 sidecar 的第二道上限；`PEINIDU_PDF_EXPORT_RATE_LIMIT_PER_MINUTE` 与 `PEINIDU_PDF_EXPORT_MAX_ACTIVE_RUNS` 分别限制每个可信 client 的创建/取消频率和全局 running+queued 数量。部署时以各层更严格的值为准。sidecar 工作目录位于 tmpfs `/work`；后端会持久化远端删除待办并在重启/后续创建时有界重试，工作目录仍不应作为备份来源。

原始 PDF 始终通过 `/api/papers/{paper_id}/original-pdf/download` 独立下载；中文导出仅在 Run 为 `done`、当前 wrapper provenance 完整且文件头、大小、页数、路径和 SHA-256 复核全部通过后提供下载。旧版无证明文件会被隔离并提示重新生成。创建/取消必须通过现有 `PEINIDU_ADMIN_TOKEN`、浏览器同源检查、独立限流和全局 active Run 上限；导出失败、取消、限流或 sidecar 崩溃不会修改 `original.pdf`、网页译文或标注。

wrapper 在 source 上传阶段、临时输出发布前和规范化保存后分别执行 fail-closed 审计。页级交互只回填页内/跨页链接、已解析命名目标、任意主机的 `http` / `https` 链接、`mailto` 链接、仅指向文档内部页的 TOC，以及不带动作的常见文字、标记和绘图批注；扫描到 JavaScript URI、启动程序、外部文件动作、文件批注、外部 TOC、表单、富媒体或带动作/不受支持批注时整份失败。xref/catalog 级还明确拒绝 `/OpenAction`、catalog/page `/AA`、`/Names/JavaScript`、`EmbeddedFiles`、`/AF`、`AcroForm`、`Collection`、Renditions、AlternatePresentations 与 PresSteps；对象解析异常同样返回 `output_validation_failed`，不会静默删除后继续发布。wrapper `1.0.1` 还比较源页与译文页的实际渲染；当非整页图片资源仍存在却被译文层遮盖时，从已审计的源页重绘该图片区域。占页面 80% 及以上的扫描图不执行该重绘，避免遮住 OCR 译文。

这仍不是通用 PDF sanitizer；只能声明上述枚举对象、链接、目录和批注边界已通过白名单/拒绝测试，不能外推到任意未来 PDF 扩展。运行中的 `/info.wrapper_source_sha256` 必须与后端烘入并公开下载的同一白名单源码计算结果一致，否则 capability 和新 Run 均 fail-closed，旧缓存也不会复用。

### Apple Silicon 构建边界

当前 `backend/Dockerfile` 会执行 Playwright 的 Chrome 和 Debian 字体安装步骤。MacBook Apple Silicon 已完成 source-run backend、固定 sidecar runtime verifier 和真实两页 PDF 创建/取消/下载/重启恢复，但没有据此声明整套 ARM64 Compose 已验收；生产 Linux AMD64 已完成 backend/frontend 构建。未来若要声明 ARM64 Compose 可用，仍须在目标平台完成整个 backend/frontend/profile 构建和真实浏览器链路，不能用局部验证代替。

可通过 `.env` 调整端口：

```bash
PEINIDU_HTTP_PORT=80
```

## 数据持久化

Compose 会挂载：

- `./data:/app/data`：论文、PDF、翻译缓存、分析结果、标注、SQLite
- `./config:/app/config`：实际配置文件
- `.env`：由 Docker Compose 读取并把所需变量注入容器，不挂载到容器文件系统

备份服务器时，至少保留 `data/`、`config/config.yaml` 和 `.env`。启用中文 PDF 导出后，`data/pdf_exports/` 保存完成的导出文件，`data/papers.db` 保存对应 Run 状态；二者必须作为同一份 `data/` 快照一起备份。原始 PDF 位于 `data/papers/<paper_id>/original.pdf`，必须继续独立保留。sidecar 的 `/work` 是临时 tmpfs，不需要也不应备份。

为避免 SQLite 状态与导出文件不同步，建议暂停写入后再快照整个目录，而不是只复制 `data/pdf_exports/`：

```bash
docker compose --profile pdf-export stop pdf-export backend
umask 077
tar -czf peinidu-data-backup.tgz data config/config.yaml .env
docker compose --profile pdf-export up -d
```

备份包包含 Provider key 和内部 token，应加密保存并限制访问。恢复时也应整体恢复 `data/`，并在启动前检查属主与权限。任何时候都可以单独下载和保留原始 PDF；恢复中文导出缓存不是打开论文或继续网页翻译的前置条件。

使用 `git archive`、`rsync` 等方式更新生产代码时，必须排除持久化的 `data/`、`.env` 和 `config/config.yaml`。即使归档中只有占位文件，覆盖也可能改变宿主机目录属主，令非 root 后端无法写入 SQLite 或原子保存配置。更新后应确认 `data/` 与 `config/` 属于容器用户 `10001:10001`，配置文件保持 0600；若属主被意外改变，修复后再重启：

```bash
chown -R 10001:10001 data
chown 10001:10001 config config/config.yaml
chmod 750 config
chmod 600 config/config.yaml
docker compose up -d
```

backend 启用 Compose `init: true`，用于回收 Playwright MCP / Chrome 派生进程。Playwright 使用持久化 profile 时，runtime 会在确认旧锁属于已退出 PID 或旧容器后清理 `Singleton*`，不会删除当前活跃浏览器的锁。

## Docker 维护约定

当前项目面向本地自部署：用户自己的论文、标注、翻译缓存、Agent 结果和专题数据默认留在部署目录的 `data/` 与 `config/` 中，不需要上传给项目维护者。

后续产品更新时，只要改动以下内容，必须同步检查 Docker 相关文件：

- 新增或调整环境变量：同步 `.env.example`、`docker-compose.yml`、本部署文档。
- 新增运行时持久化文件或目录：同步 compose volumes、fresh deploy 自动创建逻辑和备份说明。
- 新增 Python / Node 依赖或系统命令：同步 `backend/Dockerfile`、`frontend/Dockerfile`。
- 新增前端构建期变量或 API 路径：同步 `frontend/Dockerfile` build args、`deploy/nginx.conf`。
- 新增公开接口、SSE、上传、静态资源路径：同步 Nginx 反代、超时、buffering、healthcheck。

每次发布前至少跑一次：

```bash
docker compose config
docker compose up -d --build
docker compose ps
```

并验证：

```text
http://127.0.0.1:8080/
http://127.0.0.1:8080/api/
http://127.0.0.1:8080/api/health
```

## 反向代理

仓库内的 `deploy/nginx.conf` 是容器内 Nginx 配置：

- `/` 转发到 Next.js
- `/api/*` 转发到 FastAPI，并去掉 `/api` 前缀
- `/api/assets/*` 可访问后端论文 PDF / 图片资源

本地 PDF 上传走 `POST /api/papers/local-file`，容器内 Nginx 默认 `client_max_body_size 50m`；如果需要上传更大的 PDF，需要同步调大内外层反代限制。

如果服务器外层还有 Nginx / Caddy，只需要把公网域名代理到 compose 暴露的 `PEINIDU_HTTP_PORT`。

外层 Nginx 示例：

```nginx
server {
    listen 80;
    server_name your.domain.com;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

## systemd

可用 `deploy/peinidu.service.example` 托管 compose：

```bash
sudo cp deploy/peinidu.service.example /etc/systemd/system/peinidu.service
sudo systemctl daemon-reload
sudo systemctl enable --now peinidu
```

复制前把 `WorkingDirectory` 改成服务器上的真实仓库路径。

## 生产配置建议

- `PEINIDU_RUNTIME_MODE`: 只允许 `self_hosted`、`local_core`、`public_portal`，默认 `self_hosted`。当前完整 Docker 部署使用 `self_hosted`；本地 Core 使用 `local_core`；公网门户使用 `public_portal`。门户模式只暴露公开学术元数据检索、可重建论文图谱、下载/隐私页、无内容匿名计数、聚合数字与 `/health`；不创建论文目录、不挂载 `/assets`，也不注册 PDF 导入、阅读、翻译、笔记、文献库、Agent、配置、内部 LLM 或 OpenAPI 路由。下载按钮只按服务端已验证的 release manifest 渲染；没有正式发布时明确显示开发预览。本地工作台入口不得声称用户已安装，只在用户已经启动 `127.0.0.1:8520` 后使用，并同时展示健康检查和源码启动说明。
- `PEINIDU_LITERATURE_MAP_CACHE_DIR`: `public_portal` 的独立图谱派生缓存目录；不得指向论文目录、portable bundle 或 local Core 数据目录。
- `translation_concurrency`: 先用 3-5，按 provider 限流和服务器负载调整。
- `agent_concurrency`: 先用 1-2，公开访问时避免四 Agent 分析压满 LLM provider。
- `PEINIDU_RATE_LIMIT_PER_MINUTE`: 保护搜索、提取、翻译、分析等昂贵 API；默认 120，公开部署建议先保持开启。
- `PEINIDU_PDF_EXPORT_RATE_LIMIT_PER_MINUTE`: 中文 PDF 创建/取消的每可信 client 独立限流，默认 12/分钟。
- `PEINIDU_PDF_EXPORT_MAX_ACTIVE_RUNS`: 全局 running+queued 上限，默认 2；默认并发 1 时相当于 1 个运行、1 个排队。
- `PEINIDU_TRUSTED_PROXY_IPS`: 默认空；只有 ASGI socket peer 命中这里列出的精确 IP/CIDR 时，后端才读取由反向代理覆写的单值 `X-Forwarded-For`。不要直接填写任意公网网段。
- `request_timeout`: 强推理模型建议 120 秒以上。
- `PEINIDU_ADMIN_TOKEN`: 公开访问时必须设置，保护 provider/API key 配置入口。
- `MINERU_API_TOKEN`: 可选。仅 MinerU 精准解析 API 需要；Agent 轻量解析无需 token。精准模式会异步轮询任务并下载结果 zip，生产环境应把 `mineru.max_wait_seconds` 按文档规模调到足够长；结果归档只读取经过路径、成员数和体积校验的 `full.md`，不会任意解压到持久化目录。
- `PEINIDU_CORS_ORIGINS`: 添加实际公网 origin（例如 `https://pet.example.com`）；中文 PDF 的浏览器创建/取消会据此做同源校验。

当前 Agent 重复任务保护依赖 SQLite `BEGIN IMMEDIATE`，能防止多 worker 对同一论文同一任务重复启动。它不是完整任务队列；如果后续开放给更多用户，建议升级为后台队列 + 任务状态轮询。

## 本地 Pet Core 打包与安全边界

本地 Core 不是远程共享服务，也不是把公网网页直接连到开放后端。启动器在
loopback 上建立单一入口：

```text
browser -> http://127.0.0.1:8520
             ├─ /api/*    -> 同进程 FastAPI content app
             ├─ /assets/* -> 当前用户本地论文缓存
             └─ /*        -> 127.0.0.1:8521 Next standalone
```

- 对外只绑定 `127.0.0.1`，不监听局域网或公网地址。
- gateway 校验实际 peer、Host、Origin 和 `Sec-Fetch-Site`；跨站 unsafe 请求与
  非顶层导航请求返回 403。
- 所有响应增加 `frame-ancestors 'none'`、`X-Frame-Options: DENY`、
  `nosniff`、`same-origin` CORP 与 `no-referrer`，外部页面不能 iframe 嵌入工作台。
- `local_core` 不启用开发 CORS；页面、API、PDF 和 SSE 均由同一 origin 访问。
- 可写数据位于用户应用数据目录的 `cache/`、`config/` 和 `logs/`，包内资源只读。
- 重复启动会先识别已有 `local_core` 健康端点，不会创建第二套 Next/FastAPI。
- 默认包只携带 Python Core、Next standalone、Node runtime 与 Poppler；Playwright
  browser 和 PDF export 都不进入本地 Core 默认包。

macOS 构建器会递归收集并重链接 Node/Poppler 动态库，随后逐文件 ad-hoc 签名；
Windows CI 使用固定 Node、Python、PyInstaller 和 Poppler 版本。两端都会：

1. 运行冻结程序 `--check-runtime`，验证 Node、Next、Poppler 与 LiteLLM 可导入。
2. 扫描包内 `.env`、用户数据目录名、当前环境凭据值和常见静态 key 形态。
3. 生成包含每个文件 SHA-256 的外部 manifest 与可重复时间戳压缩包。

这只是可审计的开发发行流程，不等于平台信任链。面向普通用户公开下载前仍需：

- Apple Developer ID 签名与 notarization；
- Windows Authenticode 签名；
- 在干净的 Intel/Apple Silicon macOS 与 Windows 主机执行安装/卸载烟测；
- 发布 manifest、压缩包 SHA-256、第三方运行时版本与许可证。

源码运行示例：

```bash
python scripts/peinidu_local_core.py --no-browser
```

应用默认地址为 `http://127.0.0.1:8520`。可用 `--app-data-dir`、`--gateway-port`
和 `--frontend-port` 指定隔离烟测目录与端口；正式用户不应把 gateway port 映射到
非 loopback 地址。

### 本地 Core 的 BYOK 凭据

- macOS 只接受 `keyring.backends.macOS`，Windows 只接受
  `keyring.backends.Windows`；Null、文件型、SecretService 或其他 fallback backend
  均 fail-closed，不会退回 YAML 明文。
- 每项凭据使用随机、稳定的 `llm:*`、`deeplx:*` 或 `mineru:*` account reference；
  `config.yaml` 只保存该非敏感引用。写入后必须立即从系统凭据库读回并做常量时间
  比较，验证失败会删除测试写入并返回稳定错误码。
- `GET /config` 只返回固定 `••••••••`、`*_configured` 与
  `credential_storage={mode,available}`，不返回 Key 前缀、真实值或 reference。
- `POST /config` 在 `local_core` 中会把新 Key 或旧 inline/env Key 迁入系统凭据库，
  再原子写入无明文 YAML；删除 Provider 时会清理不再引用的凭据。
- `POST /config/credentials/delete` 只在 `local_core` 存在，可显式删除 Provider、
  MinerU 或 DeepLX 凭据；`POST /config/deeplx/test` 使用已保存的 DeepLX Key 做一次
  短句连接测试。模型发现允许只传 provider 名称，由本地 Core 读取 Key，不要求
  浏览器再次回传。
- 浏览器只在当前 React 表单内短暂持有用户刚输入的 Key，不写入 localStorage、
  IndexedDB、portable bundle 或论文目录。公网门户没有任何 `/config` 路由。
- `self_hosted` 保持 `${ENV_VAR}` 和现有配置兼容；它不会自动使用桌面系统凭据库。

本地 Core 构建依赖：

```bash
python -m pip install -r backend/requirements-local-core.txt
```

### 从旧 self-hosted 迁移到本地 Core

迁移全程由浏览器直接写入用户选择的本地目录，公网门户不接收中间文件：

1. 在旧 self-hosted 的“设置 → 本地文献库”选择一个空目录或已有陪你读目录。
2. 点击“将服务端全部保存到此目录”。每篇 portable bundle 会逐文件写入、重读并
   校验 SHA-256；任何一篇失败时不会发布新的根工作区清单。
3. 启动本地 Core，在设置页重新选择同一目录。File System Access handle 按 origin
   保存在各自浏览器的 IndexedDB，不能也不需要从旧站复制。
4. 点击“从此目录恢复全部论文”。local Core 会先验证
   `peinidu-workspace.json` 的 revision，再验证每篇 manifest，顺序恢复论文与同论文
   chat，最后按名称恢复专题 membership；已有同名专题会复用，空专题也保留。
5. 任一论文或专题失败都会单独列出，源目录不删除，也不会静默覆盖 revision 冲突。

工作区只包含论文 portable bundle、专题名称与 membership。Provider/DeepLX/MinerU
Key、管理员令牌、服务器 URL、全局 Memory、Skill、系统凭据引用和目录 handle 均
不迁移；新设备的 Key 必须在本地 Core 设置页重新写入系统凭据库。

Next standalone 必须以 `images.unoptimized=true` 构建。本地 Core 的产品图片直接
读取已优化静态资产，不能在运行时向签名 `.app` 的 `.next/cache/images` 写文件；
打包器会读取 `required-server-files.json` 并拒绝不满足该约束的构建。

## 公网入口、发行清单与匿名统计

当前主入口为 `https://readwithyou.xiaoyu666.cyou`；旧
`https://pet.xiaoyu666.cyou` 只保留同路径 308 跳转，不再直接承载门户。

公网入口必须作为独立 `public_portal` 进程运行，不能与完整
`self_hosted` 内容服务共用容器或数据卷：

```bash
PEINIDU_RUNTIME_MODE=public_portal \
PEINIDU_PORTAL_DATA_DIR=/var/lib/peinidu-portal \
PEINIDU_LITERATURE_MAP_CACHE_DIR=/var/lib/peinidu-portal/literature-maps \
PEINIDU_RELEASE_MANIFEST=/etc/peinidu/release.json \
uvicorn backend.api.main:app --host 127.0.0.1 --port 8540 --no-access-log
```

仓库同时提供独立的生产 Compose 与宿主机 Nginx 模板。它们只启动门户后端，不启动
完整 frontend/backend/nginx 组合，也不读取完整部署的 `.env`、`config/` 或
`data/`：

```bash
install -d -o 10001 -g 10001 /var/lib/peinidu-portal
PEINIDU_PORTAL_DATA_DIR=/var/lib/peinidu-portal \
PEINIDU_LITERATURE_MAP_CACHE_DIR=/var/lib/peinidu-portal/literature-maps \
docker compose -p peinidu-portal \
  -f deploy/public-portal.compose.yml up -d --build

install -m 0644 deploy/public-portal.nginx.conf \
  /etc/nginx/sites-available/readwithyou.xiaoyu666.cyou.conf
nginx -t
systemctl reload nginx
```

门户容器只绑定 `127.0.0.1:8540`，使用只读根文件系统、全部 capability drop、
128 PID、256 MiB 和 0.5 CPU 上限。当前 RackNerd 主机的旧 Docker/kernel 组合在
启用 `no-new-privileges` 时会在执行 Python 前直接返回 `operation not permitted`，
因此模板没有声明该选项；升级宿主机运行时后应重新验证并恢复。部署源码包必须排除
`.env`、`data/` 和本地凭据，旧完整服务可以保留作回滚，但公网 Nginx 只能代理
门户的 `8540`。

发行清单格式见 `config/release-manifest.example.json`。下载源必须是 HTTPS，清单
非法或未配置时按钮保持不可用，门户不得退回开发包或猜测下载地址。正式发布前仍需
完成 Developer ID/notarization、Authenticode、干净机烟测、公开 SHA-256 和第三方
许可清单。

自 2026-07-26 起，产品默认发送无内容的匿名使用计数，不显示授权卡或统计开关。
公网记录 `portal_visited`、`search_submitted`、`map_opened`，local Core 记录
`core_started` 与首个可读 PDF 对应的 `reader_opened`。这些事件只用于回答当天
有多少人访问、检索、打开图谱或真正打开阅读器；发送失败不得影响任何产品流程。

历史兼容接口仍只接受以下固定 JSON 字段：

```text
event_date / daily_id / event / platform / app_version
```

- `daily_id` 由只保存在本机的随机安装 ID 和 UTC 日期派生；跨日不同，服务端无法
  用它建立长期设备轨迹。
- 同一匿名日 ID、同一日、同一事件只计一次；公网随机 ID 每日轮换，local Core
  由只保存在系统本机的随机安装 ID 按 UTC 日期派生，二者都不能跨日关联。
- 公网 SQLite 只保存每日匿名 ID、固定枚举、平台、版本和接收时间；用于去重的行
  最多保留 35 天，长期只保留按日期/事件/平台的数字。
- 论文 ID、标题、PDF、路径、选区、笔记、问题、回答、Provider、Key 和原始安装 ID
  不在 schema 中，额外字段直接返回 422。
- 聚合数字是使用事件计数，不是账号数、安装总数或长期独立用户数。

应用层不得持久化 IP 或 User-Agent。运行 Uvicorn 时使用 `--no-access-log`；Nginx
为该站点设置 `access_log off;`，不要把请求体写入错误日志或 APM。Cloudflare 仍会
作为网络与安全服务处理连接元数据，应关闭不需要的 Web Analytics/第三方脚本、
采用最短可用日志保留并在隐私说明中如实披露，不能宣传成“网络链路无人处理 IP”。

公开白名单：

```text
GET  /
GET  /privacy
GET  /health
GET  /api/portal/releases/latest
GET  /api/portal/download/{macos_arm64|windows_x64}
POST /api/portal/telemetry
GET  /api/portal/stats
POST /api/portal/search
GET  /literature-map/{paper_ref}
GET  /api/portal/literature-map/{paper_ref}
```

公开检索把用户当前提交的查询发送给 arXiv / Semantic Scholar 以完成功能，但不把
查询写入匿名统计或服务端检索缓存；图谱缓存只含公开、可重建的学术元数据。
`/papers`、`/assets`、`/translate`、`/agent`、`/config`、内部 LLM、OpenAPI、
PDF 导入、笔记、文献库和 portable bundle 在 portal 模式中必须继续返回 404。

## 验证

```bash
docker compose ps
docker compose logs -f backend
curl http://127.0.0.1:8080/api/
curl http://127.0.0.1:8080/api/health
```

本地代码验证：

```bash
python -m unittest discover backend/tests
python scripts/audit_pdf_mapping.py
cd frontend && npm run build
```
