"""Minimal, secret-safe DeepLX HTTP client for paper block translation."""

from __future__ import annotations

import os
import re
from urllib.parse import quote

import httpx

from ..llm.config import get_config
from ..security.credentials import CredentialStoreError, resolve_secret

DEEPLX_API_KEY_ENV = "DEEPLX_API_KEY"
DEEPLX_API_BASE_ENV = "DEEPLX_API_BASE"
DEEPLX_TIMEOUT_ENV = "DEEPLX_TIMEOUT_SECONDS"

_DEFAULT_BASE_URL = "https://api.deeplx.org"
_DEFAULT_TIMEOUT_SECONDS = 30.0
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,200}$")
_URL_ONLY_RE = re.compile(r"https?://\S+", flags=re.IGNORECASE)


class DeepLXError(RuntimeError):
    """Stable error code that never includes the credential-bearing URL."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


async def translate_text(
    text: str,
    *,
    source_lang: str = "EN",
    target_lang: str = "ZH",
    client: httpx.AsyncClient | None = None,
) -> str:
    """Translate one protected paper block through DeepLX.

    The API key is part of the provider's URL path, so raw ``httpx`` errors are
    always converted to stable codes before they can reach application logs.
    """
    source_text = text.strip()
    if not source_text:
        raise DeepLXError("deeplx_empty_source")

    endpoint = _endpoint_from_environment()
    payload = {
        "text": source_text,
        "source_lang": source_lang,
        "target_lang": target_lang,
    }

    owns_client = client is None
    active_client = client or httpx.AsyncClient(timeout=_timeout_from_environment())
    try:
        try:
            response = await active_client.post(endpoint, json=payload)
        except httpx.TimeoutException as exc:
            raise DeepLXError("deeplx_timeout") from exc
        except httpx.RequestError as exc:
            raise DeepLXError("deeplx_request_failed") from exc

        if response.status_code in {401, 403}:
            raise DeepLXError("deeplx_authentication_failed")
        if response.status_code == 429:
            raise DeepLXError("deeplx_rate_limited")
        if response.status_code >= 400:
            raise DeepLXError("deeplx_http_error")

        try:
            body = response.json()
        except ValueError as exc:
            raise DeepLXError("deeplx_invalid_response") from exc
        if not isinstance(body, dict):
            raise DeepLXError("deeplx_invalid_response")

        code = body.get("code")
        if code in {401, 403}:
            raise DeepLXError("deeplx_authentication_failed")
        if code == 429:
            raise DeepLXError("deeplx_rate_limited")
        if code != 200:
            raise DeepLXError("deeplx_provider_error")

        translated = body.get("data")
        if not isinstance(translated, str) or not translated.strip():
            raise DeepLXError("deeplx_empty_translation")
        translated = translated.strip()
        if _looks_like_service_notice(source_text, translated):
            raise DeepLXError("deeplx_invalid_translation")
        return translated
    finally:
        if owns_client:
            await active_client.aclose()


def _endpoint_from_environment() -> str:
    config = get_config().deeplx
    token = os.environ.get(DEEPLX_API_KEY_ENV, "").strip()
    if not token:
        try:
            token = resolve_secret(config.api_key, config.api_key_ref).strip()
        except CredentialStoreError as exc:
            raise DeepLXError("deeplx_credential_store_unavailable") from exc
    if not _TOKEN_RE.fullmatch(token):
        raise DeepLXError("deeplx_not_configured")

    base_url = os.environ.get(
        DEEPLX_API_BASE_ENV,
        config.base_url or _DEFAULT_BASE_URL,
    ).strip().rstrip("/")
    try:
        parsed = httpx.URL(base_url)
    except Exception as exc:
        raise DeepLXError("deeplx_invalid_base_url") from exc
    if (
        parsed.scheme != "https"
        or not parsed.host
        or parsed.userinfo
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise DeepLXError("deeplx_invalid_base_url")
    return f"{base_url}/{quote(token, safe='')}/translate"


def _timeout_from_environment() -> float:
    raw = os.environ.get(
        DEEPLX_TIMEOUT_ENV,
        str(get_config().deeplx.timeout_seconds or _DEFAULT_TIMEOUT_SECONDS),
    ).strip()
    try:
        timeout = float(raw)
    except ValueError as exc:
        raise DeepLXError("deeplx_invalid_timeout") from exc
    if not 0 < timeout <= 120:
        raise DeepLXError("deeplx_invalid_timeout")
    return timeout


def _looks_like_service_notice(source_text: str, translated: str) -> bool:
    return bool(
        _URL_ONLY_RE.fullmatch(translated)
        and not _URL_ONLY_RE.fullmatch(source_text)
    )
