"""按任务角色选择模型，失败时自动降级。"""

from __future__ import annotations

import logging
import os
from typing import Any, Literal

from openai import OpenAI, OpenAIError


logger = logging.getLogger(__name__)

ModelRole = Literal["cheap", "strong", "default"]
DEFAULT_MODEL = "deepseek-v4-flash"

# 可降级的瞬时/容量类错误；模型名无效也降级到备用。
_FALLBACK_STATUS_CODES = {400, 404, 408, 429, 500, 502, 503, 504}

ROLE_ASSIGNMENTS: dict[str, ModelRole] = {
    "chat": "strong",
    "plan": "strong",
    "travel": "strong",
    "judge": "strong",
    "planner": "cheap",
    "executor": "strong",
    "reviewer": "strong",
    "workflow_llm": "strong",
    "workflow_agent": "strong",
}


def resolve_model(role: ModelRole = "default", *, override: str | None = None) -> str:
    """解析某角色实际使用的模型名。override 非空时优先。"""
    if override and override.strip():
        return override.strip()
    base = (os.getenv("DEEPSEEK_MODEL") or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    if role == "cheap":
        return (os.getenv("DEEPSEEK_MODEL_CHEAP") or base).strip() or base
    if role == "strong":
        return (os.getenv("DEEPSEEK_MODEL_STRONG") or base).strip() or base
    return base


def fallback_chain(primary: str) -> list[str]:
    """构造降级链：主模型 → 环境配置或 cheap/strong 互备。"""
    configured = os.getenv("DEEPSEEK_MODEL_FALLBACK", "").strip()
    if configured:
        extras = [item.strip() for item in configured.split(",") if item.strip()]
        chain = [primary, *[item for item in extras if item != primary]]
    else:
        chain = [primary]
        for alt in (resolve_model("strong"), resolve_model("cheap")):
            if alt not in chain:
                chain.append(alt)

    seen: set[str] = set()
    ordered: list[str] = []
    for item in chain:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def routing_config() -> dict[str, object]:
    """供监控看板与健康检查展示的路由配置快照。"""
    default = resolve_model("default")
    cheap = resolve_model("cheap")
    strong = resolve_model("strong")
    return {
        "default": default,
        "cheap": cheap,
        "strong": strong,
        "fallback_chain": fallback_chain(default),
        "roles": dict(ROLE_ASSIGNMENTS),
        "note": (
            "便宜模型做分类/规划拆解，强模型做写作与裁判；"
            "调用失败时按 fallback_chain 自动降级。"
        ),
    }


def _error_status(error: OpenAIError) -> int | None:
    status = getattr(error, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(error, "response", None)
    if response is not None:
        code = getattr(response, "status_code", None)
        if isinstance(code, int):
            return code
    return None


def should_fallback(error: BaseException) -> bool:
    if not isinstance(error, OpenAIError):
        return False
    name = type(error).__name__
    if name in {
        "APIConnectionError",
        "APITimeoutError",
        "InternalServerError",
        "RateLimitError",
        "APIStatusError",
    }:
        return True
    status = _error_status(error)
    if status in _FALLBACK_STATUS_CODES:
        return True
    message = str(error).lower()
    return any(
        token in message
        for token in ("timeout", "overloaded", "rate limit", "model", "not found")
    )


def create_chat_completion(
    client: OpenAI,
    *,
    role: ModelRole = "default",
    model: str | None = None,
    **kwargs: Any,
) -> tuple[Any, str]:
    """带角色路由与自动降级的 chat.completions.create。

    返回 (response, used_model)。stream=True 时仅在创建流失败时降级。
    """
    primary = resolve_model(role, override=model)
    chain = fallback_chain(primary)
    errors: list[str] = []

    for index, candidate in enumerate(chain):
        try:
            response = client.chat.completions.create(model=candidate, **kwargs)
            if candidate != primary:
                logger.warning("模型降级成功：%s → %s", primary, candidate)
            return response, candidate
        except OpenAIError as error:
            errors.append(f"{candidate}: {error}")
            is_last = index >= len(chain) - 1
            if is_last or not should_fallback(error):
                if is_last and len(chain) > 1:
                    break
                raise
            logger.warning(
                "模型调用失败，尝试降级：%s → 下一候选 (%s)",
                candidate,
                error,
            )

    raise RuntimeError("所有模型均调用失败：" + "; ".join(errors))
