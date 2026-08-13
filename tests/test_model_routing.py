"""多模型路由与失败降级测试。"""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from openai import OpenAIError

from hello_agent.model_routing import (
    create_chat_completion,
    fallback_chain,
    resolve_model,
    routing_config,
    should_fallback,
)
from hello_agent.usage import estimate_cost_by_models


class _FakeAPIError(OpenAIError):
    def __init__(self, message: str, status_code: int = 503) -> None:
        super().__init__(message)
        self.status_code = status_code


class ModelRoutingTest(unittest.TestCase):
    def test_resolves_cheap_and_strong_from_env(self) -> None:
        with patch.dict(
            os.environ,
            {
                "DEEPSEEK_MODEL": "deepseek-v4-flash",
                "DEEPSEEK_MODEL_CHEAP": "cheap-model",
                "DEEPSEEK_MODEL_STRONG": "strong-model",
                "DEEPSEEK_MODEL_FALLBACK": "",
            },
            clear=False,
        ):
            self.assertEqual(resolve_model("cheap"), "cheap-model")
            self.assertEqual(resolve_model("strong"), "strong-model")
            self.assertEqual(resolve_model("default"), "deepseek-v4-flash")
            self.assertEqual(resolve_model("strong", override="custom"), "custom")

    def test_fallback_chain_uses_explicit_list(self) -> None:
        with patch.dict(
            os.environ,
            {
                "DEEPSEEK_MODEL": "a",
                "DEEPSEEK_MODEL_CHEAP": "c",
                "DEEPSEEK_MODEL_STRONG": "b",
                "DEEPSEEK_MODEL_FALLBACK": "b,c,a",
            },
            clear=False,
        ):
            self.assertEqual(fallback_chain("a"), ["a", "b", "c"])

    def test_fallback_chain_auto_adds_role_models(self) -> None:
        with patch.dict(
            os.environ,
            {
                "DEEPSEEK_MODEL": "flash",
                "DEEPSEEK_MODEL_CHEAP": "flash",
                "DEEPSEEK_MODEL_STRONG": "pro",
                "DEEPSEEK_MODEL_FALLBACK": "",
            },
            clear=False,
        ):
            self.assertEqual(fallback_chain("pro"), ["pro", "flash"])

    def test_create_falls_back_on_transient_error(self) -> None:
        client = MagicMock()
        good = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])
        client.chat.completions.create.side_effect = [
            _FakeAPIError("overloaded", 503),
            good,
        ]
        with patch.dict(
            os.environ,
            {
                "DEEPSEEK_MODEL": "primary",
                "DEEPSEEK_MODEL_CHEAP": "backup",
                "DEEPSEEK_MODEL_STRONG": "primary",
                "DEEPSEEK_MODEL_FALLBACK": "backup",
            },
            clear=False,
        ):
            response, used = create_chat_completion(
                client,
                role="strong",
                messages=[{"role": "user", "content": "hi"}],
            )
        self.assertIs(response, good)
        self.assertEqual(used, "backup")
        self.assertEqual(client.chat.completions.create.call_count, 2)

    def test_should_fallback_on_rate_limit(self) -> None:
        self.assertTrue(should_fallback(_FakeAPIError("rate limit", 429)))

    def test_routing_config_exposes_roles(self) -> None:
        with patch.dict(
            os.environ,
            {
                "DEEPSEEK_MODEL": "flash",
                "DEEPSEEK_MODEL_CHEAP": "flash",
                "DEEPSEEK_MODEL_STRONG": "pro",
                "DEEPSEEK_MODEL_FALLBACK": "",
            },
            clear=False,
        ):
            config = routing_config()
        self.assertEqual(config["cheap"], "flash")
        self.assertEqual(config["strong"], "pro")
        self.assertEqual(config["roles"]["judge"], "strong")
        self.assertEqual(config["roles"]["planner"], "cheap")

    def test_estimate_cost_by_models_sums_per_model(self) -> None:
        cost = estimate_cost_by_models(
            {
                "deepseek-v4-flash": {
                    "prompt_tokens": 1_000_000,
                    "completion_tokens": 0,
                    "prompt_cache_hit_tokens": 0,
                    "prompt_cache_miss_tokens": 1_000_000,
                },
                "deepseek-v4-pro": {
                    "prompt_tokens": 0,
                    "completion_tokens": 1_000_000,
                    "prompt_cache_hit_tokens": 0,
                    "prompt_cache_miss_tokens": 0,
                },
            }
        )
        # flash input 0.14 + pro output 0.87
        self.assertEqual(cost, 1.01)


if __name__ == "__main__":
    unittest.main()
