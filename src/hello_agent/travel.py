"""使用 DeepSeek 生成经过校验的结构化旅行计划。"""

from datetime import date
import json
import logging
from collections.abc import Callable
from typing import Literal

from openai import OpenAI
from pydantic import BaseModel, Field, ValidationError, model_validator

from hello_agent.model_routing import create_chat_completion


logger = logging.getLogger(__name__)

TravelStatus = Literal["needs_input", "ready", "confirmed"]
MissingTravelField = Literal[
    "origin",
    "destination",
    "start_date",
    "end_date",
    "travelers",
    "budget",
]


class TravelActivity(BaseModel):
    time: str = Field(min_length=1, max_length=20)
    title: str = Field(min_length=1, max_length=80)
    location: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=300)
    estimated_cost: int = Field(ge=0, le=100_000)


class TravelDay(BaseModel):
    day_number: int = Field(ge=1, le=30)
    date: date
    title: str = Field(min_length=1, max_length=100)
    activities: list[TravelActivity] = Field(min_length=1, max_length=8)


class TravelPlan(BaseModel):
    status: TravelStatus
    title: str = Field(min_length=1, max_length=100)
    summary: str = Field(min_length=1, max_length=400)
    origin: str | None = Field(default=None, max_length=100)
    destination: str | None = Field(default=None, max_length=100)
    start_date: date | None = None
    end_date: date | None = None
    travelers: int | None = Field(default=None, ge=1, le=50)
    budget: int | None = Field(default=None, ge=0, le=10_000_000)
    preferences: list[str] = Field(default_factory=list, max_length=10)
    missing_fields: list[MissingTravelField] = Field(default_factory=list)
    clarification_questions: list[str] = Field(default_factory=list, max_length=6)
    days: list[TravelDay] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def validate_status_payload(self) -> "TravelPlan":
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("结束日期不能早于开始日期。")
        if self.status == "needs_input":
            if not self.missing_fields or not self.clarification_questions:
                raise ValueError("信息不完整时必须列出缺失字段和追问。")
            if self.days:
                raise ValueError("信息不完整时不能生成正式行程。")
            return self

        required_values = {
            "origin": self.origin,
            "destination": self.destination,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "travelers": self.travelers,
            "budget": self.budget,
        }
        missing = [name for name, value in required_values.items() if value is None]
        if missing:
            raise ValueError(f"完整行程缺少字段：{', '.join(missing)}")
        if self.missing_fields or self.clarification_questions:
            raise ValueError("完整行程不能保留缺失字段或追问。")
        if not self.days:
            raise ValueError("完整行程至少需要一天安排。")
        return self

    @property
    def estimated_total_cost(self) -> int:
        return sum(
            activity.estimated_cost
            for day in self.days
            for activity in day.activities
        )

    def to_markdown(self) -> str:
        if self.status == "needs_input":
            lines = [f"## {self.title}", "", self.summary, "", "还需要确认："]
            lines.extend(
                f"- {question}" for question in self.clarification_questions
            )
            return "\n".join(lines)

        lines = [
            f"## {self.title}",
            "",
            self.summary,
            "",
            f"路线：**{self.origin} → {self.destination}**",
            f"日期：**{self.start_date} 至 {self.end_date}** · "
            f"人数：**{self.travelers} 人** · 预算：**¥{self.budget}**",
            f"当前行程项目预计：**¥{self.estimated_total_cost}**",
            "",
        ]
        for day in self.days:
            lines.append(f"### 第 {day.day_number} 天 · {day.date} · {day.title}")
            for activity in day.activities:
                lines.append(
                    f"- **{activity.time} {activity.title}**｜{activity.location}｜"
                    f"约 ¥{activity.estimated_cost}：{activity.description}"
                )
            lines.append("")
        return "\n".join(lines).strip()


TRAVEL_SYSTEM_PROMPT = """
你是一名谨慎的中国旅行规划 Agent。请分析用户需求，并且只输出 JSON。
JSON 必须严格使用下面的结构，不要增加字段：
{
  "status": "ready",
  "title": "旅行标题",
  "summary": "行程摘要",
  "origin": "出发地或 null",
  "destination": "目的地或 null",
  "start_date": "YYYY-MM-DD 或 null",
  "end_date": "YYYY-MM-DD 或 null",
  "travelers": 2,
  "budget": 5000,
  "preferences": ["历史文化"],
  "missing_fields": [],
  "clarification_questions": [],
  "days": [
    {
      "day_number": 1,
      "date": "YYYY-MM-DD",
      "title": "当天主题",
      "activities": [
        {
          "time": "09:00",
          "title": "活动标题",
          "location": "地点",
          "description": "具体建议",
          "estimated_cost": 100
        }
      ]
    }
  ]
}

关键规则：
1. 出发地、目的地、开始日期、结束日期、人数、总预算全部明确时，status 才能是 ready。
2. 任一关键字段缺失时，status 必须是 needs_input；列出 missing_fields 和自然简短的 clarification_questions；days 必须是空数组。
3. ready 时 missing_fields 和 clarification_questions 必须为空，并按日期生成每天安排。
4. 所有 estimated_cost 使用人民币整数；总预计费用尽量不超过用户预算，并预留交通或应急费用。
5. 不虚构实时票价、营业状态或订票成功；summary 中明确价格只是规划估算。
6. 当前日期会由用户消息提供。不要输出 Markdown 代码块。
""".strip()


def _extract_json(content: str) -> str:
    cleaned = content.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    return cleaned[start : end + 1] if start >= 0 and end > start else cleaned


def _parse_travel_plan(content: str) -> TravelPlan:
    return TravelPlan.model_validate(json.loads(_extract_json(content)))


def _validation_message(error: json.JSONDecodeError | ValidationError) -> str:
    if isinstance(error, json.JSONDecodeError):
        return f"JSON 语法错误：{error.msg}"
    return "；".join(
        f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
        for item in error.errors()[:8]
    )


def generate_travel_plan(
    client: OpenAI,
    request: str,
    model: str,
    today: date | None = None,
    previous_plan: TravelPlan | None = None,
    usage_callback: Callable[..., None] | None = None,
) -> TravelPlan:
    request = request.strip()
    if not request:
        raise ValueError("旅行需求不能为空。")
    current_date = today or date.today()
    previous_context = ""
    if previous_plan is not None:
        previous_context = (
            "\n这是同一个旅行任务上一次保存的 JSON 状态。请保留其中已明确的信息，"
            "用本次补充更新缺失字段，不要要求用户重复提供：\n"
            f"{previous_plan.model_dump_json()}\n"
        )
    messages = [
        {"role": "system", "content": TRAVEL_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"当前日期：{current_date.isoformat()}"
                f"{previous_context}\n本次旅行需求或补充信息：{request}"
            ),
        },
    ]
    response, used_model = create_chat_completion(
        client,
        role="strong",
        model=model,
        messages=messages,
        response_format={"type": "json_object"},
        max_tokens=5000,
    )
    if usage_callback is not None:
        usage_callback(response, used_model)
    content = response.choices[0].message.content
    if not content:
        raise RuntimeError("DeepSeek 返回了空的旅行计划。")
    try:
        return _parse_travel_plan(content)
    except (json.JSONDecodeError, ValidationError) as first_error:
        problem = _validation_message(first_error)
        logger.warning("旅行计划首次校验失败，将自动修正：%s", problem)

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
                    "上面的 JSON 未通过程序校验。请根据校验问题重新输出完整 JSON，"
                    f"不要解释。校验问题：{problem}"
                ),
            },
        ],
        response_format={"type": "json_object"},
        max_tokens=5000,
    )
    if usage_callback is not None:
        usage_callback(repair_response, repair_model)
    repaired = repair_response.choices[0].message.content
    if not repaired:
        raise RuntimeError("DeepSeek 自动修正旅行计划时返回了空内容。")
    try:
        return _parse_travel_plan(repaired)
    except (json.JSONDecodeError, ValidationError) as second_error:
        raise RuntimeError(
            "DeepSeek 返回的旅行计划格式不符合要求，自动修正后仍然失败。"
        ) from second_error
