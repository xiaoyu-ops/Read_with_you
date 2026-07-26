from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.storage import db as db_module


class AgentTaskStorageTest(unittest.TestCase):
    def test_agent_task_history_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "papers.db"
            with patch.object(db_module, "DB_PATH", db_path):
                asyncio.run(_exercise_agent_task_flow())


async def _exercise_agent_task_flow() -> None:
    await db_module.init_db()
    await db_module.insert_paper(
        arxiv_id="1706.03762",
        title="Attention Is All You Need",
        authors=["A. Vaswani"],
        source="ar5iv",
        file_path="/tmp/1706.03762",
    )

    task_id = await db_module.create_agent_task(
        arxiv_id="1706.03762",
        task_type="four_agent_analysis",
        summary="Agent 深度分析",
    )
    duplicate_task_id, created = await db_module.try_create_agent_task(
        arxiv_id="1706.03762",
        task_type="four_agent_analysis",
        summary="重复 Agent 深度分析",
    )
    assert duplicate_task_id == task_id
    assert not created

    await db_module.update_agent_task(task_id, "done", "Agent 深度分析完成")

    next_task_id, created = await db_module.try_create_agent_task(
        arxiv_id="1706.03762",
        task_type="four_agent_analysis",
        summary="新一轮 Agent 深度分析",
    )
    assert created
    assert next_task_id != task_id
    await db_module.update_agent_task(next_task_id, "done", "新一轮 Agent 深度分析完成")

    tasks = await db_module.list_agent_tasks()
    assert len(tasks) == 2
    assert tasks[0]["id"] == next_task_id
    assert tasks[0]["arxiv_id"] == "1706.03762"
    assert tasks[0]["paper_title"] == "Attention Is All You Need"
    assert tasks[0]["status"] == "done"
    assert tasks[0]["completed_at"]

    first_lock = await db_module.try_update_status(
        "1706.03762",
        "translating",
        blocked_status="translating",
    )
    duplicate_lock = await db_module.try_update_status(
        "1706.03762",
        "translating",
        blocked_status="translating",
    )
    await db_module.update_status("1706.03762", "translated")
    next_lock = await db_module.try_update_status(
        "1706.03762",
        "translating",
        blocked_status="translating",
    )

    assert first_lock
    assert not duplicate_lock
    assert next_lock


if __name__ == "__main__":
    unittest.main()
