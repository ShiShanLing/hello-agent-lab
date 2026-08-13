"""多 Agent 协作：规划、可调用工具的执行、可打回的审核。"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
import json
from time import perf_counter
from typing import Literal

from hello_agent.app import AgentSession, ToolActivity


MAX_REVISIONS = 1

PLANNER_INSTRUCTIONS = (
    "你是任务规划专家。"
    "把用户目标拆成清晰、可验证的步骤，指出必要假设、风险，以及执行阶段应调用的工具。"
    "只能建议下方「可用工具」清单中的工具，不要发明不存在的工具名。"
    "不要直接给出最终答案，也不要假装已经调用过工具。"
)

EXECUTOR_INSTRUCTIONS = (
    "你是领域执行专家。严格依据用户目标和规划完成任务，并在需要时真实调用工具。"
    "精确计算用 calculate；短 Python 处理用 run_python；中国天气用 get_weather；"
    "记录待办用 add_todo，查看计划用 list_todo_plans / get_todo_plan / list_todos；"
    "稳定偏好或约束用 remember_fact；知识库问题优先依据已提供资料，必要时 search_knowledge 或 web_search；"
    "用户已导入的 OpenAPI 工具可直接按名称调用；若结果带 offloaded=true，需要全文时用 fetch_tool_result。"
    "不要心算，不要假装已经保存或查询过。"
    "不能完成需要用户当场确认的操作（例如 complete_todo、确认旅行计划、OpenAPI 写操作）。"
    "给出具体、可执行的结果；事实不确定时明确说明。"
)

REVIEWER_INSTRUCTIONS = (
    "你是质量审核与交付专家。"
    "检查执行结果是否遗漏、算错、未使用必要工具，或与用户目标不符。"
    "若规划要求调用工具而执行结果没有对应工具证据，必须打回。"
    "若必须返工，调用 request_revision，并写清缺什么、应调用哪些工具。"
    "若可以交付，调用 approve_delivery，answer 必须是可直接给用户的最终中文答案，"
    "不要提及内部 Agent、审核过程或工具名称。"
)

APPROVE_DELIVERY_TOOL = {
    "type": "function",
    "function": {
        "name": "approve_delivery",
        "description": "执行结果合格，提交给用户的最终答案。",
        "parameters": {
            "type": "object",
            "properties": {
                "answer": {
                    "type": "string",
                    "description": "可直接交付给用户的最终中文答案",
                }
            },
            "required": ["answer"],
            "additionalProperties": False,
        },
    },
}

REQUEST_REVISION_TOOL = {
    "type": "function",
    "function": {
        "name": "request_revision",
        "description": "执行结果不合格，打回执行 Agent 返工。",
        "parameters": {
            "type": "object",
            "properties": {
                "feedback": {
                    "type": "string",
                    "description": "必须修改的问题和建议调用的工具",
                }
            },
            "required": ["feedback"],
            "additionalProperties": False,
        },
    },
}

REVIEWER_TOOLS = [APPROVE_DELIVERY_TOOL, REQUEST_REVISION_TOOL]


@dataclass(frozen=True)
class CollaborationEvent:
    agent: str
    label: str
    status: Literal[
        "running",
        "completed",
        "failed",
        "revision",
        "waiting_approval",
        "tool_calling",
        "tool_completed",
        "tool_failed",
    ]
    detail: str
    content: str | None = None
    duration_ms: int = 0
    stage_id: str | None = None
    call_id: str | None = None
    tool_name: str | None = None
    tool_source: str | None = None
    collaboration_id: str | None = None


def collaboration_tool_catalog(tools: list[dict[str, object]]) -> str:
    """给规划 Agent 看的可读工具清单，避免建议不存在的工具。"""
    lines: list[str] = []
    for tool in tools:
        function = tool.get("function")
        if not isinstance(function, dict):
            continue
        name = str(function.get("name") or "").strip()
        if not name:
            continue
        description = str(function.get("description") or "").strip()
        if description:
            lines.append(f"- {name}：{description[:120]}")
        else:
            lines.append(f"- {name}")
    if not lines:
        return "可用工具：无（仅能基于已有文字完成）。"
    return "可用工具：\n" + "\n".join(lines)


def _parse_reviewer_arguments(arguments: str) -> dict[str, str]:
    try:
        payload = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        key: str(value).strip()
        for key, value in payload.items()
        if isinstance(value, str)
    }


def _activity_event(agent: str, label: str, activity: ToolActivity) -> CollaborationEvent:
    status: Literal["tool_calling", "tool_completed", "tool_failed"]
    if activity.status == "calling":
        status = "tool_calling"
    elif activity.status == "failed":
        status = "tool_failed"
    else:
        status = "tool_completed"
    return CollaborationEvent(
        agent=agent,
        label=label,
        status=status,
        detail=activity.message,
        stage_id=activity.call_id,
        call_id=activity.call_id,
        tool_name=activity.tool_name,
        tool_source=activity.source,
    )


def _run_role(
    agent: AgentSession,
    *,
    role_agent: str,
    label: str,
    stage_id: str,
    running_detail: str,
    completed_detail: str,
    system: str,
    prompt: str,
    tools: list[dict[str, object]] | None,
    include_knowledge: bool,
    tool_handler=None,
    model_role: Literal["cheap", "strong", "default"] = "strong",
) -> Iterator[CollaborationEvent | str]:
    yield CollaborationEvent(
        role_agent, label, "running", running_detail, stage_id=stage_id,
    )
    started = perf_counter()
    content = ""
    for event in agent.run_ephemeral(
        system,
        prompt,
        tools=tools,
        include_knowledge=include_knowledge,
        tool_handler=tool_handler,
        model_role=model_role,
    ):
        if isinstance(event, ToolActivity):
            yield _activity_event(role_agent, label, event)
            continue
        content = event
    duration_ms = max(0, int((perf_counter() - started) * 1000))
    agent._record_step("sub_agent", label, "success", started, completed_detail)
    yield CollaborationEvent(
        role_agent,
        label,
        "completed",
        completed_detail,
        content or None,
        duration_ms,
        stage_id=stage_id,
    )
    yield content


def _run_planner(goal: str, agent: AgentSession) -> Iterator[CollaborationEvent | str]:
    executor_tools = agent.collaboration_tools()
    tool_catalog = collaboration_tool_catalog(executor_tools)
    yield from _run_role(
        agent,
        role_agent="planner",
        label="规划 Agent",
        stage_id="planner",
        running_detail="正在分析目标并制定可调用工具的执行方案",
        completed_detail="已产出执行方案",
        system=PLANNER_INSTRUCTIONS,
        prompt=(
            f"{goal}\n\n{tool_catalog}\n\n"
            "请输出分步计划，并在相关步骤旁标注建议调用的工具名。"
        ),
        tools=[],
        include_knowledge=False,
        model_role="cheap",
    )


def _run_executor_and_reviewer(
    goal: str,
    plan: str,
    agent: AgentSession,
    *,
    max_revisions: int = MAX_REVISIONS,
) -> Iterator[CollaborationEvent]:
    executor_tools = agent.collaboration_tools()
    tool_catalog = collaboration_tool_catalog(executor_tools)
    feedback = ""
    execution = ""
    for attempt in range(max_revisions + 1):
        stage_id = f"executor-{attempt + 1}"
        prompt = (
            f"用户目标：\n{goal}\n\n规划 Agent 的方案：\n{plan}"
            f"\n\n{tool_catalog}"
        )
        if feedback:
            prompt += (
                f"\n\n审核 Agent 的返工意见：\n{feedback}\n"
                "请按意见修正，并在需要时重新调用工具。"
            )
        running = (
            "正在根据审核意见返工并调用工具"
            if feedback
            else "正在根据计划调用工具并给出结果"
        )
        completed = "已根据审核意见完成修正" if feedback else "已完成初步结果"
        for event in _run_role(
            agent,
            role_agent="executor",
            label="执行 Agent",
            stage_id=stage_id,
            running_detail=running,
            completed_detail=completed,
            system=EXECUTOR_INSTRUCTIONS,
            prompt=prompt,
            tools=executor_tools,
            include_knowledge=True,
            model_role="strong",
        ):
            if isinstance(event, CollaborationEvent):
                yield event
            else:
                execution = event

        decision: dict[str, str] = {}

        def reviewer_handler(name: str, arguments: str) -> str:
            payload = _parse_reviewer_arguments(arguments)
            if name == "request_revision":
                decision["status"] = "revise"
                decision["feedback"] = (
                    payload.get("feedback")
                    or "请补充具体结果并调用必要工具。"
                )
                return json.dumps(
                    {"status": "revise", "feedback": decision["feedback"]},
                    ensure_ascii=False,
                )
            answer = payload.get("answer") or ""
            decision["status"] = "accept"
            decision["answer"] = answer
            return json.dumps(
                {"status": "accepted", "answer": answer},
                ensure_ascii=False,
            )

        allow_revision = attempt < max_revisions
        reviewer_prompt = (
            f"用户目标：\n{goal}\n\n规划：\n{plan}"
            f"\n\n执行结果：\n{execution}"
        )
        if not allow_revision:
            reviewer_prompt += (
                "\n\n这是最后一轮审核，必须调用 approve_delivery 提交最终答案，不能再打回。"
            )
        reviewer_tools = (
            REVIEWER_TOOLS if allow_revision else [APPROVE_DELIVERY_TOOL]
        )
        reviewer_text = ""
        stage_id = f"reviewer-{attempt + 1}"
        for event in _run_role(
            agent,
            role_agent="reviewer",
            label="审核 Agent",
            stage_id=stage_id,
            running_detail="正在检查完整性、工具使用和表达",
            completed_detail="已完成审核",
            system=REVIEWER_INSTRUCTIONS,
            prompt=reviewer_prompt,
            tools=reviewer_tools,
            include_knowledge=False,
            tool_handler=reviewer_handler,
            model_role="strong",
        ):
            if isinstance(event, CollaborationEvent):
                if event.status == "completed":
                    continue
                yield event
            else:
                reviewer_text = event

        if decision.get("status") == "revise" and allow_revision:
            feedback = decision["feedback"]
            yield CollaborationEvent(
                "reviewer",
                "审核 Agent",
                "revision",
                "已打回执行 Agent 返工",
                feedback,
                stage_id=stage_id,
            )
            continue

        final_answer = decision.get("answer") or reviewer_text or execution
        yield CollaborationEvent(
            "reviewer",
            "审核 Agent",
            "completed",
            "已完成审核并生成最终交付",
            final_answer,
            stage_id=stage_id,
        )
        return

    raise RuntimeError("协作审核未能给出最终答案。")


def run_collaboration(
    goal: str,
    agent: AgentSession,
    *,
    max_revisions: int = MAX_REVISIONS,
    pause_after_plan: bool = True,
    collaboration_id: str | None = None,
) -> Iterator[CollaborationEvent]:
    """规划 →（可选人机确认）→ 工具执行 → 审核打回。"""
    normalized_goal = goal.strip()
    if not normalized_goal:
        raise ValueError("协作目标不能为空。")

    plan = ""
    for event in _run_planner(normalized_goal, agent):
        if isinstance(event, CollaborationEvent):
            yield event
        else:
            plan = event

    if pause_after_plan:
        yield CollaborationEvent(
            agent="planner",
            label="规划 Agent",
            status="waiting_approval",
            detail="计划已就绪，等待你确认后开始执行",
            content=plan or None,
            stage_id="plan-gate",
            collaboration_id=collaboration_id,
        )
        return

    yield from _run_executor_and_reviewer(
        normalized_goal,
        plan,
        agent,
        max_revisions=max_revisions,
    )


def resume_collaboration(
    goal: str,
    plan: str,
    agent: AgentSession,
    *,
    max_revisions: int = MAX_REVISIONS,
) -> Iterator[CollaborationEvent]:
    """用户确认计划后，继续执行与审核。"""
    normalized_goal = goal.strip()
    normalized_plan = plan.strip()
    if not normalized_goal:
        raise ValueError("协作目标不能为空。")
    if not normalized_plan:
        raise ValueError("协作计划不能为空。")
    yield from _run_executor_and_reviewer(
        normalized_goal,
        normalized_plan,
        agent,
        max_revisions=max_revisions,
    )
