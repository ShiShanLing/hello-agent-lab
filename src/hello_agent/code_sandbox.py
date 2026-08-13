"""在受限子进程中执行短 Python 代码，供 Agent 做计算/数据处理。"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import tempfile
from pathlib import Path

MAX_CODE_CHARS = 4000
MAX_OUTPUT_CHARS = 8000
DEFAULT_TIMEOUT_SECONDS = 5.0
MAX_TIMEOUT_SECONDS = 10.0

FORBIDDEN_MODULES = frozenset(
    {
        "os",
        "sys",
        "subprocess",
        "socket",
        "ctypes",
        "multiprocessing",
        "threading",
        "shutil",
        "pathlib",
        "importlib",
        "builtins",
        "signal",
        "resource",
        "pty",
        "fcntl",
        "http",
        "httpx",
        "urllib",
        "requests",
        "aiohttp",
        "pickle",
        "marshal",
        "shelve",
        "tempfile",
        "glob",
        "fnmatch",
        "sqlite3",
        "dbm",
        "multiprocessing",
        "concurrent",
        "asyncio",
        "code",
        "codeop",
        "inspect",
        "gc",
        "pty",
        "pwd",
        "grp",
        "posix",
        "nt",
        "winreg",
        "webbrowser",
        "antigravity",
    }
)

ALLOWED_MODULES = frozenset(
    {
        "math",
        "statistics",
        "decimal",
        "fractions",
        "random",
        "json",
        "re",
        "datetime",
        "collections",
        "itertools",
        "functools",
        "operator",
        "string",
        "textwrap",
        "typing",
        "heapq",
        "bisect",
        "array",
        "copy",
        "dataclasses",
        "enum",
        "numbers",
        "cmath",
    }
)

FORBIDDEN_NAMES = frozenset(
    {
        "eval",
        "exec",
        "compile",
        "open",
        "__import__",
        "breakpoint",
        "input",
        "help",
        "exit",
        "quit",
        "memoryview",
        "globals",
        "locals",
        "vars",
        "dir",
        "getattr",
        "setattr",
        "delattr",
        "hasattr",
        "classmethod",
        "staticmethod",
        "type",
        "object",
        "super",
    }
)

_RUNNER_SOURCE = r'''
import builtins as _builtins
import sys as _sys

_ALLOWED = set(__ALLOWED_MODULES__)
_FORBIDDEN = set(__FORBIDDEN_MODULES__)

_real_import = _builtins.__import__

def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    root = name.split(".", 1)[0]
    if root in _FORBIDDEN or root not in _ALLOWED:
        raise ImportError(f"沙箱禁止导入模块：{name}")
    return _real_import(name, globals, locals, fromlist, level)

_safe_builtins = {
    "abs": abs, "all": all, "any": any, "bool": bool, "dict": dict,
    "enumerate": enumerate, "filter": filter, "float": float, "format": format,
    "frozenset": frozenset, "int": int, "isinstance": isinstance,
    "issubclass": issubclass, "iter": iter, "len": len, "list": list,
    "map": map, "max": max, "min": min, "next": next, "pow": pow,
    "print": print, "range": range, "repr": repr, "reversed": reversed,
    "round": round, "set": set, "slice": slice, "sorted": sorted,
    "str": str, "sum": sum, "tuple": tuple, "zip": zip,
    "True": True, "False": False, "None": None,
    "__import__": _safe_import,
    "__build_class__": _builtins.__build_class__,
    "__name__": "__main__",
}

_code = open(_sys.argv[1], "r", encoding="utf-8").read()
_compiled = compile(_code, "<sandbox>", "exec")
exec(_compiled, {"__builtins__": _safe_builtins}, None)
'''


class CodeSandboxError(ValueError):
    """沙箱拒绝执行或执行失败。"""


def run_python(code: str, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> dict[str, object]:
    cleaned = code.strip()
    if not cleaned:
        raise CodeSandboxError("代码不能为空。")
    if len(cleaned) > MAX_CODE_CHARS:
        raise CodeSandboxError(f"代码不能超过 {MAX_CODE_CHARS} 个字符。")
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
        raise CodeSandboxError("timeout_seconds 必须是数字。")
    timeout = max(1.0, min(float(timeout_seconds), MAX_TIMEOUT_SECONDS))

    _validate_code_ast(cleaned)

    with tempfile.TemporaryDirectory(prefix="hello-agent-sandbox-") as directory:
        workdir = Path(directory)
        code_path = workdir / "user_code.py"
        runner_path = workdir / "runner.py"
        code_path.write_text(cleaned + "\n", encoding="utf-8")
        runner_path.write_text(
            _RUNNER_SOURCE
            .replace("__ALLOWED_MODULES__", repr(sorted(ALLOWED_MODULES)))
            .replace("__FORBIDDEN_MODULES__", repr(sorted(FORBIDDEN_MODULES))),
            encoding="utf-8",
        )

        env = {
            "PATH": os.environ.get("PATH", ""),
            "LANG": os.environ.get("LANG", "en_US.UTF-8"),
            "LC_ALL": os.environ.get("LC_ALL", "en_US.UTF-8"),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
        }
        try:
            completed = subprocess.run(
                [sys.executable, "-I", str(runner_path), str(code_path)],
                cwd=str(workdir),
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise CodeSandboxError(
                f"代码执行超时（>{timeout:.0f}s），已终止。"
            ) from error

        stdout = (completed.stdout or "")[:MAX_OUTPUT_CHARS]
        stderr = (completed.stderr or "")[:MAX_OUTPUT_CHARS]
        if completed.returncode != 0:
            message = stderr.strip() or stdout.strip() or "代码执行失败。"
            # 去掉冗长 traceback 路径噪音，保留核心错误。
            message = _simplify_error(message)
            raise CodeSandboxError(message)

        return {
            "stdout": stdout,
            "stderr": stderr,
            "returncode": completed.returncode,
            "timeout_seconds": timeout,
        }


def _validate_code_ast(code: str) -> None:
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as error:
        raise CodeSandboxError(f"代码语法错误：{error.msg}") from error

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _ensure_module_allowed(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                raise CodeSandboxError("沙箱禁止相对导入。")
            module = node.module or ""
            _ensure_module_allowed(module)
        elif isinstance(node, ast.Call):
            _ensure_call_allowed(node)
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("__") and node.attr.endswith("__"):
                if node.attr not in {"__name__", "__doc__"}:
                    raise CodeSandboxError(f"沙箱禁止访问属性：{node.attr}")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            raise CodeSandboxError(f"沙箱禁止使用：{node.id}")


def _ensure_module_allowed(name: str) -> None:
    root = (name or "").split(".", 1)[0]
    if not root:
        raise CodeSandboxError("沙箱禁止空导入。")
    if root in FORBIDDEN_MODULES or root not in ALLOWED_MODULES:
        raise CodeSandboxError(f"沙箱禁止导入模块：{root}")


def _ensure_call_allowed(node: ast.Call) -> None:
    func = node.func
    if isinstance(func, ast.Name) and func.id in FORBIDDEN_NAMES:
        raise CodeSandboxError(f"沙箱禁止调用：{func.id}")
    if isinstance(func, ast.Attribute) and func.attr in {"system", "popen", "remove", "rmtree"}:
        raise CodeSandboxError(f"沙箱禁止调用：{func.attr}")


def _simplify_error(message: str) -> str:
    lines = [line.rstrip() for line in message.strip().splitlines() if line.strip()]
    if not lines:
        return "代码执行失败。"
    # 优先返回最后一行异常信息。
    return lines[-1][:500]
