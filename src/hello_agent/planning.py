"""使用 DeepSeek JSON Output 生成可验证的结构化计划。"""

import json
import logging
from collections.abc import Callable
from typing import Literal

from openai import OpenAI
from pydantic import BaseModel, Field, ValidationError

from hello_agent.model_routing import create_chat_completion, resolve_model


logger = logging.getLogger(__name__)


class PlanStep(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=300)
    minutes: int = Field(ge=5, le=480)


class GoalPlan(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    summary: str = Field(min_length=1, max_length=300)
    priority: Literal["low", "medium", "high"]
    steps: list[PlanStep] = Field(min_length=1, max_length=6)

    @property
    def total_minutes(self) -> int:
        return sum(step.minutes for step in self.steps)

    def to_markdown(self) -> str:
        priority_labels = {"low": "低", "medium": "中", "high": "高"}
        lines = [
            f"## {self.title}",
            "",
            self.summary,
            "",
            f"优先级：**{priority_labels[self.priority]}** · "
            f"预计用时：**{self.total_minutes} 分钟**",
            "",
        ]
        for index, step in enumerate(self.steps, start=1):
            lines.extend(
                [
                    f"### {index}. {step.title}（{step.minutes} 分钟）",
                    step.description,
                    "",
                ]
            )
        return "\n".join(lines).strip()


PLAN_SYSTEM_PROMPT = """
你是一名学习计划助手。请把用户目标拆成可以立即执行的计划，并且只输出 JSON。
JSON 必须严格使用下面的结构，不要增加其他字段：
{
  "title": "计划标题",
  "summary": "一句话说明",
  "priority": "medium",
  "steps": [
    {
      "title": "步骤标题",
      "description": "具体行动说明",
      "minutes": 30
    }
  ]
}
要求：priority 只能是 low、medium、high 三个英文值之一；生成 2 到 6 个步骤；
minutes 必须是 5 到 480 之间的整数。不要使用 Markdown 代码块。
""".strip()


def _parse_plan(content: str) -> GoalPlan:
    """兼容模型偶尔附加的 Markdown 代码围栏和中文优先级。"""
    cleaned = content.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        cleaned = cleaned[start : end + 1]

    data = json.loads(cleaned)
    if isinstance(data, dict):
        priority_aliases = {
            "低": "low",
            "低优先级": "low",
            "中": "medium",
            "中优先级": "medium",
            "高": "high",
            "高优先级": "high",
        }
        priority = data.get("priority")
        if isinstance(priority, str):
            data["priority"] = priority_aliases.get(
                priority.strip(), priority.strip().lower()
            )
    return GoalPlan.model_validate(data)


def _format_validation_error(error: json.JSONDecodeError | ValidationError) -> str:
    if isinstance(error, json.JSONDecodeError):
        return f"JSON 语法错误：{error.msg}"
    details = []
    for item in error.errors()[:5]:
        location = ".".join(str(part) for part in item["loc"])
        details.append(f"{location}: {item['msg']}")
    return "；".join(details)


def generate_goal_plan(
    client: OpenAI,
    goal: str,
    model: str,
    usage_callback: Callable[..., None] | None = None,
) -> GoalPlan:
    goal = goal.strip()
    if not goal:
        raise ValueError("目标不能为空。")

    messages = [
        {"role": "system", "content": PLAN_SYSTEM_PROMPT},
        {"role": "user", "content": f"请为这个目标生成 JSON 计划：{goal}"},
    ]
    response, used_model = create_chat_completion(
        client,
        role="strong",
        model=model,
        messages=messages,
        response_format={"type": "json_object"},
        max_tokens=1600,
    )
    if usage_callback is not None:
        usage_callback(response, used_model)
    content = response.choices[0].message.content
    if not content:
        raise RuntimeError("DeepSeek 返回了空的计划。")

    try:
        return _parse_plan(content)
    except (json.JSONDecodeError, ValidationError) as first_error:
        validation_message = _format_validation_error(first_error)
        logger.warning("计划首次校验失败，将自动修正：%s", validation_message)

    repair_response, repair_model = create_chat_completion(
        client,
        role="strong",
        model=model,
        messages=[
            *messages,
            {"role": "assistant", "content": content},
            {
                "role": "user",
                "content": (
                    "上面的结果没有通过程序校验。请修正后重新输出完整 JSON，"
                    f"不要解释、不要使用代码块。校验问题：{validation_message}"
                ),
            },
        ],
        response_format={"type": "json_object"},
        max_tokens=1600,
    )
    if usage_callback is not None:
        usage_callback(repair_response, repair_model)
    repaired_content = repair_response.choices[0].message.content
    if not repaired_content:
        raise RuntimeError("DeepSeek 自动修正时返回了空计划。")
    try:
        return _parse_plan(repaired_content)
    except (json.JSONDecodeError, ValidationError) as second_error:
        logger.warning(
            "计划自动修正后仍校验失败：%s",
            _format_validation_error(second_error),
        )
        raise RuntimeError(
            "DeepSeek 返回的计划格式不符合要求，自动修正后仍然失败。请换一种简短目标重试。"
        ) from second_error


def configured_model(default_model: str) -> str:
    """计划/写作类任务默认走强模型；未配置 STRONG 时回落到 DEEPSEEK_MODEL。"""
    return resolve_model("strong") or default_model
