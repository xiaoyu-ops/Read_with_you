"""多 Agent 模块 — 四项分析编排（摘要 / 可复现性 / 改进点 / 亮点）。

架构模仿 oh-my-opencode-slim：声明式 AgentDefinition + preset 系统 +
variant=reasoning effort + Orchestrator 用 asyncio.gather 并行扇出。
"""
