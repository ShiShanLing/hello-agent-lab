"""定时扫描并触发自动化任务。"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from pathlib import Path

from hello_agent.automations import briefing_timezone
from hello_agent.database import SqliteAutomationStore
from hello_agent.task_queue import schedule_daily_todo_briefing


logger = logging.getLogger(__name__)


def scheduler_enabled() -> bool:
    return os.getenv("AUTOMATION_SCHEDULER_ENABLED", "false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def tick_scheduled_automations(database_path: Path) -> int:
    """扫描到期自动化并投递后台任务。返回本次触发的任务数。"""
    if not scheduler_enabled():
        return 0

    now = datetime.now(briefing_timezone())
    store = SqliteAutomationStore(database_path)
    date_key = now.date().isoformat()
    triggered = 0

    for user_id in store.list_daily_briefing_user_ids(now.hour):
        if not store.try_claim_daily_briefing(user_id, date_key):
            continue
        try:
            schedule_daily_todo_briefing(
                database_path,
                user_id,
                trigger="scheduled",
            )
            triggered += 1
            logger.info("已投递每日 Todo 简报：user=%s date=%s", user_id, date_key)
        except Exception:
            logger.exception("投递每日 Todo 简报失败：user=%s", user_id)

    return triggered


def run_scheduler_loop(database_path: Path, *, interval_seconds: int = 60) -> None:
    path = database_path.resolve()
    logger.info(
        "自动化调度器已启动：db=%s interval=%ss enabled=%s",
        path,
        interval_seconds,
        scheduler_enabled(),
    )
    while True:
        try:
            count = tick_scheduled_automations(path)
            if count:
                logger.info("本轮触发 %s 个自动化任务", count)
        except Exception:
            logger.exception("自动化调度扫描失败")
        time.sleep(max(15, interval_seconds))


def main() -> None:
    import logging as logging_module

    logging_module.basicConfig(
        level=os.getenv("AUTOMATION_SCHEDULER_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    database_file = Path(
        os.getenv("TODO_DATABASE_FILE", "data/agent.db")
    ).resolve()
    interval = int(os.getenv("AUTOMATION_SCHEDULER_INTERVAL_SECONDS", "60"))
    run_scheduler_loop(database_file, interval_seconds=interval)


if __name__ == "__main__":
    main()
