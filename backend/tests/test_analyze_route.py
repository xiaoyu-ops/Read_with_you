from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from backend.api import routes_analyze


class AnalyzeRouteTest(unittest.TestCase):
    def test_run_analysis_reuses_cached_result_by_default(self) -> None:
        cached = {
            "summary": "Cached summary.",
            "reproducibility": None,
            "improvements": [],
            "highlights": [],
        }

        with (
            patch.object(routes_analyze, "load_document", return_value=object()),
            patch.object(routes_analyze, "load_analysis", return_value=cached),
            patch.object(routes_analyze, "analyze_paper", new_callable=AsyncMock) as analyze,
        ):
            response = asyncio.run(routes_analyze.run_analysis("1706.03762", force=False))

        self.assertEqual(response.summary, "Cached summary.")
        analyze.assert_not_awaited()

    def test_run_analysis_rejects_duplicate_running_task(self) -> None:
        with (
            patch.object(routes_analyze, "load_document", return_value=object()),
            patch.object(routes_analyze, "load_analysis", return_value=None),
            patch.object(routes_analyze, "try_create_agent_task", new=AsyncMock(return_value=(12, False))),
            patch.object(routes_analyze, "analyze_paper", new_callable=AsyncMock) as analyze,
        ):
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(routes_analyze.run_analysis("1706.03762", force=False))

        self.assertEqual(caught.exception.status_code, 409)
        analyze.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
