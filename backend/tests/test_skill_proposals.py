from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.api import routes_agent_chat
from backend.storage import agent_workspace


class SkillProposalTest(unittest.TestCase):
    def test_proposal_requires_approval_before_skill_is_discoverable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            with (
                patch.object(agent_workspace, "SKILLS_PATH", workspace / "skills.json"),
                patch.object(agent_workspace, "SKILL_PROPOSALS_PATH", workspace / "proposals.json"),
            ):
                proposal = agent_workspace.create_skill_proposal(
                    {
                        "name": "实验表格核对",
                        "description": "核对实验表的设置和结论。",
                        "trigger": "用户要求核对实验表。",
                        "trigger_keywords": ["实验表"],
                        "steps": ["定位表格。", "核对设置。"],
                    }
                )
                assert all(item["id"] != proposal["skill"]["id"] for item in agent_workspace.load_skills())
                assert agent_workspace.reject_skill_proposal(proposal["id"])["status"] == "rejected"
                assert all(item["id"] != proposal["skill"]["id"] for item in agent_workspace.load_skills())

                second = agent_workspace.create_skill_proposal(
                    {**proposal["skill"], "name": "实验表核对"},
                    action="create",
                )
                applied = agent_workspace.apply_skill_proposal(second["id"])
                assert applied and applied["status"] == "applied"
                skill = next(item for item in agent_workspace.load_skills() if item["id"] == second["skill"]["id"])
                assert skill["name"] == "实验表核对"

    def test_proposal_api_apply_and_reject_are_auditable(self) -> None:
        async def scenario() -> None:
            request = routes_agent_chat.AgentSkillProposalRequest(
                action="create",
                skill=routes_agent_chat.AgentSkillItem(
                    id="proposal-skill",
                    name="提案测试",
                    description="用于测试审批边界。",
                    trigger="用户提出测试流程。",
                    steps=["先核对。"],
                    source="custom",
                ),
            )
            proposal = await routes_agent_chat.propose_agent_skill(request)
            assert proposal.status == "pending"
            assert proposal.id in {item.id for item in await routes_agent_chat.get_skill_proposals("pending")}
            rejected = await routes_agent_chat.reject_agent_skill_proposal(proposal.id)
            assert rejected.status == "rejected"

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            with (
                patch.object(agent_workspace, "SKILLS_PATH", workspace / "skills.json"),
                patch.object(agent_workspace, "SKILL_PROPOSALS_PATH", workspace / "proposals.json"),
            ):
                import asyncio
                asyncio.run(scenario())
