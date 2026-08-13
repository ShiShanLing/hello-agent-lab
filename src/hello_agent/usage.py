"""按 DeepSeek 返回的 Token 估算费用。API Key 无法读取开放平台账单。"""

from __future__ import annotations

import os
from typing import Any


# 官方公开价（USD / 1M tokens，cache miss）。来源：https://api-docs.deepseek.com/quick_start/pricing
_FLASH_PRICES = {"input": 0.14, "cache_hit": 0.0028, "output": 0.28}
_PRO_PRICES = {"input": 0.435, "cache_hit": 0.003625, "output": 0.87}


def default_prices_for_model(model: str | None) -> dict[str, float]:
    name = (model or os.getenv("DEEPSEEK_MODEL") or "deepseek-v4-flash").lower()
    if "pro" in name:
        return dict(_PRO_PRICES)
    return dict(_FLASH_PRICES)


def pricing_info(model: str | None = None) -> dict[str, object]:
    defaults = default_prices_for_model(model)
    input_env = os.getenv("DEEPSEEK_INPUT_PRICE_PER_MILLION")
    output_env = os.getenv("DEEPSEEK_OUTPUT_PRICE_PER_MILLION")
    cache_env = os.getenv("DEEPSEEK_CACHE_HIT_PRICE_PER_MILLION")
    source = "official_default"
    input_price = defaults["input"]
    output_price = defaults["output"]
    cache_price = defaults["cache_hit"]
    if input_env not in {None, ""} and output_env not in {None, ""}:
        try:
            input_price = float(input_env)
            output_price = float(output_env)
            cache_price = float(cache_env) if cache_env not in {None, ""} else cache_price
            source = "env"
        except ValueError:
            source = "official_default"
    usd_to_cny = _optional_float(os.getenv("DEEPSEEK_USD_TO_CNY"))
    return {
        "currency": "USD",
        "model": model or os.getenv("DEEPSEEK_MODEL") or "deepseek-v4-flash",
        "input_per_million": input_price,
        "cache_hit_per_million": cache_price,
        "output_per_million": output_price,
        "source": source,
        "usd_to_cny": usd_to_cny,
        "note": (
            "费用按每次模型响应里的 usage Token × DeepSeek 公开单价估算，"
            "不是用 API Key 去拉取开放平台账户余额或账单。"
        ),
    }


def extract_usage(response: object) -> dict[str, int]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return empty_usage()
    prompt = _usage_int(usage, "prompt_tokens")
    completion = _usage_int(usage, "completion_tokens")
    total = _usage_int(usage, "total_tokens") or prompt + completion
    cache_hit = _usage_int(usage, "prompt_cache_hit_tokens")
    cache_miss = _usage_int(usage, "prompt_cache_miss_tokens")
    if cache_miss == 0 and prompt:
        cache_miss = max(0, prompt - cache_hit)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "prompt_cache_hit_tokens": cache_hit,
        "prompt_cache_miss_tokens": cache_miss,
    }


def empty_usage() -> dict[str, int]:
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "prompt_cache_hit_tokens": 0,
        "prompt_cache_miss_tokens": 0,
    }


def add_usage(total: dict[str, int], increment: dict[str, int] | None) -> dict[str, int]:
    merged = dict(total)
    for key, value in (increment or empty_usage()).items():
        merged[key] = int(merged.get(key, 0)) + int(value)
    return merged


def estimate_cost(
    usage: dict[str, Any] | None,
    model: str | None = None,
) -> float:
    info = pricing_info(model)
    usage = usage or empty_usage()
    prompt = int(usage.get("prompt_tokens", 0) or 0)
    completion = int(usage.get("completion_tokens", 0) or 0)
    cache_hit = int(usage.get("prompt_cache_hit_tokens", 0) or 0)
    cache_miss = int(usage.get("prompt_cache_miss_tokens", 0) or 0)
    if cache_miss == 0 and cache_hit == 0:
        cache_miss = prompt
    elif cache_miss == 0 and prompt:
        cache_miss = max(0, prompt - cache_hit)
    cost = (
        cache_miss / 1_000_000 * float(info["input_per_million"])
        + cache_hit / 1_000_000 * float(info["cache_hit_per_million"])
        + completion / 1_000_000 * float(info["output_per_million"])
    )
    return round(cost, 6)


def estimate_cost_by_models(usage_by_model: dict[str, dict[str, Any]] | None) -> float:
    """按实际使用的多个模型分别计价后求和（多模型路由/降级场景）。"""
    if not usage_by_model:
        return 0.0
    total = sum(estimate_cost(usage, model) for model, usage in usage_by_model.items())
    return round(total, 6)


def estimate_cost_from_total_tokens(total_tokens: int, model: str | None = None) -> float:
    half = max(0, int(total_tokens) // 2)
    return estimate_cost(
        {
            "prompt_tokens": half,
            "completion_tokens": max(0, int(total_tokens) - half),
            "prompt_cache_hit_tokens": 0,
            "prompt_cache_miss_tokens": half,
        },
        model,
    )


def convert_usd_to_cny(amount_usd: float, usd_to_cny: float | None) -> float | None:
    if usd_to_cny is None or usd_to_cny <= 0:
        return None
    return round(amount_usd * usd_to_cny, 4)


def _usage_int(usage: object, key: str) -> int:
    if isinstance(usage, dict):
        return max(0, int(usage.get(key, 0) or 0))
    return max(0, int(getattr(usage, key, 0) or 0))


def _optional_float(raw: str | None) -> float | None:
    if raw in {None, ""}:
        return None
    try:
        return float(raw)
    except ValueError:
        return None
