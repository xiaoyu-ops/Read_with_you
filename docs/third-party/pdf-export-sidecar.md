# 中文 PDF 导出第三方声明

陪你读的网页原位阅读器由项目自身实现。可下载的单语中文 PDF 是独立、默认关闭的可选能力，不参与网页 `TranslationLayout`、标注或 Agent 证据定位。

## 固定上游

| 项目 | 固定版本 | 固定来源 |
|---|---|---|
| PDFMathTranslate-next | `v2.9.0` / commit `f8dffcf4c3a33b254391d43514439b975ce8d966` | <https://github.com/PDFMathTranslate-next/PDFMathTranslate-next/tree/v2.9.0> |
| 官方容器 | 多架构 OCI digest `sha256:c737d5342c9220a56026733f3a42182581bb4d8e5052b133e3326babffea109a` | <https://hub.docker.com/r/awwaawwa/pdfmathtranslate-next> |
| BabelDOC | 固定镜像内实际安装的 `v0.6.2` | <https://github.com/funstory-ai/BabelDOC/tree/v0.6.2> |
| 陪你读 wrapper | `1.0.1` | `GET /api/pdf-exports/wrapper-source` 确定性源码包 |

发布镜像的 OCI 元数据将上述 digest 指向 commit `f8dffcf4c3a33b254391d43514439b975ce8d966`；部署只接受该 digest，不使用浮动 `latest`。

## 许可证与源码

- PDFMathTranslate-next 与 BabelDOC 均以 GNU Affero General Public License v3.0（AGPL-3.0）提供。对应版本的许可证全文和源码分别见 [PDFMathTranslate-next v2.9.0 source](https://github.com/PDFMathTranslate-next/PDFMathTranslate-next/tree/v2.9.0)、[PDFMathTranslate-next LICENSE](https://github.com/PDFMathTranslate-next/PDFMathTranslate-next/blob/v2.9.0/LICENSE)、[BabelDOC v0.6.2 source](https://github.com/funstory-ai/BabelDOC/tree/v0.6.2) 与 [BabelDOC LICENSE](https://github.com/funstory-ai/BabelDOC/blob/v0.6.2/LICENSE)。
- 陪你读不修改上游 PDFMathTranslate-next 或 BabelDOC 源码。项目只提供独立的作业编排 wrapper；对应源码随仓库发布于 `sidecar/pdf_export`，部署核查入口为 `scripts/verify_pdf_export_sidecar.py`。运行中的后端通过 `GET /api/pdf-exports/wrapper-source` 提供 wrapper `1.0.1` 的确定性源码 ZIP；归档只读取固定白名单，固定文件顺序、时间戳和权限，拒绝符号链接，且不包含 `.env`、Provider 配置、用户数据或缓存。该 ZIP 自带验证器的全部静态依赖，可在解压目录用标准库独立核查。
- 如果未来修改上游 sidecar，必须先公开对应修改源码、版本与构建方式，并更新本页；在此之前生产配置必须保持 `license_disclosure_complete: false`。
- 本页是工程来源披露，不构成法律意见。

## 隔离边界

- sidecar 只生成单语中文 PDF，不生成左右双语 PDF。
- 公网 Compose 形态只加入 `internal: true` 的 `pdf-export-internal` 网络，不加入默认或公网网络；后端同时加入默认网络与该内部网络。T13 的 MacBook 本地形态只绑定 `127.0.0.1:8091` 并调用本机 backend，不对公网开放。两种形态都不接收上游 Provider 密钥，模型请求只发送到受共享 token 保护的后端 OpenAI-compatible 入口，再由后端通过 LiteLLM 调用当前 `translation` 模型。
- 公网 Compose sidecar 不映射宿主端口、不经过 Nginx；本地 8091 只允许 loopback。公网 `/api/internal/*` 被明确拒绝。
- 原 PDF 不会被覆盖。导出结果只有在 PDF 头、文件大小、页数和 Run 终态校验通过后，才通过受控下载接口发布。
- 交互回填只允许白名单中的页链接、内部 TOC 和无动作常见批注；JavaScript URI、启动程序、外部文件动作、文件批注、外部 TOC、表单、富媒体或带动作/不受支持的批注令整份导出 fail-closed。source 上传、临时 output 与规范化 output 还会拒绝 catalog/page `/OpenAction`、`/AA`、`/Names/JavaScript`、`EmbeddedFiles`、`/AF`、`AcroForm`、`Collection`、Renditions、AlternatePresentations 与 PresSteps；xref 解析异常同样拒绝。`http` / `https` 当前只校验 scheme，不限制主机名。
- wrapper `1.0.1` 会在发布前核对图片资源的实际可见性：非整页图片若仍存在但被译文层遮盖，则从安全源页重绘；整页扫描图不重绘，避免遮住 OCR 译文。它仍不是通用 PDF sanitizer；只能声明上述枚举对象和白名单交互已受测试，不能外推到任意未来 PDF 扩展。
- 导出失败、取消或 sidecar 崩溃不改变 `original.pdf`、`translation.json`、`translation_layout.json` 或网页阅读状态。

## 启用条件

运行时 `GET /api/pdf-exports/capability` 只在以下四项同时满足时返回可用：

1. 配置显式启用中文 PDF 导出。
2. `license_disclosure_complete` 为 `true`。
3. 内部共享 token 已通过环境变量注入后端与 sidecar。
4. 固定 digest 的 sidecar 健康、运行 wrapper 源码 hash 与后端公开副本一致，且内部 LiteLLM 入口可用。

任何一项不满足时，前端继续提供原 PDF 下载，并以自然语言说明中文 PDF 导出未启用。

T13 的最终目标是在 MacBook loopback 本地启用；真实创建、取消、下载、逐页视觉与重启恢复已完成。当前生产 VPS 总内存仅 3.8 GiB，作为最终边界保持配置禁用且不启动 sidecar。未来若另行公网激活，仍有一项尚未由 capability 自动检测的运维门：主机至少有 8 GiB 可供本部署使用的内存，并重新完成目标平台与真实 PDF 验收。本地完成不表示公网中文 PDF 导出已上线。
