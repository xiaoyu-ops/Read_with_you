from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


class SlimRuntimeContractTest(unittest.TestCase):
    def test_next_production_runner_uses_standalone_output(self) -> None:
        next_config = (ROOT / "frontend" / "next.config.ts").read_text()
        dockerfile = (ROOT / "frontend" / "Dockerfile").read_text()
        runner = dockerfile.split("FROM node:22-slim AS runner", maxsplit=1)[1]

        self.assertIn('output: "standalone"', next_config)
        self.assertIn("/app/.next/standalone", runner)
        self.assertIn("/app/.next/static", runner)
        self.assertIn('CMD ["node", "server.js"]', runner)
        self.assertNotIn("/app/node_modules ./node_modules", runner)
        self.assertNotIn('CMD ["npm", "run", "start"', runner)

    def test_backend_healthcheck_does_not_require_curl(self) -> None:
        dockerfile = (ROOT / "backend" / "Dockerfile").read_text()
        compose = (ROOT / "docker-compose.yml").read_text()

        self.assertNotIn("poppler-utils curl", dockerfile)
        self.assertIn("urllib.request.urlopen", compose)
        self.assertNotIn('"CMD", "curl"', compose)

    def test_default_core_services_fit_the_two_gib_memory_envelope(self) -> None:
        compose = yaml.safe_load(
            (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        )
        services = compose["services"]
        expected_limits = {
            "backend": "1g",
            "frontend": "512m",
            "nginx": "128m",
        }
        multipliers = {"m": 1024**2, "g": 1024**3}

        total = 0
        for service_name, expected in expected_limits.items():
            service = services[service_name]
            self.assertNotIn("profiles", service)
            self.assertEqual(service["mem_limit"], expected)
            total += int(expected[:-1]) * multipliers[expected[-1]]

        self.assertLessEqual(total, 2 * 1024**3)
        self.assertEqual(services["browser"]["profiles"], ["browser"])
        self.assertEqual(services["pdf-export"]["profiles"], ["pdf-export"])

    def test_generated_frontend_artifacts_stay_out_of_build_context(self) -> None:
        dockerignore = (ROOT / ".dockerignore").read_text().splitlines()

        self.assertIn("frontend/.next*", dockerignore)
        self.assertIn("frontend/node_modules", dockerignore)
        self.assertIn("frontend/private", dockerignore)

    def test_litellm_import_is_deferred_until_the_first_model_call(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; import backend.llm.client; "
                "raise SystemExit(1 if 'litellm' in sys.modules else 0)",
            ],
            cwd=ROOT,
            check=False,
        )

        self.assertEqual(completed.returncode, 0)


if __name__ == "__main__":
    unittest.main()
