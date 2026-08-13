"""Redis/RQ 任务分发与知识库任务编排。"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile

from redis import Redis
from rq import Queue

from hello_agent.database import (
    SqliteEvaluationStore,
    SqliteKnowledgeDocumentStore,
    SqliteTaskStore,
    SqliteWorkflowStore,
)


QUEUE_NAME = "hello-agent"


def redis_url() -> str:
    return os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")


def redis_connection() -> Redis:
    return Redis.from_url(
        redis_url(),
        socket_connect_timeout=2,
        socket_timeout=3,
        health_check_interval=30,
    )


def dispatch_task(database_path: Path, task: dict[str, object], user_id: str) -> bool:
    """把已持久化的任务提交给 RQ；临时测试数据库不连接 Redis。"""
    temporary_root = Path(tempfile.gettempdir()).resolve()
    if (
        database_path.resolve().is_relative_to(temporary_root)
        and os.getenv("TASK_QUEUE_TEMP_DATABASES", "false").lower() != "true"
    ):
        return False
    connection = redis_connection()
    connection.ping()
    Queue(QUEUE_NAME, connection=connection).enqueue_call(
        "hello_agent.task_worker.execute_background_task",
        args=(str(database_path.resolve()), str(task["id"]), user_id),
        timeout=30 * 60,
        result_ttl=24 * 60 * 60,
        failure_ttl=7 * 24 * 60 * 60,
        job_id=f"{task['id']}-{task['retry_count']}",
        description=str(task["title"]),
    )
    return True


def schedule_knowledge_index(
    database_path: Path,
    user_id: str,
    document_id: str,
    original_name: str,
) -> dict[str, object]:
    task_store = SqliteTaskStore(database_path, user_id)
    task = task_store.create(
        "knowledge_index",
        f"向量化《{original_name}》",
        {"document_id": document_id},
    )
    try:
        dispatch_task(database_path, task, user_id)
    except Exception as error:
        message = f"任务队列暂不可用：{error}"
        task_store.mark_failed(str(task["id"]), message)
        try:
            SqliteKnowledgeDocumentStore(database_path, user_id).update_index_status(
                document_id,
                status="failed",
                progress=0,
                error_message=message,
            )
        except ValueError:
            pass
    return task_store.get(str(task["id"]))


def schedule_evaluation_run(
    database_path: Path,
    user_id: str,
    run_id: str,
    prompt_version_id: str,
    prompt_version_name: str,
    dataset_id: str,
    dataset_name: str,
    model: str,
    scorer: str = "llm_judge",
    judge_enabled: bool = True,
    baseline_run_id: str | None = None,
) -> dict[str, object]:
    task_store = SqliteTaskStore(database_path, user_id)
    task = task_store.create(
        "evaluation_run",
        f"评测 {prompt_version_name} · {dataset_name}",
        {
            "run_id": run_id,
            "prompt_version_id": prompt_version_id,
            "dataset_id": dataset_id,
            "model": model,
            "scorer": scorer,
            "judge_enabled": judge_enabled,
            "baseline_run_id": baseline_run_id,
        },
    )
    try:
        dispatch_task(database_path, task, user_id)
    except Exception as error:
        message = f"任务队列暂不可用：{error}"
        task_store.mark_failed(str(task["id"]), message)
        SqliteEvaluationStore(database_path, user_id).finish(
            run_id, 0, 0, status="failed"
        )
    return task_store.get(str(task["id"]))


def schedule_daily_todo_briefing(
    database_path: Path,
    user_id: str,
    *,
    trigger: str = "manual",
) -> dict[str, object]:
    task_store = SqliteTaskStore(database_path, user_id)
    title = (
        "每日 Todo 简报（定时）"
        if trigger == "scheduled"
        else "每日 Todo 简报（手动）"
    )
    task = task_store.create(
        "daily_todo_briefing",
        title,
        {"trigger": trigger},
        max_retries=2,
    )
    try:
        dispatch_task(database_path, task, user_id)
    except Exception as error:
        message = f"任务队列暂不可用：{error}"
        task_store.mark_failed(str(task["id"]), message)
    return task_store.get(str(task["id"]))


def schedule_workflow_run(
    database_path: Path,
    user_id: str,
    workflow_id: str,
    run_id: str,
    workflow_name: str,
    approved: bool = False,
) -> dict[str, object]:
    task_store = SqliteTaskStore(database_path, user_id)
    task = task_store.create(
        "workflow_run",
        f"运行工作流「{workflow_name}」",
        {
            "workflow_id": workflow_id,
            "run_id": run_id,
            "approved": approved,
        },
    )
    try:
        dispatch_task(database_path, task, user_id)
    except Exception as error:
        message = f"任务队列暂不可用：{error}"
        task_store.mark_failed(str(task["id"]), message)
        SqliteWorkflowStore(database_path, user_id).finish_run(
            run_id, "failed", "", [], message
        )
    return task_store.get(str(task["id"]))


def resume_workflow_run(
    database_path: Path, user_id: str, run_id: str
) -> dict[str, object]:
    task_store = SqliteTaskStore(database_path, user_id)
    task = task_store.find_by_payload("workflow_run", "run_id", run_id)
    if task is None:
        raise ValueError("找不到对应的工作流后台任务。")
    task = task_store.requeue(str(task["id"]), {"approved": True})
    try:
        dispatch_task(database_path, task, user_id)
    except Exception as error:
        task = task_store.mark_failed(str(task["id"]), f"任务队列暂不可用：{error}")
    return task


def cancel_task(database_path: Path, user_id: str, task_id: str) -> dict[str, object]:
    store = SqliteTaskStore(database_path, user_id)
    task = store.mark_cancelled(task_id)
    payload = task["payload"]
    if task["task_type"] == "evaluation_run" and payload.get("run_id"):
        try:
            SqliteEvaluationStore(database_path, user_id).finish(
                str(payload["run_id"]), 0, 0, status="failed"
            )
        except Exception:
            pass
    if task["task_type"] == "workflow_run" and payload.get("run_id"):
        try:
            workflow_store = SqliteWorkflowStore(database_path, user_id)
            run = workflow_store.get_run(str(payload["run_id"]))
            if run["status"] not in {"success", "failed", "cancelled"}:
                workflow_store.finish_run(
                    str(payload["run_id"]), "cancelled", "", list(run["steps"]), "任务已取消。"
                )
        except Exception:
            pass
    _cancel_rq_job(task)
    return task


def retry_task(database_path: Path, user_id: str, task_id: str) -> dict[str, object]:
    store = SqliteTaskStore(database_path, user_id)
    original = store.get(task_id)
    payload_patch = (
        {"approved": False} if original["task_type"] == "workflow_run" else None
    )
    task = store.retry(task_id, payload_patch=payload_patch)
    if task["task_type"] == "evaluation_run":
        SqliteEvaluationStore(database_path, user_id).reset(
            str(task["payload"]["run_id"])
        )
    if task["task_type"] == "workflow_run":
        SqliteWorkflowStore(database_path, user_id).reset_run(
            str(task["payload"]["run_id"])
        )
    try:
        dispatch_task(database_path, task, user_id)
    except Exception as error:
        task = store.mark_failed(task_id, f"任务队列暂不可用：{error}")
    return task


def _cancel_rq_job(task: dict[str, object]) -> None:
    try:
        job = Queue(QUEUE_NAME, connection=redis_connection()).fetch_job(
            f"{task['id']}-{task['retry_count']}"
        )
        if job is not None:
            job.cancel()
    except Exception:
        return
