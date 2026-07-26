from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.api.main import create_app
from backend.llm.client import LLMClient
from backend.llm.config import mask_config
from backend.llm.models import AppConfig, DeepLXConfig, MinerUConfig, Provider
from backend.runtime import RuntimeMode
from backend.security import credentials
from backend.security.credentials import (
    CredentialStoreError,
    SERVICE_NAME,
    SystemCredentialStore,
)


class FakeMacBackend:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, account: str):
        return self.values.get((service, account))

    def set_password(self, service: str, account: str, value: str) -> None:
        self.values[(service, account)] = value

    def delete_password(self, service: str, account: str) -> None:
        del self.values[(service, account)]


FakeMacBackend.__module__ = "keyring.backends.macOS"


class FakeWindowsBackend(FakeMacBackend):
    pass


FakeWindowsBackend.__module__ = "keyring.backends.Windows"


class NullBackend(FakeMacBackend):
    pass


NullBackend.__module__ = "keyring.backends.null"


class BrokenWriteBackend(FakeMacBackend):
    def set_password(self, service: str, account: str, value: str) -> None:
        self.values[(service, account)] = "different"


BrokenWriteBackend.__module__ = "keyring.backends.macOS"


class SystemCredentialStoreTest(unittest.TestCase):
    def tearDown(self) -> None:
        credentials.reset_system_credential_store()

    def test_mac_store_round_trip_and_delete(self) -> None:
        backend = FakeMacBackend()
        store = SystemCredentialStore(backend, platform="darwin")
        ref = "llm:" + "a" * 32

        store.set(ref, "sk-private-value")

        self.assertEqual(store.get(ref), "sk-private-value")
        self.assertTrue(store.contains(ref))
        self.assertEqual(backend.values[(SERVICE_NAME, ref)], "sk-private-value")
        store.delete(ref)
        self.assertIsNone(store.get(ref))

    def test_fallback_backend_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            CredentialStoreError,
            "unsupported_credential_store",
        ):
            SystemCredentialStore(NullBackend(), platform="darwin")

    def test_windows_credential_manager_backend_is_accepted(self) -> None:
        store = SystemCredentialStore(FakeWindowsBackend(), platform="win32")
        ref = "deeplx:" + "e" * 32
        store.set(ref, "windows-private-value")
        self.assertEqual(store.get(ref), "windows-private-value")

    def test_failed_round_trip_is_fail_closed(self) -> None:
        store = SystemCredentialStore(BrokenWriteBackend(), platform="darwin")
        with self.assertRaisesRegex(
            CredentialStoreError,
            "credential_verification_failed",
        ):
            store.set("llm:" + "b" * 32, "sk-private-value")

    def test_invalid_reference_is_rejected(self) -> None:
        store = SystemCredentialStore(FakeMacBackend(), platform="darwin")
        with self.assertRaisesRegex(
            CredentialStoreError,
            "invalid_credential_reference",
        ):
            store.get("../../config")

    def test_mask_config_never_exposes_prefix_or_reference(self) -> None:
        backend = FakeMacBackend()
        store = SystemCredentialStore(backend, platform="darwin")
        credentials._store = store
        provider_ref = "llm:" + "c" * 32
        store.set(provider_ref, "sk-super-secret")
        config = AppConfig(
            llm_providers=[
                Provider(
                    name="test",
                    type="openai",
                    api_key_ref=provider_ref,
                    models=["test-model"],
                )
            ],
            deeplx=DeepLXConfig(api_key="deeplx-secret-value"),
            mineru=MinerUConfig(api_token="mineru-secret-value"),
        )

        masked = mask_config(config, RuntimeMode.LOCAL_CORE)

        provider = masked["llm_providers"][0]
        self.assertEqual(provider["api_key"], "••••••••")
        self.assertTrue(provider["api_key_configured"])
        self.assertNotIn("api_key_ref", provider)
        self.assertNotIn("sk-", str(masked))
        self.assertNotIn("deeplx-secret-value", str(masked))
        self.assertNotIn("mineru-secret-value", str(masked))

    def test_llm_client_resolves_provider_key_from_store(self) -> None:
        store = SystemCredentialStore(FakeMacBackend(), platform="darwin")
        credentials._store = store
        ref = "llm:" + "d" * 32
        store.set(ref, "sk-system-key")
        provider = Provider(
            name="test",
            type="openai",
            api_key_ref=ref,
            models=["test-model"],
        )
        client = LLMClient(
            AppConfig(
                llm_providers=[provider],
                default_provider="test",
                default_model="test-model",
            )
        )

        params = client._litellm_params(
            "test-model",
            provider,
            [{"role": "user", "content": "hello"}],
            temperature=None,
            stream=False,
        )

        self.assertEqual(params["api_key"], "sk-system-key")

    def test_local_config_save_moves_secret_out_of_config_model(self) -> None:
        store = SystemCredentialStore(FakeMacBackend(), platform="darwin")
        credentials._store = store
        existing = AppConfig(
            llm_providers=[
                Provider(name="test", type="openai", models=["test-model"])
            ],
            default_provider="test",
            default_model="test-model",
        )
        payload = mask_config(existing, RuntimeMode.LOCAL_CORE)
        payload["llm_providers"][0]["api_key"] = "sk-new-private-value"
        saved: list[AppConfig] = []
        app = create_app(RuntimeMode.LOCAL_CORE)

        with (
            patch("backend.api.routes_config.get_config", return_value=existing),
            patch(
                "backend.api.routes_config.get_system_credential_store",
                return_value=store,
            ),
            patch(
                "backend.api.routes_config.save_config",
                side_effect=lambda value: saved.append(value),
            ),
            patch("backend.api.routes_config._reset_config_consumers"),
            TestClient(app) as client,
        ):
            response = client.post("/config", json=payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(saved), 1)
        provider = saved[0].llm_providers[0]
        self.assertEqual(provider.api_key, "")
        self.assertTrue(provider.api_key_ref.startswith("llm:"))
        self.assertEqual(store.get(provider.api_key_ref), "sk-new-private-value")
        self.assertNotIn("sk-new-private-value", saved[0].model_dump_json())

    def test_public_portal_has_no_credential_routes(self) -> None:
        app = create_app(RuntimeMode.PUBLIC_PORTAL)
        with TestClient(app) as client:
            self.assertEqual(client.get("/config").status_code, 404)
            self.assertEqual(
                client.post(
                    "/config/credentials/delete",
                    json={"kind": "deeplx"},
                ).status_code,
                404,
            )


if __name__ == "__main__":
    unittest.main()
