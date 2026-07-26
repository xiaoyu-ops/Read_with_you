"""配置 schema — Pydantic 模型定义。

对应立项文档第 7.2 节配置方案 + 第 8 节 Agent preset 系统。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Provider(BaseModel):
    """一个 LLM provider 配置（官方 / 中转站）。"""

    name: str
    # LiteLLM 的 provider type，如 "openai" / "anthropic" / "gemini"
    type: str = "openai"
    api_key: str = ""
    # local Core 只把非敏感引用写入 YAML；真实 key 位于系统凭据库。
    api_key_ref: str = ""
    # 中转站只需改 api_base（D18：OpenAI 兼容格式）
    api_base: str | None = None
    models: list[str] = Field(default_factory=list)


class TaskModels(BaseModel):
    """按任务分配模型（第 7.2 节 task_models）。

    不同任务用不同模型，控制成本：翻译用便宜模型，分析用强模型。
    key = task 名（translation / agent_summary / ...），value = model 名。
    agent_chat / agent_intent 为空时跟随 default_model（通常是快模型，
    保证 Pet 对话和意图分类低延迟）。
    """

    translation: str = "gpt-4o-mini"
    agent_summary: str = "gpt-4o"
    agent_reproducibility: str = "gpt-4o"
    agent_improvement: str = "gpt-4o"
    agent_highlights: str = "gpt-4o-mini"
    # Pet 普通对话 / 工具结果汇总 / 选区解释（交互路径，优先低延迟）
    agent_chat: str = ""
    # LLM 意图分类（每条消息前置调用，必须便宜快速）
    agent_intent: str = ""


class AgentOverride(BaseModel):
    """单个 agent 在 preset 中的覆盖配置（模仿 oh-my-opencode-slim）。

    model + variant 决定该 agent 用哪个模型 + 多大推理强度。
    """

    model: str
    # variant → temperature：low=0.1 / medium=0.4 / high=0.7 / max=1.0
    variant: Literal["low", "medium", "high", "max"] = "medium"


class Preset(BaseModel):
    """一个 preset = 一组 agent 的 model + variant 配置。"""

    orchestrator: AgentOverride = AgentOverride(model="gpt-4o", variant="medium")
    summary: AgentOverride = AgentOverride(model="gpt-4o", variant="high")
    reproducibility: AgentOverride = AgentOverride(model="gpt-4o", variant="high")
    improvement: AgentOverride = AgentOverride(model="gpt-4o", variant="medium")
    highlights: AgentOverride = AgentOverride(model="gpt-4o-mini", variant="low")


class MCPServerConfig(BaseModel):
    """MCP / 外部工具 server 配置。

    支持标准 stdio / Streamable HTTP 的 MCP server。tool_name 为空时，旧兼容
    执行器会从 tools/list 选择工具；统一 Loop 直接暴露 allowed_tools 过滤后的 schema。
    """

    name: str
    transport: Literal["stdio", "http"] = "stdio"
    command: str = ""
    args: list[str] = Field(default_factory=list)
    url: str | None = None
    enabled: bool = False
    tool_name: str = ""
    timeout_seconds: float = 12.0
    permission_scopes: list[
        Literal["mcp_tool", "external_search", "long_task", "browser_control"]
    ] = Field(
        default_factory=lambda: ["mcp_tool"]
    )
    # 空数组表示暴露 server discovery 返回的全部工具。
    allowed_tools: list[str] = Field(default_factory=list)


class MinerUConfig(BaseModel):
    """MinerU 文档解析配置。

    默认关闭；Agent 轻量接口无需 token，精准解析接口使用 api_token。
    """

    enabled: bool = False
    base_url: str = "https://mineru.net"
    mode: Literal["agent_lite", "standard"] = "agent_lite"
    api_token: str = ""
    api_token_ref: str = ""
    language: str = "en"
    page_range: str | None = None
    enable_table: bool = True
    enable_formula: bool = True
    is_ocr: bool = False
    poll_interval_seconds: float = 2.0
    max_wait_seconds: float = 120.0


class DeepLXConfig(BaseModel):
    """Basic translation provider used outside the Agent/LLM path."""

    base_url: str = "https://api.deeplx.org"
    api_key: str = ""
    api_key_ref: str = ""
    timeout_seconds: float = Field(default=30.0, gt=0, le=120)


class PdfExportConfig(BaseModel):
    """中文 PDF 导出 sidecar 配置。

    sidecar 地址和内部 token 只从环境变量读取，避免凭据进入 config.yaml。
    功能默认关闭；只有许可证披露完成后才允许启用。
    """

    enabled: bool = False
    license_disclosure_complete: bool = False
    wrapper_version: str = Field(
        default="1.0.1",
        pattern=r"^[0-9A-Za-z][0-9A-Za-z._-]{0,63}$",
    )
    sidecar_name: str = "PDFMathTranslate-next"
    sidecar_version: str = "v2.9.0"
    sidecar_commit: str = "f8dffcf4c3a33b254391d43514439b975ce8d966"
    sidecar_image_digest: str = (
        "sha256:c737d5342c9220a56026733f3a42182581bb4d8e5052b133e3326babffea109a"
    )
    source_code_url: str = (
        "https://github.com/PDFMathTranslate-next/PDFMathTranslate-next/tree/v2.9.0"
    )
    modified_source_url: str = "/pdf-exports/wrapper-source"
    license_name: str = "AGPL-3.0"
    target_language: Literal["zh-CN"] = "zh-CN"
    max_source_bytes: int = Field(default=50 * 1024 * 1024, ge=1)
    max_pages: int = Field(default=200, ge=1)
    max_output_bytes: int = Field(default=300 * 1024 * 1024, ge=1)
    max_concurrent_runs: int = Field(default=1, ge=1, le=16)
    timeout_seconds: float = Field(default=1800.0, gt=0)
    poll_interval_seconds: float = Field(default=2.0, gt=0)


class AppConfig(BaseModel):
    """应用总配置。"""

    llm_providers: list[Provider] = Field(default_factory=list)
    default_provider: str = "openai-official"
    default_model: str = "gpt-4o"
    task_models: TaskModels = Field(default_factory=TaskModels)
    presets: dict[str, Preset] = Field(default_factory=dict)
    default_preset: str = "openai"
    mcp_servers: list[MCPServerConfig] = Field(default_factory=list)
    mineru: MinerUConfig = Field(default_factory=MinerUConfig)
    deeplx: DeepLXConfig = Field(default_factory=DeepLXConfig)
    pdf_export: PdfExportConfig = Field(default_factory=PdfExportConfig)
    # 自定义翻译系统 prompt；为空时使用内置默认 prompt
    translation_prompt: str = ""
    # 翻译并发上限
    translation_concurrency: int = 5
    # Agent LLM 调用并发上限（单进程内生效）
    agent_concurrency: int = 2
    # 请求超时（秒）
    request_timeout: float = 60.0
