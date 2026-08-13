---
name: python-sandbox
description: 在隔离沙箱中执行短 Python 代码。用户提到写代码、跑一段 Python、处理列表、算法演练或需要 print 结果时使用。
---

# Python 沙箱

1. 需要执行代码时调用本地工具 `run_python`，用 `print` 输出结果。
2. 简单四则运算优先 `calculate`，不要为了 `1+1` 启沙箱。
3. 只能使用安全模块（如 math、statistics、json、re、datetime、collections）。
4. 不要尝试读写文件、访问网络、调用系统命令或绕过沙箱。
5. 向用户解释结果时，附上关键输出，不要假装已经执行。
