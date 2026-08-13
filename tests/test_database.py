"""SQLite Todo 数据库测试。"""

import json
import tempfile
import unittest
from pathlib import Path

from hello_agent.database import (
    SqliteConversationStore,
    SqliteEvaluationStore,
    SqliteTodoStore,
    SqliteTaskStore,
    dispose_database_connections,
)


class SqliteTodoStoreTest(unittest.TestCase):
    def tearDown(self) -> None:
        dispose_database_connections()

    def test_add_list_complete_and_reload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "agent.db"
            store = SqliteTodoStore(database_path, "session-a")

            added = store.add("学习 SQLAlchemy")
            completed = store.complete(added["id"])
            reopened = store.set_completed(added["id"], False)
            reloaded = SqliteTodoStore(database_path, "session-a")

            self.assertTrue(completed["completed"])
            self.assertFalse(reopened["completed"])
            self.assertEqual(reloaded.list_all(), [reopened])

            store.delete(added["id"])
            self.assertEqual(reloaded.list_all(), [])

    def test_isolates_sessions_in_same_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "agent.db"
            first = SqliteTodoStore(database_path, "session-a")
            second = SqliteTodoStore(database_path, "session-b")

            first.add("A 的任务")
            second.add("B 的任务")

            self.assertEqual(first.list_all()[0]["title"], "A 的任务")
            self.assertEqual(second.list_all()[0]["title"], "B 的任务")

    def test_migrates_legacy_json_without_deleting_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database_path = root / "agent.db"
            json_path = root / "todos.json"
            json_path.write_text(
                json.dumps(
                    [{"id": 3, "title": "旧任务", "completed": True}],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            store = SqliteTodoStore(database_path, "session-a", json_path)

            self.assertEqual(
                store.list_all(),
                [{"id": 3, "title": "旧任务", "completed": True}],
            )
            self.assertTrue(json_path.exists())

    def test_groups_tasks_under_plan_and_calculates_progress(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteTodoStore(Path(directory) / "agent.db", "user-a")
            plan = store.create_plan_with_steps(
                "AI Agent 学习计划",
                "从基础到实践",
                "high",
                [
                    {"title": "学习 Python", "description": "掌握基础", "minutes": 60},
                    {"title": "学习工具调用", "description": "完成练习", "minutes": 90},
                ],
            )
            store.complete(plan["todos"][0]["id"])

            detail = store.get_plan(plan["id"])
            grouped = store.list_plans()

            self.assertEqual(detail["total_count"], 2)
            self.assertEqual(detail["completed_count"], 1)
            self.assertEqual(detail["remaining_count"], 1)
            self.assertEqual(detail["total_minutes"], 150)
            self.assertEqual(grouped["plans"][0]["title"], "AI Agent 学习计划")

            other_user = SqliteTodoStore(Path(directory) / "agent.db", "user-b")
            with self.assertRaisesRegex(ValueError, "找不到"):
                other_user.get_plan(plan["id"])

    def test_persists_conversation_and_isolates_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "agent.db"
            first = SqliteConversationStore(database_path, "session-a")
            second = SqliteConversationStore(database_path, "session-b")

            first.append_turn(
                "可以了，同步到旅行计划中吧。",
                "已经同步到旅行计划。",
                operation_type="travel",
            )
            second.append_turn("我叫小红", "你好，小红。")

            reloaded = SqliteConversationStore(database_path, "session-a")
            self.assertEqual(
                reloaded.list_recent(40),
                [
                    {"role": "user", "content": "可以了，同步到旅行计划中吧。"},
                    {"role": "assistant", "content": "已经同步到旅行计划。"},
                ],
            )
            self.assertEqual(
                reloaded.list_history(40)[0]["operation_type"],
                "travel",
            )

    def test_persists_assistant_citation_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "agent.db"
            store = SqliteConversationStore(database_path, "session-cite")
            sources = [
                {
                    "kind": "knowledge",
                    "title": "03_考勤工作时间与加班.md",
                    "source": "03_考勤工作时间与加班.md",
                    "chunk": 1,
                    "snippet": "摘要",
                    "body": "## 标题\n\n正文",
                    "url": None,
                }
            ]
            store.append_turn(
                "迟到怎么处理？",
                "频繁异常由负责人先沟通 [03_考勤工作时间与加班.md#片段1]",
                sources=sources,
                confidence="high",
                grounding="knowledge",
            )
            history = SqliteConversationStore(database_path, "session-cite").list_history(10)
            self.assertEqual(history[0]["role"], "user")
            self.assertEqual(history[1]["role"], "assistant")
            self.assertEqual(history[1]["sources"], sources)
            self.assertEqual(history[1]["confidence"], "high")
            self.assertEqual(history[1]["grounding"], "knowledge")

    def test_persists_background_task_progress_and_notifications(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "agent.db"
            first = SqliteTaskStore(database_path, "user-a")
            second = SqliteTaskStore(database_path, "user-b")

            task = first.create(
                "knowledge_index",
                "向量化《制度.md》",
                {"document_id": "document-a"},
            )
            first.mark_running(task["id"])
            first.update_progress(task["id"], 55)
            completed = first.mark_succeeded(task["id"], {"chunks": 3})

            self.assertEqual(completed["status"], "succeeded")
            self.assertEqual(completed["progress"], 100)
            self.assertEqual(first.unread_count(), 1)
            self.assertEqual(second.list(), [])
            notification = first.list_notifications()[0]
            first.mark_notification_read(notification["id"])
            self.assertEqual(first.unread_count(), 0)

    def test_retries_failed_background_task_with_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteTaskStore(Path(directory) / "agent.db", "user-a")
            task = store.create("knowledge_index", "失败任务", {}, max_retries=1)
            store.mark_failed(task["id"], "模型错误")
            retried = store.retry(task["id"])

            self.assertEqual(retried["status"], "queued")
            self.assertEqual(retried["retry_count"], 1)
            store.mark_failed(task["id"], "再次错误")
            with self.assertRaisesRegex(ValueError, "最大重试"):
                store.retry(task["id"])

    def test_marks_waiting_cancelled_and_requeues_background_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteTaskStore(Path(directory) / "agent.db", "user-a")
            task = store.create(
                "workflow_run",
                "运行工作流",
                {"run_id": "run-a", "approved": False},
            )
            store.mark_running(task["id"])
            waiting = store.mark_waiting(task["id"], "请确认后继续。")
            self.assertEqual(waiting["status"], "waiting")
            self.assertEqual(store.list_notifications()[0]["level"], "warning")
            resumed = store.requeue(task["id"], {"approved": True})
            self.assertEqual(resumed["status"], "queued")
            self.assertTrue(resumed["payload"]["approved"])
            store.mark_running(task["id"])
            cancelled = store.mark_cancelled(task["id"])
            self.assertEqual(cancelled["status"], "cancelled")
            with self.assertRaisesRegex(ValueError, "不能取消"):
                store.mark_cancelled(task["id"])

    def test_conversation_context_has_a_recent_message_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteConversationStore(Path(directory) / "agent.db", "session-a")
            store.append_turn("问题 1", "回答 1")
            store.append_turn("问题 2", "回答 2")

            self.assertEqual(
                store.list_recent(2),
                [
                    {"role": "user", "content": "问题 2"},
                    {"role": "assistant", "content": "回答 2"},
                ],
            )

    def test_persists_extended_evaluation_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteEvaluationStore(Path(directory) / "agent.db", "user-a")
            run_id = store.start(
                "deepseek-v4-flash",
                1,
                scorer="llm_judge",
                judge_enabled=True,
                baseline_run_id="baseline-1",
            )
            store.add_case(
                run_id,
                "case-1",
                "幻觉拒答",
                "safety",
                False,
                128,
                "明确说明没有依据",
                "我猜测是某位市长",
                score=42.5,
                scorer="llm_judge",
                confidence="medium",
                failure_type="hallucination",
                failure_reason="回答编造了不存在的事实。",
                judge_score=42.5,
                judge_summary="未通过",
                judge_reasoning="没有拒绝无依据问题。",
                signals={"keyword_score": 0},
                baseline_status="passed",
                regression_label="new_failure",
            )
            store.finish(
                run_id,
                0,
                1234,
                score=42.5,
                confidence="medium",
                regression_summary={"new_failures": 1},
                export_metadata={"formats": ["json", "csv"]},
            )

            detail = store.get(run_id)

            self.assertEqual(detail["scorer"], "llm_judge")
            self.assertTrue(detail["judge_enabled"])
            self.assertEqual(detail["confidence"], "medium")
            self.assertEqual(detail["baseline_run_id"], "baseline-1")
            self.assertEqual(detail["regression_summary"]["new_failures"], 1)
            self.assertEqual(detail["export_metadata"]["formats"], ["json", "csv"])
            self.assertEqual(detail["cases"][0]["failure_type"], "hallucination")
            self.assertEqual(detail["cases"][0]["regression_label"], "new_failure")


if __name__ == "__main__":
    unittest.main()
