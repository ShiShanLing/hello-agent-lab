"""Agent Skills 目录发现与按需加载。"""

import json
import tempfile
import unittest
from pathlib import Path

from hello_agent.skills import (
    execute_load_skill,
    list_skills,
    load_skill,
    parse_skill,
    with_skill_catalog,
)


class SkillsTest(unittest.TestCase):
    def test_discovers_bundled_learning_skills(self) -> None:
        catalog = list_skills()
        names = {item.name for item in catalog}

        self.assertGreaterEqual(len(catalog), 4)
        self.assertTrue(
            {
                "precise-calculate",
                "todo-hygiene",
                "goal-planning",
                "knowledge-first",
            }.issubset(names)
        )
        self.assertTrue(all(item.description for item in catalog))

    def test_loads_precise_calculate_instructions(self) -> None:
        skill = load_skill("precise-calculate")

        self.assertEqual(skill.name, "precise-calculate")
        self.assertIn("calculate", skill.body)
        self.assertIn("不要心算", skill.body)

    def test_rejects_unknown_skill(self) -> None:
        with self.assertRaisesRegex(ValueError, "找不到 Skill"):
            load_skill("not-a-real-skill")

    def test_skips_invalid_skill_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            valid = root / "valid-skill"
            valid.mkdir()
            (valid / "SKILL.md").write_text(
                "---\nname: valid-skill\ndescription: 有效示例。\n---\n\n# 正文\n",
                encoding="utf-8",
            )
            broken = root / "broken-skill"
            broken.mkdir()
            (broken / "SKILL.md").write_text("没有 frontmatter", encoding="utf-8")

            catalog = list_skills(root)

        self.assertEqual([item.name for item in catalog], ["valid-skill"])

    def test_execute_load_skill_returns_instructions(self) -> None:
        result = json.loads(execute_load_skill('{"name":"precise-calculate"}'))

        self.assertEqual(result["name"], "precise-calculate")
        self.assertIn("calculate", result["instructions"])

    def test_execute_load_skill_returns_error_payload(self) -> None:
        result = json.loads(execute_load_skill('{"name":"missing-skill"}'))

        self.assertIn("error", result)

    def test_catalog_prompt_only_lists_names(self) -> None:
        prompt = with_skill_catalog("基础说明")

        self.assertIn("基础说明", prompt)
        self.assertIn("precise-calculate", prompt)
        self.assertIn("load_skill", prompt)
        self.assertNotIn("不要心算", prompt)

    def test_parse_skill_requires_description(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "SKILL.md"
            path.write_text("---\nname: empty-desc\ndescription: \n---\n\n正文\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "description"):
                parse_skill(path)


if __name__ == "__main__":
    unittest.main()
