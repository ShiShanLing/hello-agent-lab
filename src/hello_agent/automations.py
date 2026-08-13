"""定时/触发型自动化任务实现。"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from hello_agent.app import create_client
from hello_agent.database import SqliteTodoStore
from hello_agent.model_routing import create_chat_completion

BriefingTrigger = Literal["scheduled", "manual"]


def briefing_timezone() -> ZoneInfo:
    name = os.getenv("DAILY_TODO_BRIEFING_TIMEZONE", "Asia/Shanghai").strip()
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("Asia/Shanghai")


def collect_todo_snapshot(database_path: Path, user_id: str) -> dict[str, object]:
    return SqliteTodoStore(database_path, user_id).list_plans()


def _pending_counts(snapshot: dict[str, object]) -> tuple[int, int]:
    inbox = snapshot.get("inbox") or []
    plans = snapshot.get("plans") or []
    inbox_pending = sum(
        1 for item in inbox if isinstance(item, dict) and not item.get("completed")
    )
    plan_pending = 0
    for plan in plans:
        if not isinstance(plan, dict):
            continue
        todos = plan.get("todos") or []
        plan_pending += sum(
            1 for item in todos if isinstance(item, dict) and not item.get("completed")
        )
    return inbox_pending, plan_pending


def generate_daily_todo_briefing(snapshot: dict[str, object]) -> str:
    """根据 Todo 快照生成每日简报正文。"""
    inbox_pending, plan_pending = _pending_counts(snapshot)
    total_pending = inbox_pending + plan_pending
    today = datetime.now(briefing_timezone()).strftime("%Y-%m-%d")

    if total_pending == 0:
        return (
            f"【{today} Todo 简报】\n"
            "当前没有未完成的待办或计划步骤，今天可以从新目标开始。"
        )

    compact = _compact_snapshot(snapshot)
    if not os.getenv("DEEPSEEK_API_KEY"):
        return _fallback_briefing(today, compact, total_pending)

    client = create_client()
    response, _model = create_chat_completion(
        client,
        role="cheap",
        max_tokens=700,
        messages=[
            {
                "role": "system",
                "content": (
                    "你是 Todo 每日简报助手。根据提供的 JSON 数据，用简洁中文写出今日待办摘要。"
                    "包含：未完成数量、各计划进度、建议优先处理的 3 项、一句鼓励。"
                    "不要编造不存在的任务；若数据为空就说暂无待办。不要使用 Markdown 标题。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {"date": today, "snapshot": compact},
                    ensure_ascii=False,
                ),
            },
        ],
    )
    content = (response.choices[0].message.content or "").strip()
    if not content:
        return _fallback_briefing(today, compact, total_pending)
    return f"【{today} Todo 简报】\n{content}"


def _compact_snapshot(snapshot: dict[str, object]) -> dict[str, object]:
    inbox = []
    for item in snapshot.get("inbox") or []:
        if not isinstance(item, dict) or item.get("completed"):
            continue
        inbox.append({"id": item.get("id"), "title": item.get("title")})
    plans = []
    for plan in snapshot.get("plans") or []:
        if not isinstance(plan, dict):
            continue
        pending = [
            {"id": todo.get("id"), "title": todo.get("title")}
            for todo in (plan.get("todos") or [])
            if isinstance(todo, dict) and not todo.get("completed")
        ]
        if not pending and int(plan.get("remaining_count") or 0) == 0:
            continue
        plans.append(
            {
                "id": plan.get("id"),
                "title": plan.get("title"),
                "priority": plan.get("priority"),
                "remaining_count": plan.get("remaining_count"),
                "pending_todos": pending[:8],
            }
        )
    return {"inbox": inbox[:12], "plans": plans[:8]}


def _fallback_briefing(
    today: str, compact: dict[str, object], total_pending: int
) -> str:
    lines = [f"【{today} Todo 简报】", f"共有 {total_pending} 项未完成。"]
    for plan in compact.get("plans") or []:
        if not isinstance(plan, dict):
            continue
        lines.append(
            f"- 计划「{plan.get('title')}」还剩 {plan.get('remaining_count')} 项"
        )
    for item in compact.get("inbox") or []:
        if isinstance(item, dict):
            lines.append(f"- 收件箱：{item.get('title')}")
    return "\n".join(lines[:12])


def run_daily_todo_briefing(
    database_path: Path,
    user_id: str,
    *,
    trigger: BriefingTrigger = "manual",
) -> dict[str, object]:
    snapshot = collect_todo_snapshot(database_path, user_id)
    briefing = generate_daily_todo_briefing(snapshot)
    inbox_pending, plan_pending = _pending_counts(snapshot)
    return {
        "trigger": trigger,
        "briefing": briefing,
        "pending_inbox": inbox_pending,
        "pending_plan_items": plan_pending,
        "generated_at": datetime.now(briefing_timezone()).isoformat(),
    }
