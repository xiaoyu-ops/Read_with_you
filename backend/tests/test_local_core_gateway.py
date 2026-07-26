from __future__ import annotations

import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.local_core.gateway import create_local_core_gateway


class LocalCoreGatewayTest(unittest.TestCase):
    def setUp(self) -> None:
        content_app = FastAPI()
        self.mutation_count = 0

        @content_app.get("/health")
        async def health():
            return {"status": "ok"}

        @content_app.post("/mutate")
        async def mutate():
            self.mutation_count += 1
            return {"status": "saved"}

        self.gateway = create_local_core_gateway(
            content_app=content_app,
            gateway_port=8520,
            paper_assets_dir=None,
        )

    def client(self, **kwargs) -> TestClient:
        return TestClient(
            self.gateway,
            base_url="http://127.0.0.1:8520",
            client=("127.0.0.1", 43125),
            **kwargs,
        )

    def test_same_origin_api_and_security_headers(self) -> None:
        with self.client() as client:
            response = client.get(
                "/api/health",
                headers={"sec-fetch-site": "same-origin"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        self.assertEqual(response.headers["x-frame-options"], "DENY")
        self.assertEqual(
            response.headers["cross-origin-resource-policy"],
            "same-origin",
        )

    def test_invalid_host_is_rejected(self) -> None:
        with self.client() as client:
            response = client.get("/api/health", headers={"host": "evil.test"})

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.json()["detail"]["code"],
            "local_core_request_rejected",
        )

    def test_non_loopback_peer_is_rejected(self) -> None:
        with TestClient(
            self.gateway,
            base_url="http://127.0.0.1:8520",
            client=("192.0.2.8", 43125),
        ) as client:
            response = client.get("/api/health")

        self.assertEqual(response.status_code, 403)

    def test_cross_site_api_read_is_rejected(self) -> None:
        with self.client() as client:
            response = client.get(
                "/api/health",
                headers={
                    "origin": "https://evil.test",
                    "sec-fetch-site": "cross-site",
                    "sec-fetch-mode": "cors",
                },
            )

        self.assertEqual(response.status_code, 403)

    def test_public_portal_can_probe_core_without_accessing_content_api(self) -> None:
        headers = {
            "origin": "https://readwithyou.xiaoyu666.cyou",
            "sec-fetch-site": "cross-site",
            "sec-fetch-mode": "cors",
        }
        with self.client() as client:
            probe = client.get("/portal-probe", headers=headers)
            content = client.get("/api/health", headers=headers)

        self.assertEqual(probe.status_code, 200)
        self.assertEqual(
            probe.json(),
            {"status": "ok", "runtime_mode": "local_core"},
        )
        self.assertEqual(
            probe.headers["access-control-allow-origin"],
            "https://readwithyou.xiaoyu666.cyou",
        )
        self.assertEqual(probe.headers["cache-control"], "no-store")
        self.assertEqual(content.status_code, 403)

    def test_public_portal_probe_supports_private_network_preflight(self) -> None:
        with self.client() as client:
            response = client.options(
                "/portal-probe",
                headers={
                    "origin": "https://readwithyou.xiaoyu666.cyou",
                    "sec-fetch-site": "cross-site",
                    "sec-fetch-mode": "cors",
                    "access-control-request-method": "GET",
                    "access-control-request-private-network": "true",
                },
            )

        self.assertEqual(response.status_code, 204)
        self.assertEqual(
            response.headers["access-control-allow-origin"],
            "https://readwithyou.xiaoyu666.cyou",
        )
        self.assertEqual(
            response.headers["access-control-allow-private-network"],
            "true",
        )

    def test_public_portal_probe_rejects_unlisted_origin(self) -> None:
        with self.client() as client:
            response = client.get(
                "/portal-probe",
                headers={
                    "origin": "https://evil.test",
                    "sec-fetch-site": "cross-site",
                    "sec-fetch-mode": "cors",
                },
            )

        self.assertEqual(response.status_code, 403)

    def test_cross_site_mutation_is_rejected(self) -> None:
        with self.client() as client:
            response = client.post(
                "/api/mutate",
                headers={
                    "origin": "https://evil.test",
                    "sec-fetch-site": "cross-site",
                    "sec-fetch-mode": "cors",
                },
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.mutation_count, 0)

    def test_same_origin_mutation_is_allowed(self) -> None:
        with self.client() as client:
            response = client.post(
                "/api/mutate",
                headers={
                    "origin": "http://127.0.0.1:8520",
                    "sec-fetch-site": "same-origin",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.mutation_count, 1)

    def test_cross_site_top_level_navigation_can_open_workspace(self) -> None:
        with patch(
            "backend.local_core.gateway.httpx.AsyncClient.send",
            side_effect=httpx.ConnectError("frontend unavailable"),
        ):
            with self.client() as client:
                response = client.get(
                    "/",
                    headers={
                        "sec-fetch-site": "cross-site",
                        "sec-fetch-mode": "navigate",
                    },
                )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["detail"]["code"],
            "local_frontend_unavailable",
        )


if __name__ == "__main__":
    unittest.main()
