"""RQ Worker 入口和后台任务执行器。"""

from __future__ import annotations

import os
from pathlib import Path

from rq import Queue, Worker

from hello_agent.database import (
    SqliteAgentOpsStore,
    SqliteEvaluationStore,
    SqliteTaskStore,
    SqliteWorkflowStore,
)
from hello_agent.automations import run_daily_todo_briefing
from hello_agent.evaluations import run_dataset_evaluation
from hello_agent.task_queue import QUEUE_NAME, redis_connection
from hello_agent.vector_knowledge import index_document
from hello_agent.workflows import (
    WorkflowDefinition,
    build_workflow_runtime,
    stream_workflow,
)


def execute_background_task(database_file: str, task_id: str, user_id: str) -> dict[str, object]:
    database_path = Path(database_file)
    store = SqliteTaskStore(database_path, user_id)
    task = store.mark_running(task_id)
    if task["status"] == "cancelled":
        return {"status": "cancelled"}
    try:
        if task["task_type"] == "knowledge_index":
            payload = task["payload"]
            document_id = str(payload["document_id"])
            index_document(
                database_path,
                user_id,
                document_id,
                progress_callback=lambda progress: store.update_progress(task_id, progress),
                raise_on_error=True,
            )
            store.mark_succeeded(
                task_id,
                {"document_id": document_id},
                "文档已经完成分段和语义向量化，可以使用混合检索。",
            )
            return {"document_id": document_id}
        if task["task_type"] == "evaluation_run":
            payload = task["payload"]
            run_id = str(payload["run_id"])
            evaluation_store = SqliteEvaluationStore(database_path, user_id)
            agentops_store = SqliteAgentOpsStore(database_path, user_id)
            prompt = agentops_store.get_prompt(str(payload["prompt_version_id"]))
            dataset = agentops_store.get_dataset(str(payload["dataset_id"]))
            result = run_dataset_evaluation(
                evaluation_store,
                run_id,
                list(dataset["cases"]),
                str(prompt["content"]),
                str(payload["model"]),
                progress_callback=lambda progress: store.update_progress(task_id, progress),
                scorer=str(payload.get("scorer") or "llm_judge"),
                judge_enabled=bool(payload.get("judge_enabled", True)),
            )
            from hello_agent.usage import estimate_cost_from_total_tokens

            estimated_cost = estimate_cost_from_total_tokens(
                int(result["total_tokens"]),
                str(payload.get("model") or ""),
            )
            evaluation_store.finish(
                run_id, result["passed_cases"], result["total_tokens"],
                estimated_cost=estimated_cost,
                score=float(result.get("score", 0)),
                confidence=str(result.get("confidence", "low")),
            )
            store.mark_succeeded(
                task_id,
                {"run_id": run_id, **result},
                f"评测完成：{result['passed_cases']}/{len(dataset['cases'])} 个用例通过。",
            )
            return {"run_id": run_id, **result}
        if task["task_type"] == "daily_todo_briefing":
            payload = task["payload"]
            trigger = str(payload.get("trigger") or "manual")
            store.update_progress(task_id, 20)
            result = run_daily_todo_briefing(
                database_path,
                user_id,
                trigger="scheduled" if trigger == "scheduled" else "manual",
            )
            store.update_progress(task_id, 90)
            preview = str(result["briefing"])[:480]
            store.mark_succeeded(
                task_id,
                result,
                preview,
            )
            return result
        if task["task_type"] == "workflow_run":
            return _execute_workflow_task(database_path, user_id, task_id, task, store)
        raise ValueError(f"不支持的后台任务类型：{task['task_type']}")
    except Exception as error:
        if task.get("task_type") == "evaluation_run":
            try:
                SqliteEvaluationStore(database_path, user_id).finish(
                    str(task["payload"]["run_id"]), 0, 0, status="failed"
                )
            except Exception:
                pass
        if task.get("task_type") == "workflow_run":
            try:
                current = store.get(task_id)
                if current["status"] != "cancelled":
                    SqliteWorkflowStore(database_path, user_id).finish_run(
                        str(task["payload"]["run_id"]), "failed", "", [], str(error)
                    )
            except Exception:
                pass
        if store.get(task_id)["status"] != "cancelled":
            store.mark_failed(task_id, str(error))
        raise


def _execute_workflow_task(
    database_path: Path,
    user_id: str,
    task_id: str,
    task: dict[str, object],
    store: SqliteTaskStore,
) -> dict[str, object]:
    payload = task["payload"]
    run_id = str(payload["run_id"])
    workflow_id = str(payload["workflow_id"])
    workflow_store = SqliteWorkflowStore(database_path, user_id)
    saved = workflow_store.get(workflow_id)
    run = workflow_store.get_run(run_id)
    workflow_store.update_run(run_id, status="running")
    runtime = build_workflow_runtime(database_path, user_id)
    definition = WorkflowDefinition.model_validate(saved["definition"])
    steps: list[dict[str, object]] = []
    node_total = max(1, len(definition.nodes))

    for event in stream_workflow(
        definition,
        str(run["input_text"]),
        list(runtime["knowledge_roots"]),
        runtime["public_knowledge_root"],  # type: ignore[arg-type]
        runtime["mcp_client"],  # type: ignore[arg-type]
        approved=bool(payload.get("approved")),
        todo_store=runtime["todo_store"],  # type: ignore[arg-type]
        workflow_store=workflow_store,
        current_workflow_id=workflow_id,
    ):
        current = store.get(task_id)
        if current["status"] == "cancelled":
            workflow_store.finish_run(run_id, "cancelled", "", steps, "任务已取消。")
            return {"status": "cancelled", "run_id": run_id}

        event_type = str(event.get("type", ""))
        if event_type in {
            "node_started",
            "node_completed",
            "node_failed",
            "node_skipped",
            "approval_required",
        }:
            step = dict(event["step"])
            steps = [item for item in steps if item.get("node_id") != step.get("node_id")]
            steps.append(step)
            completed = sum(1 for item in steps if item.get("status") in {"success", "skipped"})
            store.update_progress(task_id, max(1, round(completed / node_total * 100)))
            workflow_store.update_run(run_id, status="running", steps=steps)

        if event_type == "approval_required":
            waiting_steps = list(event.get("steps", steps))
            workflow_store.finish_run(run_id, "waiting_approval", "", waiting_steps)
            store.mark_waiting(
                task_id,
                str(event.get("step", {}).get("summary") or "工作流等待你确认后继续执行。"),
            )
            return {"status": "waiting", "run_id": run_id}

        if event_type == "node_failed":
            failed_steps = list(event.get("steps", steps))
            message = str(event.get("message", "工作流执行失败"))
            workflow_store.finish_run(run_id, "failed", "", failed_steps, message)
            raise RuntimeError(message)

        if event_type == "run_completed":
            output = str(event.get("output", ""))
            completed_steps = list(event.get("steps", steps))
            workflow_store.finish_run(run_id, "success", output, completed_steps)
            store.mark_succeeded(
                task_id,
                {"run_id": run_id, "workflow_id": workflow_id, "output": output},
                "工作流已经执行完成。",
            )
            return {"run_id": run_id, "output": output}

    raise RuntimeError("工作流没有返回完成状态。")


def main() -> None:
    connection = redis_connection()
    queue = Queue(QUEUE_NAME, connection=connection)
    worker = Worker(
        [queue],
        connection=connection,
        name=os.getenv("TASK_WORKER_NAME", "hello-agent-worker"),
        job_monitoring_interval=5,
    )
    worker.work(logging_level=os.getenv("TASK_WORKER_LOG_LEVEL", "INFO"))


if __name__ == "__main__":
    main()
