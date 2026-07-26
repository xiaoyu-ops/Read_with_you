"""Fail-closed access to the operating system credential store.

Local Core credentials are referenced from ``config.yaml`` by an opaque,
non-secret account id.  The secret itself is stored by ``keyring`` in macOS
Keychain or Windows Credential Manager.  Unsupported or fallback keyring
backends are deliberately rejected instead of writing plaintext.
"""

from __future__ import annotations

import secrets
import sys
import uuid
from typing import Any, Literal


SERVICE_NAME = "com.xiaoyu.peinidu"
CredentialKind = Literal["llm", "mineru", "deeplx"]
_ALLOWED_BACKEND_MODULES = {
    "darwin": "keyring.backends.macos",
    "win32": "keyring.backends.windows",
}


class CredentialStoreError(RuntimeError):
    """Stable, secret-free credential-store failure."""


class SystemCredentialStore:
    """Small verified wrapper around the platform keyring backend."""

    def __init__(self, backend: Any | None = None, *, platform: str | None = None) -> None:
        self.platform = platform or sys.platform
        self.backend = backend if backend is not None else self._load_backend()
        self._validate_backend()

    @staticmethod
    def _load_backend() -> Any:
        try:
            import keyring
        except ImportError as exc:
            raise CredentialStoreError("system_credential_store_unavailable") from exc
        try:
            return keyring.get_keyring()
        except Exception as exc:
            raise CredentialStoreError("system_credential_store_unavailable") from exc

    def _validate_backend(self) -> None:
        expected = _ALLOWED_BACKEND_MODULES.get(self.platform)
        module = self.backend.__class__.__module__.casefold()
        if expected is None or not module.startswith(expected):
            raise CredentialStoreError("unsupported_credential_store")

    @staticmethod
    def _validate_ref(credential_ref: str) -> None:
        parts = credential_ref.split(":")
        if (
            len(parts) != 2
            or parts[0] not in {"llm", "mineru", "deeplx"}
            or len(parts[1]) != 32
            or any(ch not in "0123456789abcdef" for ch in parts[1])
        ):
            raise CredentialStoreError("invalid_credential_reference")

    def get(self, credential_ref: str) -> str | None:
        self._validate_ref(credential_ref)
        try:
            value = self.backend.get_password(SERVICE_NAME, credential_ref)
        except Exception as exc:
            raise CredentialStoreError("credential_read_failed") from exc
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise CredentialStoreError("credential_read_failed")
        return value

    def set(self, credential_ref: str, value: str) -> None:
        self._validate_ref(credential_ref)
        if not isinstance(value, str) or not value.strip() or len(value) > 8192:
            raise CredentialStoreError("invalid_credential_value")
        try:
            self.backend.set_password(SERVICE_NAME, credential_ref, value)
            stored = self.backend.get_password(SERVICE_NAME, credential_ref)
        except Exception as exc:
            raise CredentialStoreError("credential_write_failed") from exc
        if not isinstance(stored, str) or not secrets.compare_digest(stored, value):
            try:
                self.backend.delete_password(SERVICE_NAME, credential_ref)
            except Exception:
                pass
            raise CredentialStoreError("credential_verification_failed")

    def delete(self, credential_ref: str) -> None:
        self._validate_ref(credential_ref)
        try:
            if self.backend.get_password(SERVICE_NAME, credential_ref) is None:
                return
            self.backend.delete_password(SERVICE_NAME, credential_ref)
        except Exception as exc:
            raise CredentialStoreError("credential_delete_failed") from exc

    def contains(self, credential_ref: str) -> bool:
        return self.get(credential_ref) is not None


_store: SystemCredentialStore | None = None


def new_credential_ref(kind: CredentialKind) -> str:
    return f"{kind}:{uuid.uuid4().hex}"


def get_system_credential_store() -> SystemCredentialStore:
    global _store
    if _store is None:
        _store = SystemCredentialStore()
    return _store


def reset_system_credential_store() -> None:
    global _store
    _store = None


def resolve_secret(inline_value: str, credential_ref: str) -> str:
    """Resolve one config secret without ever falling back to plaintext files."""
    if credential_ref:
        value = get_system_credential_store().get(credential_ref)
        if value is None:
            raise CredentialStoreError("credential_not_found")
        return value
    return inline_value


def credential_is_configured(inline_value: str, credential_ref: str) -> bool:
    if credential_ref:
        try:
            return get_system_credential_store().contains(credential_ref)
        except CredentialStoreError:
            return False
    return bool(inline_value)
