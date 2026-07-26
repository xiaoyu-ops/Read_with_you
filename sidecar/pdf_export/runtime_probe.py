"""Read-only-container probe for the pinned PDF export runtime."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path
from unittest.mock import patch

import fitz
from pdf2zh_next.high_level import create_babeldoc_config
from pdf2zh_next.translator.translator_impl.openai import OpenAITranslator

from app import MODEL_ALIAS, UPSTREAM_SOURCE, _make_settings


probe_root = Path("/work/runtime-probe")
shutil.rmtree(probe_root, ignore_errors=True)
probe_root.mkdir(parents=True)
pdf_path = probe_root / "probe.pdf"
with fitz.open() as document:
    document.new_page().insert_text((72, 72), "PDF export runtime probe")
    document.save(pdf_path)

root_mount = next(
    line.split()
    for line in Path("/proc/mounts").read_text(encoding="utf-8").splitlines()
    if len(line.split()) >= 4 and line.split()[1] == "/"
)
assert "ro" in root_mount[3].split(",")

settings = _make_settings(probe_root / "output")
settings.validate_settings()
# Upstream performs a one-word provider smoke call while constructing the
# translator. The probe validates local initialization only; do not contact a
# provider or require the backend to be running.
with patch.object(OpenAITranslator, "do_translate", return_value="你好"):
    config = create_babeldoc_config(settings, pdf_path)
cache = Path.home() / ".cache"
assert os.getuid() == 10001
assert (cache / ".pet-preloaded").is_file()
assert settings.pdf.no_dual is True
assert settings.pdf.no_mono is False
assert settings.pdf.watermark_output_mode == "watermarked"
assert settings.translation.lang_in == "en"
assert settings.translation.lang_out == "zh-CN"
assert settings.translation.no_auto_extract_glossary is True
assert settings.translate_engine_settings.openai_model == MODEL_ALIAS
assert config.input_file == pdf_path

token = os.environ["PEINIDU_PDF_EXPORT_INTERNAL_TOKEN"]
server = subprocess.Popen(
    [
        "python",
        "-m",
        "uvicorn",
        "app:app",
        "--app-dir",
        "/pet-sidecar",
        "--host",
        "127.0.0.1",
        "--port",
        "18090",
        "--log-level",
        "warning",
    ],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
try:
    for _ in range(50):
        try:
            request = urllib.request.Request(
                "http://127.0.0.1:18090/health",
                headers={"Authorization": f"Bearer {token}"},
            )
            with urllib.request.urlopen(request, timeout=1) as response:
                health = json.load(response)
            break
        except Exception:
            if server.poll() is not None:
                raise RuntimeError("sidecar HTTP process exited during probe")
            time.sleep(0.1)
    else:
        raise RuntimeError("sidecar health probe timed out")
    info_request = urllib.request.Request(
        "http://127.0.0.1:18090/info",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(info_request, timeout=2) as response:
        info = json.load(response)
    assert health["status"] == "ok"
    assert info["source"] == UPSTREAM_SOURCE
    assert info["output"] == "monolingual-watermarked-zh-CN"
finally:
    server.terminate()
    try:
        server.wait(timeout=5)
    except subprocess.TimeoutExpired:
        server.kill()
        server.wait(timeout=5)
print(
    json.dumps(
        {
            "status": "ok",
            "uid": os.getuid(),
            "read_only_runtime": True,
            "cache_preloaded": True,
            "export_initialized": True,
            "health": "ok",
            "model": MODEL_ALIAS,
        }
    )
)
shutil.rmtree(probe_root, ignore_errors=True)
