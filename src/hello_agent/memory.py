"""跨会话长期记忆：抽取、检索注入和 Agent 工具。"""

from __future__ import annotations

import json
import re
from typing import Literal, Protocol, TypedDict

MemoryCategory = Literal["preference", "fact", "goal", "constraint"]
MemoryAction = Literal["remember", "forget"]

MEMORY_CATEGORIES: tuple[MemoryCategory, ...] = (
    "preference",
    "fact",
    "goal",
    "constraint",
)
CATEGORY_LABELS: dict[MemoryCategory, str] = {
    "preference": "偏好",
    "fact": "事实",
    "goal": "目标",
    "constraint": "约束",
}
MAX_MEMORY_CONTENT_LENGTH = 200
MAX_MEMORIES_PER_USER = 50
MAX_RELEVANT_MEMORIES = 8
MAX_EXTRACTED_PER_TURN = 3

MEMORY_INSTRUCTIONS = (
    "长期记忆、当前会话聊天记录和知识库检索是三类不同信息。"
    "长期记忆跨会话保留用户的稳定偏好、身份事实、长期目标和硬性约束；"
    "聊天记录只覆盖本会话最近对话；知识库是文档资料，不是用户画像。"
    "当用户明确要求记住、忘记，或说出稳定的个人偏好、身份、约束时，"
    "必须使用 remember_fact 或 forget_fact，不要只在口头上答应。"
    "一次性任务、临时行程和当前问题不要写成长期记忆。"
    "回答时必须遵守已提供的长期记忆，尤其是约束；不要向无关问题主动复述隐私细节。"
)

QUESTION_MARKERS = ("吗", "呢", "？", "?", "是不是", "能不能", "要不要", "会不会")
_FORGET_PATTERNS = (
    re.compile(r"(?:请)?忘记(?:掉)?(?:我)?(.{2,80}?)(?:[。.!！]|$)"),
    re.compile(r"删掉(?:这条|这些)?记忆[：:，,\s]*(.+)"),
    re.compile(r"不要再记住(.{2,80}?)(?:[。.!！]|$)"),
)
_REMEMBER_PATTERNS: tuple[tuple[re.Pattern[str], MemoryCategory], ...] = (
    (re.compile(r"(?:请)?记住[：:，,\s]*(.+)"), "fact"),
    (re.compile(r"我(?:的名字)?叫([\u4e00-\u9fffA-Za-z0-9·]{1,20})"), "fact"),
    (re.compile(r"我是(?!不是|否|谁)(.{2,40}?)(?:[。.!！，,]|$)"), "fact"),
    (re.compile(r"我不喜欢(.{2,60}?)(?:[。.!！]|$)"), "preference"),
    (re.compile(r"我喜欢(.{2,60}?)(?:[。.!！]|$)"), "preference"),
    (re.compile(r"我讨厌(.{2,60}?)(?:[。.!！]|$)"), "preference"),
    (re.compile(r"以后(?:请)?不要(.{2,60}?)(?:[。.!！]|$)"), "constraint"),
    (re.compile(r"不要(?:再)?(?:给我)?(?:推荐|安排|使用)(.{2,60}?)(?:[。.!！]|$)"), "constraint"),
    (re.compile(r"我住在(.{2,40}?)(?:[。.!！，,]|$)"), "fact"),
    (re.compile(r"我在(.{2,30}?)工作"), "fact"),
)


class MemoryRecord(TypedDict, total=False):
    id: str
    category: MemoryCategory
    content: str
    normalized: str
    source_session_id: str | None
    created_at: str
    updated_at: str
    last_used_at: str | None


class MemoryDraft(TypedDict):
    action: MemoryAction
    category: MemoryCategory
    content: str


class MemoryStoreProtocol(Protocol):
    def list(self, category: str | None = None, limit: int = 100) -> list[MemoryRecord]: ...

    def upsert(
        self,
        category: str,
        content: str,
        source_session_id: str | None = None,
    ) -> MemoryRecord: ...

    def delete(self, memory_id: str) -> MemoryRecord: ...

    def delete_matching(self, query: str) -> list[MemoryRecord]: ...

    def touch(self, memory_ids: list[str]) -> None: ...


REMEMBER_FACT_TOOL = {
    "type": "function",
    "function": {
        "name": "remember_fact",
        "description": (
            "把用户稳定的个人偏好、身份事实、长期目标或硬性约束写入跨会话长期记忆。"
            "一次性任务不要使用此工具。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": list(MEMORY_CATEGORIES),
                    "description": "preference 偏好，fact 身份/事实，goal 长期目标，constraint 约束",
                },
                "content": {
                    "type": "string",
                    "description": "一句简洁、可复用的记忆，不超过 200 字",
                },
            },
            "required": ["category", "content"],
            "additionalProperties": False,
        },
    },
}

FORGET_FACT_TOOL = {
    "type": "function",
    "function": {
        "name": "forget_fact",
        "description": "按记忆 ID 或关键词删除用户的长期记忆。",
        "parameters": {
            "type": "object",
            "properties": {
                "memory_id": {
                    "type": "string",
                    "description": "要删除的记忆 ID",
                },
                "query": {
                    "type": "string",
                    "description": "当不知道 ID 时，用关键词匹配要忘记的内容",
                },
            },
            "additionalProperties": False,
        },
    },
}

LIST_MEMORIES_TOOL = {
    "type": "function",
    "function": {
        "name": "list_memories",
        "description": "查看当前用户已保存的跨会话长期记忆。",
        "parameters": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": list(MEMORY_CATEGORIES),
                    "description": "可选，只查看某一类记忆",
                },
            },
            "additionalProperties": False,
        },
    },
}

MEMORY_TOOLS = [REMEMBER_FACT_TOOL, FORGET_FACT_TOOL, LIST_MEMORIES_TOOL]
MEMORY_TOOL_NAMES = {
    tool["function"]["name"]
    for tool in MEMORY_TOOLS
}


def normalize_memory_content(content: str) -> str:
    text = re.sub(r"\s+", "", content.strip().lower())
    text = re.sub(r"[，。！？、,.!?:;：；\"'“”‘’（）()【】\[\]《》]", "", text)
    for prefix in ("请记住", "记住", "别忘了", "请忘记", "忘记"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
    return text


def validate_memory_category(category: str) -> MemoryCategory:
    if category not in MEMORY_CATEGORIES:
        raise ValueError("记忆类型必须是 preference、fact、goal 或 constraint。")
    return category  # type: ignore[return-value]


def validate_memory_content(content: str) -> str:
    cleaned = re.sub(r"\s+", " ", content).strip()
    if len(cleaned) < 2:
        raise ValueError("记忆内容太短。")
    if len(cleaned) > MAX_MEMORY_CONTENT_LENGTH:
        raise ValueError(f"记忆内容不能超过 {MAX_MEMORY_CONTENT_LENGTH} 字。")
    if not normalize_memory_content(cleaned):
        raise ValueError("记忆内容无效。")
    return cleaned


def _looks_like_question(text: str) -> bool:
    return any(marker in text for marker in QUESTION_MARKERS)


def extract_candidate_memories(user_message: str) -> list[MemoryDraft]:
    text = re.sub(r"\s+", " ", user_message).strip()
    if len(text) < 4 or len(text) > 400:
        return []

    drafts: list[MemoryDraft] = []
    for pattern in _FORGET_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        content = validate_memory_content(match.group(1))
        if _looks_like_question(content):
            continue
        drafts.append(
            {"action": "forget", "category": "fact", "content": content}
        )

    remember_explicitly = text.startswith("请记住") or "记住" in text[:8]
    for pattern, category in _REMEMBER_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        raw = match.group(1).strip(" ，,。.!！")
        if not remember_explicitly and _looks_like_question(text):
            continue
        if _looks_like_question(raw) and not remember_explicitly:
            continue
        if category == "constraint":
            raw = re.sub(r"^以后", "", match.group(0)).strip(" ，,。.!！")
        elif category == "preference" and "不喜欢" in match.group(0) and "不喜欢" not in raw:
            raw = f"不喜欢{raw}"
        elif category == "preference" and "讨厌" in match.group(0) and not raw.startswith("讨厌"):
            raw = f"讨厌{raw}"
        try:
            content = validate_memory_content(raw)
        except ValueError:
            continue
        drafts.append(
            {"action": "remember", "category": category, "content": content}
        )

    unique: list[MemoryDraft] = []
    seen: set[str] = set()
    for draft in drafts:
        key = f"{draft['action']}:{normalize_memory_content(draft['content'])}"
        if key in seen:
            continue
        seen.add(key)
        unique.append(draft)
        if len(unique) >= MAX_EXTRACTED_PER_TURN:
            break
    return unique


def apply_extracted_memories(
    store: MemoryStoreProtocol,
    user_message: str,
    source_session_id: str | None = None,
) -> list[MemoryRecord]:
    applied: list[MemoryRecord] = []
    for draft in extract_candidate_memories(user_message):
        if draft["action"] == "forget":
            applied.extend(store.delete_matching(draft["content"]))
            continue
        applied.append(
            store.upsert(draft["category"], draft["content"], source_session_id)
        )
    return applied


def _tokenize(text: str) -> set[str]:
    lowered = text.lower()
    tokens = {match.group() for match in re.finditer(r"[a-z0-9]{2,}", lowered)}
    cjk = re.findall(r"[\u4e00-\u9fff]", lowered)
    tokens.update(cjk)
    tokens.update(cjk[index] + cjk[index + 1] for index in range(len(cjk) - 1))
    return {token for token in tokens if token}


def score_memory(memory: MemoryRecord, query: str) -> float:
    query_tokens = _tokenize(query)
    content_tokens = _tokenize(str(memory.get("content", "")))
    if not content_tokens:
        return 0.0
    overlap = len(query_tokens & content_tokens)
    coverage = overlap / max(1, len(content_tokens))
    score = overlap * 2 + coverage
    if memory.get("category") == "constraint":
        score += 0.8
    if not query_tokens:
        score += 0.2
    return score


def select_relevant_memories(
    memories: list[MemoryRecord],
    query: str,
    limit: int = MAX_RELEVANT_MEMORIES,
) -> list[MemoryRecord]:
    if not memories or limit < 1:
        return []
    ranked = sorted(
        memories,
        key=lambda item: (
            score_memory(item, query),
            1 if item.get("category") == "constraint" else 0,
        ),
        reverse=True,
    )
    selected: list[MemoryRecord] = []
    for memory in ranked:
        if score_memory(memory, query) <= 0 and memory.get("category") != "constraint":
            if query.strip() and len(selected) >= 2:
                continue
        selected.append(memory)
        if len(selected) >= limit:
            break
    if not selected:
        return ranked[: min(4, limit)]
    return selected


def format_memory_context(memories: list[MemoryRecord]) -> str | None:
    if not memories:
        return None
    lines = []
    for memory in memories:
        category = str(memory.get("category", "fact"))
        label = CATEGORY_LABELS.get(category, "事实")  # type: ignore[arg-type]
        content = str(memory.get("content", "")).strip()
        if content:
            lines.append(f"[{label}] {content}")
    if not lines:
        return None
    return (
        "以下是该用户的长期记忆，跨会话有效。"
        "请遵守其中的偏好和约束，不要把一次性任务写成新记忆。\n\n"
        + "\n".join(lines)
    )


def run_memory_tool(
    name: str,
    arguments: str,
    store: MemoryStoreProtocol,
) -> str:
    try:
        payload = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return json.dumps({"error": "工具参数必须是 JSON。"}, ensure_ascii=False)
    if not isinstance(payload, dict):
        return json.dumps({"error": "工具参数必须是对象。"}, ensure_ascii=False)

    try:
        if name == "remember_fact":
            memory = store.upsert(
                str(payload.get("category", "fact")),
                str(payload.get("content", "")),
            )
            return json.dumps(
                {"status": "saved", "memory": _public_memory(memory)},
                ensure_ascii=False,
            )
        if name == "forget_fact":
            memory_id = str(payload.get("memory_id") or "").strip()
            query = str(payload.get("query") or "").strip()
            if memory_id:
                deleted = [store.delete(memory_id)]
            elif query:
                deleted = store.delete_matching(query)
            else:
                raise ValueError("请提供 memory_id 或 query。")
            return json.dumps(
                {
                    "status": "deleted",
                    "count": len(deleted),
                    "memories": [_public_memory(item) for item in deleted],
                },
                ensure_ascii=False,
            )
        if name == "list_memories":
            category = payload.get("category")
            memories = store.list(
                None if category in (None, "") else str(category)
            )
            return json.dumps(
                {
                    "count": len(memories),
                    "memories": [_public_memory(item) for item in memories],
                },
                ensure_ascii=False,
            )
    except ValueError as error:
        return json.dumps({"error": str(error)}, ensure_ascii=False)
    return json.dumps({"error": f"未知记忆工具：{name}"}, ensure_ascii=False)


def _public_memory(memory: MemoryRecord) -> dict[str, object]:
    return {
        "id": memory.get("id"),
        "category": memory.get("category"),
        "content": memory.get("content"),
        "updated_at": memory.get("updated_at"),
    }
