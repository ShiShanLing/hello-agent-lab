"""发现并按需加载 Agent Skills，只把目录常驻到上下文。"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path


DEFAULT_SKILLS_DIR = Path(__file__).resolve().parents[2] / "skills"
SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9-]{1,64}$")
LOAD_SKILL_TOOL = {
    "type": "function",
    "function": {
        "name": "load_skill",
        "description": (
            "按名称加载一个 Agent Skill 的完整工作说明书。"
            "当用户任务匹配 Skill 目录中的用途时先调用本工具，再按说明书使用其他工具。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Skill 名称，例如 precise-calculate",
                }
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
}


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str
    path: str


def skills_dir() -> Path:
    return Path(os.getenv("SKILLS_DIR", str(DEFAULT_SKILLS_DIR))).resolve()


def list_skills(root: Path | None = None) -> list[Skill]:
    """扫描 Skill 目录，返回按名称排序的可用 Skills。"""
    base = root or skills_dir()
    if not base.is_dir():
        return []
    skills: list[Skill] = []
    for skill_file in sorted(base.glob("*/SKILL.md")):
        try:
            skills.append(parse_skill(skill_file))
        except ValueError:
            continue
    return skills


def load_skill(name: str, root: Path | None = None) -> Skill:
    """读取指定 Skill 的完整说明书。"""
    requested = name.strip()
    if not SKILL_NAME_PATTERN.fullmatch(requested):
        raise ValueError("Skill 名称无效。")
    for skill in list_skills(root):
        if skill.name == requested:
            return skill
    available = "、".join(item.name for item in list_skills(root)) or "无"
    raise ValueError(f"找不到 Skill：{requested}。可用：{available}")


def parse_skill(path: Path) -> Skill:
    text = path.read_text(encoding="utf-8")
    meta, body = _parse_frontmatter(text)
    name = meta.get("name", "").strip()
    description = meta.get("description", "").strip()
    if not SKILL_NAME_PATTERN.fullmatch(name):
        raise ValueError(f"{path} 的 name 无效。")
    if not description:
        raise ValueError(f"{path} 缺少 description。")
    if not body.strip():
        raise ValueError(f"{path} 缺少正文。")
    return Skill(
        name=name,
        description=description[:1024],
        body=body.strip(),
        path=str(path),
    )


def skill_catalog_prompt(skills: list[Skill] | None = None) -> str:
    items = skills if skills is not None else list_skills()
    if not items:
        return ""
    lines = "\n".join(f"- {item.name}：{item.description}" for item in items)
    return (
        "\n\n你可以使用 load_skill 按需加载 Agent Skills。"
        "下面只列出名称和用途，完整工作说明书不在上下文中。"
        "当任务匹配某个 Skill 的用途时，先调用 load_skill，再按返回的说明书使用对应工具。"
        "不要把尚未加载的 Skill 正文当成已经知道。\n"
        f"可用 Skills：\n{lines}"
    )


def with_skill_catalog(base_instructions: str, skills: list[Skill] | None = None) -> str:
    return base_instructions + skill_catalog_prompt(skills)


def execute_load_skill(arguments: str, root: Path | None = None) -> str:
    try:
        data = json.loads(arguments)
        name = data["name"]
        if not isinstance(name, str):
            raise ValueError("name 必须是字符串。")
        skill = load_skill(name, root)
        return json.dumps(
            {
                "name": skill.name,
                "description": skill.description,
                "instructions": skill.body,
            },
            ensure_ascii=False,
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        return json.dumps({"error": str(error)}, ensure_ascii=False)


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        raise ValueError("SKILL.md 缺少 YAML frontmatter。")
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise ValueError("SKILL.md frontmatter 不完整。")
    metadata: dict[str, str] = {}
    for raw_line in parts[1].splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        metadata[key.strip()] = value.strip().strip("'").strip('"')
    return metadata, parts[2].lstrip("\n")
