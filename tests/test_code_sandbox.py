"""代码执行沙箱测试。"""

from __future__ import annotations

import json
import unittest

from hello_agent.code_sandbox import CodeSandboxError, run_python
from hello_agent.tools import run_tool


class CodeSandboxTest(unittest.TestCase):
    def test_runs_simple_print(self) -> None:
        result = run_python("print(sum(range(1, 11)))")
        self.assertEqual(result["stdout"].strip(), "55")
        self.assertEqual(result["returncode"], 0)

    def test_allows_math_module(self) -> None:
        result = run_python("import math\nprint(round(math.pi, 2))")
        self.assertEqual(result["stdout"].strip(), "3.14")

    def test_blocks_os_import(self) -> None:
        with self.assertRaisesRegex(CodeSandboxError, "禁止导入"):
            run_python("import os\nprint(os.getcwd())")

    def test_blocks_open(self) -> None:
        with self.assertRaisesRegex(CodeSandboxError, "open"):
            run_python("print(open('/etc/passwd').read())")

    def test_timeout(self) -> None:
        with self.assertRaisesRegex(CodeSandboxError, "超时"):
            run_python("while True:\n    pass\n", timeout_seconds=1)

    def test_tool_wrapper(self) -> None:
        payload = json.loads(
            run_tool(
                "run_python",
                json.dumps({"code": "print([x*x for x in range(5)])"}),
            )
        )
        self.assertEqual(payload["stdout"].strip(), "[0, 1, 4, 9, 16]")

    def test_tool_returns_error_json(self) -> None:
        payload = json.loads(
            run_tool("run_python", json.dumps({"code": "import subprocess"}))
        )
        self.assertIn("error", payload)


if __name__ == "__main__":
    unittest.main()
