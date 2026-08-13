"""计算器工具测试。"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from hello_agent.china_locations import resolve_china_location
from hello_agent.database import SqliteTodoStore, dispose_database_connections
from hello_agent.tools import TodoStore, calculate, get_weather, run_tool
from hello_agent.weather_mcp_server import query_weather


class CalculatorTest(unittest.TestCase):
    def test_calculates_expression(self) -> None:
        self.assertEqual(calculate("(12 + 8) * 3"), "60")
        self.assertEqual(calculate("7 / 2"), "3.5")

    def test_rejects_unsafe_python_code(self) -> None:
        with self.assertRaisesRegex(ValueError, "基本数学运算"):
            calculate("__import__('os').getcwd()")

    def test_returns_tool_error_instead_of_crashing(self) -> None:
        result = json.loads(run_tool("calculate", '{"expression": "1 / 0"}'))

        self.assertEqual(result, {"error": "不能除以零。"})

    def test_rejects_complex_result(self) -> None:
        with self.assertRaisesRegex(ValueError, "复数"):
            calculate("(-1) ** 0.5")


class TodoStoreTest(unittest.TestCase):
    def tearDown(self) -> None:
        dispose_database_connections()

    def test_adds_and_lists_todos(self) -> None:
        store = TodoStore()

        added = json.loads(
            run_tool("add_todo", '{"title": "学习 Tool Calling"}', store)
        )
        listed = json.loads(run_tool("list_todos", "{}", store))

        self.assertEqual(added["todo"]["id"], 1)
        self.assertEqual(added["todo"]["title"], "学习 Tool Calling")
        self.assertEqual(listed["todos"], [added["todo"]])

    def test_rejects_empty_todo(self) -> None:
        result = json.loads(run_tool("add_todo", '{"title": "  "}', TodoStore()))

        self.assertEqual(result, {"error": "待办事项不能为空。"})

    def test_reloads_todos_from_json_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            file_path = Path(directory) / "todos.json"
            first_store = TodoStore(file_path)
            first_store.add("学习 JSON 持久化")

            reloaded_store = TodoStore(file_path)

            self.assertEqual(
                reloaded_store.list_all(),
                [
                    {
                        "id": 1,
                        "title": "学习 JSON 持久化",
                        "completed": False,
                    }
                ],
            )

    def test_rejects_damaged_json_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            file_path = Path(directory) / "todos.json"
            file_path.write_text("not-json", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "格式损坏"):
                TodoStore(file_path)

    def test_completes_todo_and_persists_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            file_path = Path(directory) / "todos.json"
            store = TodoStore(file_path)
            store.add("完成 Agent 练习")

            result = json.loads(run_tool("complete_todo", '{"id": 1}', store))
            reloaded_store = TodoStore(file_path)

            self.assertTrue(result["todo"]["completed"])
            self.assertTrue(reloaded_store.list_all()[0]["completed"])

    def test_plan_tools_list_and_get_plan_details(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteTodoStore(Path(directory) / "agent.db", "user-a")
            plan = store.create_plan_with_steps(
                "旅行计划",
                "准备出发",
                "medium",
                [{"title": "订车票", "description": "选择车次", "minutes": 20}],
            )

            listed = json.loads(run_tool("list_todo_plans", "{}", store))
            detail = json.loads(
                run_tool("get_todo_plan", json.dumps({"plan_id": plan["id"]}), store)
            )

            self.assertEqual(listed["plans"][0]["title"], "旅行计划")
            self.assertEqual(detail["todos"][0]["title"], "订车票")
            self.assertEqual(detail["remaining_count"], 1)


class WeatherToolTest(unittest.TestCase):
    def _forecast_response(self):
        forecast = Mock()
        forecast.raise_for_status.return_value = None
        forecast.json.return_value = {
            "timezone": "Asia/Shanghai",
            "current": {
                "time": "2026-08-05T12:00",
                "temperature_2m": 31.2,
                "apparent_temperature": 36.1,
                "relative_humidity_2m": 70,
                "precipitation": 0.0,
                "weather_code": 2,
                "wind_speed_10m": 12.5,
            },
            "daily": {
                "time": ["2026-08-05", "2026-08-06"],
                "weather_code": [2, 61],
                "temperature_2m_max": [34.0, 32.0],
                "temperature_2m_min": [27.0, 26.0],
                "precipitation_probability_max": [20, 70],
            },
        }
        return forecast

    def test_resolves_zhumadian_from_local_china_data(self) -> None:
        result = resolve_china_location("驻马店市")

        self.assertEqual(result["name"], "驻马店市")
        self.assertEqual(result["country_code"], "CN")
        self.assertEqual(result["resolved_by"], "中国行政区划离线数据")

    def test_resolves_compound_china_address(self) -> None:
        result = resolve_china_location("河南省驻马店市驿城区")

        self.assertEqual(result["name"], "驿城区")
        self.assertEqual(result["admin1"], "河南省")
        self.assertEqual(result["admin2"], "驻马店市")

    def test_requires_more_context_for_ambiguous_district(self) -> None:
        with self.assertRaisesRegex(ValueError, "存在多个匹配"):
            resolve_china_location("南山区")

    @patch("hello_agent.tools.httpx.get")
    def test_queries_location_and_weather_forecast(self, http_get) -> None:
        http_get.return_value = self._forecast_response()

        result = get_weather("上海", days=2)

        self.assertEqual(result["location"]["name"], "上海市")
        self.assertEqual(result["location"]["country_code"], "CN")
        self.assertEqual(result["current"]["weather"], "局部多云")
        self.assertEqual(result["forecast"][1]["weather"], "小雨")
        self.assertEqual(http_get.call_count, 1)
        self.assertEqual(http_get.call_args.kwargs["params"]["forecast_days"], 2)

    @patch("hello_agent.tools.httpx.get")
    def test_weather_mcp_function_returns_data_for_agent(self, http_get) -> None:
        http_get.return_value = self._forecast_response()

        result = query_weather("上海", days=2)

        self.assertEqual(result["weather"]["source"], "Open-Meteo")
        self.assertEqual(len(result["weather"]["forecast"]), 2)

    @patch("hello_agent.china_locations.httpx.get")
    def test_reports_unknown_location(self, geocoding_get) -> None:
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {}
        geocoding_get.return_value = response

        with self.assertRaisesRegex(ValueError, "找不到.*地点"):
            query_weather("不存在城市")

    @patch("hello_agent.china_locations.httpx.get")
    def test_rejects_geocoding_results_outside_china(self, geocoding_get) -> None:
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "results": [
                {
                    "name": "Tokyo",
                    "country": "日本",
                    "country_code": "JP",
                    "latitude": 35.68,
                    "longitude": 139.69,
                }
            ]
        }
        geocoding_get.return_value = response

        with self.assertRaisesRegex(ValueError, "中国境内"):
            resolve_china_location("Tokyo")

    @patch("hello_agent.china_locations.httpx.get")
    def test_pinyin_fallback_is_restricted_to_china(self, geocoding_get) -> None:
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "results": [
                {
                    "name": "Zhumadian",
                    "country": "中国",
                    "country_code": "CN",
                    "latitude": 32.98,
                    "longitude": 114.02,
                }
            ]
        }
        geocoding_get.return_value = response

        result = resolve_china_location("Zhumadian")

        self.assertEqual(result["country_code"], "CN")
        self.assertEqual(
            geocoding_get.call_args.kwargs["params"]["countryCode"],
            "CN",
        )

    def test_rejects_invalid_forecast_days(self) -> None:
        with self.assertRaisesRegex(ValueError, "1 到 7"):
            query_weather("上海", days=8)


if __name__ == "__main__":
    unittest.main()
