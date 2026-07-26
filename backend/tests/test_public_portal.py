from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from backend.api import main as api_main
from backend.api.routes_search import SearchResponse
from backend.telemetry import portal_store


class PublicPortalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manifest = self.root / "release.json"
        self.manifest.write_text(
            json.dumps(
                {
                    "version": "0.2.0",
                    "downloads": {
                        "macos_arm64": {
                            "url": "https://downloads.example.com/peinidu-mac.zip",
                            "sha256": "a" * 64,
                            "size_bytes": 1024,
                        },
                        "windows_x64": {
                            "url": "https://downloads.example.com/peinidu-win.zip",
                            "sha256": "b" * 64,
                            "size_bytes": 2048,
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        self.env = patch.dict(
            "os.environ",
            {
                "PEINIDU_PORTAL_DATA_DIR": str(self.root / "portal-data"),
                "PEINIDU_RELEASE_MANIFEST": str(self.manifest),
                "PEINIDU_RATE_LIMIT_PER_MINUTE": "0",
            },
            clear=False,
        )
        self.env.start()
        self.app = api_main.create_app("public_portal")
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.client.close()
        self.env.stop()
        self.temp.cleanup()

    def _event(self, **overrides):
        payload = {
            "event_date": datetime.now(timezone.utc).date().isoformat(),
            "daily_id": "c" * 64,
            "event": "core_started",
            "platform": "macos",
            "app_version": "0.2.0",
        }
        payload.update(overrides)
        return payload

    def test_portal_home_shows_fixed_product_demo_and_local_core_entry(self) -> None:
        home = self.client.get("/")
        privacy = self.client.get("/privacy")
        mascot = self.client.get("/api/portal/mascot.png")

        self.assertEqual(home.status_code, 200)
        self.assertIn("网页是入口，论文仍在你的电脑里", home.text)
        self.assertIn('id="product-demo"', home.text)
        self.assertIn("产品快速体验", home.text)
        self.assertIn("Attention Is All You Need", home.text)
        self.assertIn("arXiv:1706.03762", home.text)
        self.assertIn("固定公开样例，译文、笔记与分析结果均为预生成内容", home.text)
        self.assertIn("第 1 页摘要已完整预翻译", home.text)
        self.assertIn("data-demo-text-layer", home.text)
        self.assertIn("data-demo-selection-original", home.text)
        self.assertIn("data-demo-selection-translation", home.text)
        self.assertIn("matchesForSelection", home.text)
        self.assertIn("没有调用模型或上传选区", home.text)
        self.assertIn("当前主流的序列转换模型", home.text)
        self.assertIn("表现最好的模型还会通过注意力机制", home.text)
        self.assertIn("我们提出一种全新的简洁网络架构 Transformer", home.text)
        self.assertIn("两项机器翻译任务的实验表明", home.text)
        self.assertIn("WMT 2014 英德翻译任务", home.text)
        self.assertIn("WMT 2014 英法翻译任务", home.text)
        self.assertIn("英语成分句法分析", home.text)
        self.assertNotIn("点击这句原文", home.text)
        self.assertIn("这篇论文的信息足够复现吗？", home.text)
        self.assertIn("部分可复现", home.text)
        self.assertIn("论文正文未提供官方代码仓库", home.text)
        self.assertIn("如果这个方向对你有帮助，欢迎 Star", home.text)
        self.assertIn("https://github.com/xiaoyu-ops/Read_with_you", home.text)
        self.assertIn("先启动，再打开", home.text)
        self.assertIn("前往 GitHub 安装 / 启动", home.text)
        self.assertIn("https://github.com/xiaoyu-ops/Read_with_you#本地启动", home.text)
        self.assertIn("http://127.0.0.1:8520/portal-probe", home.text)
        self.assertIn('id="open-core"', home.text)
        self.assertIn('id="retry-core"', home.text)
        self.assertIn("网页只能确认本机", home.text)
        self.assertIn("不能静默读取电脑里是否装过应用", home.text)
        self.assertIn("即使检测被浏览器拦截，也可以直接尝试打开本地工作台", home.text)
        self.assertIn('targetAddressSpace:"local"', home.text)
        self.assertIn('/api/portal/mascot.png', home.text)
        self.assertNotIn("PRIVACY RECEIPT", home.text)
        self.assertNotIn("统计先征得同意", home.text)
        self.assertNotIn("已同意统计", home.text)
        self.assertNotIn("默认关闭", home.text)
        self.assertNotIn("安装包尚未开放", home.text)
        self.assertNotIn("不需要安装，也不需要登录", home.text)
        self.assertIn("不需要网站账号", home.text)
        self.assertIn('target="_blank" rel="noopener noreferrer"', home.text)
        self.assertEqual(home.headers["Referrer-Policy"], "no-referrer")
        self.assertIn("frame-ancestors 'none'", home.headers["Content-Security-Policy"])
        self.assertEqual(mascot.status_code, 200)
        self.assertEqual(mascot.headers["content-type"], "image/png")
        self.assertEqual(privacy.status_code, 200)
        self.assertIn("你的论文不是我们的数据", privacy.text)
        self.assertIn("默认匿名计数", privacy.text)
        self.assertIn("不会写入匿名使用统计", privacy.text)
        self.assertNotIn("你主动开启统计后", privacy.text)
        for path in (
            "/papers",
            "/config",
            "/telemetry/settings",
            "/agent/chat/example",
            "/assets/example/original.pdf",
            "/openapi.json",
        ):
            self.assertEqual(self.client.get(path).status_code, 404, path)

    def test_demo_assets_are_fixed_immutable_and_public_portal_only(self) -> None:
        total_size = 0
        for name in ("attention-p1-v1.webp", "attention-p7-v1.webp"):
            response = self.client.get(f"/api/portal/demo-assets/{name}")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["content-type"], "image/webp")
            self.assertEqual(
                response.headers["cache-control"],
                "public, max-age=31536000, immutable",
            )
            self.assertEqual(response.headers["x-content-type-options"], "nosniff")
            total_size += len(response.content)
        self.assertLessEqual(total_size, 300_000)

        for path in (
            "/api/portal/demo-assets/not-allowed.webp",
            "/api/portal/demo-assets/%2E%2E%2Fattention-p1-v1.webp",
            "/api/portal/demo-assets/folder/attention-p1-v1.webp",
        ):
            self.assertEqual(self.client.get(path).status_code, 404, path)

        for mode in ("self_hosted", "local_core"):
            with TestClient(api_main.create_app(mode)) as client:
                self.assertEqual(
                    client.get(
                        "/api/portal/demo-assets/attention-p1-v1.webp"
                    ).status_code,
                    404,
                    mode,
                )

    def test_portal_home_does_not_depend_on_release_manifest(self) -> None:
        with patch(
            "backend.api.routes_public_portal.load_release_manifest",
            return_value=None,
        ):
            home = self.client.get("/")

        self.assertEqual(home.status_code, 200)
        self.assertNotIn("安装包尚未开放", home.text)
        self.assertNotIn("当前为开发预览", home.text)
        self.assertNotIn("/api/portal/download/macos_arm64", home.text)
        self.assertIn("前往 GitHub 安装 / 启动", home.text)
        self.assertIn("网页只能确认本机", home.text)

    def test_public_search_and_map_are_explicit_metadata_allowlist(self) -> None:
        candidate = {
            "arxiv_id": "1706.03762",
            "paper_id": "a" * 40,
            "title": "Attention Is All You Need",
            "authors": ["Ashish Vaswani"],
            "abstract": "Transformer architecture.",
            "url": "https://arxiv.org/abs/1706.03762",
            "source": "arxiv+semantic_scholar",
        }
        graph = {
            "version": 1,
            "origin": {"id": "a" * 40, "title": candidate["title"]},
            "nodes": [{"id": "a" * 40, "title": candidate["title"]}],
            "edges": [],
            "prior_works": [],
            "derivative_works": [],
            "status": "complete",
            "provider": "semantic_scholar",
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "cached": False,
            "stale": False,
            "warnings": [],
        }
        with (
            patch(
                "backend.api.routes_public_portal.search_papers",
                new=AsyncMock(
                    return_value=SearchResponse(
                        query=candidate["title"],
                        candidates=[candidate],
                        count=1,
                    )
                ),
            ) as search,
            patch(
                "backend.api.routes_public_portal.get_literature_map",
                new=AsyncMock(return_value=graph),
            ) as literature_map,
        ):
            search_response = self.client.post(
                "/api/portal/search",
                json={"query": candidate["title"]},
            )
            map_response = self.client.get(
                f"/api/portal/literature-map/{'a' * 40}"
            )

        self.assertEqual(search_response.status_code, 200)
        self.assertEqual(search_response.json()["candidates"][0]["paper_id"], "a" * 40)
        search.assert_awaited_once_with(
            candidate["title"],
            max_results=10,
            use_cache=False,
        )
        self.assertEqual(map_response.status_code, 200)
        self.assertEqual(map_response.json()["provider"], "semantic_scholar")
        literature_map.assert_awaited_once_with("a" * 40, max_nodes=40)

        page = self.client.get(f"/literature-map/{'a' * 40}")
        self.assertEqual(page.status_code, 200)
        for label in ("图谱", "先行工作", "后续工作", "列表", "筛选", "引用关系"):
            self.assertIn(label, page.text)
        self.assertEqual(
            self.client.get("/literature-map/not-a-paper").status_code,
            404,
        )

    def test_release_metadata_hides_origin_url_and_download_counts(self) -> None:
        release = self.client.get("/api/portal/releases/latest")
        self.assertEqual(release.status_code, 200)
        text = release.text
        self.assertNotIn("downloads.example.com", text)
        self.assertEqual(len(release.json()["downloads"]), 2)

        download = self.client.get(
            "/api/portal/download/macos_arm64",
            follow_redirects=False,
        )
        self.assertEqual(download.status_code, 307)
        self.assertEqual(
            download.headers["location"],
            "https://downloads.example.com/peinidu-mac.zip",
        )
        self.assertEqual(self.client.get("/api/portal/stats").json()["total_downloads"], 1)

    def test_telemetry_is_strict_daily_deduplicated_and_aggregated(self) -> None:
        first = self.client.post("/api/portal/telemetry", json=self._event())
        duplicate = self.client.post("/api/portal/telemetry", json=self._event())
        reader = self.client.post(
            "/api/portal/telemetry",
            json=self._event(event="reader_opened"),
        )
        portal = self.client.post(
            "/api/portal/telemetry",
            json=self._event(
                event="portal_visited",
                platform="web",
                daily_id="d" * 64,
                app_version="portal",
            ),
        )
        stats = self.client.get("/api/portal/stats").json()

        self.assertEqual(first.json(), {"status": "recorded"})
        self.assertEqual(duplicate.json(), {"status": "duplicate"})
        self.assertEqual(reader.json(), {"status": "recorded"})
        self.assertEqual(portal.json(), {"status": "recorded"})
        self.assertEqual(stats["active_today"], 2)
        self.assertEqual(stats["portal_active_today"], 1)
        self.assertEqual(stats["core_active_today"], 1)
        self.assertEqual(stats["readers_today"], 1)
        self.assertFalse(stats["privacy"]["cross_day_identifier"])
        self.assertFalse(stats["privacy"]["content_collected"])

    def test_telemetry_rejects_content_and_invalid_identity(self) -> None:
        with_content = self._event(paper_title="secret paper")
        invalid_id = self._event(daily_id="device-123")
        old_date = self._event(
            event_date=(datetime.now(timezone.utc).date() - timedelta(days=3)).isoformat()
        )

        self.assertEqual(
            self.client.post("/api/portal/telemetry", json=with_content).status_code,
            422,
        )
        self.assertEqual(
            self.client.post("/api/portal/telemetry", json=invalid_id).status_code,
            422,
        )
        self.assertEqual(
            self.client.post("/api/portal/telemetry", json=old_date).status_code,
            422,
        )

    def test_database_has_no_ip_user_agent_or_content_columns(self) -> None:
        self.client.post("/api/portal/telemetry", json=self._event())
        with closing(sqlite3.connect(portal_store.database_path())) as connection:
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(daily_events)").fetchall()
            }
            stored = connection.execute(
                "SELECT daily_id, event, platform, app_version FROM daily_events"
            ).fetchone()

        self.assertEqual(
            columns,
            {
                "event_date",
                "daily_id",
                "event",
                "platform",
                "app_version",
                "received_at",
            },
        )
        self.assertEqual(stored[0], "c" * 64)
        self.assertFalse(
            {"ip", "user_agent", "paper_id", "title", "path", "prompt", "key"} & columns
        )

    def test_invalid_release_manifest_fails_closed(self) -> None:
        self.manifest.write_text(
            json.dumps(
                {
                    "version": "0.2.0",
                    "downloads": {
                        "macos_arm64": {
                            "url": "http://insecure.example.com/app.zip",
                            "sha256": "a" * 64,
                            "size_bytes": 1024,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual(
            self.client.get("/api/portal/releases/latest").status_code,
            503,
        )
        self.assertEqual(
            self.client.get(
                "/api/portal/download/macos_arm64",
                follow_redirects=False,
            ).status_code,
            404,
        )


if __name__ == "__main__":
    unittest.main()
