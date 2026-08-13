"""结构化旅行计划生成测试。"""

from datetime import date
import json
from types import SimpleNamespace
import unittest

from hello_agent.travel import generate_travel_plan


class FakeCompletions:
    def __init__(self, contents: list[str]) -> None:
        self.contents = iter(contents)

    def create(self, **_kwargs):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=next(self.contents))
                )
            ]
        )


class FakeClient:
    def __init__(self, contents: list[str]) -> None:
        self.chat = SimpleNamespace(completions=FakeCompletions(contents))


class TravelPlanTest(unittest.TestCase):
    def test_returns_questions_when_required_information_is_missing(self) -> None:
        content = json.dumps(
            {
                "status": "needs_input",
                "title": "北京旅行需求",
                "summary": "需要补充关键信息后才能生成可靠行程。",
                "origin": None,
                "destination": "北京",
                "start_date": None,
                "end_date": None,
                "travelers": None,
                "budget": None,
                "preferences": [],
                "missing_fields": [
                    "origin",
                    "start_date",
                    "end_date",
                    "travelers",
                    "budget",
                ],
                "clarification_questions": ["从哪里出发？", "计划哪几天出行？"],
                "days": [],
            },
            ensure_ascii=False,
        )

        plan = generate_travel_plan(
            FakeClient([content]),
            "我想去北京玩",
            "deepseek-test",
            today=date(2026, 8, 6),
        )

        self.assertEqual(plan.status, "needs_input")
        self.assertIn("origin", plan.missing_fields)
        self.assertEqual(plan.days, [])

    def test_returns_a_valid_day_by_day_itinerary(self) -> None:
        content = json.dumps(
            {
                "status": "ready",
                "title": "上海至北京双人旅行",
                "summary": "费用为规划估算，请以实际价格为准。",
                "origin": "上海",
                "destination": "北京",
                "start_date": "2026-10-01",
                "end_date": "2026-10-02",
                "travelers": 2,
                "budget": 5000,
                "preferences": ["历史文化"],
                "missing_fields": [],
                "clarification_questions": [],
                "days": [
                    {
                        "day_number": 1,
                        "date": "2026-10-01",
                        "title": "故宫与景山",
                        "activities": [
                            {
                                "time": "09:00",
                                "title": "参观故宫",
                                "location": "故宫博物院",
                                "description": "提前预约。",
                                "estimated_cost": 120,
                            }
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        )

        plan = generate_travel_plan(
            FakeClient([content]),
            "国庆上海到北京，两个人，预算5000元",
            "deepseek-test",
            today=date(2026, 8, 6),
        )

        self.assertEqual(plan.status, "ready")
        self.assertEqual(plan.estimated_total_cost, 120)
        self.assertIn("第 1 天", plan.to_markdown())


if __name__ == "__main__":
    unittest.main()
