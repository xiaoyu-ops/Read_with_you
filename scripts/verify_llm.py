#!/usr/bin/env python3
"""Step 0 冒烟测试 — 验证 LiteLLM + config 配置链路通畅。

用法：
    # 用 config.yaml（需填入真实 LLM provider key）
    python scripts/verify_llm.py

    # 指定配置文件
    python scripts/verify_llm.py --config path/to/config.yaml

成功标准：打印一句 LLM 返回的中文回复。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# 让脚本能 import backend 包
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 与 FastAPI 启动逻辑保持一致：本地开发优先从根目录 .env 读取 key。
try:
    from dotenv import load_dotenv  # noqa: E402

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except ImportError:
    pass

from backend.llm.config import load_config  # noqa: E402
from backend.llm.client import LLMClient  # noqa: E402


async def main(config_path: str | None) -> int:
    cfg = load_config(config_path)
    client = LLMClient(cfg)

    print(f"[verify] default_provider = {cfg.default_provider}")
    print(f"[verify] default_model    = {cfg.default_model}")
    print(f"[verify] translation model = {cfg.task_models.translation}")
    print(f"[verify] preset            = {cfg.default_preset}")
    # 显示 provider 类型（确认 LiteLLM 路由前缀）
    for p in cfg.llm_providers:
        if p.name == cfg.default_provider:
            print(f"[verify] provider type    = {p.type}（LiteLLM 路由: {p.type}/<model>）")
            print(f"[verify] api_key 已配置    = {bool(p.api_key)}")
    print()

    # 用翻译任务模型做一次最小调用
    messages = [
        {"role": "system", "content": "你是一个翻译助手。把用户输入的英文翻译成中文，只输出译文。"},
        {"role": "user", "content": "Attention is all you need."},
    ]
    print("[verify] 调用 LLM（task=translation）...")
    try:
        reply = await client.acomplete(messages, task="translation")
    except Exception as e:
        print(f"[verify] ❌ 调用失败: {type(e).__name__}: {e}")
        print("[verify] 请检查 config/config.yaml 的 api_key 是否已填入真实值。")
        return 1

    print(f"[verify] ✅ LLM 返回: {reply}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="验证 LLM 配置链路")
    parser.add_argument("--config", default=None, help="配置文件路径")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.config)))
