from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.api.main import app
from backend.llm import client as client_module
from backend.llm.client import LLMClient, reset_client
from backend.llm.config import load_config, mask_config, reset_config, save_config
from backend.llm.models import AppConfig, Provider


class ConfigPersistenceTest(unittest.TestCase):
    def tearDown(self) -> None:
        reset_config()
        reset_client()

    def test_save_config_preserves_env_key_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.yaml"
            config_path.write_text(
                """
llm_providers:
  - name: deepseek-official
    type: deepseek
    api_key: ${DEEPSEEK_API_KEY}
    models: [deepseek-chat]
default_provider: deepseek-official
default_model: deepseek-chat
""".strip(),
                encoding="utf-8",
            )

            with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "sk-secret-test-key"}):
                config = load_config(config_path)
                self.assertEqual(config.llm_providers[0].api_key, "sk-secret-test-key")
                save_config(config, config_path)

            saved = config_path.read_text(encoding="utf-8")
            self.assertIn("${DEEPSEEK_API_KEY}", saved)
            self.assertNotIn("sk-secret-test-key", saved)

    def test_save_config_preserves_mineru_token_env_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.yaml"
            config_path.write_text(
                """
mineru:
  enabled: true
  api_token: ${MINERU_API_TOKEN}
""".strip(),
                encoding="utf-8",
            )

            with patch.dict("os.environ", {"MINERU_API_TOKEN": "mineru-secret-token"}):
                config = load_config(config_path)
                self.assertEqual(config.mineru.api_token, "mineru-secret-token")
                masked = mask_config(config)
                self.assertEqual(masked["mineru"]["api_token"], "••••••••")
                self.assertTrue(masked["mineru"]["api_token_configured"])
                save_config(config, config_path)

            saved = config_path.read_text(encoding="utf-8")
            self.assertIn("${MINERU_API_TOKEN}", saved)
            self.assertNotIn("mineru-secret-token", saved)

    def test_reset_client_clears_cached_llm_config(self) -> None:
        config = AppConfig(
            llm_providers=[Provider(name="deepseek-official", type="deepseek", models=["deepseek-v4-flash"])],
            default_provider="deepseek-official",
            default_model="deepseek-v4-flash",
        )
        client_module._client = LLMClient(config)

        reset_client()

        self.assertIsNone(client_module._client)

    def test_reasoner_params_skip_temperature(self) -> None:
        provider = Provider(name="deepseek-official", type="deepseek", models=["deepseek-v4-pro"])
        config = AppConfig(
            llm_providers=[provider],
            default_provider="deepseek-official",
            default_model="deepseek-v4-pro",
        )
        client = LLMClient(config)

        params = client._litellm_params(
            "deepseek-v4-pro",
            provider,
            [{"role": "user", "content": "hi"}],
            temperature=0.7,
            stream=False,
        )

        self.assertEqual(params["model"], "deepseek/deepseek-v4-pro")
        self.assertNotIn("temperature", params)

    def test_config_routes_require_admin_token_when_configured(self) -> None:
        with patch.dict("os.environ", {"PEINIDU_ADMIN_TOKEN": "test-admin-token"}):
            with TestClient(app) as client:
                missing = client.get("/config")
                wrong = client.get("/config", headers={"X-Peinidu-Admin-Token": "wrong"})
                ok = client.get("/config", headers={"X-Peinidu-Admin-Token": "test-admin-token"})
                models = client.post(
                    "/config/models",
                    json={"base_url": "https://api.example.com", "api_key": "sk-test"},
                )

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(models.status_code, 401)


if __name__ == "__main__":
    unittest.main()
