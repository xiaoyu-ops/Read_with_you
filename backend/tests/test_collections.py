from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.storage import db as db_module


class CollectionsStorageTest(unittest.TestCase):
    def test_collection_paper_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "papers.db"
            with patch.object(db_module, "DB_PATH", db_path):
                asyncio.run(_exercise_collection_flow())


async def _exercise_collection_flow() -> None:
    await db_module.init_db()
    await db_module.insert_paper(
        arxiv_id="1706.03762",
        title="Attention Is All You Need",
        authors=["A. Vaswani"],
        source="ar5iv",
        file_path="/tmp/1706.03762",
    )

    first = await db_module.create_collection("Transformer 基础")
    second = await db_module.create_collection("Transformer 基础")
    assert first["id"] == second["id"]

    await db_module.add_paper_to_collection(first["id"], "1706.03762")
    collections = await db_module.list_collections(arxiv_id="1706.03762")
    assert len(collections) == 1
    assert collections[0]["paper_count"] == 1
    assert bool(collections[0]["contains_paper"]) is True

    detail = await db_module.get_collection(first["id"])
    assert detail is not None
    assert detail["papers"][0]["arxiv_id"] == "1706.03762"
    assert detail["papers"][0]["authors"] == ["A. Vaswani"]

    await db_module.remove_paper_from_collection(first["id"], "1706.03762")
    detail = await db_module.get_collection(first["id"])
    assert detail is not None
    assert detail["papers"] == []


if __name__ == "__main__":
    unittest.main()
