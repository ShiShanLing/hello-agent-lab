"""隔离执行核心 Agent 能力评测，不污染用户业务数据。"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from hello_agent.app import AgentSession, SYSTEM_INSTRUCTIONS, create_client
from hello_agent.database import SqliteTodoStore, SqliteTravelStore, dispose_database_connection
from hello_agent.model_routing import create_chat_completion
from hello_agent.skills import list_skills, load_skill


AgentFactory = Callable[[str, Path], AgentSession]

ConfidenceLabel = Literal["high", "medium", "low"]
ScorerName = Literal["keyword", "exact", "llm_judge"]


class JudgeVerdict(BaseModel):
    passed: bool
    score: float = Field(ge=0, le=100)
    confidence: ConfidenceLabel
    failure_type: str | None = None
    failure_reason: str | None = None
    summary: str = Field(min_length=1, max_length=500)
    reasoning: str = Field(min_length=1)


@dataclass
class EvaluationCaseResult:
    passed: bool
    score: float
    confidence: ConfidenceLabel
    scorer: str
    expected: str
    actual: str
    tokens: int
    failure_type: str | None = None
    failure_reason: str | None = None
    judge_score: float | None = None
    judge_summary: str | None = None
    judge_reasoning: str | None = None
    signals: dict[str, object] | None = None


def _normalize_text(value: str) -> str:
    return " ".join(value.casefold().replace(",", " ").split())


def _confidence_label(value: float) -> ConfidenceLabel:
    if value >= 80:
        return "high"
    if value >= 55:
        return "medium"
    return "low"


def _keyword_score(answer: str, keywords: list[str]) -> tuple[float, list[str]]:
    normalized = _normalize_text(answer)
    cleaned = [item.strip() for item in keywords if item.strip()]
    if not cleaned:
        return 0.0, []
    matched = [
        keyword for keyword in cleaned
        if _normalize_text(keyword) in normalized
    ]
    return round(len(matched) / len(cleaned) * 100, 2), matched


def _exact_score(answer: str, expected_answer: str | None) -> float:
    if not expected_answer:
        return 0.0
    return 100.0 if _normalize_text(answer) == _normalize_text(expected_answer) else 0.0


def _judge_with_llm(
    *,
    prompt_content: str,
    model: str,
    input_text: str,
    answer: str,
    expected_keywords: list[str],
    expected_answer: str | None,
    judge_rubric: str | None,
) -> tuple[JudgeVerdict, int]:
    _ = model  # 评测对象模型；裁判固定走强模型角色 + 降级
    client = create_client()
    system_prompt = (
        "你是一名严格的 Agent 自动评测裁判。"
        "请仅输出 JSON，并根据用户问题、Agent 回答、期望关键词、标准答案和评分规则进行评分。"
        "score 必须是 0 到 100 的数字；confidence 只能是 high、medium、low。"
        "如果回答产生幻觉、违背约束、遗漏关键结论或没有完成任务，要明确写出 failure_type 和 failure_reason。"
    )
    rubric = judge_rubric or "根据任务完成度、事实性、是否满足关键约束综合评分。"
    response, _used_model = create_chat_completion(
        client,
        role="strong",
        response_format={"type": "json_object"},
        max_tokens=900,
        messages=[
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "prompt_context": prompt_content[:4000],
                        "input_text": input_text,
                        "answer": answer,
                        "expected_keywords": expected_keywords,
                        "expected_answer": expected_answer,
                        "judge_rubric": rubric,
                        "required_schema": {
                            "passed": True,
                            "score": 86,
                            "confidence": "high",
                            "failure_type": "missing_requirement_or_hallucination_or_tool_or_reasoning_or_format_or_runtime",
                            "failure_reason": "一句话说明失败原因，成功时可为 null",
                            "summary": "一句话总结结论",
                            "reasoning": "简要说明评分依据",
                        },
                    },
                    ensure_ascii=False,
                ),
            },
        ],
    )
    content = response.choices[0].message.content or ""
    usage = int(getattr(response.usage, "total_tokens", 0) or 0)
    try:
        verdict = JudgeVerdict.model_validate(json.loads(content))
    except (json.JSONDecodeError, ValidationError) as error:
        raise RuntimeError(f"裁判模型返回了无法解析的 JSON：{error}") from error
    return verdict, usage


def evaluate_dataset_case(
    *,
    prompt_content: str,
    model: str,
    case: dict[str, object],
    answer: str,
    answer_tokens: int,
    scorer_override: str | None = None,
    judge_enabled: bool = False,
) -> EvaluationCaseResult:
    expected_keywords = [str(item) for item in case.get("expected_keywords", [])]
    expected_answer = (
        str(case["expected_answer"]) if case.get("expected_answer") else None
    )
    judge_rubric = str(case["judge_rubric"]) if case.get("judge_rubric") else None
    scorer = str(scorer_override or case.get("scoring_method") or "keyword")
    total_tokens = answer_tokens

    keyword_score, matched_keywords = _keyword_score(answer, expected_keywords)
    exact_score = _exact_score(answer, expected_answer)
    signals = {
        "matched_keywords": matched_keywords,
        "keyword_count": len(expected_keywords),
        "keyword_score": keyword_score,
        "exact_score": exact_score,
    }

    if scorer == "exact":
        score = exact_score
        passed = score >= 100
        confidence = "high" if passed else "medium"
        return EvaluationCaseResult(
            passed=passed,
            score=score,
            confidence=confidence,
            scorer="exact",
            expected=expected_answer or "与标准答案完全一致",
            actual=answer[:500],
            tokens=total_tokens,
            failure_type=None if passed else "mismatch",
            failure_reason=None if passed else "回答与标准答案不一致。",
            signals=signals,
        )

    if scorer == "llm_judge" or judge_enabled:
        verdict, judge_tokens = _judge_with_llm(
            prompt_content=prompt_content,
            model=model,
            input_text=str(case["input_text"]),
            answer=answer,
            expected_keywords=expected_keywords,
            expected_answer=expected_answer,
            judge_rubric=judge_rubric,
        )
        total_tokens += judge_tokens
        return EvaluationCaseResult(
            passed=verdict.passed,
            score=round(verdict.score, 2),
            confidence=verdict.confidence,
            scorer="llm_judge",
            expected=expected_answer or (
                "包含关键词：" + "、".join(expected_keywords) if expected_keywords else "满足评分规则"
            ),
            actual=answer[:500],
            tokens=total_tokens,
            failure_type=verdict.failure_type,
            failure_reason=verdict.failure_reason,
            judge_score=round(verdict.score, 2),
            judge_summary=verdict.summary,
            judge_reasoning=verdict.reasoning,
            signals={**signals, "judge_rubric": judge_rubric},
        )

    passed = keyword_score >= 100 and bool(expected_keywords)
    return EvaluationCaseResult(
        passed=passed,
        score=keyword_score,
        confidence=_confidence_label(keyword_score),
        scorer="keyword",
        expected="包含关键词：" + "、".join(expected_keywords),
        actual=answer[:500],
        tokens=total_tokens,
        failure_type=None if passed else "missing_keyword",
        failure_reason=None if passed else "回答未覆盖全部关键预期。",
        signals=signals,
    )


def summarize_case_confidence(case_results: list[EvaluationCaseResult]) -> ConfidenceLabel:
    if not case_results:
        return "low"
    score = sum(
        100 if item.confidence == "high" else 65 if item.confidence == "medium" else 35
        for item in case_results
    ) / len(case_results)
    return _confidence_label(score)


def run_dataset_evaluation(
    result_store,
    run_id: str,
    cases: list[dict[str, object]],
    prompt_content: str,
    model: str,
    progress_callback: Callable[[int], None] | None = None,
    agent_factory: AgentFactory | None = None,
    scorer: str | None = None,
    judge_enabled: bool = False,
) -> dict[str, object]:
    """在隔离存储中运行用户测试集，并支持可插拔评分与裁判。"""
    enabled_cases = [case for case in cases if bool(case.get("enabled", True))]
    passed_cases = 0
    total_tokens = 0
    case_results: list[EvaluationCaseResult] = []
    with TemporaryDirectory(prefix="hello-agent-dataset-eval-") as directory:
        database_path = Path(directory) / "evaluation.db"
        for index, case in enumerate(enabled_cases, start=1):
            case_id = str(case["id"])
            namespace = f"evaluation-{run_id}-{case_id}"
            agent = (
                agent_factory(case_id, database_path)
                if agent_factory is not None
                else AgentSession(
                    todo_store=SqliteTodoStore(database_path, namespace),
                    travel_store=SqliteTravelStore(database_path, namespace),
                    conversation_store=None,
                    auto_search_knowledge=False,
                    system_instructions=(
                        f"{SYSTEM_INSTRUCTIONS}\n\n当前 Prompt 版本的补充规则：\n{prompt_content}"
                    ),
                    model=model,
                )
            )
            started_at = perf_counter()
            error_message = None
            try:
                answer = agent.ask(str(case["input_text"]))
                case_result = evaluate_dataset_case(
                    prompt_content=prompt_content,
                    model=model,
                    case=case,
                    answer=answer,
                    answer_tokens=int(agent.model_usage["total_tokens"]),
                    scorer_override=scorer,
                    judge_enabled=judge_enabled,
                )
            except Exception as error:
                case_result = EvaluationCaseResult(
                    passed=False,
                    score=0,
                    confidence="low",
                    scorer=str(scorer or case.get("scoring_method") or "keyword"),
                    expected=(
                        str(case.get("expected_answer"))
                        if case.get("expected_answer")
                        else "包含关键词：" + "、".join(
                            str(item) for item in case.get("expected_keywords", [])
                        )
                    ),
                    actual="执行异常",
                    tokens=0,
                    failure_type="runtime",
                    failure_reason=str(error),
                    signals={"exception": type(error).__name__},
                )
                error_message = str(error)
            duration_ms = int((perf_counter() - started_at) * 1000)
            passed_cases += int(case_result.passed)
            total_tokens += case_result.tokens
            case_results.append(case_result)
            result_store.add_case(
                run_id, case_id, str(case["name"]), str(case["category"]),
                case_result.passed, duration_ms,
                case_result.expected,
                case_result.actual,
                error_message,
                score=case_result.score,
                scorer=case_result.scorer,
                confidence=case_result.confidence,
                failure_type=case_result.failure_type,
                failure_reason=case_result.failure_reason,
                judge_score=case_result.judge_score,
                judge_summary=case_result.judge_summary,
                judge_reasoning=case_result.judge_reasoning,
                signals=case_result.signals,
            )
            if progress_callback:
                progress_callback(max(1, round(index / max(1, len(enabled_cases)) * 100)))
        dispose_database_connection(database_path)
    average_score = round(
        sum(item.score for item in case_results) / len(case_results), 2
    ) if case_results else 0.0
    return {
        "passed_cases": passed_cases,
        "total_tokens": total_tokens,
        "score": average_score,
        "confidence": summarize_case_confidence(case_results),
    }


def run_core_evaluation(
    result_store,
    run_id: str,
    agent_factory: AgentFactory | None = None,
) -> dict[str, int]:
    """运行 4 个真实模型用例和 3 个确定性工程用例。"""
    passed_cases = 0
    total_tokens = 0

    with TemporaryDirectory(prefix="hello-agent-eval-") as directory:
        database_path = Path(directory) / "evaluation.db"

        def create_agent(case_id: str) -> AgentSession:
            if agent_factory is not None:
                return agent_factory(case_id, database_path)
            namespace = f"evaluation-{run_id}-{case_id}"
            return AgentSession(
                todo_store=SqliteTodoStore(database_path, namespace),
                travel_store=SqliteTravelStore(database_path, namespace),
                conversation_store=None,
                auto_search_knowledge=False,
            )

        def record(
            case_id: str,
            name: str,
            category: str,
            expected: str,
            evaluator: Callable[[], tuple[bool, str, int]],
        ) -> None:
            nonlocal passed_cases, total_tokens
            started_at = perf_counter()
            try:
                passed, actual, tokens = evaluator()
                error_message = None
            except Exception as error:  # 单个用例失败不能中断整套评测。
                passed, actual, tokens = False, "执行异常", 0
                error_message = str(error)
            duration_ms = int((perf_counter() - started_at) * 1000)
            total_tokens += tokens
            if passed:
                passed_cases += 1
            result_store.add_case(
                run_id,
                case_id,
                name,
                category,
                passed,
                duration_ms,
                expected,
                actual,
                error_message,
            )

        def calculator_case() -> tuple[bool, str, int]:
            agent = create_agent("calculator")
            answer = agent.ask("请精确计算 123 × 456，并告诉我结果。")
            tool_names = [
                str(step["name"])
                for step in agent.trace_steps
                if step["step_type"] == "tool"
            ]
            passed = "calculate" in tool_names and "56088" in answer.replace(",", "")
            return passed, f"工具={tool_names}；回答={answer[:180]}", agent.model_usage["total_tokens"]

        def todo_case() -> tuple[bool, str, int]:
            agent = create_agent("todo")
            answer = agent.ask("请把‘预订北京酒店’记录到我的 Todo。")
            todos = agent.todo_store.list_all()
            tool_names = [
                str(step["name"])
                for step in agent.trace_steps
                if step["step_type"] == "tool"
            ]
            passed = (
                "add_todo" in tool_names
                and any("预订北京酒店" in todo["title"] for todo in todos)
            )
            return passed, f"工具={tool_names}；Todo={todos}；回答={answer[:100]}", agent.model_usage["total_tokens"]

        def incomplete_travel_case() -> tuple[bool, str, int]:
            agent = create_agent("travel_missing")
            plan = agent.create_travel_plan("我想去北京旅游")
            passed = (
                plan.status == "needs_input"
                and bool(plan.missing_fields)
                and bool(plan.clarification_questions)
            )
            return passed, f"状态={plan.status}；缺失={plan.missing_fields}", agent.model_usage["total_tokens"]

        def complete_travel_case() -> tuple[bool, str, int]:
            agent = create_agent("travel_complete")
            plan = agent.create_travel_plan(
                "2026年10月1日至2日，2个人从上海去杭州，预算5000元，喜欢历史文化。"
            )
            passed = (
                plan.status == "ready"
                and bool(plan.destination)
                and "杭州" in plan.destination
                and len(plan.days) == 2
                and plan.budget == 5000
            )
            return passed, (
                f"状态={plan.status}；目的地={plan.destination}；"
                f"天数={len(plan.days)}；预算={plan.budget}"
            ), agent.model_usage["total_tokens"]

        def isolation_case() -> tuple[bool, str, int]:
            first = SqliteTodoStore(database_path, "eval-user-a")
            second = SqliteTodoStore(database_path, "eval-user-b")
            plan = first.create_plan("私有计划")
            try:
                second.add("越权任务", plan_id=int(plan["id"]))
            except ValueError:
                return True, "其他账号访问被拒绝", 0
            return False, "其他账号成功写入了私有计划", 0

        def idempotency_case() -> tuple[bool, str, int]:
            store = SqliteTodoStore(database_path, "eval-idempotency")
            steps = [
                {"title": "订票", "description": "预订车票", "minutes": 20}
            ]
            first = store.create_plan_with_steps(
                "旅行准备", "测试幂等", "high", steps,
                source_travel_plan_id=999,
            )
            second = store.create_plan_with_steps(
                "旅行准备", "测试幂等", "high", steps,
                source_travel_plan_id=999,
            )
            passed = first["id"] == second["id"] and len(store.list_plans()["plans"]) == 1
            return passed, f"首次计划={first['id']}；重复计划={second['id']}", 0

        def skill_catalog_case() -> tuple[bool, str, int]:
            catalog = list_skills()
            names = {item.name for item in catalog}
            skill = load_skill("precise-calculate")
            passed = {
                "precise-calculate",
                "todo-hygiene",
                "goal-planning",
                "knowledge-first",
            }.issubset(names) and "calculate" in skill.body
            return passed, f"目录={sorted(names)}", 0

        record("calculator_tool", "精确计算工具路由", "tool_routing", "调用 calculate 且结果为 56088", calculator_case)
        record("todo_write", "Todo 真实写入", "tool_routing", "调用 add_todo 并写入隔离存储", todo_case)
        record("travel_missing", "旅行信息缺失追问", "structured_output", "返回 needs_input 和追问", incomplete_travel_case)
        record("travel_complete", "完整旅行计划生成", "structured_output", "返回两天杭州行程且预算正确", complete_travel_case)
        record("account_isolation", "账号数据隔离", "security", "拒绝跨账号写入", isolation_case)
        record("todo_idempotency", "Todo 同步幂等", "workflow", "重复同步只生成一个计划", idempotency_case)
        record("skill_catalog", "Skill 目录与按需加载", "skills", "能列出 4 个 Skill 并加载 precise-calculate", skill_catalog_case)

        dispose_database_connection(database_path)

    return {"passed_cases": passed_cases, "total_tokens": total_tokens}
