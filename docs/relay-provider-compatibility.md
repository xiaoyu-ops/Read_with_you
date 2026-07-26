# 中转站 Provider 兼容报告

> 更新时间：2026-07-18 15:20 CST
> 状态：T12D 完成

## 配置边界

- Provider：`opencode`，LiteLLM 类型为 `openai`。
- Base URL：`https://opencode.ai/zen/go/v1`。
- 凭据只保存在本地与服务器 0600 `.env` 的 `OPENCODE_API_KEY`；`config/config.yaml` 仅保留 `${OPENCODE_API_KEY}`，报告与仓库均不包含密钥。
- 本阶段不增加 Provider 专用 SDK 或调用路径，仍统一走 `LLMClient -> litellm.acompletion`。

## 当前真实结果

| 场景 | 结果 | 证据 / 限制 |
|---|---|---|
| 普通回答 | 通过 | `deepseek-v4-flash` 返回 `RELAY_OK`，耗时 5.87 秒 |
| 流式输出 | 通过 | 3 个内容 chunk 合并为 `STREAM_OK`，耗时 2.15 秒 |
| 流式原生 tool call | 通过 | `astream_with_tools` 正确组装 `lookup(query="streamed tools")`，耗时 2.74 秒 |
| 原生 tool calls | 通过 | `tool_choice=auto` 返回合法 `lookup` call，耗时 3.03 秒 |
| tool result 回填 | 通过 | 合法 assistant/tool 消息被模型接受并继续选择下一工具或自然回答 |
| 连续多工具 | 通过 | 真实 Provider 完成 `search -> fetch -> final`，最终无额外 tool call，耗时 6.32 秒 |
| 统一 Agent Loop | 通过 | 3 次模型迭代、2 次工具调用，真实执行 `search -> fetch -> 自然回答`，耗时 6.64 秒，无 limits |
| 鉴权失败 | 通过 | 无效测试 key 确定返回 HTTP 401 / LiteLLM `AuthenticationError` |
| 超时 | 通过 | `request_timeout=0.001` 确定返回 LiteLLM `Timeout`，HTTP status 408 |
| 取消 | 通过 | 发起真实请求后取消 asyncio task，0.108 秒内收到 `CancelledError` |
| 429 Pet 说明 | 通过 | 真实 Agent Loop 返回 `model_rate_limited`，自然回答为“模型服务当前限流、繁忙或额度已用完，请稍后重试或切换模型。” |
| 模型不存在 | 通过 | 中转站以 HTTP 401 / `AuthenticationError` 包装“model is not supported”；Loop 现在优先按错误文本归类为 `model_not_found` |
| 不支持参数 | 通过并记录限制 | 强制指定函数的 `tool_choice={...}` 返回 HTTP 400 `Upstream request failed`；生产默认 `tool_choice=auto` 可用，无需 Provider 专用分支 |
| 429 | 通过 | 用量窗口耗尽时真实返回 HTTP 429 `GoUsageLimitError`，Loop 输出 `model_rate_limited` 人话说明 |

## 推荐配置

- Provider type：`openai`。
- 模型：交互 Agent 使用 `deepseek-v4-flash`。
- Tool choice：保持项目默认 `auto`；不要强制发送指定函数的字典形式。
- Timeout：普通 Agent 请求保持现有 45–60 秒预算；整体 Loop 继续受 90 秒上限保护。
- 429、401、模型不存在和不支持参数继续使用统一错误分类，不增加中转站专用调用路径。

## 结论

中转站能够驱动 Pet 的统一 Agent Loop。唯一明确兼容限制是“强制指定具体函数”的 `tool_choice` 字典会被上游拒绝，而项目生产路径默认使用 `auto`，因此不需要增加兼容代码或 Provider 专用分支。
