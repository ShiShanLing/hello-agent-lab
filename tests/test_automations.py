"""自动化调度与每日 Todo 简报测试。"""

from __future__ import annotations

from datetime import datetime
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hello_agent.automations import (
    briefing_timezone,
    collect_todo_snapshot,
    generate_daily_todo_briefing,
)
from hello_agent.database import SqliteAutomationStore, SqliteTodoStore, dispose_database_connection
from hello_agent.scheduler import tick_scheduled_automations


class AutomationsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "agent.db"

    def tearDown(self) -> None:
        dispose_database_connection(self.database_path)
        self.temporary_directory.cleanup()

    def test_generates_fallback_briefing_from_todos(self) -> None:
        todo_store = SqliteTodoStore(self.database_path, "user-1")
        todo_store.add("收件箱任务")
        plan = todo_store.create_plan_with_steps(
            "学习计划",
            "先做最小步骤",
            "high",
            [{"title": "完成 demo", "description": "先跑通一次", "minutes": 20}],
        )

        snapshot = collect_todo_snapshot(self.database_path, "user-1")
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": ""}, clear=False):
            briefing = generate_daily_todo_briefing(snapshot)

        self.assertIn("Todo 简报", briefing)
        self.assertIn("学习计划", briefing)
        self.assertEqual(plan["remaining_count"], 1)

    def test_tick_schedules_enabled_users_once_per_day(self) -> None:
        current_hour = datetime.now(briefing_timezone()).hour
        automation_store = SqliteAutomationStore(self.database_path)
        automation_store.update_settings(
            "user-1",
            daily_todo_briefing_enabled=True,
            daily_todo_briefing_hour=current_hour,
        )

        with patch("hello_agent.scheduler.scheduler_enabled", return_value=True), patch(
            "hello_agent.scheduler.schedule_daily_todo_briefing"
        ) as schedule:
            count = tick_scheduled_automations(self.database_path)
            count_again = tick_scheduled_automations(self.database_path)

        self.assertEqual(count, 1)
        self.assertEqual(count_again, 0)
        schedule.assert_called_once()


if __name__ == "__main__":
    unittest.main()
