"""Agent 可以调用的本地工具。"""

import ast
import json
import math
import operator
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import httpx

from hello_agent.china_locations import resolve_china_location
from hello_agent.code_sandbox import CodeSandboxError, run_python
from hello_agent.web_search import search_web


Number = int | float
Todo = dict[str, int | str | bool]
MAX_EXPRESSION_LENGTH = 100
MAX_ABSOLUTE_VALUE = 1e100
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

RUN_PYTHON_TOOL = {
    "type": "function",
    "function": {
        "name": "run_python",
        "description": (
            "在隔离沙箱中执行短 Python 代码，适合数据处理、列表推导、小段算法或需要 print 输出的计算。"
            "禁止访问网络、文件和系统命令；可用 math/statistics/json/re/datetime 等安全模块。"
            "简单四则运算优先使用 calculate。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "要执行的 Python 代码，请用 print 输出结果",
                },
                "timeout_seconds": {
                    "type": "number",
                    "description": "超时秒数，默认 5，最大 10",
                },
            },
            "required": ["code"],
            "additionalProperties": False,
        },
    },
}

WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "联网搜索公开网页摘要。仅在本地知识库未命中或依据不足时使用；"
            "回答时必须标注网页标题与 URL，不要编造搜索结果里没有的内容。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词或完整问题",
                },
                "limit": {
                    "type": "integer",
                    "description": "返回条数，默认 3，最多 5",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
}

CALCULATOR_TOOL = {
    "type": "function",
    "function": {
        "name": "calculate",
        "description": "计算一个数学表达式，支持加、减、乘、除、取余和乘方。",
        "parameters": {
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "需要计算的表达式，例如 (12 + 8) * 3",
                }
            },
            "required": ["expression"],
            "additionalProperties": False,
        },
    },
}

ADD_TODO_TOOL = {
    "type": "function",
    "function": {
        "name": "add_todo",
        "description": "把用户明确要求记录的待办事项添加到任务列表。",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "简洁、明确的待办事项内容",
                }
            },
            "required": ["title"],
            "additionalProperties": False,
        },
    },
}

LIST_TODOS_TOOL = {
    "type": "function",
    "function": {
        "name": "list_todos",
        "description": "查看当前会话中已经保存的全部待办事项。",
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
}

COMPLETE_TODO_TOOL = {
    "type": "function",
    "function": {
        "name": "complete_todo",
        "description": "根据任务 ID 将一条待办事项标记为已完成。此操作需要用户确认。",
        "parameters": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "integer",
                    "description": "需要标记为完成的任务 ID",
                }
            },
            "required": ["id"],
            "additionalProperties": False,
        },
    },
}

LIST_TODO_PLANS_TOOL = {
    "type": "function",
    "function": {
        "name": "list_todo_plans",
        "description": "查看当前登录用户的全部计划、完成进度和计划内任务。",
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
}

GET_TODO_PLAN_TOOL = {
    "type": "function",
    "function": {
        "name": "get_todo_plan",
        "description": "根据计划 ID 查看一个计划的说明、进度和全部步骤。",
        "parameters": {
            "type": "object",
            "properties": {
                "plan_id": {
                    "type": "integer",
                    "description": "需要查看的计划 ID",
                }
            },
            "required": ["plan_id"],
            "additionalProperties": False,
        },
    },
}

LIST_TRAVEL_PLANS_TOOL = {
    "type": "function",
    "function": {
        "name": "list_travel_plans",
        "description": "查看当前登录账号中真正保存的旅行计划、路线、日期和确认状态。不要用 Todo 计划代替旅行计划。",
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
}

GET_TRAVEL_PLAN_TOOL = {
    "type": "function",
    "function": {
        "name": "get_travel_plan",
        "description": "根据旅行计划 ID 查看路线、预算、每日行程和具体活动。",
        "parameters": {
            "type": "object",
            "properties": {
                "travel_plan_id": {
                    "type": "integer",
                    "description": "旅行计划 ID",
                }
            },
            "required": ["travel_plan_id"],
            "additionalProperties": False,
        },
    },
}

CONFIRM_TRAVEL_PLAN_TOOL = {
    "type": "function",
    "function": {
        "name": "confirm_travel_plan",
        "description": "仅当用户明确说确认或保存某个已完成的旅行计划时，将草稿标记为已确认。重复确认不会创建新记录。",
        "parameters": {
            "type": "object",
            "properties": {
                "travel_plan_id": {
                    "type": "integer",
                    "description": "需要确认的旅行计划 ID",
                }
            },
            "required": ["travel_plan_id"],
            "additionalProperties": False,
        },
    },
}

ALL_TOOLS = [
    CALCULATOR_TOOL,
    RUN_PYTHON_TOOL,
    WEB_SEARCH_TOOL,
    ADD_TODO_TOOL,
    LIST_TODOS_TOOL,
    COMPLETE_TODO_TOOL,
    LIST_TODO_PLANS_TOOL,
    GET_TODO_PLAN_TOOL,
    LIST_TRAVEL_PLANS_TOOL,
    GET_TRAVEL_PLAN_TOOL,
    CONFIRM_TRAVEL_PLAN_TOOL,
]
LOCAL_TOOL_NAMES = {
    tool["function"]["name"]
    for tool in ALL_TOOLS
}
CONFIRMATION_REQUIRED_TOOLS = {"complete_todo"}

_BINARY_OPERATORS: dict[type[ast.operator], Callable[[Number, Number], Number]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPERATORS: dict[type[ast.unaryop], Callable[[Number], Number]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def calculate(expression: str) -> str:
    """安全计算基本数学表达式，不使用 eval。"""
    expression = expression.strip()
    if not expression:
        raise ValueError("表达式不能为空。")
    if len(expression) > MAX_EXPRESSION_LENGTH:
        raise ValueError("表达式过长。")

    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as error:
        raise ValueError("表达式格式不正确。") from error

    try:
        result = _evaluate_node(tree.body)
    except ZeroDivisionError as error:
        raise ValueError("不能除以零。") from error

    if isinstance(result, complex):
        raise ValueError("不支持复数计算。")
    if abs(result) > MAX_ABSOLUTE_VALUE or not math.isfinite(float(result)):
        raise ValueError("计算结果过大。")
    return str(result) if isinstance(result, int) else f"{result:g}"


WEATHER_CODE_LABELS = {
    0: "晴",
    1: "大致晴朗",
    2: "局部多云",
    3: "阴",
    45: "有雾",
    48: "雾凇",
    51: "小毛毛雨",
    53: "毛毛雨",
    55: "强毛毛雨",
    56: "轻微冻毛毛雨",
    57: "强冻毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "轻微冻雨",
    67: "强冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "米雪",
    80: "小阵雨",
    81: "中阵雨",
    82: "强阵雨",
    85: "小阵雪",
    86: "强阵雪",
    95: "雷暴",
    96: "雷暴伴小冰雹",
    99: "雷暴伴大冰雹",
}


def get_weather(location: str, days: int = 3) -> dict[str, object]:
    """解析中国行政区经纬度，再通过 Open-Meteo 查询真实天气。"""
    location = location.strip()
    if not location:
        raise ValueError("地点不能为空。")
    if len(location) > 100:
        raise ValueError("地点名称不能超过 100 个字符。")
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 7:
        raise ValueError("预报天数必须是 1 到 7 之间的整数。")

    try:
        place = resolve_china_location(location)
        forecast_response = httpx.get(
            FORECAST_URL,
            params={
                "latitude": place["latitude"],
                "longitude": place["longitude"],
                "current": (
                    "temperature_2m,apparent_temperature,relative_humidity_2m,"
                    "precipitation,weather_code,wind_speed_10m"
                ),
                "daily": (
                    "weather_code,temperature_2m_max,temperature_2m_min,"
                    "precipitation_probability_max"
                ),
                "timezone": "auto",
                "forecast_days": days,
            },
            timeout=8.0,
        )
        forecast_response.raise_for_status()
        weather = forecast_response.json()
    except httpx.HTTPError as error:
        raise ValueError("天气服务暂时不可用，请稍后重试。") from error

    current = weather["current"]
    daily = weather["daily"]
    forecast = []
    for index, date in enumerate(daily["time"]):
        code = int(daily["weather_code"][index])
        forecast.append(
            {
                "date": date,
                "weather": WEATHER_CODE_LABELS.get(code, f"天气代码 {code}"),
                "temperature_max_c": daily["temperature_2m_max"][index],
                "temperature_min_c": daily["temperature_2m_min"][index],
                "precipitation_probability_max_percent": daily[
                    "precipitation_probability_max"
                ][index],
            }
        )

    current_code = int(current["weather_code"])
    return {
        "location": {
            "name": place["name"],
            "admin1": place.get("admin1"),
            "admin2": place.get("admin2"),
            "country": place.get("country"),
            "country_code": place.get("country_code"),
            "latitude": place["latitude"],
            "longitude": place["longitude"],
            "timezone": weather.get("timezone"),
            "adcode": place.get("adcode"),
            "level": place.get("level"),
            "resolved_by": place.get("resolved_by"),
        },
        "current": {
            "time": current["time"],
            "weather": WEATHER_CODE_LABELS.get(
                current_code, f"天气代码 {current_code}"
            ),
            "temperature_c": current["temperature_2m"],
            "apparent_temperature_c": current["apparent_temperature"],
            "relative_humidity_percent": current["relative_humidity_2m"],
            "precipitation_mm": current["precipitation"],
            "wind_speed_kmh": current["wind_speed_10m"],
        },
        "forecast": forecast,
        "source": "Open-Meteo",
    }


class TodoStore:
    """保存待办事项；传入文件路径时自动持久化为 JSON。"""

    def __init__(self, file_path: Path | None = None) -> None:
        self.file_path = file_path
        self._todos: list[Todo] = []
        self._next_id = 1
        self._load()

    def add(self, title: str) -> Todo:
        title = title.strip()
        if not title:
            raise ValueError("待办事项不能为空。")
        if len(title) > 100:
            raise ValueError("待办事项不能超过 100 个字符。")

        todo: Todo = {"id": self._next_id, "title": title, "completed": False}
        self._todos.append(todo)
        self._next_id += 1
        try:
            self._save()
        except OSError:
            self._todos.pop()
            self._next_id -= 1
            raise
        return todo.copy()

    def list_all(self) -> list[Todo]:
        return [todo.copy() for todo in self._todos]

    def complete(self, todo_id: int) -> Todo:
        if isinstance(todo_id, bool) or not isinstance(todo_id, int):
            raise ValueError("任务 ID 必须是整数。")

        for todo in self._todos:
            if todo["id"] != todo_id:
                continue
            was_completed = todo["completed"]
            todo["completed"] = True
            try:
                self._save()
            except OSError:
                todo["completed"] = was_completed
                raise
            return todo.copy()

        raise ValueError(f"找不到 ID 为 {todo_id} 的任务。")

    def _load(self) -> None:
        if self.file_path is None or not self.file_path.exists():
            return

        try:
            data = json.loads(self.file_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise ValueError(f"Todo 文件格式损坏：{self.file_path}") from error

        if not isinstance(data, list):
            raise ValueError(f"Todo 文件必须包含一个列表：{self.file_path}")

        todos: list[Todo] = []
        for item in data:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("id"), int)
                or not isinstance(item.get("title"), str)
                or not isinstance(item.get("completed", False), bool)
            ):
                raise ValueError(f"Todo 文件包含无效任务：{self.file_path}")
            todos.append(
                {
                    "id": item["id"],
                    "title": item["title"],
                    "completed": item.get("completed", False),
                }
            )

        self._todos = todos
        self._next_id = max((int(todo["id"]) for todo in todos), default=0) + 1

    def _save(self) -> None:
        if self.file_path is None:
            return

        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.file_path.with_suffix(self.file_path.suffix + ".tmp")
        temporary_path.write_text(
            json.dumps(self._todos, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(self.file_path)


class TodoStoreProtocol(Protocol):
    """Agent 工具依赖的存储接口，底层可以是内存、JSON 或数据库。"""

    def add(self, title: str) -> Todo: ...

    def list_all(self) -> list[Todo]: ...

    def complete(self, todo_id: int) -> Todo: ...


class TravelStoreProtocol(Protocol):
    def list_all(self) -> list[dict[str, object]]: ...

    def get(self, travel_plan_id: int) -> dict[str, object]: ...

    def confirm(self, travel_plan_id: int) -> dict[str, object]: ...


def _evaluate_node(node: ast.AST) -> Number:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError("表达式中只能包含数字。")
        return node.value

    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPERATORS:
        value = _evaluate_node(node.operand)
        return _UNARY_OPERATORS[type(node.op)](value)

    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY_OPERATORS:
        left = _evaluate_node(node.left)
        right = _evaluate_node(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 10:
            raise ValueError("乘方指数不能超过 10。")
        return _BINARY_OPERATORS[type(node.op)](left, right)

    raise ValueError("只支持基本数学运算。")


def run_tool(
    name: str,
    arguments: str,
    todo_store: TodoStoreProtocol | None = None,
    travel_store: TravelStoreProtocol | None = None,
) -> str:
    """验证模型生成的参数，并执行对应工具。"""
    try:
        data = json.loads(arguments)

        if name == "calculate":
            expression = data["expression"]
            if not isinstance(expression, str):
                raise ValueError("expression 必须是字符串。")
            return json.dumps({"result": calculate(expression)}, ensure_ascii=False)

        if name == "run_python":
            code = data["code"]
            if not isinstance(code, str):
                raise ValueError("code 必须是字符串。")
            timeout_seconds = data.get("timeout_seconds", 5)
            try:
                return json.dumps(
                    run_python(code, timeout_seconds=timeout_seconds),
                    ensure_ascii=False,
                )
            except CodeSandboxError as error:
                return json.dumps({"error": str(error)}, ensure_ascii=False)

        if name == "web_search":
            query = data["query"]
            if not isinstance(query, str):
                raise ValueError("query 必须是字符串。")
            limit = data.get("limit", 3)
            return json.dumps(search_web(query, limit=limit), ensure_ascii=False)

        if name == "add_todo":
            if todo_store is None:
                raise ValueError("Todo 存储尚未初始化。")
            title = data["title"]
            if not isinstance(title, str):
                raise ValueError("title 必须是字符串。")
            return json.dumps({"todo": todo_store.add(title)}, ensure_ascii=False)

        if name == "list_todos":
            if todo_store is None:
                raise ValueError("Todo 存储尚未初始化。")
            return json.dumps({"todos": todo_store.list_all()}, ensure_ascii=False)

        if name == "complete_todo":
            if todo_store is None:
                raise ValueError("Todo 存储尚未初始化。")
            todo_id = data["id"]
            if isinstance(todo_id, bool) or not isinstance(todo_id, int):
                raise ValueError("id 必须是整数。")
            return json.dumps(
                {"todo": todo_store.complete(todo_id)},
                ensure_ascii=False,
            )

        if name == "list_todo_plans":
            if todo_store is None:
                raise ValueError("Todo 存储尚未初始化。")
            list_plans = getattr(todo_store, "list_plans", None)
            if not callable(list_plans):
                raise ValueError("当前 Todo 存储不支持计划查询。")
            return json.dumps(list_plans(), ensure_ascii=False)

        if name == "get_todo_plan":
            if todo_store is None:
                raise ValueError("Todo 存储尚未初始化。")
            plan_id = data["plan_id"]
            if isinstance(plan_id, bool) or not isinstance(plan_id, int):
                raise ValueError("plan_id 必须是整数。")
            get_plan = getattr(todo_store, "get_plan", None)
            if not callable(get_plan):
                raise ValueError("当前 Todo 存储不支持计划查询。")
            return json.dumps(get_plan(plan_id), ensure_ascii=False)

        if name == "list_travel_plans":
            if travel_store is None:
                raise ValueError("旅行计划存储尚未初始化。")
            return json.dumps(
                {"travel_plans": travel_store.list_all()}, ensure_ascii=False
            )

        if name in {"get_travel_plan", "confirm_travel_plan"}:
            if travel_store is None:
                raise ValueError("旅行计划存储尚未初始化。")
            travel_plan_id = data["travel_plan_id"]
            if isinstance(travel_plan_id, bool) or not isinstance(
                travel_plan_id, int
            ):
                raise ValueError("travel_plan_id 必须是整数。")
            result = (
                travel_store.get(travel_plan_id)
                if name == "get_travel_plan"
                else travel_store.confirm(travel_plan_id)
            )
            return json.dumps({"travel_plan": result}, ensure_ascii=False)

        return json.dumps({"error": f"未知工具：{name}"}, ensure_ascii=False)
    except (json.JSONDecodeError, KeyError, OSError, TypeError, ValueError) as error:
        return json.dumps({"error": str(error)}, ensure_ascii=False)
