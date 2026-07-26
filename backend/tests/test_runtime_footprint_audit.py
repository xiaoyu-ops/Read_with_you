from __future__ import annotations

import importlib.util
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = ROOT / "scripts" / "audit_runtime_footprint.py"
SPEC = importlib.util.spec_from_file_location("audit_runtime_footprint", SCRIPT_PATH)
assert SPEC and SPEC.loader
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


class RuntimeFootprintAuditTests(unittest.TestCase):
    def test_parse_size_supports_docker_units(self) -> None:
        self.assertEqual(audit.parse_size("517.5MiB"), int(517.5 * 1024**2))
        self.assertEqual(audit.parse_size("1.2 GB"), int(1.2 * 1000**3))
        self.assertEqual(audit.parse_size("0B"), 0)
        with self.assertRaises(ValueError):
            audit.parse_size("unknown")

    def test_budget_gate_passes_complete_lightweight_snapshot(self) -> None:
        snapshot = {
            "images": {
                "backend": {"available": True, "size_bytes": 700 * audit.MIB},
                "frontend": {"available": True, "size_bytes": 350 * audit.MIB},
                "nginx": {"available": True, "size_bytes": 50 * audit.MIB},
            },
            "default_image_total_bytes": 1100 * audit.MIB,
            "runtime": {"idle_memory_total_bytes": 200 * audit.MIB},
            "experience": {
                "first_readable_page_ms": 2200,
                "selection_translation_ms": 990,
                "pet_first_sse_ms": 495,
                "agent_first_sse_ms": 1100,
            },
        }
        checks = audit.evaluate_budgets(
            snapshot,
            baseline_experience={
                "selection_translation_ms": 900,
                "pet_first_sse_ms": 450,
                "agent_first_sse_ms": 1000,
            },
        )
        self.assertTrue(all(item["status"] == "pass" for item in checks))

    def test_budget_gate_fails_missing_or_regressed_metrics(self) -> None:
        snapshot = {
            "images": {
                "backend": {"available": False},
                "frontend": {"available": True, "size_bytes": 500 * audit.MIB},
                "nginx": {"available": True, "size_bytes": 50 * audit.MIB},
            },
            "default_image_total_bytes": None,
            "runtime": {"idle_memory_total_bytes": None},
            "experience": {
                "first_readable_page_ms": 2600,
                "selection_translation_ms": 1200,
                "pet_first_sse_ms": 600,
                "agent_first_sse_ms": 1300,
            },
        }
        checks = audit.evaluate_budgets(
            snapshot,
            baseline_experience={
                "selection_translation_ms": 900,
                "pet_first_sse_ms": 450,
                "agent_first_sse_ms": 1000,
            },
        )
        failed = {item["name"] for item in checks if item["status"] == "fail"}
        self.assertIn("image_backend", failed)
        self.assertIn("image_frontend", failed)
        self.assertIn("image_default_total", failed)
        self.assertIn("runtime_idle_memory", failed)
        self.assertIn("experience_first_readable_page", failed)
        self.assertIn("experience_selection_translation", failed)

    def test_cgroup_fallback_excludes_inactive_file_cache(self) -> None:
        responses = [
            subprocess.CompletedProcess([], 0, stdout="262144000\n", stderr=""),
            subprocess.CompletedProcess(
                [],
                0,
                stdout="anon 104857600\ninactive_file 52428800\n",
                stderr="",
            ),
        ]
        with patch.object(audit, "_run", side_effect=responses):
            memory = audit._cgroup_memory_bytes("container-id")

        self.assertEqual(memory, 209715200)


if __name__ == "__main__":
    unittest.main()
