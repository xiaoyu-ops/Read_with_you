"""配置加载 — 读取 config.yaml（缺失回退 example），环境变量替换 ${VAR}。"""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

from ..runtime import RuntimeMode, resolve_runtime_mode
from ..security.credentials import (
    CredentialStoreError,
    credential_is_configured,
    get_system_credential_store,
    resolve_secret,
)
from .models import AppConfig, Provider

# ${VAR_NAME} → 环境变量值
_ENV_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")

# config/ 目录（源码环境位于仓库根；冻结包由启动器提供只读资源根）。
_PACKAGE_ROOT = Path(
    os.environ.get("PEINIDU_PACKAGE_ROOT", str(Path(__file__).resolve().parents[2]))
).expanduser()
_CONFIG_DIR = _PACKAGE_ROOT / "config"
_CONFIG_PATH = Path(
    os.environ.get("PEINIDU_CONFIG_PATH", str(_CONFIG_DIR / "config.yaml"))
).expanduser()
_EXAMPLE_PATH = _CONFIG_DIR / "config.example.yaml"

_config: AppConfig | None = None


def _expand_env(value):
    """递归把字符串里的 ${VAR} 替换成环境变量值，未设置则替换为空串。"""
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def load_config(path: str | os.PathLike | None = None) -> AppConfig:
    """加载配置文件。优先 config.yaml，缺失则用 config.example.yaml。"""
    global _config
    if path is not None:
        p = Path(path)
    elif _CONFIG_PATH.exists():
        p = _CONFIG_PATH
    else:
        p = _EXAMPLE_PATH
    with open(p, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    _config = AppConfig(**_expand_env(raw))
    return _config


def get_config() -> AppConfig:
    """获取已加载的配置，首次调用时懒加载。"""
    global _config
    if _config is None:
        _config = load_config()
    return _config


def get_model_provider_map(config: AppConfig | None = None) -> dict[str, Provider]:
    """model 名 → provider 的映射，供 client 解析模型归属。"""
    cfg = config or get_config()
    mapping: dict[str, Provider] = {}
    for p in cfg.llm_providers:
        for m in p.models:
            # default_provider 优先；否则先到先得，避免后声明的中转站覆盖官方 provider
            if m not in mapping or p.name == cfg.default_provider:
                mapping[m] = p
    return mapping


def reset_config() -> None:
    """重置单例（测试用）。"""
    global _config
    _config = None


# ── 写回 config.yaml（参考 ccswitch 的原子写；单层 yaml SSOT）──


def _mask_api_key(key: str) -> str:
    """Return a non-reversible placeholder without leaking a key prefix."""
    return "••••••••" if key else ""


def mask_config(
    config: AppConfig,
    runtime_mode: RuntimeMode | str | None = None,
) -> dict:
    """Return config metadata without exposing secrets or credential refs."""
    data = config.model_dump()
    for p in data.get("llm_providers", []):
        raw_key = p.get("api_key", "")
        credential_ref = p.pop("api_key_ref", "")
        configured = credential_is_configured(raw_key, credential_ref)
        p["api_key"] = _mask_api_key("configured" if configured else "")
        p["api_key_configured"] = configured
    mineru = data.get("mineru")
    if isinstance(mineru, dict):
        raw_token = mineru.get("api_token", "")
        credential_ref = mineru.pop("api_token_ref", "")
        configured = credential_is_configured(raw_token, credential_ref)
        mineru["api_token"] = _mask_api_key("configured" if configured else "")
        mineru["api_token_configured"] = configured
    deeplx = data.get("deeplx")
    if isinstance(deeplx, dict):
        raw_key = deeplx.get("api_key", "")
        credential_ref = deeplx.pop("api_key_ref", "")
        configured = credential_is_configured(raw_key, credential_ref)
        deeplx["api_key"] = _mask_api_key("configured" if configured else "")
        deeplx["api_key_configured"] = configured
    mode = (
        RuntimeMode(runtime_mode)
        if runtime_mode is not None
        else resolve_runtime_mode()
    )
    storage = {"mode": "config_or_env", "available": True}
    if mode is RuntimeMode.LOCAL_CORE:
        storage["mode"] = "system"
        try:
            get_system_credential_store()
        except CredentialStoreError:
            storage["available"] = False
    data["credential_storage"] = storage
    return data


def resolve_provider_api_key(provider: Provider) -> str:
    return resolve_secret(provider.api_key, provider.api_key_ref)


def resolve_mineru_config(config=None):
    source = config or get_config().mineru
    return source.model_copy(
        update={"api_token": resolve_secret(source.api_token, source.api_token_ref)}
    )


def _read_raw_env_refs(
    path: Path,
) -> tuple[dict[str, str], str | None, str | None]:
    """读原始 yaml，返回 provider key 与 MinerU token 的 ${ENV_VAR} 引用。

    用于 save_config 时还原环境变量引用——若某 provider 的 key 原本写成
    ${DEEPSEEK_API_KEY}，或 MinerU token 写成 ${MINERU_API_TOKEN}，
    保存时写回引用而非展开后的明文，避免泄露真实 key。
    """
    if not path.exists():
        return {}, None, None
    try:
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except OSError:
        return {}, None, None
    env_keys: dict[str, str] = {}
    for p in raw.get("llm_providers", []) or []:
        name = p.get("name", "")
        key = p.get("api_key", "")
        if isinstance(key, str) and _ENV_RE.search(key):
            env_keys[name] = key  # 保留整个 ${VAR} 引用串
    mineru_token_ref = None
    mineru = raw.get("mineru") or {}
    if isinstance(mineru, dict):
        token = mineru.get("api_token", "")
        if isinstance(token, str) and _ENV_RE.search(token):
            mineru_token_ref = token
    deeplx_key_ref = None
    deeplx = raw.get("deeplx") or {}
    if isinstance(deeplx, dict):
        key = deeplx.get("api_key", "")
        if isinstance(key, str) and _ENV_RE.search(key):
            deeplx_key_ref = key
    return env_keys, mineru_token_ref, deeplx_key_ref


def save_config(config: AppConfig, path: str | os.PathLike | None = None) -> None:
    """把 AppConfig 序列化写回 config.yaml（原子写：写 .tmp 再 rename）。

    config.yaml 保持唯一真相源（参考 ccswitch 的原子写机制；本项目是单层 yaml，
    不做 ccswitch 那种"写 live CLI 配置文件"的第二层投影——单客户端不需要）。
    环境变量引用保留：若原 yaml 里某 provider 的 key 是 ${ENV_VAR} 形式，
    保存时写回引用而非展开后的明文，避免真实 key 落盘。
    """
    import tempfile

    p = Path(path) if path else _CONFIG_PATH
    # 确保目录存在
    p.parent.mkdir(parents=True, exist_ok=True)

    # 读原始 yaml，拿回 ${ENV_VAR} 引用（避免写回展开后的明文 key）
    env_key_refs, mineru_token_ref, deeplx_key_ref = _read_raw_env_refs(p)

    # 序列化为 yaml dict
    data = config.model_dump()
    # 还原环境变量引用：原值是 ${VAR} 的，写回引用而非明文
    for provider in data.get("llm_providers", []):
        name = provider.get("name", "")
        if provider.get("api_key_ref"):
            provider["api_key"] = ""
        elif name in env_key_refs:
            provider["api_key"] = env_key_refs[name]
    mineru = data.get("mineru")
    if isinstance(mineru, dict):
        if mineru.get("api_token_ref"):
            mineru["api_token"] = ""
        elif mineru_token_ref:
            mineru["api_token"] = mineru_token_ref
    deeplx = data.get("deeplx")
    if isinstance(deeplx, dict):
        if deeplx.get("api_key_ref"):
            deeplx["api_key"] = ""
        elif deeplx_key_ref:
            deeplx["api_key"] = deeplx_key_ref
    yaml_text = yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False)

    # 原子写：先写临时文件再 rename（防写一半崩溃损坏 config）
    fd, tmp_path = tempfile.mkstemp(
        dir=str(p.parent), prefix=".config.yaml.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(yaml_text)
        os.replace(tmp_path, str(p))
    except Exception:
        # 出错时清理临时文件
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    # 更新内存单例
    global _config
    _config = config
