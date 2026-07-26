from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from backend.api import main as api_main
from backend.api import routes_pdf_exports


def _request(
    *,
    client: str = "198.51.100.9",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Request:
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/papers/1706.03762/pdf-exports",
            "raw_path": b"/papers/1706.03762/pdf-exports",
            "query_string": b"",
            "root_path": "",
            "headers": headers or [(b"host", b"reader.example")],
            "client": (client, 41234),
            "server": ("backend", 8000),
        }
    )


class TrustedClientIdentityTest(unittest.TestCase):
    def test_forwarded_for_is_ignored_without_an_explicit_trusted_proxy(self) -> None:
        request = _request(
            headers=[
                (b"host", b"reader.example"),
                (b"x-forwarded-for", b"203.0.113.7"),
            ]
        )
        with patch.dict(os.environ, {"PEINIDU_TRUSTED_PROXY_IPS": ""}):
            self.assertEqual(api_main._client_ip(request), "198.51.100.9")

    def test_trusted_proxy_accepts_only_one_valid_forwarded_ip(self) -> None:
        trusted = _request(
            client="10.0.0.8",
            headers=[
                (b"host", b"reader.example"),
                (b"x-forwarded-for", b"203.0.113.7"),
            ],
        )
        appended = _request(
            client="10.0.0.8",
            headers=[
                (b"host", b"reader.example"),
                (b"x-forwarded-for", b"203.0.113.7, 198.51.100.2"),
            ],
        )
        duplicated = _request(
            client="10.0.0.8",
            headers=[
                (b"host", b"reader.example"),
                (b"x-forwarded-for", b"203.0.113.7"),
                (b"x-forwarded-for", b"198.51.100.2"),
            ],
        )
        with patch.dict(
            os.environ,
            {"PEINIDU_TRUSTED_PROXY_IPS": "10.0.0.0/24"},
        ):
            self.assertEqual(api_main._client_ip(trusted), "203.0.113.7")
            self.assertEqual(api_main._client_ip(appended), "10.0.0.8")
            self.assertEqual(api_main._client_ip(duplicated), "10.0.0.8")


class PdfExportOriginGateTest(unittest.TestCase):
    def test_same_or_explicit_frontend_origin_is_allowed(self) -> None:
        same_origin = _request(
            headers=[
                (b"host", b"reader.example"),
                (b"origin", b"https://reader.example"),
                (b"sec-fetch-site", b"same-origin"),
            ]
        )
        configured_frontend = _request(
            headers=[
                (b"host", b"api.example"),
                (b"origin", b"https://reader.example"),
                (b"sec-fetch-site", b"same-site"),
            ]
        )
        with patch.dict(
            os.environ,
            {"PEINIDU_CORS_ORIGINS": "https://reader.example"},
        ):
            self.assertTrue(api_main._pdf_export_origin_allowed(same_origin))
            self.assertTrue(api_main._pdf_export_origin_allowed(configured_frontend))

    def test_cross_origin_and_browser_request_without_origin_are_rejected(self) -> None:
        cross_origin = _request(
            headers=[
                (b"host", b"reader.example"),
                (b"origin", b"https://attacker.example"),
                (b"sec-fetch-site", b"cross-site"),
            ]
        )
        missing_origin = _request(
            headers=[
                (b"host", b"reader.example"),
                (b"sec-fetch-site", b"same-site"),
            ]
        )
        self.assertFalse(api_main._pdf_export_origin_allowed(cross_origin))
        self.assertFalse(api_main._pdf_export_origin_allowed(missing_origin))

    def test_non_browser_internal_and_test_clients_remain_compatible(self) -> None:
        self.assertTrue(api_main._pdf_export_origin_allowed(_request()))


class PdfExportMutationMiddlewareTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        api_main._RATE_LIMIT_STATE.clear()

    async def asyncTearDown(self) -> None:
        api_main._RATE_LIMIT_STATE.clear()

    async def test_cross_origin_request_is_stopped_before_the_route(self) -> None:
        request = _request(
            headers=[
                (b"host", b"reader.example"),
                (b"origin", b"https://attacker.example"),
                (b"sec-fetch-site", b"cross-site"),
            ]
        )
        call_next = AsyncMock(return_value=JSONResponse({"ok": True}))

        response = await api_main.rate_limit_middleware(request, call_next)

        self.assertEqual(response.status_code, 403)
        call_next.assert_not_awaited()

    async def test_spoofed_forwarded_ips_cannot_evade_mutation_rate_limit(self) -> None:
        first = _request(
            headers=[
                (b"host", b"reader.example"),
                (b"x-forwarded-for", b"203.0.113.1"),
            ]
        )
        second = _request(
            headers=[
                (b"host", b"reader.example"),
                (b"x-forwarded-for", b"203.0.113.2"),
            ]
        )

        async def call_next(_: Request) -> JSONResponse:
            return JSONResponse({"ok": True})

        with patch.dict(
            os.environ,
            {
                "PEINIDU_TRUSTED_PROXY_IPS": "",
                "PEINIDU_PDF_EXPORT_RATE_LIMIT_PER_MINUTE": "1",
                "PEINIDU_RATE_LIMIT_PER_MINUTE": "0",
            },
        ):
            allowed = await api_main.rate_limit_middleware(first, call_next)
            blocked = await api_main.rate_limit_middleware(second, call_next)

        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(blocked.headers["retry-after"], "60")


class PdfExportActiveRunLimitTest(unittest.IsolatedAsyncioTestCase):
    async def test_create_and_cancel_require_configured_admin_token(self) -> None:
        app = FastAPI()
        app.include_router(routes_pdf_exports.router)
        with (
            patch.dict(
                os.environ,
                {
                    "PEINIDU_ADMIN_TOKEN": "server-only-token",
                    "PEINIDU_PDF_EXPORT_MAX_ACTIVE_RUNS": "1",
                },
            ),
            patch.object(
                routes_pdf_exports,
                "list_active_pdf_export_runs",
                AsyncMock(
                    return_value=[
                        {"id": "active", "arxiv_id": "another-paper"}
                    ]
                ),
            ),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                create = await client.post("/papers/1706.03762/pdf-exports")
                cancel = await client.post(
                    "/papers/1706.03762/pdf-exports/run-1/cancel"
                )
                authorized = await client.post(
                    "/papers/1706.03762/pdf-exports",
                    headers={
                        "X-Peinidu-Admin-Token": "server-only-token"
                    },
                )

        self.assertEqual(create.status_code, 401)
        self.assertEqual(cancel.status_code, 401)
        self.assertEqual(authorized.status_code, 429)

    async def test_global_active_run_limit_rejects_a_different_paper(self) -> None:
        active = [{"id": "active", "arxiv_id": "existing-paper"}]
        with (
            patch.dict(
                os.environ,
                {"PEINIDU_PDF_EXPORT_MAX_ACTIVE_RUNS": "1"},
            ),
            patch.object(
                routes_pdf_exports,
                "list_active_pdf_export_runs",
                AsyncMock(return_value=active),
            ),
            patch.object(
                routes_pdf_exports,
                "create_pdf_export_run",
                AsyncMock(),
            ) as create,
        ):
            with self.assertRaises(HTTPException) as failed:
                await routes_pdf_exports.create_pdf_export("new-paper")

        self.assertEqual(failed.exception.status_code, 429)
        self.assertEqual(failed.exception.detail["code"], "export_capacity_reached")
        create.assert_not_awaited()


class WrapperSourceBundleTest(unittest.TestCase):
    def test_extracted_archive_runs_the_static_verifier(self) -> None:
        archive_bytes = routes_pdf_exports._build_wrapper_source_archive()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
                archive.extractall(root)
            completed = subprocess.run(
                [sys.executable, "-S", "scripts/verify_pdf_export_sidecar.py"],
                cwd=root,
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("verification passed", completed.stdout)

    def test_production_archive_uses_the_attested_baked_wrapper_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "app"
            baked = root / "pdf_export_wrapper_source"
            for archive_path in routes_pdf_exports._WRAPPER_SOURCE_FILES:
                source = root / archive_path
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_bytes(f"repository:{archive_path}".encode())
            for archive_path in routes_pdf_exports._WRAPPER_SOURCE_FILES:
                if not archive_path.startswith(
                    routes_pdf_exports._WRAPPER_ARCHIVE_PREFIX
                ):
                    continue
                relative = archive_path.removeprefix(
                    routes_pdf_exports._WRAPPER_ARCHIVE_PREFIX
                )
                source = baked / relative
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_bytes(f"baked:{relative}".encode())

            with (
                patch.object(routes_pdf_exports, "_REPOSITORY_ROOT", root),
                patch.object(
                    routes_pdf_exports,
                    "_BAKED_WRAPPER_SOURCE_ROOT",
                    baked,
                ),
            ):
                archive_bytes = routes_pdf_exports._build_wrapper_source_archive(root)

            with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
                self.assertEqual(
                    archive.read("sidecar/pdf_export/app.py"),
                    b"baked:app.py",
                )
                self.assertEqual(
                    archive.read("deploy/nginx.conf"),
                    b"repository:deploy/nginx.conf",
                )


if __name__ == "__main__":
    unittest.main()
