from __future__ import annotations

import json
import os
import sys
import urllib.request


token = os.environ.get("PEINIDU_PDF_EXPORT_INTERNAL_TOKEN", "")
if not token:
    raise SystemExit(1)
request = urllib.request.Request(
    "http://127.0.0.1:8090/health",
    headers={"Authorization": f"Bearer {token}"},
)
try:
    with urllib.request.urlopen(request, timeout=4) as response:
        payload = json.load(response)
except Exception:
    raise SystemExit(1) from None
sys.exit(0 if payload.get("status") == "ok" else 1)
