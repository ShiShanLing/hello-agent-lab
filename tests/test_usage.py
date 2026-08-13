"""DeepSeek Token 费用估算测试。"""

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hello_agent.usage import estimate_cost, estimate_cost_from_total_tokens, extract_usage, pricing_info


class UsageTest(unittest.TestCase):
    def test_uses_official_flash_prices_by_default(self) -> None:
        with patch.dict(
            os.environ,
            {
                "DEEPSEEK_MODEL": "deepseek-v4-flash",
                "DEEPSEEK_INPUT_PRICE_PER_MILLION": "",
                "DEEPSEEK_OUTPUT_PRICE_PER_MILLION": "",
            },
            clear=False,
        ):
            info = pricing_info("deepseek-v4-flash")
            self.assertEqual(info["currency"], "USD")
            self.assertEqual(info["input_per_million"], 0.14)
            self.assertEqual(info["output_per_million"], 0.28)
            self.assertEqual(info["source"], "official_default")

    def test_estimates_cost_from_prompt_and_completion_tokens(self) -> None:
        cost = estimate_cost(
            {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000},
            "deepseek-v4-flash",
        )
        self.assertEqual(cost, 0.42)

    def test_uses_cheaper_cache_hit_rate_when_present(self) -> None:
        cost = estimate_cost(
            {
                "prompt_tokens": 1_000_000,
                "completion_tokens": 0,
                "prompt_cache_hit_tokens": 1_000_000,
                "prompt_cache_miss_tokens": 0,
            },
            "deepseek-v4-flash",
        )
        self.assertEqual(cost, 0.0028)

    def test_extracts_cache_tokens_from_response_usage(self) -> None:
        usage = extract_usage(
            SimpleNamespace(
                usage=SimpleNamespace(
                    prompt_tokens=10,
                    completion_tokens=4,
                    total_tokens=14,
                    prompt_cache_hit_tokens=6,
                    prompt_cache_miss_tokens=4,
                )
            )
        )
        self.assertEqual(usage["prompt_cache_hit_tokens"], 6)
        self.assertEqual(usage["prompt_cache_miss_tokens"], 4)

    def test_splits_total_tokens_when_eval_has_no_breakdown(self) -> None:
        cost = estimate_cost_from_total_tokens(2_000_000, "deepseek-v4-flash")
        self.assertEqual(cost, 0.42)


if __name__ == "__main__":
    unittest.main()
