"""模型发现 — 调 OpenAI 兼容的 GET /v1/models 端点（参考 ccswitch model_fetch.rs）。

ccswitch 的候选 URL fallback 机制：
- base_url 以 /vN 结尾 → {base_url}/models
- 否则 → {base_url}/v1/models
- 兼容后缀剥离（/anthropic /claude 等剥掉再试 /v1/models）
- 按候选顺序尝试，404/405 继续下一个，200 解析返回模型列表

DeepSeek: https://api.deepseek.com → /v1/models（官方支持）
OpenAI:   https://api.openai.com/v1 → /models
中转站:    通常都是 OpenAI 兼容，/v1/models 通用
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

# 已知的兼容后缀（照 ccswitch KNOWN_COMPAT_SUFFIXES，长的优先）
_KNOWN_COMPAT_SUFFIXES: tuple[str, ...] = (
    "/api/anthropic",
    "/anthropic",
    "/claudecode",
    "/api/coding",
    "/step_plan",
    "/claude",
    "/api",
)

# /v1 /v2 等版本段
_VERSION_RE = re.compile(r"/v\d+$")


@dataclass
class DiscoveredModel:
    """发现的一个模型。"""

    id: str
    owned_by: str = ""

    def to_dict(self) -> dict:
        return {"id": self.id, "owned_by": self.owned_by}


def build_models_url_candidates(base_url: str, models_url: str | None = None) -> list[str]:
    """生成候选 models URL 列表（照 ccswitch build_models_url_candidates）。"""
    base = base_url.rstrip("/")

    # 显式覆盖优先
    if models_url:
        return [models_url]

    candidates: list[str] = []

    # 版本段结尾 → 直接加 /models
    if _VERSION_RE.search(base):
        candidates.append(f"{base}/models")
        # 非 /v1 的版本段也试 /v1/models
        if not base.endswith("/v1"):
            candidates.append(f"{base}/v1/models")
    else:
        candidates.append(f"{base}/v1/models")

    # 兼容后缀剥离：剥掉后缀再试 /v1/models 和 /models
    for suffix in _KNOWN_COMPAT_SUFFIXES:
        if base.endswith(suffix):
            root = base[: -len(suffix)].rstrip("/")
            extra1 = f"{root}/v1/models"
            extra2 = f"{root}/models"
            for u in (extra1, extra2):
                if u not in candidates:
                    candidates.append(u)
            break

    # 去重保序
    seen: set[str] = set()
    unique: list[str] = []
    for u in candidates:
        if u not in seen:
            seen.add(u)
            unique.append(u)
    return unique[:4]  # 最多 4 个候选


async def discover_models(
    base_url: str,
    api_key: str,
    models_url: str | None = None,
    timeout: float = 15.0,
) -> list[DiscoveredModel]:
    """发现可用模型。按候选 URL 顺序尝试，返回第一个成功的模型列表。

    异常情况：
    - 401/403 → 认证失败（key 错），直接抛错
    - 404/405 → 端点不对，试下一个候选
    - 全部 404/405 → 所有候选都失败
    - 超时 → 抛错
    """
    if not base_url.strip():
        raise ValueError("base_url 不能为空")
    if not api_key.strip():
        raise ValueError("api_key 不能为空")

    candidates = build_models_url_candidates(base_url, models_url)
    errors: list[str] = []

    headers = {
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "PeiNiDu/0.1",
    }

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        for url in candidates:
            try:
                resp = await client.get(url, headers=headers)
            except httpx.TimeoutException:
                errors.append(f"{url}: 超时")
                continue
            except httpx.HTTPError as e:
                errors.append(f"{url}: {e}")
                continue

            if resp.status_code == 200:
                return _parse_models_response(resp.json())
            elif resp.status_code in (401, 403):
                # 认证失败，不用试其他候选
                raise PermissionError(f"认证失败 (HTTP {resp.status_code})：请检查 api_key")
            elif resp.status_code in (404, 405):
                errors.append(f"{url}: HTTP {resp.status_code}")
                continue
            else:
                errors.append(f"{url}: HTTP {resp.status_code}")

    raise RuntimeError(f"所有候选 URL 都失败: {'; '.join(errors)}")


def _parse_models_response(data: dict) -> list[DiscoveredModel]:
    """解析 OpenAI /v1/models 标准响应 {"data": [{"id": "...", "owned_by": "..."}]}。"""
    models: list[DiscoveredModel] = []
    for item in data.get("data", []):
        model_id = item.get("id", "")
        if model_id:
            models.append(DiscoveredModel(id=model_id, owned_by=item.get("owned_by", "")))
    # 按 id 排序
    models.sort(key=lambda m: m.id)
    return models
