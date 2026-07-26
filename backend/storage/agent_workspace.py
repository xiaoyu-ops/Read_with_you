"""Agent 工作区持久化。

这里先实现一个本地、可审计的最小版本：对话按论文存，记忆和 skill
按全局工作区存。它不是完整长期记忆系统，但给后续接 LLM / 子 Agent
编排留下稳定的数据边界。
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from uuid import uuid4

from .files import DATA_DIR, _write_json, now_iso

AGENT_WORKSPACE_DIR = DATA_DIR / "agent_workspace"
MEMORY_PATH = AGENT_WORKSPACE_DIR / "memory.json"
SKILLS_PATH = AGENT_WORKSPACE_DIR / "skills.json"
SKILL_PROPOSALS_PATH = AGENT_WORKSPACE_DIR / "skill_proposals.json"

# 单篇论文对话 transcript 上限：超过后丢最旧的，防止文件无界增长
MAX_CHAT_MESSAGES = 200
# 全局阅读记忆上限：Prompt 只召回其中最近更新的少量记录。
MAX_MEMORIES = 100
_MEMORY_TRAILING_PUNCTUATION = "。！？!?；;，,、."


def _safe_id(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "_", value).strip("_") or "unknown"


def _chat_path(arxiv_id: str) -> Path:
    return AGENT_WORKSPACE_DIR / "chats" / f"{_safe_id(arxiv_id)}.json"


def _runs_path(arxiv_id: str) -> Path:
    return AGENT_WORKSPACE_DIR / "runs" / f"{_safe_id(arxiv_id)}.json"


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def load_chat(arxiv_id: str) -> dict:
    data = _read_json(_chat_path(arxiv_id), {})
    return {
        "arxiv_id": arxiv_id,
        "messages": data.get("messages", []),
        "updated_at": data.get("updated_at"),
    }


def save_chat(arxiv_id: str, messages: list[dict]) -> dict:
    data = {
        "arxiv_id": arxiv_id,
        "messages": messages,
        "updated_at": now_iso(),
    }
    _write_json(_chat_path(arxiv_id), data)
    return data


def append_message(arxiv_id: str, role: str, content: str, meta: dict | None = None) -> dict:
    state = load_chat(arxiv_id)
    message = {
        "id": uuid4().hex,
        "role": role,
        "content": content.strip(),
        "created_at": now_iso(),
        "meta": meta or {},
    }
    messages = (state["messages"] + [message])[-MAX_CHAT_MESSAGES:]
    save_chat(arxiv_id, messages)
    return message


def clear_chat(arxiv_id: str) -> dict:
    """清空当前论文对话（不动 runs 历史与全局 memory）。"""
    return save_chat(arxiv_id, [])


def list_chat_summaries(limit: int = 50) -> list[dict]:
    """列出有内容的论文级对话摘要，供 Agent 页恢复聊天。"""
    chat_dir = AGENT_WORKSPACE_DIR / "chats"
    summaries: list[dict] = []
    if not chat_dir.exists():
        return summaries

    for path in chat_dir.glob("*.json"):
        data = _read_json(path, {})
        messages = data.get("messages", [])
        if not isinstance(messages, list) or not messages:
            continue
        last = next((item for item in reversed(messages) if item.get("content")), None)
        if not last:
            continue
        summaries.append(
            {
                "arxiv_id": str(data.get("arxiv_id") or path.stem),
                "message_count": len(messages),
                "last_role": last.get("role", ""),
                "last_message": str(last.get("content", "")),
                "updated_at": data.get("updated_at"),
            }
        )

    summaries.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    return summaries[:limit]


def list_chat_states() -> list[dict]:
    """列出全部论文级对话文件，包括已清空的会话。

    Session 搜索索引只把这些 JSON 当作权威数据；空会话也必须返回，
    这样派生索引才能在清空对话后删除旧消息。
    """
    chat_dir = AGENT_WORKSPACE_DIR / "chats"
    states: list[dict] = []
    if not chat_dir.exists():
        return states

    for path in chat_dir.glob("*.json"):
        data = _read_json(path, {})
        messages = data.get("messages", [])
        states.append(
            {
                "arxiv_id": str(data.get("arxiv_id") or path.stem),
                "messages": messages if isinstance(messages, list) else [],
                "updated_at": data.get("updated_at"),
            }
        )
    return states


def load_runs(arxiv_id: str, limit: int = 20) -> list[dict]:
    data = _read_json(_runs_path(arxiv_id), [])
    runs = data if isinstance(data, list) else []
    return runs[-limit:]


def _save_runs(arxiv_id: str, runs: list[dict]) -> None:
    _write_json(_runs_path(arxiv_id), runs)


def create_run(
    arxiv_id: str,
    task_type: str,
    title: str,
    user_message: str,
    *,
    task_id: int | None = None,
    inputs: list[str] | None = None,
    context: dict | None = None,
    status: str = "running",
) -> dict:
    runs = load_runs(arxiv_id, limit=10_000)
    ts = now_iso()
    run = {
        "id": uuid4().hex,
        "arxiv_id": arxiv_id,
        "task_type": task_type,
        "title": title,
        "status": status,
        "user_message": user_message.strip(),
        "inputs": inputs or [],
        "context": context or {},
        "result": "",
        "result_data": None,
        "error": "",
        "task_id": task_id,
        "created_at": ts,
        "updated_at": ts,
        "completed_at": None,
    }
    runs.append(run)
    _save_runs(arxiv_id, runs)
    return run


def get_run(arxiv_id: str, run_id: str) -> dict | None:
    return next((run for run in load_runs(arxiv_id, limit=10_000) if run.get("id") == run_id), None)


TERMINAL_RUN_STATUSES = ("done", "error", "cancelled")


def update_run(
    arxiv_id: str,
    run_id: str,
    *,
    status: str | None = None,
    result: str | None = None,
    result_data: dict | None = None,
    error: str | None = None,
    inputs: list[str] | None = None,
    context: dict | None = None,
) -> dict | None:
    runs = load_runs(arxiv_id, limit=10_000)
    updated = None
    ts = now_iso()
    for run in runs:
        if run.get("id") != run_id:
            continue
        # 终态不迁移：cancelled/done/error 的 Run 不允许被后到的写入改成别的终态
        # （例如取消后执行器抛异常，不能把 cancelled 覆盖成 error）
        if (
            status is not None
            and run.get("status") in TERMINAL_RUN_STATUSES
            and status != run.get("status")
        ):
            return run
        if status is not None:
            run["status"] = status
            if status in TERMINAL_RUN_STATUSES:
                run["completed_at"] = ts
        if result is not None:
            run["result"] = result
        if result_data is not None:
            run["result_data"] = result_data
        if error is not None:
            run["error"] = error
        if inputs is not None:
            run["inputs"] = inputs
        if context is not None:
            run["context"] = context
        run["updated_at"] = ts
        updated = run
        break
    if updated is not None:
        _save_runs(arxiv_id, runs)
    return updated


def sweep_stale_runs(error_message: str = "后端重启，任务中断。请重新发起。") -> int:
    """启动清扫：把上一进程遗留的 running Run 标记为 error。

    后台 Run 的执行体只存活在进程内；重启/崩溃后 runs.json 里的 running
    状态永远不会再被更新，前端会无限轮询“执行中”。
    """
    runs_dir = AGENT_WORKSPACE_DIR / "runs"
    if not runs_dir.exists():
        return 0
    swept = 0
    ts = now_iso()
    for path in runs_dir.glob("*.json"):
        runs = _read_json(path, [])
        if not isinstance(runs, list):
            continue
        changed = False
        for run in runs:
            if isinstance(run, dict) and run.get("status") == "running":
                run["status"] = "error"
                run["error"] = error_message
                run["updated_at"] = ts
                run["completed_at"] = ts
                changed = True
                swept += 1
        if changed:
            _write_json(path, runs)
    return swept


def cancel_run(arxiv_id: str, run_id: str) -> dict | None:
    run = get_run(arxiv_id, run_id)
    if run is None:
        return None
    if run.get("status") not in ("running", "waiting_permission"):
        return run
    return update_run(arxiv_id, run_id, status="cancelled", result="用户取消了这个后台任务。")


def load_memories(limit: int = 20) -> list[dict]:
    data = _read_json(MEMORY_PATH, [])
    memories = data if isinstance(data, list) else []
    normalized: list[dict] = []
    for item in memories:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        memory = dict(item)
        memory["content"] = content
        memory["updated_at"] = memory.get("updated_at") or memory.get("created_at") or ""
        normalized.append(memory)
    return normalized[-max(0, limit):] if limit else []


def normalize_memory_content(content: str) -> str:
    """生成仅用于去重的稳定 key，不改变用户看到的原始文案。"""
    value = unicodedata.normalize("NFKC", str(content or "")).casefold()
    value = re.sub(r"\s+", " ", value).strip()
    return value.rstrip(_MEMORY_TRAILING_PUNCTUATION).strip()


def _save_memories(memories: list[dict]) -> None:
    _write_json(MEMORY_PATH, memories[-MAX_MEMORIES:])


def add_memory(
    content: str,
    *,
    kind: str = "preference",
    arxiv_id: str | None = None,
    source: str = "user",
) -> dict:
    clean_content = content.strip()
    if not clean_content:
        raise ValueError("记忆内容不能为空")
    normalized_content = normalize_memory_content(clean_content)
    if not normalized_content:
        raise ValueError("记忆内容不能为空")
    memories = load_memories(limit=10_000)
    ts = now_iso()
    for index in range(len(memories) - 1, -1, -1):
        existing = memories[index]
        if normalize_memory_content(str(existing.get("content") or "")) != normalized_content:
            continue
        memory = {
            **existing,
            "kind": kind.strip() or str(existing.get("kind") or "preference"),
            "content": clean_content,
            "arxiv_id": arxiv_id if arxiv_id is not None else existing.get("arxiv_id"),
            "source": source,
            "updated_at": ts,
        }
        memories.pop(index)
        memories.append(memory)
        _save_memories(memories)
        return memory

    memory = {
        "id": uuid4().hex,
        "kind": kind.strip() or "preference",
        "content": clean_content,
        "arxiv_id": arxiv_id,
        "source": source,
        "created_at": ts,
        "updated_at": ts,
    }
    memories.append(memory)
    _save_memories(memories)
    return memory


def update_memory(
    memory_id: str,
    *,
    content: str | None = None,
    kind: str | None = None,
) -> dict | None:
    memories = load_memories(limit=10_000)
    target_index = next(
        (index for index, item in enumerate(memories) if item.get("id") == memory_id),
        None,
    )
    if target_index is None:
        return None
    memory = dict(memories[target_index])
    if content is not None:
        clean_content = content.strip()
        normalized_content = normalize_memory_content(clean_content)
        if not normalized_content:
            raise ValueError("记忆内容不能为空")
        duplicate = next(
            (
                item
                for item in memories
                if item.get("id") != memory_id
                and normalize_memory_content(str(item.get("content") or "")) == normalized_content
            ),
            None,
        )
        if duplicate is not None:
            raise ValueError("相同记忆已存在")
        memory["content"] = clean_content
    if kind is not None:
        clean_kind = kind.strip()
        if not clean_kind:
            raise ValueError("记忆类型不能为空")
        memory["kind"] = clean_kind
    memory["updated_at"] = now_iso()
    memories.pop(target_index)
    memories.append(memory)
    _save_memories(memories)
    return memory


def delete_memory(memory_id: str) -> dict | None:
    memories = load_memories(limit=10_000)
    target_index = next(
        (index for index, item in enumerate(memories) if item.get("id") == memory_id),
        None,
    )
    if target_index is None:
        return None
    memory = memories.pop(target_index)
    _save_memories(memories)
    return memory


DEFAULT_SKILLS = [
    {
        "id": "reproducibility_review",
        "name": "可复现性审查",
        "description": "按数据集、代码、超参数、硬件环境四个维度检查证据。",
        "trigger": "用户要求判断论文是否可信、能否复现、实验是否扎实。",
        "task_type": "reproducibility_deep_dive",
        "trigger_keywords": [
            "复现",
            "可复现",
            "reproduc",
            "可信",
            "实验扎实",
            "开源代码",
            "复现代码",
            "数据集",
            "超参数",
            "硬件",
        ],
        "steps": [
            "定位实验设置和附录。",
            "逐项提取数据集、代码、超参数、硬件环境证据。",
            "把缺失项明确标为未提，不用猜测补齐。",
            "给出置信度和下一步核验建议。",
        ],
        "source": "builtin",
        "updated_at": None,
    },
    {
        "id": "method_explanation",
        "name": "方法拆解",
        "description": "把论文方法拆成目标、核心模块、输入输出和关键假设。",
        "trigger": "用户要求解释方法、读懂模型、梳理公式或算法。",
        "task_type": "method_explanation",
        "trigger_keywords": [
            "方法",
            "解释",
            "读懂",
            "公式",
            "算法",
            "method",
            "模型",
            "模块",
        ],
        "steps": [
            "先找方法章节和图表。",
            "用输入、变换、输出描述主链路。",
            "标出论文没有说明清楚的假设。",
            "给用户一个可继续追问的问题列表。",
        ],
        "source": "builtin",
        "updated_at": None,
    },
]


def load_skills() -> list[dict]:
    data = _read_json(SKILLS_PATH, [])
    custom = data if isinstance(data, list) else []
    custom_ids = {item.get("id") for item in custom}
    return [item for item in DEFAULT_SKILLS if item["id"] not in custom_ids] + custom


def load_skill_proposals(status: str | None = None) -> list[dict]:
    data = _read_json(SKILL_PROPOSALS_PATH, [])
    proposals = data if isinstance(data, list) else []
    filtered = [dict(item) for item in proposals if isinstance(item, dict)]
    if status:
        filtered = [item for item in filtered if item.get("status") == status]
    return sorted(filtered, key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)


def get_skill_proposal(proposal_id: str) -> dict | None:
    return next((item for item in load_skill_proposals() if item.get("id") == proposal_id), None)


def create_skill_proposal(skill: dict, action: str = "create") -> dict:
    if action not in ("create", "update"):
        raise ValueError("skill proposal action must be create or update")
    name = str(skill.get("name") or "").strip()
    description = str(skill.get("description") or "").strip()
    trigger = str(skill.get("trigger") or "").strip()
    steps = skill.get("steps")
    if not name or not description or not trigger or not isinstance(steps, list) or not steps:
        raise ValueError("skill proposal requires name, description, trigger and steps")
    skill_id = _safe_id(str(skill.get("id") or name))
    normalized = {
        "id": skill_id,
        "name": name,
        "description": description,
        "trigger": trigger,
        "task_type": str(skill.get("task_type") or "").strip() or None,
        "trigger_keywords": [str(item).strip() for item in skill.get("trigger_keywords", []) if str(item).strip()][:12],
        "steps": [str(item).strip() for item in steps if str(item).strip()][:12],
        "source": "custom",
    }
    ts = now_iso()
    proposal = {
        "id": uuid4().hex,
        "action": action,
        "status": "pending",
        "skill": normalized,
        "diff": f"{action} skill {skill_id}: {name}",
        "created_at": ts,
        "updated_at": ts,
    }
    proposals = load_skill_proposals()
    proposals.append(proposal)
    _write_json(SKILL_PROPOSALS_PATH, proposals)
    return proposal


def apply_skill_proposal(proposal_id: str) -> dict | None:
    proposal = get_skill_proposal(proposal_id)
    if proposal is None or proposal.get("status") != "pending":
        return None
    raw_custom = _read_json(SKILLS_PATH, [])
    custom = raw_custom if isinstance(raw_custom, list) else []
    skill = dict(proposal["skill"])
    skill["updated_at"] = now_iso()
    custom = [item for item in custom if isinstance(item, dict) and item.get("id") != skill["id"]]
    custom.append(skill)
    _write_json(SKILLS_PATH, custom)
    return _update_skill_proposal_status(proposal_id, "applied")


def reject_skill_proposal(proposal_id: str) -> dict | None:
    proposal = get_skill_proposal(proposal_id)
    if proposal is None or proposal.get("status") != "pending":
        return None
    return _update_skill_proposal_status(proposal_id, "rejected")


def _update_skill_proposal_status(proposal_id: str, status: str) -> dict | None:
    proposals = load_skill_proposals()
    for proposal in proposals:
        if proposal.get("id") == proposal_id:
            proposal["status"] = status
            proposal["updated_at"] = now_iso()
            _write_json(SKILL_PROPOSALS_PATH, proposals)
            return proposal
    return None


def infer_agent_intent(content: str) -> dict:
    text = content.lower()
    for skill in load_skills():
        task_type = str(skill.get("task_type") or "").strip()
        if not task_type:
            continue
        keywords = skill.get("trigger_keywords")
        if not isinstance(keywords, list):
            keywords = []
        skill_name = str(skill.get("name") or "").lower()
        keyword_hit = any(str(keyword).lower() in text for keyword in keywords)
        name_hit = bool(skill_name and skill_name in text)
        if keyword_hit or name_hit:
            return {"task_type": task_type, "confidence": "medium", "source": "skill", "skill_id": skill.get("id")}
    checks = [
        ("reproducibility_deep_dive", ("复现", "可复现", "reproduc", "可信", "实验扎实")),
        ("method_explanation", ("方法", "解释", "读懂", "公式", "算法", "method")),
        ("annotation_questions", ("标注", "划线", "问题", "疑问", "annotation")),
        ("collection_compare", ("专题", "对比", "比较", "横向", "compare")),
        ("four_agent_analysis", ("总结", "摘要", "亮点", "改进", "分析", "summary")),
    ]
    for task_type, keywords in checks:
        if any(keyword in text for keyword in keywords):
            return {"task_type": task_type, "confidence": "medium"}
    return {"task_type": None, "confidence": "low"}


def should_save_memory(content: str) -> bool:
    text = content.lower()
    return any(keyword in text for keyword in ("记住", "以后", "偏好", "纠正", "下次", "preference"))
