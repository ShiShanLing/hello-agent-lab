"""平台监控指标：从本地运行记录汇总，费用按 Token × DeepSeek 单价估算。"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from sqlalchemy import case, func, select

from hello_agent.database import (
    AgentRunRecord,
    AgentRunStepRecord,
    BackgroundTaskRecord,
    EvaluationCaseResultRecord,
    EvaluationRunRecord,
    KnowledgeDocumentRecord,
    UserRecord,
    WorkflowRunRecord,
    _create_session_factory,
)
from hello_agent.usage import (
    convert_usd_to_cny,
    estimate_cost,
    estimate_cost_from_total_tokens,
    pricing_info,
)
from hello_agent.model_routing import routing_config


def collect_metrics(
    database_path: Path,
    user_id: str,
    days: int = 7,
    model: str | None = None,
    prompt_version_id: str | None = None,
) -> dict[str, object]:
    days = max(1, min(90, days))
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    start = now - timedelta(days=days)
    factory = _create_session_factory(str(database_path.resolve()))
    model_filter = (model or "").strip() or None
    prompt_filter = (prompt_version_id or "").strip() or None
    info = pricing_info(model_filter)

    with factory() as session:
        run_query = select(AgentRunRecord).where(
            AgentRunRecord.user_id == user_id,
            AgentRunRecord.started_at >= start,
        )
        if model_filter:
            run_query = run_query.where(AgentRunRecord.model == model_filter)
        runs = list(session.scalars(run_query).all())

        eval_query = select(EvaluationRunRecord).where(
            EvaluationRunRecord.user_id == user_id,
            EvaluationRunRecord.started_at >= start,
        )
        if model_filter:
            eval_query = eval_query.where(EvaluationRunRecord.model == model_filter)
        if prompt_filter:
            eval_query = eval_query.where(
                EvaluationRunRecord.prompt_version_id == prompt_filter
            )
        evaluations = list(session.scalars(eval_query).all())

        workflows = list(
            session.scalars(
                select(WorkflowRunRecord).where(
                    WorkflowRunRecord.user_id == user_id,
                    WorkflowRunRecord.started_at >= start,
                )
            ).all()
        )
        tasks = list(
            session.scalars(
                select(BackgroundTaskRecord).where(
                    BackgroundTaskRecord.user_id == user_id
                )
            ).all()
        )
        documents = list(
            session.scalars(
                select(KnowledgeDocumentRecord).where(
                    KnowledgeDocumentRecord.user_id == user_id
                )
            ).all()
        )
        run_ids = [run.id for run in runs]
        rag_steps = []
        if run_ids:
            rag_steps = list(
                session.scalars(
                    select(AgentRunStepRecord).where(
                        AgentRunStepRecord.run_id.in_(run_ids),
                        AgentRunStepRecord.name == "search_knowledge",
                    )
                ).all()
            )
        eval_ids = [item.id for item in evaluations]
        low_confidence_cases = 0
        total_eval_cases = 0
        if eval_ids:
            case_rows = session.execute(
                select(
                    func.count(EvaluationCaseResultRecord.id),
                    func.sum(
                        case(
                            (EvaluationCaseResultRecord.confidence == "low", 1),
                            else_=0,
                        )
                    ),
                ).where(EvaluationCaseResultRecord.evaluation_run_id.in_(eval_ids))
            ).one()
            total_eval_cases = int(case_rows[0] or 0)
            low_confidence_cases = int(case_rows[1] or 0)

        models = sorted(
            {
                str(item.model)
                for item in [*runs, *evaluations]
                if item.model
            }
        )
        prompt_versions = sorted(
            {
                (str(item.prompt_version_id), str(item.prompt_version_name or "未命名"))
                for item in evaluations
                if item.prompt_version_id
            }
        )

    run_stats = _summarize_agent_runs(runs)
    eval_cost = sum(_eval_cost(item) for item in evaluations)
    completed_evals = [item for item in evaluations if item.status == "completed"]
    low_eval_runs = sum(1 for item in completed_evals if item.confidence == "low")
    workflow_stats = _summarize_workflows(workflows)
    rag_calls = len(rag_steps) + int(workflow_stats["knowledge_calls"])
    rag_hits = (
        sum(1 for step in rag_steps if step.status == "success")
        + int(workflow_stats["knowledge_hits"])
    )
    indexed = sum(1 for item in documents if int(item.chunk_count or 0) > 0)

    daily = _daily_series(days, now, runs, evaluations, workflows)
    usd_to_cny = info.get("usd_to_cny")
    total_cost = round(run_stats["estimated_cost_usd"] + eval_cost, 6)

    return {
        "period_days": days,
        "generated_at": now.isoformat(),
        "filters": {
            "model": model_filter,
            "prompt_version_id": prompt_filter,
            "models": models,
            "prompt_versions": [
                {"id": item_id, "name": name} for item_id, name in prompt_versions
            ],
        },
        "pricing": info,
        "runs": run_stats,
        "evaluations": {
            "total": len(evaluations),
            "completed": len(completed_evals),
            "average_score": round(
                sum(item.score for item in completed_evals) / len(completed_evals), 1
            )
            if completed_evals
            else 0,
            "low_confidence_run_rate": _rate(low_eval_runs, len(completed_evals)),
            "low_confidence_case_rate": _rate(low_confidence_cases, total_eval_cases),
            "total_tokens": sum(item.total_tokens or 0 for item in evaluations),
            "estimated_cost_usd": round(eval_cost, 6),
        },
        "workflows": workflow_stats,
        "queue": _queue_snapshot(tasks),
        "rag": {
            "calls": rag_calls,
            "hits": rag_hits,
            "hit_rate": _rate(rag_hits, rag_calls),
            "documents": len(documents),
            "indexed_documents": indexed,
        },
        "totals": {
            "estimated_cost_usd": total_cost,
            "estimated_cost_cny": convert_usd_to_cny(total_cost, usd_to_cny if isinstance(usd_to_cny, float) else None),
            "total_tokens": run_stats["total_tokens"]
            + sum(item.total_tokens or 0 for item in evaluations),
        },
        "daily": daily,
        "by_model": _group_by_model(runs, evaluations),
        "by_run_type": _group_by_run_type(runs),
        "routing": routing_config(),
    }


PRIVACY_NOTICE = "统计数据不包含聊天内容、Todo、旅行计划、用户问题或私人文档名称。"
_PERIOD_META = {
    "day": {"current": "今天", "previous": "昨天", "compare": "较昨日", "buckets": 14},
    "week": {"current": "本周", "previous": "上周", "compare": "较上周", "buckets": 8},
    "month": {"current": "本月", "previous": "上月", "compare": "较上月", "buckets": 6},
}


def collect_platform_cost(
    database_path: Path,
    period: str = "day",
    model: str | None = None,
) -> dict[str, object]:
    """汇总全部用户的 Token 费用，按日 / 周 / 月分桶。"""
    if period not in _PERIOD_META:
        period = "day"
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    model_filter = (model or "").strip() or None
    info = pricing_info(model_filter)
    usd_to_cny = info.get("usd_to_cny")
    usd_rate = usd_to_cny if isinstance(usd_to_cny, float) else None
    buckets, current_key, previous_key, start = _platform_buckets(period, now)
    factory = _create_session_factory(str(database_path.resolve()))

    with factory() as session:
        run_query = select(AgentRunRecord).where(AgentRunRecord.started_at >= start)
        eval_query = select(EvaluationRunRecord).where(
            EvaluationRunRecord.started_at >= start
        )
        if model_filter:
            run_query = run_query.where(AgentRunRecord.model == model_filter)
            eval_query = eval_query.where(EvaluationRunRecord.model == model_filter)
        runs = list(session.scalars(run_query).all())
        evaluations = list(session.scalars(eval_query).all())
        users = {
            item.id: item for item in session.scalars(select(UserRecord)).all()
        }

    current_users: set[str] = set()
    previous_users: set[str] = set()
    lookback_users: set[str] = set()
    user_stats: dict[str, dict[str, float]] = defaultdict(
        lambda: {"runs": 0, "evaluation_runs": 0, "tokens": 0, "cost_usd": 0}
    )
    model_stats: dict[str, dict[str, float]] = defaultdict(
        lambda: {"runs": 0, "tokens": 0, "cost_usd": 0}
    )

    def _apply(
        user_id: str,
        started_at: datetime,
        tokens: int,
        cost: float,
        model_name: str | None,
        is_eval: bool,
    ) -> None:
        key = _period_key(started_at, period)
        bucket = buckets.get(key)
        if bucket is None:
            return
        if is_eval:
            bucket["evaluation_runs"] += 1
        else:
            bucket["agent_runs"] += 1
        bucket["tokens"] += tokens
        bucket["cost_usd"] += cost
        bucket["user_ids"].add(user_id)
        lookback_users.add(user_id)
        if key == current_key:
            current_users.add(user_id)
            user_stats[user_id]["evaluation_runs" if is_eval else "runs"] += 1
            user_stats[user_id]["tokens"] += tokens
            user_stats[user_id]["cost_usd"] += cost
        elif key == previous_key:
            previous_users.add(user_id)
        model_key = model_name or "unknown"
        model_stats[model_key]["runs"] += 1
        model_stats[model_key]["tokens"] += tokens
        model_stats[model_key]["cost_usd"] += cost

    for item in runs:
        _apply(
            item.user_id,
            item.started_at,
            item.total_tokens or 0,
            _run_cost(item),
            item.model,
            False,
        )
    for item in evaluations:
        _apply(
            item.user_id,
            item.started_at,
            item.total_tokens or 0,
            _eval_cost(item),
            item.model,
            True,
        )

    series = [
        {
            "key": key,
            "label": _period_bucket_label(key, period),
            "cost_usd": round(values["cost_usd"], 6),
            "tokens": int(values["tokens"]),
            "runs": int(values["agent_runs"]),
            "evaluation_runs": int(values["evaluation_runs"]),
            "active_users": len(values["user_ids"]),
        }
        for key, values in buckets.items()
    ]
    current = buckets[current_key]
    previous = buckets[previous_key]
    current_cost = round(current["cost_usd"], 6)
    previous_cost = round(previous["cost_usd"], 6)
    lookback_cost = round(sum(item["cost_usd"] for item in series), 6)
    lookback_tokens = sum(item["tokens"] for item in series)
    lookback_runs = sum(item["runs"] for item in series)

    by_user = []
    for user_id, values in sorted(
        user_stats.items(), key=lambda item: item[1]["cost_usd"], reverse=True
    ):
        user = users.get(user_id)
        by_user.append(
            {
                "user_id": user_id,
                "email": user.email if user else "",
                "display_name": user.display_name if user else "已删除用户",
                "runs": int(values["runs"]),
                "evaluation_runs": int(values["evaluation_runs"]),
                "tokens": int(values["tokens"]),
                "cost_usd": round(values["cost_usd"], 6),
            }
        )

    return {
        "period": period,
        "generated_at": now.isoformat(),
        "privacy_notice": PRIVACY_NOTICE,
        "pricing": info,
        "labels": {
            "current": _PERIOD_META[period]["current"],
            "previous": _PERIOD_META[period]["previous"],
            "compare": _PERIOD_META[period]["compare"],
        },
        "current": {
            "key": current_key,
            "cost_usd": current_cost,
            "cost_cny": convert_usd_to_cny(current_cost, usd_rate),
            "tokens": int(current["tokens"]),
            "runs": int(current["agent_runs"]),
            "evaluation_runs": int(current["evaluation_runs"]),
            "active_users": len(current_users),
        },
        "previous": {
            "key": previous_key,
            "cost_usd": previous_cost,
            "cost_cny": convert_usd_to_cny(previous_cost, usd_rate),
            "tokens": int(previous["tokens"]),
            "runs": int(previous["agent_runs"]),
            "evaluation_runs": int(previous["evaluation_runs"]),
            "active_users": len(previous_users),
            "cost_delta_pct": _delta_pct(current_cost, previous_cost),
        },
        "lookback": {
            "buckets": len(series),
            "cost_usd": lookback_cost,
            "cost_cny": convert_usd_to_cny(lookback_cost, usd_rate),
            "tokens": int(lookback_tokens),
            "runs": int(lookback_runs),
            "active_users": len(lookback_users),
        },
        "series": series,
        "by_user": by_user,
        "by_model": [
            {
                "model": name,
                "runs": int(values["runs"]),
                "tokens": int(values["tokens"]),
                "cost_usd": round(values["cost_usd"], 6),
            }
            for name, values in sorted(
                model_stats.items(), key=lambda item: item[1]["cost_usd"], reverse=True
            )
        ],
    }


def _summarize_agent_runs(runs: list[AgentRunRecord]) -> dict[str, object]:
    total = len(runs)
    success = sum(1 for item in runs if item.status == "success")
    failed = sum(1 for item in runs if item.status == "failed")
    durations = [item.duration_ms for item in runs if item.duration_ms]
    prompt_tokens = sum(item.prompt_tokens or 0 for item in runs)
    completion_tokens = sum(item.completion_tokens or 0 for item in runs)
    total_tokens = sum(item.total_tokens or 0 for item in runs)
    cost = sum(_run_cost(item) for item in runs)
    return {
        "total": total,
        "success": success,
        "failed": failed,
        "success_rate": _rate(success, total),
        "average_duration_ms": round(sum(durations) / len(durations)) if durations else 0,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "estimated_cost_usd": round(cost, 6),
    }


def _summarize_workflows(runs: list[WorkflowRunRecord]) -> dict[str, object]:
    total = len(runs)
    success = sum(1 for item in runs if item.status == "success")
    failed = sum(1 for item in runs if item.status == "failed")
    waiting = sum(1 for item in runs if item.status == "waiting_approval")
    cancelled = sum(1 for item in runs if item.status == "cancelled")
    node_total = 0
    node_failed = 0
    knowledge_calls = 0
    knowledge_hits = 0
    for item in runs:
        try:
            steps = json.loads(item.steps_json or "[]")
        except Exception:
            continue
        if not isinstance(steps, list):
            continue
        for step in steps:
            if not isinstance(step, dict):
                continue
            node_total += 1
            if step.get("status") == "failed":
                node_failed += 1
            if step.get("node_type") != "knowledge":
                continue
            knowledge_calls += 1
            summary = str(step.get("summary") or "")
            if "命中 0" not in summary and step.get("status") == "success":
                knowledge_hits += 1
    return {
        "total": total,
        "success": success,
        "failed": failed,
        "waiting": waiting,
        "cancelled": cancelled,
        "success_rate": _rate(success, total),
        "node_total": node_total,
        "node_failed": node_failed,
        "node_failure_rate": _rate(node_failed, node_total),
        "knowledge_calls": knowledge_calls,
        "knowledge_hits": knowledge_hits,
        "knowledge_hit_rate": _rate(knowledge_hits, knowledge_calls),
    }


def _queue_snapshot(tasks: list[BackgroundTaskRecord]) -> dict[str, object]:
    counts = defaultdict(int)
    for task in tasks:
        counts[str(task.status)] += 1
    redis_backlog: int | None = None
    try:
        from hello_agent.task_queue import QUEUE_NAME, redis_connection

        redis_backlog = int(redis_connection().llen(f"rq:queue:{QUEUE_NAME}"))
    except Exception:
        redis_backlog = None
    return {
        "queued": counts["queued"],
        "running": counts["running"],
        "waiting": counts["waiting"],
        "failed": counts["failed"],
        "succeeded": counts["succeeded"],
        "cancelled": counts["cancelled"],
        "redis_backlog": redis_backlog,
    }


def _daily_series(
    days: int,
    now: datetime,
    runs: list[AgentRunRecord],
    evaluations: list[EvaluationRunRecord],
    workflows: list[WorkflowRunRecord],
) -> list[dict[str, object]]:
    buckets: dict[str, dict[str, float]] = {}
    for offset in range(days - 1, -1, -1):
        day = (now - timedelta(days=offset)).date().isoformat()
        buckets[day] = {
            "runs": 0,
            "tokens": 0,
            "cost_usd": 0,
            "workflow_runs": 0,
            "evaluation_runs": 0,
        }
    for item in runs:
        day = item.started_at.date().isoformat()
        if day not in buckets:
            continue
        buckets[day]["runs"] += 1
        buckets[day]["tokens"] += item.total_tokens or 0
        buckets[day]["cost_usd"] += _run_cost(item)
    for item in evaluations:
        day = item.started_at.date().isoformat()
        if day not in buckets:
            continue
        buckets[day]["evaluation_runs"] += 1
        buckets[day]["tokens"] += item.total_tokens or 0
        buckets[day]["cost_usd"] += _eval_cost(item)
    for item in workflows:
        day = item.started_at.date().isoformat()
        if day not in buckets:
            continue
        buckets[day]["workflow_runs"] += 1
    return [
        {
            "date": day,
            "runs": int(values["runs"]),
            "tokens": int(values["tokens"]),
            "cost_usd": round(values["cost_usd"], 6),
            "workflow_runs": int(values["workflow_runs"]),
            "evaluation_runs": int(values["evaluation_runs"]),
        }
        for day, values in buckets.items()
    ]


def _group_by_model(
    runs: list[AgentRunRecord], evaluations: list[EvaluationRunRecord]
) -> list[dict[str, object]]:
    grouped: dict[str, dict[str, float]] = defaultdict(
        lambda: {"runs": 0, "tokens": 0, "cost_usd": 0}
    )
    for item in runs:
        key = item.model or "unknown"
        grouped[key]["runs"] += 1
        grouped[key]["tokens"] += item.total_tokens or 0
        grouped[key]["cost_usd"] += _run_cost(item)
    for item in evaluations:
        key = item.model or "unknown"
        grouped[key]["runs"] += 1
        grouped[key]["tokens"] += item.total_tokens or 0
        grouped[key]["cost_usd"] += _eval_cost(item)
    return [
        {
            "model": name,
            "runs": int(values["runs"]),
            "tokens": int(values["tokens"]),
            "cost_usd": round(values["cost_usd"], 6),
        }
        for name, values in sorted(grouped.items())
    ]


def _group_by_run_type(runs: list[AgentRunRecord]) -> list[dict[str, object]]:
    grouped: dict[str, dict[str, float]] = defaultdict(
        lambda: {"runs": 0, "success": 0, "tokens": 0, "cost_usd": 0}
    )
    for item in runs:
        key = item.run_type or "unknown"
        grouped[key]["runs"] += 1
        grouped[key]["success"] += 1 if item.status == "success" else 0
        grouped[key]["tokens"] += item.total_tokens or 0
        grouped[key]["cost_usd"] += _run_cost(item)
    return [
        {
            "run_type": name,
            "runs": int(values["runs"]),
            "success_rate": _rate(int(values["success"]), int(values["runs"])),
            "tokens": int(values["tokens"]),
            "cost_usd": round(values["cost_usd"], 6),
        }
        for name, values in sorted(grouped.items())
    ]


def _rate(part: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return round(part / total * 100, 1)


def _run_cost(item: AgentRunRecord) -> float:
    if item.estimated_cost is not None:
        return float(item.estimated_cost)
    return estimate_cost(
        {
            "prompt_tokens": item.prompt_tokens or 0,
            "completion_tokens": item.completion_tokens or 0,
            "total_tokens": item.total_tokens or 0,
        },
        item.model,
    )


def _eval_cost(item: EvaluationRunRecord) -> float:
    if item.estimated_cost is not None:
        return float(item.estimated_cost)
    return estimate_cost_from_total_tokens(item.total_tokens or 0, item.model)


def _delta_pct(current: float, previous: float) -> float | None:
    if previous <= 0:
        return None if current <= 0 else 100.0
    return round((current - previous) / previous * 100, 1)


def _add_months(year: int, month: int, delta: int) -> tuple[int, int]:
    total = year * 12 + (month - 1) + delta
    return total // 12, total % 12 + 1


def _period_key(started_at: datetime, period: str) -> str:
    day = started_at.date()
    if period == "week":
        return (day - timedelta(days=day.weekday())).isoformat()
    if period == "month":
        return f"{day.year:04d}-{day.month:02d}"
    return day.isoformat()


def _period_bucket_label(key: str, period: str) -> str:
    if period == "week":
        return f"{key[5:]} 周"
    if period == "month":
        return key
    return key[5:]


def _empty_bucket() -> dict[str, object]:
    return {
        "agent_runs": 0,
        "evaluation_runs": 0,
        "tokens": 0,
        "cost_usd": 0.0,
        "user_ids": set(),
    }


def _platform_buckets(
    period: str, now: datetime
) -> tuple[dict[str, dict[str, object]], str, str, datetime]:
    today = now.date()
    count = int(_PERIOD_META[period]["buckets"])
    buckets: dict[str, dict[str, object]] = {}
    if period == "week":
        current = today - timedelta(days=today.weekday())
        previous = current - timedelta(weeks=1)
        start_day = current - timedelta(weeks=count - 1)
        cursor = start_day
        while cursor <= current:
            buckets[cursor.isoformat()] = _empty_bucket()
            cursor += timedelta(weeks=1)
        return buckets, current.isoformat(), previous.isoformat(), datetime.combine(
            start_day, datetime.min.time()
        )
    if period == "month":
        current = f"{today.year:04d}-{today.month:02d}"
        previous_year, previous_month = _add_months(today.year, today.month, -1)
        previous = f"{previous_year:04d}-{previous_month:02d}"
        start_year, start_month = _add_months(today.year, today.month, -(count - 1))
        year, month = start_year, start_month
        while (year, month) <= (today.year, today.month):
            buckets[f"{year:04d}-{month:02d}"] = _empty_bucket()
            year, month = _add_months(year, month, 1)
        return buckets, current, previous, datetime(start_year, start_month, 1)
    current = today.isoformat()
    previous = (today - timedelta(days=1)).isoformat()
    start_day = today - timedelta(days=count - 1)
    cursor = start_day
    while cursor <= today:
        buckets[cursor.isoformat()] = _empty_bucket()
        cursor += timedelta(days=1)
    return buckets, current, previous, datetime.combine(start_day, datetime.min.time())
