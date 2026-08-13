"""判断用户目标是否应走多 Agent 协作，而不是普通单 Agent 对话。"""

from __future__ import annotations

import re
from typing import Literal

RoutingMode = Literal["chat", "collaboration"]

# 明确要求协作 / 多角色时强制协作
_FORCE_COLLAB_PATTERNS = (
    r"多智能体",
    r"多\s*agent",
    r"多角色",
    r"规划\s*[→\->]+\s*执行",
    r"先规划再执行",
    r"规划.?执行.?审核",
    r"请.?协作完成",
    r"分阶段(完成|推进|落地)",
    r"给出完整(方案|计划|路线)",
    r"从零(到一|开始).{0,12}(方案|落地|搭建)",
    r"调研并(给出|输出|形成).{0,8}(方案|报告|结论)",
)

# 明显是单轮工具 / 闲聊，强制普通对话
_FORCE_CHAT_PATTERNS = (
    r"^(你好|您好|嗨|在吗|谢谢|感谢)[\s!！。.?？]*$",
    r"精确计算",
    r"算一下",
    r"计算[:：]?",
    r"(今天|明天|这周)?.{0,6}天气",
    r"(查(一下)?|查询).{0,8}天气",
    r"添加(一个)?(任务|待办|todo)",
    r"帮我记(一个|一条)?(任务|待办)?",
    r"查看(我的)?(任务|待办|todo)",
    r"完成任务",
    r"记住",
    r"忘记",
    r"什么是",
    r"解释一下",
    r"根据(本地)?知识库",
    r"列出.{0,8}(知识|文件)",
)

# 协作信号：复杂目标、多阶段、需审核验收
_COLLAB_SIGNAL_PATTERNS = (
    r"分步",
    r"步骤",
    r"阶段",
    r"里程碑",
    r"实施方案",
    r"落地计划",
    r"可行性",
    r"风险评估",
    r"验收",
    r"审核",
    r"打回",
    r"返工",
    r"先.{1,20}再.{1,20}然后",
    r"首先.{1,30}其次",
    r"并且(需要|要求|同时).{0,12}(验证|检查|确认|评估)",
    r"(分析|调研|梳理).{0,16}(并|后).{0,12}(给出|输出|形成|制定)",
    r"(制定|设计).{0,12}(方案|流程|架构|计划).{0,20}(执行|落地|实现)",
)


def route_conversation_mode(message: str) -> dict[str, object]:
    """返回路由结果：mode / use_collaboration / reason / score。"""
    text = (message or "").strip()
    if not text:
        return {
            "mode": "chat",
            "use_collaboration": False,
            "reason": "空消息，使用普通对话。",
            "score": 0,
        }

    lowered = text.lower()
    for pattern in _FORCE_COLLAB_PATTERNS:
        if re.search(pattern, text, flags=re.IGNORECASE):
            return {
                "mode": "collaboration",
                "use_collaboration": True,
                "reason": "检测到明确的多 Agent / 协作规划意图。",
                "score": 100,
            }

    # 「计算并记 Todo」这类双工具短任务，单 Agent 足够
    if _is_simple_multi_tool(text):
        return {
            "mode": "chat",
            "use_collaboration": False,
            "reason": "属于可直接完成的简单工具组合，使用普通对话。",
            "score": 0,
        }

    for pattern in _FORCE_CHAT_PATTERNS:
        if re.search(pattern, text, flags=re.IGNORECASE):
            # 短问题命中单工具模式
            if len(text) < 120 or _looks_like_single_intent(text):
                return {
                    "mode": "chat",
                    "use_collaboration": False,
                    "reason": "像单轮工具或知识问答，使用普通对话。",
                    "score": 0,
                }

    score = 0
    hits: list[str] = []
    for pattern in _COLLAB_SIGNAL_PATTERNS:
        if re.search(pattern, text, flags=re.IGNORECASE):
            score += 28
            hits.append(pattern)

    # 长目标 + 多个动作子句
    clauses = [part for part in re.split(r"[。！？!?;；\n]", text) if part.strip()]
    if len(text) >= 80:
        score += 12
    if len(text) >= 160:
        score += 12
    if len(clauses) >= 3:
        score += 18
    action_verbs = len(
        re.findall(
            r"(计算|查询|检索|分析|制定|设计|实现|编写|生成|评估|验证|整理|总结|对比|规划|落地)",
            text,
        )
    )
    if action_verbs >= 3:
        score += 24
        hits.append("multi_actions")
    if action_verbs >= 5:
        score += 12

    # 「帮我做一个完整的…」类开放目标
    if re.search(r"(帮我|请).{0,8}(做|完成|搞定|设计|搭建).{0,20}(完整|系统|流程|方案)", text):
        score += 30
        hits.append("open_ended_delivery")

    if score >= 50:
        return {
            "mode": "collaboration",
            "use_collaboration": True,
            "reason": "目标较复杂，适合规划→执行→审核协作。",
            "score": score,
            "signals": hits[:6],
        }

    return {
        "mode": "chat",
        "use_collaboration": False,
        "reason": "更适合单 Agent 直接处理。",
        "score": score,
        "signals": hits[:6],
    }


def _looks_like_single_intent(text: str) -> bool:
    connectors = len(re.findall(r"(然后|接着|其次|并且还要|同时还要|另外还要)", text))
    return connectors == 0 and len(text) < 160


def _is_simple_multi_tool(text: str) -> bool:
    """识别「算一下并记 Todo」这类仍应由普通对话处理的短组合。"""
    if len(text) > 100:
        return False
    has_calc = bool(re.search(r"(计算|算一下|等于|表达式|\+|\*|/)", text))
    has_todo = bool(re.search(r"(待办|任务|todo|记(成|一|条|下))", text, flags=re.IGNORECASE))
    has_weather = bool(re.search(r"天气", text))
    toolish = sum([has_calc, has_todo, has_weather])
    if toolish < 2:
        return False
    # 没有明显的方案/阶段/审核诉求
    if re.search(r"(方案|阶段|审核|验收|调研|架构|里程碑)", text):
        return False
    return True
